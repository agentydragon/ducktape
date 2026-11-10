"""Synthetic Chrome cookie databases for profile extraction tests."""

import sqlite3
from collections.abc import Iterable
from dataclasses import astuple, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CookieRow:
    host: str
    name: str
    value: str = ""
    encrypted_value: bytes = b""
    path: str = "/"
    expires_utc: int = 0
    secure: bool = True
    http_only: bool = True
    same_site: int = 1
    partition: str = ""


def create_cookie_db(path: Path, cookies: Iterable[CookieRow], *, version: int = 24) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        db.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
            "path TEXT, expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
            "samesite INTEGER, top_frame_site_key TEXT)"
        )
        db.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", map(astuple, cookies))
