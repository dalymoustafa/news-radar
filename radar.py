#!/usr/bin/env python3
"""
News Radar
==========
Checks Google News, Reddit, X and any RSS feeds for the keywords in config.yaml,
asks Claude which new items actually matter, and sends those to Telegram.

    python radar.py              normal check (what the scheduler runs)
    python radar.py --dry-run    print alerts instead of sending them; save nothing
    python radar.py --no-ai      skip Claude and treat every new item as relevant

Needs these environment variables (on GitHub they are "repository secrets"):
    ANTHROPIC_API_KEY     your Claude API key
    TELEGRAM_BOT_TOKEN    from @BotFather
    TELEGRAM_CHAT_ID      optional: found automatically once you press Start on your bot
    X_BEARER_TOKEN        optional: only if X is enabled in config.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "memory.json"
USER_AGENT = "news-radar/1.0 (personal news alert tool)"
UTC = dt.timezone.utc
SEEN_RETENTION = dt.timedelta(days=3)     # how long to remember items already handled
ALERTED_RETENTION = dt.timedelta(hours=48)  # how long to remember stories already sent
AI_BATCH_SIZE = 40
MAX_ITEMS_PER_CHECK = 160                   # safety cap on Claude calls per check
FIRST_RUN_PREVIEW = 80                      # items Claude looks at on the very first run

LEVELS = {5: "🔴 Major", 4: "🟠 Important", 3: "🟡 Notable", 2: "⚪ Minor", 1: "⚪ Minor"}


# ─── small helpers ──────────────────────────────────────────────────────────

def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = 20) -> requests.Response:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def http_post(url: str, payload: dict, timeout: int = 20) -> requests.Response:
    return requests.post(url, json=payload, timeout=timeout,
                         headers={"User-Agent": USER_AGENT})


def clean(text: str | None) -> str:
    """Strip HTML tags and squeeze whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def entry_time(entry) -> dt.datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return dt.datetime(*t[:6], tzinfo=UTC) if t else None


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def unwrap_google_redirect(url: str) -> str:
    """Google Alerts links look like google.com/url?...&url=<real link>."""
    if url and "google.com/url" in url:
        real = parse_qs(urlparse(url).query).get("url")
        if real:
            return real[0]
    return url or ""


def norm_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:90]


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:14]


# ─── items and sources ──────────────────────────────────────────────────────

@dataclass
class Item:
    platform: str               # "Google News", "Reddit", "X", or a feed name
    outlet: str                 # publication, subreddit or account
    title: str
    url: str
    snippet: str = ""
    published: dt.datetime | None = None
    topic_hint: str | None = None

    def keys(self) -> list[str]:
        ks = []
        if self.url:
            ks.append("u" + short_hash(norm_url(self.url)))
        t = norm_title(self.title)
        if len(t) >= 25:  # only dedupe on titles long enough to be distinctive
            ks.append("t" + short_hash(t))
        return ks


@dataclass
class SourceResult:
    name: str
    items: list[Item] = field(default_factory=list)
    requests_made: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # a source counts as failed only if every request it made failed
        return self.requests_made == 0 or len(self.errors) < self.requests_made


def all_keywords(cfg: dict) -> list[str]:
    return [str(k) for t in cfg["topics"].values() for k in (t.get("keywords") or [])]


