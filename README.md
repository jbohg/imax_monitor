# Odyssey Ticket Monitor — AMC Metreon 16 & IMAX

Watches AMC showtimes for *The Odyssey* at AMC Metreon 16 (San Francisco), filters
them against your weekday/time/format preferences, and pings you on Telegram when
matching tickets are available — including when a previously sold-out IMAX show
frees up (people return tickets all the time).

Runs entirely on GitHub Actions. No server needed.

## Setup

### 1. Get your Telegram chat ID

You already have a bot token from BotFather. Now:

1. Open a chat with your bot in Telegram and send it any message (e.g. `/start`).
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
3. Find `"chat":{"id": 123456789, ...}` in the response — that number is your chat ID.

### 2. Get an AMC API key (recommended)

Request a free key at https://developers.amctheatres.com (Getting Started →
obtaining an API key). The official API is far more reliable than scraping and
exposes `isSoldOut` / `isAlmostSoldOut` per showtime.

If you skip this, the script falls back to scraping the AMC showtimes page —
this works until it doesn't (site changes, or AMC's bot protection blocks
GitHub's datacenter IPs). The monitor will message you if all fetches start failing.

### 3. Create the repo

1. Create a **private** GitHub repo and push these files.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN` — from BotFather
   - `TELEGRAM_CHAT_ID` — from step 1
   - `AMC_API_KEY` — from step 2 (omit to use the scrape fallback)

### 4. Set your preferences

Edit `config.yaml` — the `schedule` block is where your weekday/time constraints
live (only showtimes *starting* inside a window count), and `formats_any`
controls format (`imax`, `70mm`, `dolby`, ...). `days_ahead` sets how far into
the future to scan.

### 5. Turn it on

Go to the **Actions** tab, enable workflows, and run **Odyssey ticket monitor**
manually once (`workflow_dispatch`) to verify you get a Telegram message or a
clean "No changes" log. After that it runs every 20 minutes automatically.

## Notes & gotchas

- **GitHub cron is UTC and best-effort** — runs can lag 5–15 minutes. Fine for
  this; don't rely on it for on-sale-second sniping.
- **Scheduled workflows pause after ~60 days of repo inactivity** on free plans.
  The workflow commits `state.json` on changes, which counts as activity, but if
  the repo goes fully quiet GitHub emails you before disabling — just re-enable.
- **Be polite**: every 20 minutes is plenty. Don't crank it to every 5.
- `state.json` is the dedupe memory. Delete it (and commit) to reset and get
  re-notified about everything currently available.
- To watch a different movie or theatre later, just edit `movie_match` /
  `theatre_query` in `config.yaml`.
