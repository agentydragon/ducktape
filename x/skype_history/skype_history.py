"""Read Skype chat history from a local SQLite database."""

import argparse
import datetime
import sqlite3
from contextlib import closing
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Skype chat history from a local SQLite database.")
    parser.add_argument("--db", type=Path, required=True, help="Path to the Skype main.db SQLite database")
    parser.add_argument(
        "--person", help="Filter messages to those from a person whose display name contains this string"
    )
    args = parser.parse_args()

    with closing(sqlite3.connect(f"{args.db.absolute().as_uri()}?mode=ro", uri=True)) as conn:
        for sender, timestamp, body in conn.execute(
            "SELECT from_dispname, timestamp, body_xml FROM Messages ORDER BY timestamp"
        ):
            if args.person is None:
                print(f"{sender} {datetime.datetime.fromtimestamp(timestamp)}: {body}")
            elif args.person in sender:
                print(body)


if __name__ == "__main__":
    main()
