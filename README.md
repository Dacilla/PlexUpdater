# Plex Beta Auto-Updater

`plex-beta-updater` keeps a Linux Plex Media Server on the Plex Pass Beta branch without restarting it in the middle of playback.

It checks for a newer beta release every day at `03:30`, asks Tautulli whether anyone is actively watching or paused, and only installs the update once the server is idle. If the server is busy, it writes a retry marker and checks again every hour until it can update safely.

## What It Does

- Detects the currently installed `plexmediaserver` version with `dpkg-query`
- Reads the Plex account token from `Preferences.xml`
- Uses Plex's updater endpoints to discover the latest beta build and download URL
- Uses Tautulli activity as the primary gate for whether the update can proceed
- Falls back to live Plex session state, then to Tautulli's SQLite `sessions` table if the API key is missing or invalid
- Downloads and installs the matching `.deb`
- Starts `plexmediaserver.service` after install and verifies the version
- Uses systemd timers plus `flock` so overlapping runs do not fight each other

## Requirements

- Ubuntu or another Debian-family distro with `dpkg`, `systemd`, and `python3`
- Plex Media Server installed from the official `.deb`
- A claimed Plex server with a valid `PlexOnlineToken` in:
  - `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml`
- Tautulli installed locally
  - Default snap path supported: `/var/snap/tautulli/current/tautulli.db`
- Root privileges when running the updater for real

## Repository Layout

- `src/plex_beta_updater.py`: main updater CLI
- `systemd/`: daily and retry service/timer units
- `config/plex-beta-updater.env.example`: root-readable config template
- `scripts/install.sh`: installs the updater and unit files onto a host
- `tests/test_updater.py`: unit tests for core behavior
- `CLAUDE.md`: contributor and coding-agent guidance

## Installation

1. Clone this repository onto the Plex host.
2. Install the updater files:

```bash
sudo ./scripts/install.sh
```

3. Review the installed config:

```bash
sudo nano /etc/plex-beta-updater.env
```

4. Dry-run the updater:

```bash
sudo /usr/local/libexec/plex-beta-updater dry-run
```

5. Enable the timers:

```bash
sudo systemctl enable --now plex-beta-updater.timer plex-beta-updater-retry.timer
```

## Configuration

The updater reads `/etc/plex-beta-updater.env` via systemd `EnvironmentFile=` and also supports `--config /path/to/file` for manual runs.

Common settings:

```dotenv
TAUTULLI_URL=http://127.0.0.1:8181
TAUTULLI_API_KEY=
TAUTULLI_DB_PATH=/var/snap/tautulli/current/tautulli.db
PLEX_BASE_URL=http://127.0.0.1:32400
PLEX_PREFERENCES_XML=/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml
DOWNLOAD_CACHE_DIR=/var/cache/plex-beta-updater
STATE_DIR=/var/lib/plex-beta-updater
RETRY_STATE_FILE=/var/lib/plex-beta-updater/retry-pending.json
```

Advanced Plex updater overrides are available if your host needs them:

```dotenv
PLEX_UPDATER_PRODUCT=5
PLEX_UPDATER_BUILD=linux-x86_64
PLEX_UPDATER_CHANNEL=16
PLEX_UPDATER_DISTRIBUTION=debian
REQUEST_TIMEOUT=30
```

`TAUTULLI_API_KEY` is optional. If it is blank or invalid, the updater checks Plex's live session endpoint and only then falls back to the local Tautulli SQLite database. The SQLite fallback ignores rows that already have a `stopped` timestamp so stale paused entries do not block updates forever.

## Systemd Units

The repo ships four units:

- `plex-beta-updater.service`: the daily check
- `plex-beta-updater.timer`: runs the daily check at `03:30`
- `plex-beta-updater-retry.service`: retries an update if the server was busy
- `plex-beta-updater-retry.timer`: runs hourly, but only if `/var/lib/plex-beta-updater/retry-pending.json` exists

Both services use:

```text
/usr/bin/flock -n /run/lock/plex-beta-updater.lock ...
```

That keeps the daily and retry jobs from colliding.

## Commands

Check current status:

```bash
sudo /usr/local/libexec/plex-beta-updater status
```

Dry-run the daily flow:

```bash
sudo /usr/local/libexec/plex-beta-updater dry-run
```

Run the daily check manually:

```bash
sudo /usr/local/libexec/plex-beta-updater run-daily
```

Run the retry flow manually:

```bash
sudo /usr/local/libexec/plex-beta-updater run-retry
```

## Logging And Troubleshooting

Inspect the last daily run:

```bash
journalctl -u plex-beta-updater.service -n 100 --no-pager
```

Inspect the retry service:

```bash
journalctl -u plex-beta-updater-retry.service -n 100 --no-pager
```

Useful checks:

- If the updater says it cannot read `Preferences.xml`, it is not running as root.
- If Tautulli API auth fails, verify the configured API key or leave it blank and let the Plex-session and SQLite fallbacks handle activity checks.
- If downloads fail with `403`, verify the Plex server is claimed and `PlexOnlineToken` is present in the preferences file.
- If hourly retries never happen, check whether `/var/lib/plex-beta-updater/retry-pending.json` exists.

## Upgrade And Removal

To upgrade from a newer version of this repo:

```bash
git pull
sudo ./scripts/install.sh
```

To remove the updater:

```bash
sudo systemctl disable --now plex-beta-updater.timer plex-beta-updater-retry.timer
sudo rm -f /etc/systemd/system/plex-beta-updater*.service /etc/systemd/system/plex-beta-updater*.timer
sudo rm -f /usr/local/libexec/plex-beta-updater /etc/plex-beta-updater.env
sudo systemctl daemon-reload
```

## Notes

- The updater treats paused sessions as busy by design.
