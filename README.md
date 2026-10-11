# NSE Corporate Announcements

A self-hosted dashboard and Telegram notifier for corporate announcements
published on the National Stock Exchange of India.

## What it does

- Fetches corporate announcements from NSE every 15 minutes
- Classifies each announcement into one of 13 standard NSE topics
- Tags each announcement with an impact direction (positive / negative / neutral)
- Presents a sortable summary of topic counts over a user-selected date range
- Drills down into a topic view that lists announcements grouped by date
- Pushes selected topics to Telegram in real time
- Links every notification directly to the source PDF hosted on NSE

## Architecture

Single Flask service on Render, backed by a local SQLite file, with a GitHub
`cache` branch used as durable storage across cold starts.

- `main` branch — application code only
- `cache` branch — runtime data (announcements table, notify settings)

Runtime artifacts that are ephemeral (OTP state, NSE session cookies) are
never persisted and rebuild themselves on demand.

## Pages

- `/announcements` — summary table of topic counts for the selected range
- `/announcements/topic/<topic>` — date-grouped announcements for one topic

Both pages sit behind an OTP login delivered via Telegram.

## Configuration

All tunables live in `config.py` and are overridable via environment
variables. See `config.py` for the full list and defaults.

## Deployment

Deployed as a Render web service on a dedicated account, separate from any
other project. Environment variables required:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SESSION_SECRET_KEY`
- `GITHUB_BACKUP_TOKEN`
- `GITHUB_REPO`

## Notes

- NSE source data is fetched directly over HTTP; no third-party mirrors.
- Announcement PDFs are never re-hosted. Every link points to NSE's own
  document URL.
- The project is intentionally independent of any other service — no shared
  code, no shared bot, no shared database.