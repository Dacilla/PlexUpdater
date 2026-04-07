#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_BIN="/usr/local/libexec/plex-beta-updater"
TARGET_CONFIG="/etc/plex-beta-updater.env"

install -D -m 0755 "${REPO_ROOT}/src/plex_beta_updater.py" "${TARGET_BIN}"

if [[ ! -f "${TARGET_CONFIG}" ]]; then
  install -D -m 0600 "${REPO_ROOT}/config/plex-beta-updater.env.example" "${TARGET_CONFIG}"
  echo "Installed default config to ${TARGET_CONFIG}"
else
  echo "Keeping existing config at ${TARGET_CONFIG}"
fi

install -D -m 0644 "${REPO_ROOT}/systemd/plex-beta-updater.service" /etc/systemd/system/plex-beta-updater.service
install -D -m 0644 "${REPO_ROOT}/systemd/plex-beta-updater.timer" /etc/systemd/system/plex-beta-updater.timer
install -D -m 0644 "${REPO_ROOT}/systemd/plex-beta-updater-retry.service" /etc/systemd/system/plex-beta-updater-retry.service
install -D -m 0644 "${REPO_ROOT}/systemd/plex-beta-updater-retry.timer" /etc/systemd/system/plex-beta-updater-retry.timer

systemctl daemon-reload

cat <<'EOF'
Installation complete.

Next steps:
  1. Review /etc/plex-beta-updater.env
  2. Optional: save your Discord webhook to /etc/plex-beta-updater.discord-webhook
  3. Enable timers:
     sudo systemctl enable --now plex-beta-updater.timer plex-beta-updater-retry.timer
  4. Dry-run the updater:
     sudo /usr/local/libexec/plex-beta-updater dry-run
EOF
