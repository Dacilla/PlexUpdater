from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plex_beta_updater import (
    ActivityInfo,
    Config,
    PlexBetaUpdater,
    UpdateInfo,
    UpdaterError,
    parse_env_file,
)


class FakeUpdater(PlexBetaUpdater):
    def __init__(
        self,
        config: Config,
        installed_version: str,
        update: UpdateInfo,
        activity: ActivityInfo,
        fail_install: bool = False,
    ) -> None:
        super().__init__(config)
        self._installed_version = installed_version
        self._update = update
        self._activity = activity
        self._fail_install = fail_install
        self.install_calls = 0
        self.discord_messages: list[str] = []

    def get_installed_version(self) -> str:
        return self._installed_version

    def read_plex_online_token(self) -> str:
        return "token"

    def check_for_update(self, installed_version: str, token: str) -> UpdateInfo:
        return self._update

    def get_activity(self, plex_token: str | None = None) -> ActivityInfo:
        return self._activity

    def install_update(self, update: UpdateInfo) -> None:
        self.install_calls += 1
        if self._fail_install:
            raise RuntimeError("boom")
        self._installed_version = update.target_version

    def ensure_service_running(self) -> None:
        return None

    def send_discord_notification(self, message: str) -> None:
        self.discord_messages.append(message)


class UpdaterTests(unittest.TestCase):
    def test_parse_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / "test.env"
            env_path.write_text("TAUTULLI_API_KEY=abc123\n# ignored\nREQUEST_TIMEOUT=90\n", encoding="utf-8")
            values = parse_env_file(env_path)
            self.assertEqual(values["TAUTULLI_API_KEY"], "abc123")
            self.assertEqual(values["REQUEST_TIMEOUT"], "90")

    def test_db_activity_reads_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tautulli.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sessions (state TEXT, user TEXT, full_title TEXT, started INTEGER, stopped INTEGER)")
            connection.execute(
                "INSERT INTO sessions (state, user, full_title, started, stopped) VALUES (?, ?, ?, ?, ?)",
                ("paused", "Dacilla", "Game Changer", 1, 0),
            )
            connection.commit()
            connection.close()

            config = Config(tautulli_db_path=str(db_path))
            updater = PlexBetaUpdater(config)
            activity = updater.get_activity_via_db()
            self.assertEqual(activity.active_count, 1)
            self.assertEqual(activity.sessions[0]["state"], "paused")

    def test_db_activity_ignores_stopped_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tautulli.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sessions (state TEXT, user TEXT, full_title TEXT, started INTEGER, stopped INTEGER)")
            connection.execute(
                "INSERT INTO sessions (state, user, full_title, started, stopped) VALUES (?, ?, ?, ?, ?)",
                ("paused", "Dacilla", "Game Changer", 1, 1755376097),
            )
            connection.commit()
            connection.close()

            config = Config(tautulli_db_path=str(db_path))
            updater = PlexBetaUpdater(config)
            activity = updater.get_activity_via_db()
            self.assertEqual(activity.active_count, 0)

    def test_busy_run_writes_retry_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            retry_path = Path(tmpdir) / "retry.json"
            config = Config(retry_state_file=str(retry_path))
            updater = FakeUpdater(
                config=config,
                installed_version="1.43.0.10492-121068a07",
                update=UpdateInfo(
                    available=True,
                    current_version="1.43.0.10492-121068a07",
                    target_version="1.43.1.10576-b446a0e28",
                    download_url="https://example.invalid/plex.deb",
                    source="test",
                ),
                activity=ActivityInfo(
                    active_count=1,
                    source="db",
                    sessions=[{"state": "paused", "user": "alex", "title": "Example"}],
                ),
            )

            result = updater.run(mode="run-daily")
            self.assertEqual(result.action, "deferred")
            self.assertTrue(retry_path.exists())
            payload = json.loads(retry_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_count"], 1)
            self.assertEqual(payload["target_version"], "1.43.1.10576-b446a0e28")
            self.assertEqual(updater.discord_messages, [])

    def test_install_failure_clears_pending_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            retry_path = Path(tmpdir) / "retry.json"
            retry_path.write_text("{}", encoding="utf-8")
            config = Config(retry_state_file=str(retry_path))
            updater = FakeUpdater(
                config=config,
                installed_version="1.43.0.10492-121068a07",
                update=UpdateInfo(
                    available=True,
                    current_version="1.43.0.10492-121068a07",
                    target_version="1.43.1.10576-b446a0e28",
                    download_url="https://example.invalid/plex.deb",
                    source="test",
                ),
                activity=ActivityInfo(active_count=0, source="db", sessions=[]),
                fail_install=True,
            )

            with self.assertRaises(UpdaterError):
                updater.run(mode="run-retry")
            self.assertFalse(retry_path.exists())
            self.assertEqual(len(updater.discord_messages), 2)
            self.assertIn("Plex update starting", updater.discord_messages[0])
            self.assertIn("Plex update failed", updater.discord_messages[1])

    def test_successful_install_sends_start_and_finish_notifications(self) -> None:
        config = Config(discord_webhook_url="https://example.invalid/webhook")
        updater = FakeUpdater(
            config=config,
            installed_version="1.43.0.10492-121068a07",
            update=UpdateInfo(
                available=True,
                current_version="1.43.0.10492-121068a07",
                target_version="1.43.1.10576-b446a0e28",
                download_url="https://example.invalid/plex.deb",
                source="remote",
            ),
            activity=ActivityInfo(active_count=0, source="db", sessions=[]),
        )

        result = updater.run(mode="run-daily")

        self.assertEqual(result.action, "installed")
        self.assertEqual(len(updater.discord_messages), 2)
        self.assertIn("Plex update starting", updater.discord_messages[0])
        self.assertIn("1.43.0.10492-121068a07 -> 1.43.1.10576-b446a0e28", updater.discord_messages[0])
        self.assertIn("Plex update finished", updater.discord_messages[1])

    def test_discord_webhook_url_reads_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            webhook_path = Path(tmpdir) / "discord-webhook"
            webhook_path.write_text("https://example.invalid/webhook\n", encoding="utf-8")

            updater = PlexBetaUpdater(Config(discord_webhook_file=str(webhook_path)))

            self.assertEqual(updater.discord_webhook_url(), "https://example.invalid/webhook")


if __name__ == "__main__":
    unittest.main()
