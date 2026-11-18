# 🛳 Cruise Reward Alerts: myVIP / POP! Slots → Telegram

**Current filter: Royal Caribbean, actual cruises only (4-night, 5-night, 8-night, 9-night…), departing from Asia.** Free play, onboard credit, discounts, NCL and non-Asia sailings are tracked silently but never alert you. Change this anytime (see *Settings* below).

Sends a Telegram message to your phone within about 5 minutes when:

* 🆕 **a new cruise reward** appears in the myVIP Rewards store, even one that is already sold out by the time it's seen
* 🔁 **a sold-out cruise reward comes back in stock**
* ♻️ **a reward that was pulled from the store returns**
* 📈 **more inventory is added** to a reward that's still on sale

Each alert includes the cruise line, ship, itinerary, sailing date(s), departure port, loyalty-point price, how many are left, the booking deadline and a link to the reward page, wherever the store provides that information.

It runs entirely on free cloud services (GitHub Actions, plus optionally cron-job.org and healthchecks.io). Your computer can be off. It never logs in to POP! Slots or myVIP, never needs your game account, and never redeems anything.

---

## How it works (and why it doesn't just reuse arushae/cruises)

The [arushae/cruises](https://github.com/arushae/cruises) project showed where the data lives: the myVIP reward store website is powered by a public JSON API that needs no login:

| What | URL |
|---|---|
| Cruise category (all lanes) | `https://loyalty-award-api.myvip.co/api/proxy/rewards/section/destination/8` |
| One reward's full page (description, T&Cs) | `https://loyalty-award-api.myvip.co/api/proxy/rewards/award/{OfferID}` |

**This API was verified as live and accurate on 16 Nov 2026.** Quantities changed in real time between requests. For example, "Comp 3-5 Day NCL Cruise" went 98 → 95 within a few hours, and sold-out cruises show `Quantity: 0` with a `SoldOut` badge.

**The upstream tracker itself, however, is not a reliable live source anymore.** Its GitHub Actions job is set to run 4×/hour, and through September it did (median gap 15 min). Since mid-October, GitHub has been running it only every ~6–7 hours (median gap in November: 387 minutes), and its `rewards.json` has grown to ~60 MB. A cruise that sells out in an hour could come and go without that site ever showing it.

So this project reads the official API **directly** and is built to avoid the same failure:

1. **Punctual triggering.** GitHub's built-in scheduler is best-effort and is known to delay or drop runs, so a free external scheduler (cron-job.org) starts the check every 5 minutes. GitHub's own `*/5` schedule remains as a backup.
2. **Watchdog.** After each successful check it pings healthchecks.io. If checks stop for any reason (GitHub outage, expired token, disabled workflow), healthchecks.io messages you on Telegram.
3. **In-band health alerts.** If the myVIP API fails 3 checks in a row, or the Cruise category suddenly returns zero rewards (a sign the API changed), the bot tells you, and tells you again when it recovers.
4. **Small state.** Only the current state of each reward is saved (about 20 KB), on a separate `state` branch, and only when something actually changes.

```
cron-job.org (every 5 min) ──┐
GitHub schedule (backup) ────┴─► GitHub Actions run (~30 s)
                                   ├─ load state from `state` branch
                                   ├─ GET myVIP Cruise feed (+ hourly sweep of other categories)
                                   ├─ compare with state → NEW / RESTOCK / RETURNED / QTY_UP
                                   ├─ fetch reward page for ship / port / dates, send Telegram alert
                                   ├─ save state (only if changed)
                                   └─ ping healthchecks.io  ──► alerts you on Telegram if pings stop
```

**Everything is tracked; only your filter alerts.** The whole Cruise category is tracked (currently Royal Caribbean and Norwegian Cruise Line, including free play, onboard credit and discount rewards). Once an hour it also sweeps the store's other categories (Las Vegas, West Coast, Canada, Asia) for anything cruise-related, in case a cruise reward is filed elsewhere.

---

## Setup (about 25 minutes, all in a web browser)

You need a GitHub account (free), the Telegram app on your iPhone, and optionally free accounts at cron-job.org and healthchecks.io. Steps 1–7 give you a working bot. Steps 8–9 make it punctual and self-monitoring, and are strongly recommended.

### Step 1: Create your private Telegram bot

1. In Telegram, open a chat with **@BotFather** (blue check mark).
2. Send `/newbot`. Give it a name (e.g. `My Cruise Alerts`) and a username ending in `bot` (e.g. `joshua_cruise_alerts_bot`).
3. BotFather replies with a **token** like `7412345678:AAH3k...`. Copy it. Treat it like a password.
4. Tap the link to your new bot and press **START**. (A bot can't message you until you've done this.)

> Only you will get alerts. The bot sends to your chat ID only, and nobody else knows its token.

### Step 2: Create a new GitHub repository

1. Go to <https://github.com/new>.
2. Name: `cruise-reward-alerts`. Visibility: **Public** (see *Why public?* below). Don't add a README.
3. Click **Create repository**.

**Upload the files.** Unzip `cruise-reward-alerts.zip` on your computer, then on the new repo page click **uploading an existing file** and drag in **everything inside** the unzipped folder, including the `.github` folder. Commit to `main`.

Afterwards, check that `.github/workflows/monitor.yml` exists in the repo. Some file managers hide folders starting with a dot. If it's missing, use **Add file → Create new file**, type `.github/workflows/monitor.yml` as the name, and paste the file's contents. Do the same for the other three `.yml` files.

<details><summary>Why public? (and is that safe?)</summary>

GitHub Actions minutes are **unlimited and free for public repositories**. A private repo gets about 2,000 free minutes a month, and checking every 5 minutes uses roughly 8,600 (each run is billed as at least 1 minute).

Making it public exposes only this code and the saved reward data, which is already public in the myVIP store. Your Telegram token, chat ID and other secrets are stored in **GitHub Secrets**, which are encrypted, hidden from logs, and never visible to visitors or forks. The chat-ID helper deliberately replies to you in Telegram instead of printing your ID in the public log.

If you'd rather go private: in step 9, set cron-job.org to every 30 minutes, and in `monitor.yml` change the cron to `*/30 * * * *`. That uses about 1,450 minutes a month.
</details>

### Step 3: Allow Actions to save state

Repo → **Settings → Actions → General** → *Workflow permissions* → select **Read and write permissions** → **Save**.

### Step 4: Add your bot token as a secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |

### Step 5: Get your chat ID

1. Repo → **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Find my Telegram chat ID** → **Run workflow** → **Run workflow**.
3. Within a minute your bot messages you: *"Your chat ID is: 123456789"*.
4. Add another secret: `TELEGRAM_CHAT_ID` = that number.

(If the run says "No chats found", press START or send "hi" to your bot, then run it again.)

### Step 6: Send a test notification

**Actions → Send test notification → Run workflow**, and pick a mode:

| Mode | What you get |
|---|---|
| `basic` | "✅ Test notification" |
| `live-sample` | the test message, plus a **real** reward from the live store right now, formatted exactly like a new-reward alert, plus a count of what's currently listed |
| `demo-alerts` | the offline test scenario's simulated alerts (new reward, restock, inventory top-up, outage/recovery), all clearly labelled DEMO |

Run `live-sample` at least once. It proves that GitHub can reach the myVIP API and that the ship/port/date extraction works on today's real rewards.

### Step 7: Start the monitor

**Actions → Cruise reward monitor → Run workflow.** You'll receive **"🛳 Cruise reward monitor is live"** with the full list of cruise rewards currently in the store and whether each can be bought. Compare it with the app: it should match. From now on you only hear about changes.

At this point the bot works, using GitHub's own (sometimes late) schedule. Steps 8 and 9 make it punctual and self-monitoring.

### Step 8 (recommended): Watchdog with healthchecks.io

1. Sign up free at <https://healthchecks.io>.
2. **Add Check.** Name it `Cruise monitor`. Set **Period = 10 minutes** and **Grace = 20 minutes**. Save.
3. Copy its **ping URL** (`https://hc-ping.com/xxxxxxxx-...`) and add it as a GitHub secret named `HEALTHCHECK_URL`.
4. In healthchecks.io go to **Integrations → Telegram → Add Integration**. It opens their bot. Press START, tap the confirmation link, and choose your project.

If no successful check happens for about 30 minutes, for any reason, you'll get a Telegram message from the Healthchecks bot. You get another when it's back to normal.

### Step 9 (recommended): Punctual 5-minute checks with cron-job.org

GitHub's scheduler often runs late or skips runs. This step starts the check precisely every 5 minutes.

**a) Create a narrowly scoped GitHub token**

