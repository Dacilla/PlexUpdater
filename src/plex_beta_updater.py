#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_VERSION = "0.1.0"
DEFAULT_CONFIG_PATH = "/etc/plex-beta-updater.env"


class UpdaterError(RuntimeError):
    pass


@dataclass
class Config:
    tautulli_url: str = "http://127.0.0.1:8181"
    tautulli_api_key: str = ""
    tautulli_db_path: str = "/var/snap/tautulli/current/tautulli.db"
    plex_base_url: str = "http://127.0.0.1:32400"
    plex_preferences_xml: str = (
        "/var/lib/plexmediaserver/Library/Application Support/"
        "Plex Media Server/Preferences.xml"
    )
    download_cache_dir: str = "/var/cache/plex-beta-updater"
    state_dir: str = "/var/lib/plex-beta-updater"
    retry_state_file: str = "/var/lib/plex-beta-updater/retry-pending.json"
    package_name: str = "plexmediaserver"
    service_name: str = "plexmediaserver.service"
    request_timeout: int = 30
    discord_webhook_url: str = ""
    discord_webhook_file: str = "/etc/plex-beta-updater.discord-webhook"
    plex_updater_product: str = "5"
    plex_updater_build: str = "linux-x86_64"
    plex_updater_channel: str = "16"
    plex_updater_distribution: str = "debian"
    plex_client_identifier: str = "plex-beta-updater"
    plex_product_name: str = "Plex Beta Updater"
    plex_device_name: str = socket.gethostname()

    @classmethod
    def from_sources(cls, config_path: str | None) -> "Config":
        values: dict[str, str] = {}
        if config_path:
            values.update(parse_env_file(Path(config_path)))
        values.update({key: value for key, value in os.environ.items() if key in ENV_MAP})

        kwargs: dict[str, Any] = {}
        for env_name, field_name in ENV_MAP.items():
            if env_name not in values:
                continue
            value: Any = values[env_name]
            if field_name == "request_timeout":
                value = int(value)
            kwargs[field_name] = value
        return cls(**kwargs)


ENV_MAP = {
    "TAUTULLI_URL": "tautulli_url",
    "TAUTULLI_API_KEY": "tautulli_api_key",
    "TAUTULLI_DB_PATH": "tautulli_db_path",
    "PLEX_BASE_URL": "plex_base_url",
    "PLEX_PREFERENCES_XML": "plex_preferences_xml",
    "DOWNLOAD_CACHE_DIR": "download_cache_dir",
    "STATE_DIR": "state_dir",
    "RETRY_STATE_FILE": "retry_state_file",
    "PLEX_PACKAGE_NAME": "package_name",
    "PLEX_SERVICE_NAME": "service_name",
    "REQUEST_TIMEOUT": "request_timeout",
    "DISCORD_WEBHOOK_URL": "discord_webhook_url",
    "DISCORD_WEBHOOK_FILE": "discord_webhook_file",
    "PLEX_UPDATER_PRODUCT": "plex_updater_product",
    "PLEX_UPDATER_BUILD": "plex_updater_build",
    "PLEX_UPDATER_CHANNEL": "plex_updater_channel",
    "PLEX_UPDATER_DISTRIBUTION": "plex_updater_distribution",
    "PLEX_CLIENT_IDENTIFIER": "plex_client_identifier",
    "PLEX_PRODUCT_NAME": "plex_product_name",
    "PLEX_DEVICE_NAME": "plex_device_name",
}


@dataclass
class UpdateInfo:
    available: bool
    current_version: str
    target_version: str = ""
    download_url: str = ""
    source: str = ""
    notes: str = ""


@dataclass
class ActivityInfo:
    active_count: int
    source: str
    sessions: list[dict[str, Any]]


@dataclass
class RunResult:
    action: str
    message: str
    installed_version: str
    update: UpdateInfo | None = None
    activity: ActivityInfo | None = None
    retry_pending: bool = False


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def version_key(version: str) -> tuple[int, int, int, int, str]:
    prefix, _, suffix = version.partition("-")
    parts = prefix.split(".")
    ints = [int(part) for part in parts[:4]]
    while len(ints) < 4:
        ints.append(0)
    return ints[0], ints[1], ints[2], ints[3], suffix


