# immi-watch

Hourly change monitor for Australian immigration and education websites — Department of Home
Affairs, the Federal Register of Legislation, every state/territory nomination site (Live in
Melbourne, NSW, QLD, SA, WA, ACT, TAS, NT) and the Department of Education / Study Australia.

Every hour GitHub Actions runs `monitor.py`, which:

1. fetches each page in [`sources.yaml`](sources.yaml) and reduces it to clean text
   (menus, footers, scripts, "last updated" stamps removed);
2. compares it with the previous snapshot in `snapshots/`;
3. re-fetches any changed page once to make sure the change is real;
4. rates each change **high / medium / low** (keyword rules, or Claude if an API key is set);
5. alerts you, and saves a record of the change in `changes/YYYY-MM/`.

## Alerts

| Severity | When | Delivered |
|---|---|---|
| 🔴 high | news items, nomination/invitation rounds, fees, occupation lists, eligibility, a page going offline or coming back, anything on a `priority: high` page | immediately |
| 🟠 medium | other substantive wording changes | immediately |
| ⚪ low | tiny cosmetic edits | one daily digest at 8 am Melbourne time |
| ⚠️ health | a page fails 3 checks in a row (moved, blocked, down), and again when it recovers | immediately |

**Channels** (all optional except the first):

- **GitHub Issue** in this repo, mentioning you → GitHub emails you. Always on.
  Make sure *Settings → Notifications → Email* is enabled on GitHub.
- **Email (SMTP)** — add repo secrets `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`,
  `SMTP_PASSWORD`, `ALERT_EMAIL_TO` (comma-separated), optionally `SMTP_FROM`.
  For Gmail use `smtp.gmail.com` and an [app password](https://myaccount.google.com/apppasswords).
- **Teams / Slack** — add secret `ALERT_WEBHOOK_URL` (an incoming-webhook URL; Teams
  "Workflows" webhooks and Slack incoming webhooks both accept the `{"text": ...}` payload).
- **Claude summaries** — add secret `ANTHROPIC_API_KEY`. Each change then gets a 1–2 sentence
  plain-English summary, key points and a severity judged by Claude (`claude-opus-5-5`,
  low effort; override with env `IMMI_AI_MODEL`). Only changed pages are sent, so cost is a
  few cents a day.

Add secrets at **Settings → Secrets and variables → Actions → New repository secret**.

## Adding or removing pages

Edit `sources.yaml` and commit. A new page is baselined on its next check (no alert), then
monitored. Methods:

- `http` (default) — normal page.
- `homeaffairs` — Home Affairs SharePoint pages (content is read from hidden JSON fields).
- `ha_news` — the Home Affairs news archive API (alerts on each new news item).
- `legislation` — Federal Register of Legislation API: every Act or instrument (incl. LIN
  instruments) registered in the last `days` whose title contains one of `terms`; alerts on
  each newly registered item.
- `rss` — RSS/Atom feed; `keywords` (regex) keeps only matching items, `match: title` matches
  titles only; alerts on each new item.
- `browser` — headless Chromium, for sites behind Cloudflare (Live in Melbourne, TAS, NT…).

Options: `selector` (CSS selector for the content area; if it matches several elements, e.g. one
per news card, all are kept), `priority: high`,
`every: 3` (check every 3 hours), `allow_missing: true` (a 404 is a normal state — you are
alerted when the page is published again, e.g. Victoria's ROI page between program years).

## Running it yourself

```bash
pip install -r requirements.txt
python -m playwright install chromium
python monitor.py --show vic-news       # see the text extracted from one page
python monitor.py --dry-run            # check everything, save nothing, send nothing
python monitor.py --only ha-500,vic-news
python monitor.py --test-alert         # test every configured alert channel
```

On GitHub: **Actions → immi-watch → Run workflow** runs a check now (optionally for selected
ids, or as a test alert).

## Notes

- Requests are spaced 1 s apart and only ~50 pages are fetched per hour.
- Some pages have no plain-HTML equivalent (e.g. the Home Affairs fee table is loaded by
  script); visa pages themselves carry their costs, and fee changes are announced in the
  news feeds, which are watched.
- Alerts tell you *that* something changed. Always read the official page before advising
  clients.

## Office-PC checker (sites that block GitHub)

Some government sites (DEWR, the Education Minister, TEQSA, ASQA, Jobs and Skills Australia,
Parliament, education.gov.au) refuse connections from cloud servers. They are marked
`runner: local` in `sources.yaml` and checked hourly from this PC instead:

- Windows Task Scheduler task **"immi-watch local check"** runs `run_local.vbs` → `run_local.ps1`
  every hour at :40 while you are logged in. It uses your `gh` login to raise the same GitHub
  Issue alerts and pushes its snapshots to this repo (`state-local.json`, log in `local-run.log`).
- If the PC checker stops for more than 24 h (PC off), the GitHub run raises a health alert.
- Remove it with `schtasks /Delete /TN "immi-watch local check" /F`.