1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Name: `cron-job.org cruise trigger`. Expiration: the longest offered (put a reminder in your calendar).
3. **Repository access: Only select repositories → `cruise-reward-alerts`.**
4. **Permissions → Repository permissions → Actions: Read and write.** Leave everything else as "No access".
5. Generate, then copy the token (`github_pat_...`).

This token can only start or stop workflows in this one repo. It can't read your other repos or change code.

**b) Create the cron job**

1. Sign up free at <https://cron-job.org> → **Create cronjob**.
2. **URL:** `https://api.github.com/repos/YOUR-GITHUB-USERNAME/cruise-reward-alerts/actions/workflows/monitor.yml/dispatches`
3. **Schedule:** every 5 minutes.
4. **Advanced** tab:
   * Request method: **POST**
   * Headers:
     * `Accept` = `application/vnd.github+json`
     * `Authorization` = `Bearer github_pat_...your token...`
     * `X-GitHub-Api-Version` = `2022-11-28`
     * `Content-Type` = `application/json`
   * Request body: `{"ref":"main"}`
5. Under notifications, enable **"notify me when execution fails"**.
6. Save, then click **Test run**. The response should be **204 No Content**. You'll see a new run appear in your repo's Actions tab.

Optional: once cron-job.org is working, you can remove the duplicate GitHub timer by deleting the two `schedule:` lines from `monitor.yml`. Leaving them is harmless, because overlapping runs queue up instead of clashing.

