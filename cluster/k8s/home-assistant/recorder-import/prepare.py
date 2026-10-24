"""Runs in the Home Assistant image as the recorder import Job's init container: wait for Home
Assistant to have stopped, snapshot its SQLite recorder database, log each table's row count, and
start Home Assistant against Postgres with a recorder-only config until the recorder has created
its schema. The load container then copies the snapshot's rows into that schema.
"""

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import psycopg2

SOURCE = Path("/config/home-assistant_v2.db")
SNAPSHOT = Path("/work/ha.db")
SCRATCH_CONFIG = Path("/work/hass")
DB_URL = os.environ["RECORDER_DB_URL"]


def wait_for_home_assistant_stopped() -> None:
    # A clean shutdown checkpoints the WAL and deletes it.
    wal = SOURCE.with_name(SOURCE.name + "-wal")
    deadline = time.monotonic() + 600
    while wal.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"{wal=} still exists after 10 minutes; is Home Assistant still running?")
        time.sleep(5)


def snapshot() -> None:
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(SNAPSHOT) as target:
        source.backup(target)
        tables = [row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        for table in tables:
            (count,) = target.execute(f'SELECT count(*) FROM "{table}"').fetchone()
            print(f"sqlite {table=} {count=}", flush=True)


def schema_exists() -> bool:
    try:
        with psycopg2.connect(DB_URL) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT max(schema_version) FROM schema_changes")
            (version,) = cursor.fetchone()
    except psycopg2.Error:
        return False
    print(f"postgres schema {version=}", flush=True)
    return version is not None


def create_schema() -> None:
    SCRATCH_CONFIG.mkdir()
    (SCRATCH_CONFIG / "configuration.yaml").write_text("recorder:\n  db_url: !env_var RECORDER_DB_URL\n")
    with subprocess.Popen(["python3", "-m", "homeassistant", "--config", str(SCRATCH_CONFIG)]) as hass:
        deadline = time.monotonic() + 600
        while not schema_exists():
            if hass.poll() is not None or time.monotonic() > deadline:
                hass.kill()
                raise SystemExit(f"recorder schema not created; {hass.returncode=}")
            time.sleep(5)
        hass.terminate()


wait_for_home_assistant_stopped()
snapshot()
create_schema()