def mentions_keyword(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def fetch_google_news(cfg: dict) -> SourceResult | None:
    gn = cfg.get("google_news") or {}
    if not gn.get("enabled", True):
        return None
    res = SourceResult("Google News")
    searches = []
    for key, topic in cfg["topics"].items():
        searches += [(key, f'"{k}"') for k in (topic.get("keywords") or [])]
        searches += [(key, q) for q in (topic.get("extra_google_queries") or [])]
    for ceid in gn.get("editions") or ["US:en"]:
        country, lang = ceid.split(":")
        params = {"hl": f"{lang.split('-')[0]}-{country}", "gl": country, "ceid": ceid}
        for topic_key, query in searches:
            res.requests_made += 1
            try:
                r = http_get("https://news.google.com/rss/search",
                             params={"q": f"{query} when:1d", **params})
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{query}: {e}")
                continue
            for e in feedparser.parse(r.content).entries:
                outlet = (e.get("source") or {}).get("title", "")
                title = clean(e.get("title"))
                if outlet and title.endswith(f" - {outlet}"):
                    title = title[: -len(outlet) - 3]
                res.items.append(Item("Google News", outlet, title, e.get("link", ""),
                                      "", entry_time(e), topic_key))
            time.sleep(0.4)
    return res


def fetch_reddit(cfg: dict) -> SourceResult | None:
    rd = cfg.get("reddit") or {}
    if not rd.get("enabled"):
        return None
    stop = rd.get("stop_after")
    if stop and now().date() > dt.date.fromisoformat(str(stop)):
        return None
    res = SourceResult("Reddit")
    for key, topic in cfg["topics"].items():
        kws = topic.get("keywords") or []
        if not kws:
            continue
        res.requests_made += 1
        try:
            r = http_get("https://www.reddit.com/search.rss",
                         params={"q": " OR ".join(f'"{k}"' for k in kws),
                                 "sort": "new", "t": "day", "limit": 50})
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{topic.get('label', key)}: {e}")
            continue
        for e in feedparser.parse(r.content).entries:
            tags = e.get("tags") or []
            sub = "Reddit"
            if tags:
                sub = tags[0].get("label") or f"r/{tags[0].get('term')}"
            body = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
            res.items.append(Item("Reddit", sub, clean(e.get("title")), e.get("link", ""),
                                  clean(body)[:500], entry_time(e), key))
        time.sleep(1)
    return res


def fetch_x(cfg: dict, state: dict) -> SourceResult | None:
    xc = cfg.get("x") or {}
    if not xc.get("enabled"):
        return None
    res = SourceResult("X")
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        res.requests_made, res.errors = 1, ["X is enabled in config.yaml but the X_BEARER_TOKEN secret is missing"]
        return res
    since = state.setdefault("x_since_id", {})
    limit = max(10, min(100, int(xc.get("max_posts_per_check", 10))))
    for query in xc.get("queries") or []:
        params = {"query": query, "max_results": limit,
                  "tweet.fields": "created_at,author_id",
                  "expansions": "author_id", "user.fields": "username,name"}
        first_time = query not in since
        if not first_time:
            params["since_id"] = since[query]
        res.requests_made += 1
        try:
            data = http_get("https://api.x.com/2/tweets/search/recent", params=params,
                            headers={"Authorization": f"Bearer {token}"}).json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and not first_time:
                since.pop(query, None)  # bookmark too old for X's 7-day window; start fresh
            res.errors.append(f"{query}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{query}: {e}")
            continue
        if data.get("meta", {}).get("newest_id"):
            since[query] = data["meta"]["newest_id"]
        if first_time:
            continue  # first time we only note where the feed is, to avoid a flood
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        for post in data.get("data", []):
            user = users.get(post.get("author_id"), {})
            handle = user.get("username", "i")
            text = clean(post.get("text"))
            res.items.append(Item("X", f"@{handle}", text[:140],
                                  f"https://x.com/{handle}/status/{post['id']}",
                                  text, parse_iso(post.get("created_at"))))
    return res


def fetch_rss_feeds(cfg: dict) -> list[SourceResult]:
    results, keywords = [], all_keywords(cfg)
    for feed in cfg.get("rss_feeds") or []:
        res = SourceResult(feed.get("name") or feed["url"], requests_made=1)
        try:
            parsed = feedparser.parse(http_get(feed["url"]).content)
        except Exception as e:  # noqa: BLE001
            res.errors.append(str(e))
            results.append(res)
            continue
        for e in parsed.entries:
            title = clean(e.get("title"))
            snippet = clean(e.get("summary"))[:500]
            if feed.get("filter_keywords", True) and not mentions_keyword(f"{title} {snippet}", keywords):
                continue
            outlet = urlparse(unwrap_google_redirect(e.get("link", ""))).netloc.removeprefix("www.")
            res.items.append(Item(res.name, outlet, title, unwrap_google_redirect(e.get("link", "")),
                                  snippet, entry_time(e)))
        results.append(res)
    return results


def collect(cfg: dict, state: dict) -> list[SourceResult]:
    results = [fetch_google_news(cfg), fetch_reddit(cfg), fetch_x(cfg, state)]
    results = [r for r in results if r is not None] + fetch_rss_feeds(cfg)
    for r in results:
        status = "ok" if r.ok else "FAILED"
        log(f"{r.name}: {len(r.items)} items from {r.requests_made} requests ({status})")
        for err in r.errors[:3]:
            log(f"   ! {err}")
        if len(r.errors) > 3:
            log(f"   ! …and {len(r.errors) - 3} more errors")
    return results


# ─── Claude triage ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You triage fresh news for one reader. You get a batch of items \
(news articles, Reddit posts, X posts) that matched keyword searches, and decide \
which ones the reader should be alerted about right now.

The reader's brief:
<brief>
{brief}
</brief>

Topic keys you may use: {topics}

How to judge:
- Judge each item by what it is actually about, not by whether a keyword appears.
- importance: 5 = major breaking development the reader must see now; 4 = clearly \
newsworthy; 3 = worth a look; 2 = minor or background; 1 = irrelevant.
- relevant: true only if the item fits the brief.
- If several items in this batch report the same story, keep the best one (most \
authoritative or original source) and set same_story_as on the others to its id.
- If an item reports a story listed under "Already sent", set same_story_as to "SENT" \
unless it adds a materially new development.
- headline: a clear, factual English headline (translate if needed, no hype).
- summary: one English sentence on what happened and why it matters to the reader.
- country: the main country or jurisdiction involved, or "" if global or none.
Call the report tool exactly once, with one entry for every item id."""


def report_tool(topic_keys: list[str]) -> dict:
    return {
        "name": "report",
        "description": "Report your verdict on every item in the batch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "relevant": {"type": "boolean"},
                            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                            "topic": {"type": "string", "enum": topic_keys + ["other"]},
                            "headline": {"type": "string"},
                            "summary": {"type": "string"},
                            "country": {"type": "string"},
                            "same_story_as": {"type": "string"},
                        },
                        "required": ["id", "relevant", "importance", "topic"],
                    },
                }
            },
            "required": ["items"],
        },
    }


def describe(item_id: str, it: Item) -> str:
    when = it.published.strftime("%Y-%m-%d %H:%M UTC") if it.published else "unknown time"
    lines = [f"[{item_id}] {it.platform} | {it.outlet or 'unknown source'} | {when}",
             f"Title: {it.title}"]
    if it.snippet and it.snippet[:80] not in it.title:
        lines.append(f"Text: {it.snippet[:400]}")
    return "\n".join(lines)


def classify(items: list[Item], cfg: dict, state: dict, use_ai: bool) -> tuple[dict[int, dict], list[str]]:
    """Returns ({index in items: verdict}, errors). Items in failed batches get no verdict."""
    if not items:
        return {}, []
    topic_keys = list(cfg["topics"].keys())
    if not use_ai:
        return {i: {"relevant": True, "importance": 3, "topic": it.topic_hint or "other"}
                for i, it in enumerate(items)}, []

    import anthropic  # imported here so --no-ai works without the package

    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT.format(brief=cfg.get("what_matters", "").strip(),
                                  topics=", ".join(topic_keys))
    sent = [a["h"] for a in state.get("alerted", [])][-60:]
    sent_text = "\n".join(f"- {h}" for h in sent) if sent else "(nothing yet)"
    model = (cfg.get("settings") or {}).get("ai_model", "claude-haiku-5-5")

    verdicts, errors = {}, []
    for start in range(0, len(items), AI_BATCH_SIZE):
        batch = list(enumerate(items))[start:start + AI_BATCH_SIZE]
        body = "\n\n".join(describe(f"i{i}", it) for i, it in batch)
        prompt = f"Already sent in the last 48 hours:\n{sent_text}\n\nNew items:\n\n{body}"
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=min(8000, 400 + 150 * len(batch)),
                system=system,
                tools=[report_tool(topic_keys)],
                tool_choice={"type": "tool", "name": "report"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
            continue
        out = next((b.input for b in resp.content if b.type == "tool_use"), {}) or {}
        got = {str(v.get("id")): v for v in out.get("items", []) if isinstance(v, dict)}
        for i, _ in batch:
            # anything Claude skipped is treated as not relevant, so it isn't retried forever
            verdicts[i] = got.get(f"i{i}", {"relevant": False, "importance": 1, "topic": "other"})
    return verdicts, errors


def pick_alerts(items: list[Item], verdicts: dict[int, dict], min_importance: int) -> list[tuple[Item, dict, list[str]]]:
    """Choose what to send. Same-story items are folded into one alert ("Also: …")."""
    keep = {i: v for i, v in verdicts.items()
            if v.get("relevant") and int(v.get("importance", 1)) >= min_importance
            and str(v.get("same_story_as", "")).upper() != "SENT"}
    also: dict[int, list[str]] = {}
    for i, v in list(keep.items()):
        ref = str(v.get("same_story_as") or "").strip().strip("[]").lower().removeprefix("i")
        if ref.isdigit():
            root = int(ref)
            if root != i and root in keep:
                also.setdefault(root, []).append(items[i].outlet or items[i].platform)
                del keep[i]
    order = sorted(keep, key=lambda i: (-int(keep[i].get("importance", 1)),
                                        items[i].published or now()))
    return [(items[i], keep[i], sorted(set(also.get(i, [])))) for i in order]


# ─── Telegram ───────────────────────────────────────────────────────────────

class Telegram:
    def __init__(self, state: dict, dry_run: bool):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or state.get("telegram_chat_id", "")
        self.state, self.dry_run = state, dry_run
        self.sent = 0

    def ready(self) -> bool:
        if self.dry_run:
            return True
        if not self.token:
            log("TELEGRAM_BOT_TOKEN is missing. Add it as a repository secret (see README).")
            return False
        if not self.chat_id:
            self.chat_id = self._find_chat()
        return bool(self.chat_id)

    def _find_chat(self) -> str:
        try:
            updates = http_get(f"https://api.telegram.org/bot{self.token}/getUpdates").json()
        except Exception as e:  # noqa: BLE001
            log(f"Couldn't reach Telegram with this bot token ({e}). Check TELEGRAM_BOT_TOKEN.")
            return ""
        for upd in reversed(updates.get("result", [])):
            msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
            chat = msg.get("chat")
            if chat:
                chat_id = str(chat["id"])
                self.state["telegram_chat_id"] = chat_id
                log(f"Found your Telegram chat ({chat_id}).")
                return chat_id
        log("No Telegram chat found yet. Open Telegram, find your bot, press Start "
            "(or send it any message), then run the radar again.")
        return ""

    def send(self, text: str) -> bool:
        if self.dry_run:
            plain = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2\n   \1", text)
            plain = "\n".join(clean(line) for line in plain.split("\n"))
            print("\n────────── MESSAGE ──────────\n" + plain)
            self.sent += 1
            return True
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                r = http_post(f"https://api.telegram.org/bot{self.token}/sendMessage", payload)
            except Exception as e:  # noqa: BLE001
                log(f"Telegram send failed: {e}")
                time.sleep(2)
                continue
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            if not r.ok:
                log(f"Telegram refused the message ({r.status_code}): {r.text[:200]}")
                return False
            self.sent += 1
            time.sleep(0.4)
            return True
        return False


def fmt_time(t: dt.datetime | None, tz: ZoneInfo) -> str:
    if not t:
        return ""
    local, today = t.astimezone(tz), now().astimezone(tz).date()
    return f"{local:%H:%M}" if local.date() == today else f"{local.day} {local:%b %H:%M}"


def source_label(it: Item) -> str:
    if it.platform == "X":
        return f"{it.outlet} on X"
    if it.platform in ("Google News", "Reddit"):
        return it.outlet or it.platform
    return it.outlet or it.platform  # custom RSS feed: the article's website


def format_alert(it: Item, v: dict, also: list[str], cfg: dict, tz: ZoneInfo) -> str:
    topic = cfg["topics"].get(v.get("topic") or "", {})
    label = topic.get("label") or "News"
    level = LEVELS.get(int(v.get("importance", 3)), "")
    headline = v.get("headline") or it.title
    lines = [f"{topic.get('emoji', '📰')} <b>{esc(label)}</b> · {level}",
             f'<b><a href="{esc(it.url)}">{esc(headline)}</a></b>']
    if v.get("summary"):
        lines.append(esc(v["summary"]))
    meta = [v.get("country") or "", source_label(it), fmt_time(it.published, tz)]
    lines.append("<i>" + esc(" · ".join(m for m in meta if m)) + "</i>")
    if also:
        lines.append("<i>Also covered by: " + esc(", ".join(also[:6])) + "</i>")
    return "\n".join(lines)


def send_alerts(alerts, tg: Telegram, cfg: dict, tz: ZoneInfo, state: dict) -> None:
    limit = int((cfg.get("settings") or {}).get("max_alerts_per_check", 10))
    for it, v, also in alerts[:limit]:
        if tg.send(format_alert(it, v, also, cfg, tz)):
            state.setdefault("alerted", []).append(
                {"t": now().isoformat(), "h": v.get("headline") or it.title})
    extra = alerts[limit:]
    if extra:
        lines = [f"➕ <b>{len(extra)} more stories</b>"]
        for it, v, _ in extra[:25]:
            lines.append(f'• <a href="{esc(it.url)}">{esc(v.get("headline") or it.title)}</a>')
            state.setdefault("alerted", []).append(
                {"t": now().isoformat(), "h": v.get("headline") or it.title})
        tg.send("\n".join(lines))


# ─── memory between checks ──────────────────────────────────────────────────

def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        log("Memory file was unreadable; starting fresh.")
        return None


def save_state(state: dict, max_age: dt.timedelta) -> None:
    cutoff_seen = (now() - max(SEEN_RETENTION, max_age + dt.timedelta(days=1))).timestamp()
    state["seen"] = {k: t for k, t in state.get("seen", {}).items() if t >= cutoff_seen}
    cutoff_alerted = now() - ALERTED_RETENTION
    state["alerted"] = [a for a in state.get("alerted", [])
                        if (parse_iso(a.get("t")) or now()) >= cutoff_alerted][-100:]
    state["last_run"] = now().isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))
    tmp.replace(STATE_PATH)


def mark_seen(state: dict, items: list[Item]) -> None:
    ts = now().timestamp()
    for it in items:
        for k in it.keys():
            state["seen"][k] = ts


def track_health(name: str, ok: bool, detail: str, state: dict, tg: Telegram, threshold: int) -> None:
    fails = state.setdefault("failures", {})
    if ok:
        if fails.get(name, 0) >= threshold:
            tg.send(f"✅ <b>{esc(name)}</b> is working again.")
        fails[name] = 0
        return
    fails[name] = fails.get(name, 0) + 1
    if fails[name] == threshold:
        tg.send(f"⚠️ <b>{esc(name)}</b> has failed {threshold} checks in a row, so you may be "
                f"missing stories from it.\n<i>{esc(detail[:300])}</i>\n"
                "Details are in the GitHub Actions log. You'll get a message when it recovers.")


# ─── main ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
    except yaml.YAMLError as e:
        sys.exit(f"config.yaml has a formatting problem (check indentation and quotes):\n{e}")
    if not isinstance(cfg, dict) or not cfg.get("topics"):
        sys.exit("config.yaml needs a 'topics' section with at least one topic.")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; save nothing")
    ap.add_argument("--no-ai", action="store_true", help="skip Claude; treat all new items as relevant")
    args = ap.parse_args()

    cfg = load_config()
    settings = cfg.get("settings") or {}
    tz = ZoneInfo(cfg.get("timezone") or "UTC")
    state = load_state()
    first_run = state is None
    state = state or {"seen": {}, "alerted": [], "failures": {}}
    use_ai = not args.no_ai

    if use_ai and not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY is missing. Add it as a repository secret (see README).")
        return 1
    tg = Telegram(state, args.dry_run)
    if not tg.ready():
        return 1

    threshold = int(settings.get("warn_after_failures", 8))
    sources = collect(cfg, state)
    if not first_run:
        for s in sources:
            track_health(s.name, s.ok, "; ".join(s.errors[:2]), state, tg, threshold)

    # keep fresh items we haven't handled before (also drops duplicates within this check)
    max_age = dt.timedelta(hours=float(settings.get("max_age_hours", 24)))
    fresh, keys_now = [], set()
    for it in (i for s in sources for i in s.items):
        if it.published and now() - it.published > max_age:
            continue
        ks = it.keys()
        if any(k in state["seen"] or k in keys_now for k in ks):
            continue
        keys_now.update(ks)
        fresh.append(it)
    fresh.sort(key=lambda i: i.published or now(), reverse=True)
    log(f"{len(fresh)} new items to review")

    min_imp = int(cfg.get("min_importance", 3))
    if first_run:
        verdicts, errors = classify(fresh[:FIRST_RUN_PREVIEW], cfg, state, use_ai)
        picks = pick_alerts(fresh, verdicts, min_imp)[:5]
        n_searches = sum(len(t.get("keywords") or []) + len(t.get("extra_google_queries") or [])
                         for t in cfg["topics"].values())
        names = [s.name for s in sources]
        where = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        intro = (f"✅ <b>Your news radar is live.</b>\nWatching {n_searches} searches on {esc(where)}. "
                 "From now on you'll get a message here shortly after a relevant story appears.")
        for s in sources:
            if not s.ok:
                intro += (f"\n\n⚠️ {esc(s.name)} couldn't be reached on this check: "
                          f"<i>{esc((s.errors or ['unknown error'])[0][:200])}</i>")
        if errors:
            intro += f"\n\n⚠️ Claude couldn't review items yet: <i>{esc(errors[0][:200])}</i>"
        elif picks:
            intro += "\n\nHere's what it would have flagged from the last 24 hours:"
        else:
            intro += "\n\nNothing from the last 24 hours met your bar, so it'll stay quiet until something does."
        tg.send(intro)
        send_alerts(picks, tg, cfg, tz, state)
        mark_seen(state, fresh)  # start with a clean slate
    else:
        batch = fresh[:MAX_ITEMS_PER_CHECK]
        verdicts, errors = classify(batch, cfg, state, use_ai)
        if use_ai:
            track_health("Claude (AI filter)", not errors or bool(verdicts), "; ".join(errors[:1]),
                         state, tg, max(2, threshold // 2))
        for e in errors[:2]:
            log(f"Claude error: {e}")
        alerts = pick_alerts(batch, verdicts, min_imp)
        log(f"{len(alerts)} alerts to send")
        send_alerts(alerts, tg, cfg, tz, state)
        # only remember items Claude actually reviewed, so failed ones are retried next check
        mark_seen(state, [it for i, it in enumerate(batch) if i in verdicts])

    if args.dry_run:
        log(f"Dry run finished: {tg.sent} messages printed, nothing saved.")
    else:
        save_state(state, max_age)
        log(f"Done: {tg.sent} messages sent.")
    all_failed = sources and not any(s.ok for s in sources)
    return 1 if all_failed else 0


if __name__ == "__main__":
    sys.exit(main())