### Step 10: iPhone notification settings

* iPhone **Settings → Notifications → Telegram**: Allow Notifications on, with Lock Screen and Banners enabled.
* In Telegram, open your bot's chat → tap its name → make sure **Notifications** are on (not muted). Consider giving it a distinctive sound.
* If you use a Focus mode, add Telegram to its allowed apps.

Alerts for new rewards and restocks make a sound. The startup list, daily check-in and recovery notices are sent silently.

---

## What the alerts look like

```
🆕 NEW cruise reward

Royal Caribbean
7 Night Western Caribbean
🚢 Ship: Wonder of the Seas
🗺 Itinerary: A Cruise for Two - 7 Night Western Caribbean 2/14 (7 nights)
📅 Sailing: 02/14/2027
⚓ Departs: Port Canaveral, Florida
💎 Price: 400,000 pts
📦 ✅ 4 left
🗓 Book cruise by: January 15, 2027
🏷 Badge: NEW REWARD!
🔗 Open in myVIP Rewards
Detected 16 Nov 09:10 +07 · ID 99001
```

```
🔁 BACK IN STOCK

Royal Caribbean
4 Night Chan May Cruise
🚢 Ship: Spectrum of the Seas
🗺 Itinerary: A Cruise for Two - 4 Night Chan May Cruise 11/16 (4 nights)
📅 Sailing: 11/16/2026
⚓ Departs: Hong Kong, China
💎 Price: 230,000 pts
📦 ✅ 3 left
↕️ Quantity: 0 → 3
```

Plus a silent **🩺 Daily check-in** each morning (09:00 Bangkok time by default) listing every cruise reward and its status, so you can see the monitor is alive even on quiet days.

---

## Settings you can change (no code needed)

Repo → **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:

| Variable | Default | Meaning |
|---|---|---|
| `ONLY_CRUISE_LINE` | `Royal Caribbean` | Only alert for this cruise line; `any` = all cruise lines |
| `ONLY_FULL_CRUISES` | `true` | Only actual "N Night" sailings; `false` also alerts on free play, credits and discounts |
| `ONLY_DEPARTURE_REGION` | `asia` | Only sailings from Asian ports (Hong Kong, Shanghai, Singapore, Japan, Taiwan, Korea, SE Asia…); `any` = anywhere. If the port can't be read, you're alerted anyway |
| `MIN_DAYS_BEFORE_SAILING` | `3` | Don't alert for a cruise that sails sooner than this many days from now (or has already sailed) |
| `ALERT_NEW` | `true` | Alert on brand-new rewards |
| `ALERT_RESTOCK` | `true` | Alert when sold-out or pulled rewards come back |
| `ALERT_QUANTITY_INCREASE` | `true` | Alert when inventory is topped up on a reward that's still for sale |
| `ALERT_SOLD_OUT` | `false` | Also send a (silent) message when something sells out |
| `ALERT_PRICE_CHANGE` | `false` | Also send a (silent) message when a point price changes |
| `DAILY_SUMMARY_HOUR` | `9` | Local hour for the daily check-in; `-1` turns it off |
| `TIMEZONE` | `Asia/Bangkok` | Time zone for timestamps and the daily check-in |
| `SWEEP_DESTINATIONS` | `1,2,3,7` | Other store categories swept hourly for cruise rewards |
| `REMOVAL_CONFIRM_CHECKS` | `2` | Consecutive checks a reward must be missing before it counts as "pulled" |
| `FAILURE_ALERT_THRESHOLD` | `3` | Consecutive API failures before you're warned |

Secrets: `TELEGRAM_BOT_TOKEN` (required), `TELEGRAM_CHAT_ID` (required), `HEALTHCHECK_URL` (recommended).

---

## Reliability details

