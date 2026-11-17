"""Private SQLite storage for provider session credentials and recovery state."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atomicwrites import atomic_write
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool


def prepare_directory(directory: Path) -> None:
    missing = []
    ancestor = directory
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # SQLite syncs entries inside its directory; newly created ancestors must
    # also survive power loss before an operation can be admitted.
    for path in reversed(missing):
        marker = path.parent / f".{path.name}.platform-sessions-journal"
        with atomic_write(marker, overwrite=True) as stream:
            stream.write("")
        # Only the directory sync matters; a leftover bootstrap file after a
        # crash is harmless and is never used as an operation fence.
        marker.unlink()


class Storage:
    def __init__(self, directory: Path):
        prepare_directory(directory)
        self.path = directory / "import-operations.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        self._engine = create_engine(URL.create("sqlite", database=str(self.path)), poolclass=NullPool)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        try:
            if self.path.stat().st_mode & 0o077:
                raise ValueError("Session database must be private")
            with self._engine.begin() as connection:
                # EXTRA also syncs rollback-journal deletion on commit.
                if connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one() != "delete":
                    raise OSError("Session database requires durable rollback mode")
                connection.exec_driver_sql("PRAGMA synchronous=EXTRA")
                yield connection
        except SQLAlchemyError:
            # SQLAlchemy exceptions can contain bound credential values.
            raise OSError("Session database transaction failed") from None

    def close(self) -> None:
        self._engine.dispose()
