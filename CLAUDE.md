# CLAUDE.md

## Purpose

This repository contains a root-run Python updater for Plex Media Server on Debian-family Linux hosts. Its job is to keep Plex on the Plex Pass Beta branch while avoiding mid-stream restarts by checking Tautulli before installing updates.

## Deployment Target

- Primary target: Ubuntu or Debian-family VMs
- Package manager: `dpkg`
- Service manager: `systemd`
- Plex installation type: official `.deb`
- Tautulli installation: local, with snap-based DB path supported by default

## Architecture

- `src/plex_beta_updater.py` is the only runtime program.
- The runtime flow is:
  1. Read config from environment file and process env
  2. Determine the installed Plex version
  3. Read `PlexOnlineToken` from Plex `Preferences.xml`
  4. Query Plex updater metadata
  5. Ask Tautulli whether any active or paused sessions exist
  6. If Tautulli API is unavailable, fall back to Plex live sessions, then to filtered Tautulli DB rows
  7. Defer by writing retry state, or download and install the new `.deb`
  8. Ensure `plexmediaserver.service` is running afterward
- `systemd/` contains the scheduling layer.
- `scripts/install.sh` copies the script, config template, and unit files into host paths.

## File Ownership And Expectations

- `/usr/local/libexec/plex-beta-updater`: installed updater executable
- `/etc/plex-beta-updater.env`: root-readable overrides
- `/var/lib/plex-beta-updater/retry-pending.json`: retry marker state
- `/var/cache/plex-beta-updater/`: downloaded package cache

Keep docs, unit files, and install paths aligned. If one path changes, update all three.

## Coding Preferences

- Prefer Python standard library only unless a dependency is clearly necessary.
- Keep the updater logic testable with small methods and injectable behavior.
- Prefer explicit JSON output for CLI commands over informal text.
- Avoid hidden magic in the install script.
- Preserve ASCII unless the file already needs something else.

## Safety Rules

- Do not commit real tokens, local secrets, or host-specific values.
- Do not add commands that modify the live Plex host during tests.
- Treat Tautulli's `sessions` table as the last-resort fallback source of truth for activity, not for Plex version metadata.
- Ignore Tautulli DB rows that already have a `stopped` timestamp.
- Treat paused sessions as busy.
- On install, auth, or download failures, clear retry state so the hourly timer does not loop forever.

## Development Guidance

- Before changing updater behavior, run:
  - `python3 -m unittest discover -s tests -v`
  - `python3 src/plex_beta_updater.py --help`
- If you change config names, update:
  - `README.md`
  - `config/plex-beta-updater.env.example`
  - `scripts/install.sh`
  - `systemd/*`
- If you change retry behavior, update both the service docs and the tests.

## Verification Checklist

- Daily and retry unit names match the docs and install script.
- `dry-run`, `run-daily`, `run-retry`, and `status` still work.
- Tautulli API failure still falls back to Plex live sessions and then SQLite.
- Retry state is written on busy servers and cleared on successful install or hard failure.
- README commands are accurate and copy-pasteable.