* **Only obtainable rewards.** Right before alerting, the bot re-reads the reward's own page in the store (the live view). It only messages you if that page confirms at least 1 is purchasable (if the page can't be read or shows no quantity, nothing is sent), the reward hasn't expired, the booking deadline hasn't passed, and the sailing date is at least 3 days away. Anything else is recorded silently. A new reward that's already sold out isn't sent; you'll hear about it if it comes back in stock.
* **No repeat alerts.** Alerts fire on a *change of state* (new → seen, sold out → available). The new state is saved only after Telegram confirms delivery. An unchanged reward never alerts twice, and if Telegram is unreachable the alert is retried on the next check rather than lost.
* **No false restocks from API blips.** A reward missing from one response isn't treated as removed. It must be missing for 2 consecutive checks. An empty Cruise feed is never treated as "everything was removed". The bot warns you instead and touches nothing.
* **The first run is a baseline**, not a flood of alerts. Newly swept categories are also baselined silently.
* **Mass changes** (more than 15 at once, e.g. a store refresh) arrive as one digest, plus individual messages for the new and restocked items.
* **A crash** (a bug, or a corrupted state file) sends a 🛑 Telegram message, marks the healthchecks.io check as failed, and GitHub emails you about the failed run. The state is never silently reset.
* **Logs.** Every run's summary is shown on its page in the Actions tab ("Cruise reward check"). The `state` branch's `state.json` also keeps a log of the last 400 events.

---

## Maintenance

| When | What to do |
|---|---|
| **Healthchecks says "DOWN"** | Open the repo's **Actions** tab. If runs are failing, open the latest one to read the error. If there are no recent runs, check cron-job.org's execution history. A **401** there means the GitHub token expired: generate a new one (step 9a) and paste it into the cron job's `Authorization` header. |
| **Your fine-grained token's expiry date** | Regenerate it and update cron-job.org (takes 2 minutes). Healthchecks will warn you if you forget. |
| **"This scheduled workflow is disabled…" banner** | Click **Enable workflow** in the Actions tab. Each run re-enables itself to prevent this, but if it happens, healthchecks will alert you. |
| **Bot says "myVIP API has failed N checks"** | Usually a temporary outage that fixes itself (you'll get a "recovered" message). If it lasts more than a day, the API may have changed: open an issue or ask for help with the error text in the message. |
| **Bot says "Cruise category returned zero rewards"** | Check the app. If cruises are listed there, the API changed. |
| **Want to start fresh** (e.g. after changing settings) | Delete the `state` branch (repo → branches → 🗑). The next run sends a new baseline list. |
| **Pause alerts** | Actions → Cruise reward monitor → ⋯ → **Disable workflow**, and pause the cron-job.org job. |
| **Check what it currently knows** | Open the `state` branch → `state.json`, or read the latest daily check-in. |

Ongoing cost: **$0**. No servers, no paid plans.

---

## Accuracy and limitations (please read)

* **It sees what the public store shows.** The API is the same one the myVIP Rewards website uses, read without logging in. Some rewards in the store are marked as tier- or segment-targeted (`MinVipTier`, `IsSegmented`). If myVIP ever shows your account a cruise reward it doesn't show the public store, this bot won't see it. Every cruise reward seen during testing was in the public feed.
* **Detection window.** Checks run every ~5 minutes, and a GitHub run takes ~30–60 seconds to start. A reward that is added *and* sold out within the same ~5-minute gap still triggers a "NEW (already sold out)" alert, as long as it's still listed when the check happens. A reward added and removed entirely between two checks can't be seen by any poller.
* **Ship, port and dates are read from the reward's text.** The API doesn't have structured fields for them, so the bot pattern-matches the title, description and Terms & Conditions. It was tested against every current cruise reward (Royal Caribbean and NCL). If a field can't be found, it's left out rather than guessed, so always confirm in the app before buying.
* **"Open in myVIP Rewards"** links to the web reward store page for that offer. Buy through the POP! Slots app or myVIP as you normally would.
* This project is independent, not affiliated with PLAYSTUDIOS or myVIP, and reads a public endpoint at a polite rate (one small request every 5 minutes, plus an hourly sweep).

---

## For the curious: running it locally

Requires Python 3.10+ and no extra packages.

```bash
python3 -m unittest discover -s tests -v   # 38 tests
python3 -m cruisewatch demo                # offline simulation of new/restock/outage detection
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 -m cruisewatch test --live
python3 -m cruisewatch check               # one real monitoring pass (state in data/state.json)
python3 -m cruisewatch status              # what's tracked + recent events
```

Layout:

```
cruisewatch/source.py    myVIP API client and parsing
cruisewatch/extract.py   ship / port / dates / itinerary from reward text
cruisewatch/detect.py    change detection and duplicate prevention
cruisewatch/filters.py   which rewards alert you (Royal Caribbean / Asia / real cruises)
cruisewatch/monitor.py   one monitoring pass, health alerts, watchdog ping
cruisewatch/messages.py  Telegram message formatting
cruisewatch/telegram.py  Telegram Bot API client
cruisewatch/demo.py      offline end-to-end scenario
scripts/state.sh         saves and loads state on the `state` branch
.github/workflows/       monitor, test notification, chat-ID helper, unit tests
```

Credit to [arushae/cruises](https://github.com/arushae/cruises) for finding the myVIP reward endpoints. No code was copied from it (it has no license); this is an independent implementation.
