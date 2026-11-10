"""Read selected state from Chrome profiles for local session exporters."""

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from python.runfiles import Runfiles

from platform_sessions.chrome.cookies import chrome_password, decrypt_cookie


@dataclass(frozen=True)
class ChromeCookie:
    host: str
    name: str
    value: str = field(repr=False)
    encrypted_value: bytes = field(repr=False)
    path: str
    expires_utc: int
    secure: bool
    http_only: bool
    same_site: int
    partition: str
    version: int

    def is_expired(self, now: float) -> bool:
        return bool(self.expires_utc and self.expires_utc // 1_000_000 - 11644473600 <= now)

    def plaintext(self, password_provider: Callable[[], bytes] = chrome_password) -> str:
        if not self.encrypted_value:
            return self.value
        password = password_provider() if self.encrypted_value.startswith(b"v11") else b""
        return decrypt_cookie(self.encrypted_value, self.host, self.version, password)


class ChromeProfile:
    """Read selected browser state without opening the live profile for writing."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser()

    def cookies(self, *, hosts: Sequence[str], names: Sequence[str], partitions: Sequence[str]) -> list[ChromeCookie]:
        if not hosts or not names or not partitions:
            raise ValueError("Cookie selectors must not be empty")
        database = self.directory / "Cookies"
        if not database.exists():
            database = self.directory / "Network" / "Cookies"

        def placeholders(values: Sequence[str]) -> str:
            return ", ".join("?" for _ in values)

        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("BEGIN")
            version_row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if not version_row:
                raise ValueError("Chrome cookie database has no schema version.")
            rows = db.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, "
                "is_httponly, samesite, top_frame_site_key FROM cookies "
                f"WHERE host_key IN ({placeholders(hosts)}) AND name IN ({placeholders(names)}) "
                f"AND top_frame_site_key IN ({placeholders(partitions)})",
                (*hosts, *names, *partitions),
            ).fetchall()
        version = int(version_row[0])
        return [
            ChromeCookie(
                host=row[0],
                name=row[1],
                value=row[2],
                encrypted_value=row[3],
                path=row[4],
                expires_utc=row[5],
                secure=row[6],
                http_only=row[7],
                same_site=row[8],
                partition=row[9],
                version=version,
            )
            for row in rows
        ]

    def local_storage(
        self, *, origin: str, keys: Sequence[str], prefixes: Sequence[str], extractor: Path | None = None
    ) -> dict[str, str]:
        if extractor is None:
            extractor = Runfiles.CreateOrRaise().root() / "ducktape/platform_sessions/chrome/chrome_storage"
        args = [str(extractor), str(self.directory / "Local Storage" / "leveldb"), origin]
        for key in keys:
            args.extend(("--key", key))
        for prefix in prefixes:
            args.extend(("--prefix", prefix))
        result = subprocess.run(args, check=True, stdout=subprocess.PIPE)
        values = json.loads(result.stdout)
        if not isinstance(values, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
        ):
            raise ValueError("Invalid Chrome local-storage helper output")
        return cast(dict[str, str], values)


def default_profile() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "google-chrome" / "Default"
