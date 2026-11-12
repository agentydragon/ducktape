"""Versioned SQLite migrations for the experiment's single-process state directory."""

from sqlalchemy import Engine

# Frozen schema, independent of today's ORM. Legacy bodies become revision 1 before
# the old column is removed. Their timestamp records migration, not original creation.
MIGRATIONS = [
    (
        "CREATE TABLE IF NOT EXISTS message (position INTEGER PRIMARY KEY, id CHAR(32) UNIQUE NOT NULL, "
        "author VARCHAR NOT NULL, body VARCHAR NOT NULL)",
        "CREATE TABLE revision (change_sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "message_id CHAR(32) NOT NULL REFERENCES message(id), revision INTEGER NOT NULL, "
        "body VARCHAR NOT NULL, recorded_at VARCHAR NOT NULL, UNIQUE(message_id, revision))",
        "INSERT INTO revision (message_id, revision, body, recorded_at) "
        "SELECT id, 1, body, strftime('%Y-%m-%dT%H:%M:%fZ', 'now') FROM message ORDER BY position",
        "ALTER TABLE message DROP COLUMN body",
    )
]


def migrate(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        if version > len(MIGRATIONS):
            raise ValueError(f"thread database schema {version} is newer than this CLI supports")
        for index in range(version, len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql(f"PRAGMA user_version = {index + 1}")
        connection.commit()
