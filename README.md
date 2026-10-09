# News Radar

News Radar watches Google News, Reddit and optionally X for RCBI, wealth-management and wealth-tax news. Every 15 minutes it checks for anything new and has Claude read each item against your brief. Stories that matter arrive on your phone through Telegram, like this:

> 🛂 **RCBI** · 🔴 Major
> **[Portugal scraps golden visa fund route](#)**
> Portugal's government has removed investment funds as a qualifying route, closing the most popular remaining option.
> *Portugal · Reuters · 14:05*
> *Also covered by: Bloomberg, Público*

The same story from several outlets is grouped into one alert, and you're never sent the same story twice. Ads, listicles and passing mentions are filtered out.

It runs for free on GitHub, so you don't need a server or a computer that stays on.

---

## Set it up (about 20 minutes, once)

### 1. Create your Telegram bot (3 minutes)
1. In Telegram, search for **@BotFather** (it has a blue tick) and open it.
2. Send `/newbot`. Choose a name such as *News Radar*, then a username ending in `bot`, such as `my_news_radar_bot`.
3. BotFather replies with a **token** that looks like `123456789:AAH…`. Copy it.
4. Tap the link to your new bot and press **Start**. The radar uses this to find you.

### 2. Get a Claude API key (5 minutes)
This is separate from your Claude chat subscription.
1. Go to **platform.claude.com** and sign in or sign up.
2. Under **Billing**, add some credit. $5 is plenty to start, and normal use costs a few dollars a month at most.
3. Under **API keys**, click **Create key** and copy it. It starts with `sk-ant-`.
4. Optional: under **Limits**, set a monthly spend limit as a safety net.

### 3. Put the radar on GitHub (10 minutes)
1. Create a free account at **github.com**.
2. Click **+** (top right), then **New repository**. Name it `news-radar`. Choose **Public** so it can run every 15 minutes for free (see *Public or private?* below), then click **Create repository**.
3. On the new page, click **uploading an existing file**. Drag in `radar.py`, `config.yaml`, `requirements.txt` and `README.md`, then click **Commit changes**.
4. Open the **Actions** tab and click **set up a workflow yourself**. Delete everything in the editor, paste in the whole of `github-workflow.yml`, and click **Commit changes**.

### 4. Add your keys as secrets (2 minutes)
In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Claude key from step 2 |
| `TELEGRAM_BOT_TOKEN` | your bot token from step 1 |

Secrets are encrypted. Nobody can see them, even in a public repository.

### 5. Switch it on
Go to **Actions**, click **News radar** on the left, then **Run workflow**. Within a minute or two Telegram should say **"✅ Your news radar is live"** and show the top stories from the last 24 hours. From then on it runs by itself every 15 minutes.

**No message?** Click the run, open the **Check the news** step, and read the last few lines. They say in plain English what's missing.

---

## Tuning it

All settings are in **`config.yaml`**. On GitHub, open the file, click the pencil icon, edit it, and click **Commit changes**. The next check uses your changes.

- **`what_matters`** is the brief Claude follows. This has the biggest effect. If an alert is junk, describe that kind of item under *Don't alert me about*.
- **`keywords`** are the phrases the radar searches for, grouped by topic. You can add, remove, or add a new topic.
- **`min_importance`** sets how high an item must score to reach you: 3 by default, 4 for fewer alerts, 2 for more.
- **`rss_feeds`** adds any other feed, such as a ministry's press releases or a Google Alert (see below).

## How fast is it?
It checks every 15 minutes, and Google News usually lists a story within minutes of publication. GitHub sometimes starts scheduled runs a few minutes late when it's busy. Expect most stories to reach you **5 to 30 minutes** after they're published.

## What it costs
- **GitHub:** free for a public repository. Private repositories get 2,000 free minutes a month, and checking every 15 minutes uses about 2,900. If you go private, change the schedule to every 30 minutes; the line to edit is marked in the workflow file.
- **Claude:** usually a few dollars a month or less. It uses Claude Haiku 5.5, the smallest and cheapest model, which is good enough for sorting news.
- **Telegram:** free.
- **X:** off by default (see below).

## Public or private?
In a public repository, anyone who finds it can see your code, keywords and brief. They can't see your keys, and they can't see the record of which stories you were sent, because that's kept in GitHub's internal cache.

---

## About each source

**Google News** is the backbone. It covers thousands of outlets and is free.

**Google Alerts** (optional) also catches blogs, government sites and other pages that Google News misses. To add one:
1. Go to google.com/alerts and enter a search such as `"golden visa"`.
2. Click **Show options**. Set *How often* to **As-it-happens** and *Deliver to* to **RSS feed**, then click **Create Alert**.
3. Right-click the RSS icon next to the alert and copy the link.
4. Paste the link into `rss_feeds` in `config.yaml` with `filter_keywords: false`.

**Reddit** will only work for a limited time. Reddit is switching off all RSS feeds on **13 November 2026**, and its API is closed to new personal projects. Reddit also blocks some cloud servers. The radar stops trying after the shutdown date, and if Reddit fails eight checks in a row you'll get one warning. For Reddit after November, **f5bot.com** sends free email alerts when your keywords are mentioned.

**X / Twitter** is optional and paid. X charges about **$0.005 for every post the radar reads**, so broad searches like "wealth tax" can cost $100 or more a month. To turn it on:
1. Create a developer account at developer.x.com, add pay-per-use credit, create an app, and copy its **Bearer Token**.
2. Add the token as a secret named `X_BEARER_TOKEN`.
3. In `config.yaml`, set `x: enabled: true`. Keep the searches narrow; watching specific accounts with `from:handle` is the cheapest option.

The first check of a new X search only marks where to start, so it doesn't send a flood of old posts.

**LinkedIn** has no way to search posts automatically, and scraping it gets accounts banned, so it isn't included. What works instead:
- Turn on the 🔔 notification bell on the profiles and company pages that matter.
- Add a Google Alert for searches such as `site:linkedin.com "golden visa"`. This catches some public LinkedIn articles.

---

## Troubleshooting

- **"No Telegram chat found yet"** in the log: open your bot in Telegram, press **Start** or send it any message, then run the workflow again.
- **Too many alerts:** raise `min_importance` to 4, or add the kind of item you don't want to *Don't alert me about*.
- **Missing stories:** add keywords, or lower `min_importance` to 2.
- **"⚠️ … has failed 8 checks in a row"** on Telegram: one source is down or blocked. The other sources keep working, and you'll get a message when it recovers.
- **It stopped running after a couple of months:** in public repositories, GitHub pauses schedules after 60 days with no changes. It emails you when this happens. Open **Actions** and click **Enable workflow**, or make any small edit to `config.yaml`.

**For developers:** run `pip install -r requirements.txt`, then `python radar.py --dry-run`. This prints alerts instead of sending them and saves nothing. Add `--no-ai` to skip Claude. The workflow belongs at `.github/workflows/radar.yml`.

## Files
| File | What it is |
|---|---|
| `config.yaml` | Your settings: keywords, brief and sources. The only file you normally edit. |
| `radar.py` | The program. |
| `github-workflow.yml` | Tells GitHub to run the radar every 15 minutes. |
| `requirements.txt` | The Python libraries it needs. |
