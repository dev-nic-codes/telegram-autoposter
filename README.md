# Telegram AutoPoster

A local-first Reddit-to-Telegram publishing system created by Nic. It supports multiple bot profiles, admin approvals, schedules, duplicate protection, media processing, analytics, health checks, and recovery controls.

> Operate only in communities and channels you are authorized to manage. Respect Reddit's API terms, subreddit rules, content rights, Telegram rules, and local law.

## Requirements

- Python 3.10 or newer; Python 3.12 is the production baseline.
- A Telegram bot token and an authorized admin user/chat ID.
- A Reddit application client ID and secret.
- `ffmpeg` and `ffprobe` for video inspection and conversion.

## Windows setup

```powershell
git clone https://github.com/dev-nic-codes/telegram-autoposter.git
cd telegram-autoposter
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\start.ps1
```

`setup.bat` provides the same setup with a double-clickable launcher. Build the
optional executable with `build.bat`; the result is `dist\TelegramAutoposter.exe`.

## Linux setup

```bash
git clone https://github.com/dev-nic-codes/telegram-autoposter.git
cd telegram-autoposter
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

`start.ps1` and `start.sh` use the non-interactive supervisor in `run_bots.py`.
It exits with an error if every bot worker dies, allowing systemd or another
process manager to restart the application.

Keep `config.json`, state files, credentials, logs, downloads, and backups out of Git. The repository ignore rules cover these runtime files.

## Reddit access

Reliable fetching requires app-only OAuth. Add your Reddit application credentials and a descriptive user agent to `config.json`:

```json
{
  "user_agent": "windows:telegram-autoposter:v2.0 (by /u/your_username)",
  "reddit_client_id": "YOUR_CLIENT_ID",
  "reddit_client_secret": "YOUR_CLIENT_SECRET"
}
```

Create an application at `https://www.reddit.com/prefs/apps`. A startup preflight check detects permanent authentication failures early.

## Core features

- Single-bot and multi-bot profiles with isolated state.
- Automatic schedules, quiet hours, peak periods, and per-day overrides.
- Private previews with approve, queue, re-roll, skip, and block actions.
- Canonical URL, crosspost, fuzzy-title, and optional author-cooldown deduplication.
- Weighted content scoring using votes, comments, freshness, media type, title quality, and source repetition.
- Configurable captions, rotated variants, spoilers, reactions, and per-subreddit rules.
- Reddit galleries, direct media, Imgur, hosted media pages, and video conversion.
- Image size, aspect, blur, screenshot, and text-density filters.
- Analytics, on-demand activity summaries, health reports, error history, and recovery tools.
- Emergency pause rules for repeated Reddit, Telegram, download, or empty-feed failures.
- Optional local dashboard for status, queue, settings, and recent activity.
- Redacted configuration exports and restorable ZIP backups.

## Multi-bot configuration

The original single-bot format remains supported. For multiple bots, add a `bots` array and keep shared defaults at the top level. Each profile receives a separate runtime thread and state file. See `config.multibot.example.json` for a complete example.

## Scheduling and source rules

`weekly_schedule` supports `weekday`, `weekend`, and individual weekday names. A schedule can change the interval, active window, quiet hours, peak-hour interval, or pause state.

Per-subreddit rules can override:

- minimum votes and maximum post age;
- allowed media type and NSFW filtering;
- caption template, footer, or variants;
- source priority weight.

## Admin controls

Use `/menu` in the configured private admin chat. The deliberately compact
panel provides three review actions: publish the pending item, skip it, or
fetch a new candidate. Scheduling, sources, media policy, captions, duplicate
protection, and recovery behavior are configured through `config.json` or the
interactive setup wizard.

Administrative messages and callback queries are restricted to configured IDs.

## Content safety and recovery

Successful and skipped items are recorded so they are not selected again. Optional scoring ranks usable candidates while retaining weighted variety. Media is validated before upload; oversized or incompatible videos can be converted or compressed when enabled.

Auto-recovery can retry failed uploads, compress media for another attempt, skip stuck previews, and alert admins after repeated failures. Emergency rules pause posting when configured failure thresholds are reached.

## Local dashboard and backups

The optional dashboard binds to the configured local address and shows bot status, queue, settings, sources, recent posts, and recent logs. Keep it private and do not expose it directly to the internet.

Backup tools can create ZIP snapshots, validate and restore them, export redacted configurations, and duplicate bot profiles. Restores create a safety backup first and never restore secrets from a redacted export.

## Validation

```bash
.venv/bin/python scripts/doctor.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py run_bots.py setup_wizard.py src scripts tests
.venv/bin/python -m ruff check .
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

Use `scripts/doctor.py --online` to verify the Telegram bot token and channel
administrator access without posting a message. The offline checks validate
the configuration and required media tools.

## Unattended service

An example unit is provided at `deploy/telegram-autoposter.service`. Adjust its
user and paths, install it under `/etc/systemd/system`, then enable it with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-autoposter.service
```

## Security

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability. Never publish bot tokens, Reddit credentials, admin IDs, state files, private links, or production logs. Use GitHub's private vulnerability reporting flow for security issues.

## License

Copyright (c) 2026 Nic. All rights reserved. This repository is source-available, not open source. See [LICENSE](LICENSE).
