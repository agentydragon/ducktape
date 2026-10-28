"""Apply the archive database's versioned migrations."""

import os

from x.ai_session_archive.database_migrate import RUNNER


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL must configure the Postgres connection")
    RUNNER.apply(database_url)


if __name__ == "__main__":
    main()
