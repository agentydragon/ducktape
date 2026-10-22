"""Provision the agentydragon user in Atuin's PostgreSQL database.

Idempotent: creates user if missing, updates password if the SOPS source
changed, no-op if password already matches.
"""

import os
import secrets
import sys

import argon2
import psycopg2

USERNAME = "agentydragon"
EMAIL = "agentydragon@allegedly.works"
DB_NAME = "atuin"
DB_USER = "atuin"


def main() -> None:
    user_password = os.environ["ATUIN_USER_PASSWORD"]
    db_password = os.environ["POSTGRES_PASSWORD"]

    # libpq reads the host and port from PGHOST and PGPORT.
    conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=db_password)
    conn.autocommit = True

    ph = argon2.PasswordHasher()

    with conn.cursor() as cur:
        cur.execute("SELECT id, password FROM users WHERE username = %s", (USERNAME,))
        row = cur.fetchone()

        if row is None:
            hashed = ph.hash(user_password)
            cur.execute(
                "INSERT INTO users (username, email, password) VALUES (%s, %s, %s) RETURNING id",
                (USERNAME, EMAIL, hashed),
            )
            user_id = cur.fetchone()[0]

            # Atuin's login endpoint requires an existing session row.
            token = secrets.token_urlsafe(24)
            cur.execute("INSERT INTO sessions (user_id, token) VALUES (%s, %s)", (user_id, token))
            print(f"Created user {USERNAME!r} (id={user_id}) with session.")
            return

        user_id, stored_hash = row

        try:
            ph.verify(stored_hash, user_password)
            print(f"User {USERNAME!r} exists and password matches. No-op.")
        except argon2.exceptions.VerifyMismatchError:
            new_hash = ph.hash(user_password)
            cur.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user_id))
            print(f"Updated password for user {USERNAME!r} (id={user_id}).")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