class PlexBetaUpdater:
    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("plex_beta_updater")

    def run(self, mode: str, dry_run: bool = False) -> RunResult:
        retry_pending = self.retry_state_path.exists()
        if mode == "run-retry" and not retry_pending:
            return RunResult(
                action="noop",
                message="No retry is currently pending.",
                installed_version=self.get_installed_version(),
                retry_pending=False,
            )

        installed_version = self.get_installed_version()
        token = self.read_plex_online_token()

        try:
            update = self.check_for_update(installed_version, token)
            if not update.available:
                if not dry_run:
                    self.clear_retry_state()
                return RunResult(
                    action="noop",
                    message="Plex is already on the latest beta version.",
                    installed_version=installed_version,
                    update=update,
                    retry_pending=False,
                )

            activity = self.get_activity(token)
            if activity.active_count > 0:
                message = (
                    f"Deferred Plex update to {update.target_version}; "
                    f"{activity.active_count} active or paused session(s) found."
                )
                if not dry_run:
                    self.write_retry_state(update, activity)
                return RunResult(
                    action="deferred",
                    message=message,
                    installed_version=installed_version,
                    update=update,
                    activity=activity,
                    retry_pending=not dry_run,
                )

            if dry_run:
                return RunResult(
                    action="dry-run",
                    message=f"Would install Plex {update.target_version}.",
                    installed_version=installed_version,
                    update=update,
                    activity=activity,
                    retry_pending=retry_pending,
                )

            self.notify_discord_update_started(mode, installed_version, update)
            try:
                self.install_update(update)
                self.ensure_service_running()
            except Exception as exc:
                self.notify_discord_update_failed(mode, installed_version, update, exc)
                raise
            self.clear_retry_state()
            new_version = self.get_installed_version()
            self.notify_discord_update_finished(mode, installed_version, new_version, update)
            return RunResult(
                action="installed",
                message=f"Installed Plex {new_version}.",
                installed_version=new_version,
                update=update,
                activity=activity,
                retry_pending=False,
            )
        except Exception as exc:
            if retry_pending and not dry_run:
                self.clear_retry_state()
            raise UpdaterError(str(exc)) from exc

    def status(self) -> dict[str, Any]:
        installed_version = self.get_installed_version()
        retry_state = self.read_retry_state()

        try:
            token = self.read_plex_online_token()
            update = asdict(self.check_for_update(installed_version, token))
        except Exception as exc:
            update = {"error": str(exc)}

        try:
            activity = asdict(self.get_activity(token))
        except Exception as exc:
            activity = {"error": str(exc)}

        return {
            "installed_version": installed_version,
            "retry_pending": self.retry_state_path.exists(),
            "retry_state": retry_state,
            "update": update,
            "activity": activity,
        }

    @property
    def retry_state_path(self) -> Path:
        return Path(self.config.retry_state_file)

    def get_installed_version(self) -> str:
        result = self.run_command(
            [
                "dpkg-query",
                "-W",
                f"-f=${{Version}}",
                self.config.package_name,
            ]
        )
        return result.stdout.strip()

    def read_plex_online_token(self) -> str:
        path = Path(self.config.plex_preferences_xml)
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise UpdaterError(f"Plex preferences file not found: {path}") from exc
        except PermissionError as exc:
            raise UpdaterError(
                f"Cannot read Plex preferences file {path}; run this updater as root."
            ) from exc

        token = root.attrib.get("PlexOnlineToken", "").strip()
        if not token:
            raise UpdaterError("PlexOnlineToken was not found in Preferences.xml.")
        return token

    def check_for_update(self, installed_version: str, token: str) -> UpdateInfo:
        try:
            local_update = self.check_local_updater(installed_version, token)
        except UpdaterError as exc:
            self.logger.warning("Local Plex updater lookup failed, falling back to plex.tv: %s", exc)
            local_update = UpdateInfo(
                available=False,
                current_version=installed_version,
                source="local",
            )

        if local_update.available:
            local_update.download_url = self.resolve_download_url(local_update, token)
            return local_update
        return self.check_remote_updater(installed_version, token)

    def check_local_updater(self, installed_version: str, token: str) -> UpdateInfo:
        base_url = self.config.plex_base_url.rstrip("/")
        query = self.plex_query({"download": "0", "X-Plex-Token": token})
        check_url = f"{base_url}/updater/check?{query}"
        status_url = f"{base_url}/updater/status?{query}"

        try:
            self.request_xml(check_url, method="PUT", allow_status={200, 204})
        except UpdaterError:
            self.request_xml(check_url, method="GET", allow_status={200, 204})
        xml_root = self.request_xml(status_url, method="GET")
        update = self.parse_update_xml(xml_root, installed_version, source="local")
        if update.available:
            return update
        return UpdateInfo(available=False, current_version=installed_version, source="local")

    def check_remote_updater(self, installed_version: str, token: str) -> UpdateInfo:
        query = self.plex_query(
            {
                "build": self.config.plex_updater_build,
                "channel": self.config.plex_updater_channel,
                "distribution": self.config.plex_updater_distribution,
                "version": installed_version,
                "X-Plex-Token": token,
            }
        )
        url = (
            "https://plex.tv/updater/products/"
            f"{self.config.plex_updater_product}/check.xml?{query}"
        )
        xml_root = self.request_xml(url, method="GET")
        update = self.parse_update_xml(xml_root, installed_version, source="remote")
        if update.available:
            update.download_url = self.resolve_download_url(update, token)
            return update
        return UpdateInfo(available=False, current_version=installed_version, source="remote")

    def parse_update_xml(
        self, xml_root: ET.Element, installed_version: str, source: str
    ) -> UpdateInfo:
        releases = xml_root.findall(".//Release")
        if not releases:
            return UpdateInfo(available=False, current_version=installed_version, source=source)

        newest = max(releases, key=lambda release: version_key(release.attrib.get("version", "0")))
        target_version = newest.attrib.get("version", "").strip()
        if not target_version or version_key(target_version) <= version_key(installed_version):
            return UpdateInfo(available=False, current_version=installed_version, source=source)

        download_url = newest.attrib.get("downloadURL", "").strip()
        if not download_url:
            download_url = xml_root.attrib.get("downloadURL", "").strip()

        return UpdateInfo(
            available=True,
            current_version=installed_version,
            target_version=target_version,
            download_url=download_url,
            source=source,
            notes=newest.attrib.get("fixed", "").strip() or newest.attrib.get("added", "").strip(),
        )

    def build_download_url(self, version: str, token: str) -> str:
        arch = self.detect_architecture()
        query = urllib.parse.urlencode({"X-Plex-Token": token})
        return (
            "https://downloads.plex.tv/plex-media-server-new/"
            f"{version}/{self.config.plex_updater_distribution}/"
            f"plexmediaserver_{version}_{arch}.deb?{query}"
        )

    def resolve_download_url(self, update: UpdateInfo, token: str) -> str:
        if not update.download_url:
            return self.build_download_url(update.target_version, token)

        parsed = urllib.parse.urlparse(update.download_url)
        filename = Path(parsed.path).name
        if not filename.endswith(".deb"):
            return self.build_download_url(update.target_version, token)
        return self.ensure_token_query(update.download_url)

    def get_activity(self, plex_token: str | None = None) -> ActivityInfo:
        if self.config.tautulli_api_key:
            try:
                return self.get_activity_via_api()
            except UpdaterError as exc:
                self.logger.warning("Tautulli API lookup failed, falling back to Plex: %s", exc)
        if plex_token:
            try:
                return self.get_activity_via_plex(plex_token)
            except UpdaterError as exc:
                self.logger.warning("Plex session lookup failed, falling back to SQLite: %s", exc)
        return self.get_activity_via_db()

    def get_activity_via_api(self) -> ActivityInfo:
        params = {
            "apikey": self.config.tautulli_api_key,
            "cmd": "get_activity",
        }
        url = f"{self.config.tautulli_url.rstrip('/')}/api/v2?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise UpdaterError(f"Tautulli API returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise UpdaterError(f"Tautulli API request failed: {exc.reason}") from exc

        body = payload.get("response", {})
        if body.get("result") != "success":
            raise UpdaterError(body.get("message", "Tautulli API rejected the request."))

        data = body.get("data") or {}
        sessions = data.get("sessions") or []
        if isinstance(sessions, list):
            active_count = len(sessions)
            normalized = [self.normalize_session(session) for session in sessions]
        else:
            active_count = int(data.get("stream_count") or 0)
            normalized = []

        if not active_count:
            active_count = int(data.get("stream_count") or 0)
        return ActivityInfo(active_count=active_count, source="api", sessions=normalized)

    def get_activity_via_plex(self, plex_token: str) -> ActivityInfo:
        base_url = self.config.plex_base_url.rstrip("/")
        query = self.plex_query({"X-Plex-Token": plex_token})
        url = f"{base_url}/status/sessions?{query}"
        root = self.request_xml(url, method="GET")

        sessions: list[dict[str, Any]] = []
        for child in root:
            if not child.attrib.get("sessionKey") and not child.attrib.get("sessionID"):
                continue
            title = self.plex_session_title(child)
            user_el = child.find("User")
            player_el = child.find("Player")
            sessions.append(
                {
                    "state": (player_el.attrib.get("state") if player_el is not None else "") or "playing",
                    "user": user_el.attrib.get("title") if user_el is not None else "",
                    "title": title,
                }
            )

        return ActivityInfo(active_count=len(sessions), source="plex", sessions=sessions)

    def get_activity_via_db(self) -> ActivityInfo:
        db_path = Path(self.config.tautulli_db_path)
        if not db_path.exists():
            raise UpdaterError(f"Tautulli database not found: {db_path}")

        uri = f"file:{db_path}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                """
                SELECT state, user, full_title
                FROM sessions
                WHERE COALESCE(stopped, 0) = 0
                ORDER BY started ASC
                """
            ).fetchall()

        sessions = [
            {"state": state, "user": user, "title": full_title}
            for state, user, full_title in rows
        ]
        return ActivityInfo(active_count=len(rows), source="db", sessions=sessions)

    def plex_session_title(self, element: ET.Element) -> str:
        grandparent = element.attrib.get("grandparentTitle", "").strip()
        parent = element.attrib.get("parentTitle", "").strip()
        title = element.attrib.get("title", "").strip()
        if grandparent and title:
            return f"{grandparent} - {title}"
        if parent and title and parent != title:
            return f"{parent} - {title}"
        return title or grandparent or parent

    def write_retry_state(self, update: UpdateInfo, activity: ActivityInfo) -> None:
        self.retry_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "target_version": update.target_version,
            "download_url": update.download_url,
            "activity_source": activity.source,
            "active_count": activity.active_count,
            "sessions": activity.sessions,
        }
        self.retry_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def read_retry_state(self) -> dict[str, Any] | None:
        if not self.retry_state_path.exists():
            return None
        return json.loads(self.retry_state_path.read_text(encoding="utf-8"))

    def clear_retry_state(self) -> None:
        if self.retry_state_path.exists():
            self.retry_state_path.unlink()

    def install_update(self, update: UpdateInfo) -> None:
        cache_dir = Path(self.config.download_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        package_path = cache_dir / Path(urllib.parse.urlparse(update.download_url).path).name
        if package_path.exists():
            cached_version = self.read_package_version(package_path)
            if cached_version != update.target_version:
                self.logger.warning(
                    "Cached package version %s does not match target %s; re-downloading.",
                    cached_version,
                    update.target_version,
                )
                package_path.unlink()

        if not package_path.exists():
            self.download_file(update.download_url, package_path)

        downloaded_version = self.read_package_version(package_path)
        if downloaded_version != update.target_version:
            raise UpdaterError(
                f"Downloaded package version mismatch: expected {update.target_version}, "
                f"got {downloaded_version}."
            )

        self.run_command(["dpkg", "-i", str(package_path)])
        installed_version = self.get_installed_version()
        if installed_version != update.target_version:
            raise UpdaterError(
                f"Installed version mismatch after update: expected {update.target_version}, "
                f"got {installed_version}."
            )

    def download_file(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = self.plex_headers()
        request = urllib.request.Request(url, headers=headers)

        with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent) as tmp_file:
            tmp_path = Path(tmp_file.name)
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                    shutil.copyfileobj(response, tmp_file)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        tmp_path.replace(destination)

    def ensure_service_running(self) -> None:
        self.run_command(["systemctl", "start", self.config.service_name])
        self.run_command(["systemctl", "is-active", "--quiet", self.config.service_name])

    def discord_webhook_url(self) -> str:
        if self.config.discord_webhook_url.strip():
            return self.config.discord_webhook_url.strip()

        webhook_file = self.config.discord_webhook_file.strip()
        if not webhook_file:
            return ""

        path = Path(webhook_file)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.logger.warning("Could not read Discord webhook file %s: %s", path, exc)
            return ""

    def notify_discord_update_started(
        self, mode: str, installed_version: str, update: UpdateInfo
    ) -> None:
        self.send_discord_notification(
            self.format_discord_message(
                "Plex update starting",
                mode,
                f"{installed_version} -> {update.target_version}",
            )
        )

    def notify_discord_update_finished(
        self, mode: str, installed_version: str, new_version: str, update: UpdateInfo
    ) -> None:
        self.send_discord_notification(
            self.format_discord_message(
                "Plex update finished",
                mode,
                f"{installed_version} -> {new_version}",
                extra=f"Source: {update.source or 'unknown'}",
            )
        )

    def notify_discord_update_failed(
        self, mode: str, installed_version: str, update: UpdateInfo, exc: Exception
    ) -> None:
        self.send_discord_notification(
            self.format_discord_message(
                "Plex update failed",
                mode,
                f"{installed_version} -> {update.target_version}",
                extra=str(exc),
            )
        )

    def format_discord_message(
        self, title: str, mode: str, versions: str, extra: str = ""
    ) -> str:
        mode_label = "retry run" if mode == "run-retry" else "daily run"
        parts = [
            title,
            f"Host: {self.config.plex_device_name}",
            f"Trigger: {mode_label}",
            f"Versions: {versions}",
        ]
        if extra:
            parts.append(f"Details: {extra}")
        message = "\n".join(parts)
        return message[:2000]

    def send_discord_notification(self, message: str) -> None:
        webhook_url = self.discord_webhook_url()
        if not webhook_url:
            return

        payload = json.dumps(
            {
                "username": self.config.plex_product_name,
                "content": message,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"{self.config.plex_product_name}/{APP_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout):
                return
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace").strip()
            if response_body:
                self.logger.warning(
                    "Discord webhook returned HTTP %s: %s", exc.code, response_body
                )
            else:
                self.logger.warning("Discord webhook returned HTTP %s.", exc.code)
        except urllib.error.URLError as exc:
            self.logger.warning("Discord webhook request failed: %s", exc.reason)

    def detect_architecture(self) -> str:
        result = self.run_command(["dpkg", "--print-architecture"])
        return result.stdout.strip()

    def plex_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/xml, text/xml, application/json",
            "X-Plex-Product": self.config.plex_product_name,
            "X-Plex-Version": APP_VERSION,
            "X-Plex-Client-Identifier": self.config.plex_client_identifier,
            "X-Plex-Platform": platform.system(),
            "X-Plex-Platform-Version": platform.release(),
            "X-Plex-Device": platform.machine(),
            "X-Plex-Device-Name": self.config.plex_device_name,
        }

    def plex_query(self, params: dict[str, str]) -> str:
        query = dict(params)
        query.update(
            {
                "X-Plex-Product": self.config.plex_product_name,
                "X-Plex-Version": APP_VERSION,
                "X-Plex-Client-Identifier": self.config.plex_client_identifier,
                "X-Plex-Platform": platform.system(),
                "X-Plex-Platform-Version": platform.release(),
                "X-Plex-Device": platform.machine(),
                "X-Plex-Device-Name": self.config.plex_device_name,
            }
        )
        return urllib.parse.urlencode(query)

    def request_xml(
        self, url: str, method: str, allow_status: set[int] | None = None
    ) -> ET.Element:
        request = urllib.request.Request(url, method=method, headers=self.plex_headers())
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            if allow_status and exc.code in allow_status:
                return ET.Element("MediaContainer")
            raise UpdaterError(f"Request to {url} returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise UpdaterError(f"Request to {url} failed: {exc.reason}") from exc

        if not content:
            return ET.Element("MediaContainer")
        return ET.fromstring(content)

    def ensure_token_query(self, url: str) -> str:
        if "X-Plex-Token=" in url:
            return url
        token = self.read_plex_online_token()
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["X-Plex-Token"] = [token]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise UpdaterError(f"Command failed ({' '.join(command)}): {stderr}")
        return result

    def read_package_version(self, package_path: Path) -> str:
        result = self.run_command(["dpkg-deb", "-f", str(package_path), "Version"])
        return result.stdout.strip()

    def normalize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": session.get("state"),
            "user": session.get("friendly_name") or session.get("user"),
            "title": session.get("full_title") or session.get("title"),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plex beta auto-updater")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the updater environment file. Defaults to {DEFAULT_CONFIG_PATH}.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-daily", help="Run the daily update check.")
    subparsers.add_parser("run-retry", help="Run the hourly retry check if a retry is pending.")
    subparsers.add_parser("dry-run", help="Show what would happen without changing the system.")
    subparsers.add_parser("status", help="Print current updater status as JSON.")
    return parser


def configure_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("plex_beta_updater")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logging(args.verbose)
    config = Config.from_sources(args.config)
    updater = PlexBetaUpdater(config=config, logger=logger)

    try:
        if args.command == "status":
            print(json.dumps(updater.status(), indent=2))
            return 0

        if args.command == "dry-run":
            result = updater.run(mode="run-daily", dry_run=True)
        else:
            result = updater.run(mode=args.command, dry_run=False)

        print(
            json.dumps(
                {
                    "action": result.action,
                    "message": result.message,
                    "installed_version": result.installed_version,
                    "retry_pending": result.retry_pending,
                    "update": asdict(result.update) if result.update else None,
                    "activity": asdict(result.activity) if result.activity else None,
                },
                indent=2,
            )
        )
        return 0
    except UpdaterError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
