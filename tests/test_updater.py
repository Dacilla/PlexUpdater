from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
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


class RemoteFeedUpdater(PlexBetaUpdater):
    def __init__(self, config: Config, xml_root: ET.Element) -> None:
        super().__init__(config)
        self.xml_root = xml_root

    def request_xml(self, url: str, method: str, allow_status: set[int] | None = None) -> ET.Element:
        return self.xml_root

    def build_download_url(self, version: str, token: str) -> str:
        return f"https://downloads.example.invalid/{version}/plexmediaserver_{version}_amd64.deb"


class InstallUpdateTester(PlexBetaUpdater):
    def __init__(
        self,
        config: Config,
        installed_version: str,
        package_versions: list[str],
    ) -> None:
        super().__init__(config)
        self._installed_version = installed_version
        self.package_versions = package_versions
        self.download_calls: list[tuple[str, Path]] = []
        self.commands: list[list[str]] = []

    def download_file(self, url: str, destination: Path) -> None:
        self.download_calls.append((url, destination))
        destination.write_text("downloaded", encoding="utf-8")

    def read_package_version(self, package_path: Path) -> str:
        return self.package_versions.pop(0)

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def get_installed_version(self) -> str:
        return self._installed_version


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

    def test_remote_updater_replaces_generic_download_url_with_direct_deb(self) -> None:
        xml_root = ET.fromstring(
            """
            <MediaContainer>
              <Release version="1.43.1.10576-06378bdcd" downloadURL="https://plex.tv/api/downloads/5?channel=beta" />
            </MediaContainer>
            """
        )
        updater = RemoteFeedUpdater(Config(), xml_root)

        update = updater.check_remote_updater("1.43.0.10492-121068a07", "token")

        self.assertTrue(update.available)
        self.assertEqual(
            update.download_url,
            "https://downloads.example.invalid/1.43.1.10576-06378bdcd/plexmediaserver_1.43.1.10576-06378bdcd_amd64.deb",
        )

    def test_install_update_redownloads_cached_package_when_version_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            package_path = cache_dir / "plexmediaserver_1.43.1.10576-06378bdcd_amd64.deb"
            package_path.write_text("stale", encoding="utf-8")

            updater = InstallUpdateTester(
                config=Config(download_cache_dir=str(cache_dir)),
                installed_version="1.43.1.10576-06378bdcd",
                package_versions=[
                    "1.43.0.10492-121068a07",
                    "1.43.1.10576-06378bdcd",
                ],
            )
            update = UpdateInfo(
                available=True,
                current_version="1.43.0.10492-121068a07",
                target_version="1.43.1.10576-06378bdcd",
                download_url="https://downloads.example.invalid/1.43.1.10576-06378bdcd/plexmediaserver_1.43.1.10576-06378bdcd_amd64.deb",
                source="remote",
            )

            updater.install_update(update)

            self.assertEqual(len(updater.download_calls), 1)
            self.assertEqual(updater.commands, [["dpkg", "-i", str(package_path)]])


if __name__ == "__main__":
    unittest.main()
