"""Read Skype chat history from a local SQLite database."""

import argparse
import datetime
import sqlite3
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Skype chat history from a local SQLite database.")
    parser.add_argument("--db", required=True, help="Path to the Skype main.db SQLite database")
    parser.add_argument("--person", help="Filter messages to those from a person whose display name contains this string")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    for row in c.execute("SELECT id, from_dispname, timestamp, body_xml FROM Messages ORDER BY Timestamp;"):
        if args.person is not None:
            if args.person not in row[1]:
                continue
            print(row[3])
        else:
            timestamp = datetime.datetime.fromtimestamp(row[2])
            print(f"{row[1]} {timestamp}: {row[3]}")

    conn.close()


if __name__ == "__main__":
    main()
