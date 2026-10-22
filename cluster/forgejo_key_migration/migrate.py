"""Offline, transactional SECRET_KEY migration for the audited Forgejo 15.0.8 schema."""

import argparse
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psycopg
from Crypto.Cipher import ChaCha20_Poly1305
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from psycopg import sql


class MigrationError(Exception):
    """An operator-visible error containing no credential or database payload."""


@dataclass(frozen=True)
class Column:
    table: str
    column: str
    domain: bytes
    aad_column: bytes


# Forgejo v15.0.8 models/{secret/secret,repo/mirror}.go. The mirror AAD name
# deliberately differs from its PostgreSQL column name.
COLUMNS = (
    Column("secret", "data", b"action_secret", b"data"),
    Column("mirror", "encrypted_remote_address", b"pullmirror", b"remote_address_auth"),
)
TABLES = ("version", "secret", "mirror", "two_factor", "push_mirror", "webhook", "task", "login_source")


def read_key(path: Path) -> bytes:
    # setting.loadSecret trims surrounding whitespace for file: URIs.
    key = path.read_text().strip().encode()
    if not key:
        raise MigrationError("Key file is empty")
    return key


def derive_key(master: bytes, domain: bytes) -> bytes:
    # modules/keying/keying.go: HKDF-SHA256, no salt, domain as HKDF info.
    # PyCryptodome returns bytes for the default num_keys=1; its stub also
    # includes the tuple returned when requesting multiple keys.
    return cast(bytes, HKDF(master, 32, b"", SHA256, context=domain))


def decrypt(key: bytes, data: bytes, aad: bytes) -> bytes:
    if len(data) < 40:  # 24-byte nonce + 16-byte authentication tag
        raise MigrationError("Truncated ciphertext")
    cipher = ChaCha20_Poly1305.new(key=key, nonce=data[:24])
    cipher.update(aad)
    try:
        return cipher.decrypt_and_verify(data[24:-16], data[-16:])
    except ValueError:
        raise MigrationError("Ciphertext authentication failed; wrong key, row binding, or format") from None


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(24)
    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + ciphertext + tag


def check_inventory(cursor: psycopg.Cursor[tuple], expected: tuple[int, int]) -> None:
    cursor.execute("SELECT version FROM version")
    if cursor.fetchall() != [(305,)]:
        raise MigrationError("Database schema differs from the audited Forgejo 15.0.8 schema (305)")

    # Refuse additional key-dependent data rather than silently orphaning it.
    # All tasks are rejected: even a completed migration can retain ciphertext.
    cursor.execute(
        """
        SELECT
          (SELECT count(*) FROM two_factor),
          (SELECT count(*) FROM push_mirror),
          (SELECT count(*) FROM webhook WHERE octet_length(header_authorization_encrypted) > 0),
          (SELECT count(*) FROM task),
          (SELECT count(*) FROM login_source WHERE type IN (2, 5))
        """
    )
    if cursor.fetchone() != (0, 0, 0, 0, 0):
        raise MigrationError("Unsupported TOTP, push-mirror, webhook, task, or LDAP data; inventory again")

    for column, count in zip(COLUMNS, expected, strict=True):
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE {} IS NOT NULL").format(
                sql.Identifier(column.table), sql.Identifier(column.column)
            )
        )
        if cursor.fetchone() != (count,):
            raise MigrationError(f"Inventory changed for {column.table}; review counts before proceeding")
    if sum(expected) == 0:
        raise MigrationError("No encrypted rows available to authenticate the old key")


def migrate(
    connection: psycopg.Connection[tuple], *, old: bytes, new: bytes, expected: tuple[int, int], apply: bool
) -> None:
    if secrets.compare_digest(old, new):
        raise MigrationError("Old and new keys must differ")
    if any(count < 0 for count in expected):
        raise MigrationError("Expected counts must be non-negative")

    with connection.transaction(), connection.cursor() as cursor:
        if not apply:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        if apply:
            # Prevent concurrent inserts as well as changes to existing records.
            # Forgejo must remain stopped after commit until the key is activated.
            cursor.execute(
                sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE NOWAIT").format(
                    sql.SQL(", ").join(sql.Identifier(table) for table in TABLES)
                )
            )
        check_inventory(cursor, expected)

        for column in COLUMNS:
            old_key = derive_key(old, column.domain)
            new_key = derive_key(new, column.domain)
            cursor.execute(
                sql.SQL("SELECT id, {} FROM {} WHERE {} IS NOT NULL ORDER BY id").format(
                    sql.Identifier(column.column), sql.Identifier(column.table), sql.Identifier(column.column)
                )
            )
            for row_id, data in cursor.fetchall():
                aad = column.aad_column + b":" + row_id.to_bytes(8, "big")
                plaintext = decrypt(old_key, bytes(data), aad)
                replacement = encrypt(new_key, plaintext, aad)
                if not secrets.compare_digest(plaintext, decrypt(new_key, replacement, aad)):
                    raise MigrationError("Replacement verification failed")
                if apply:
                    cursor.execute(
                        sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
                            sql.Identifier(column.table), sql.Identifier(column.column)
                        ),
                        (replacement, row_id),
                    )
                    if cursor.rowcount != 1:
                        raise MigrationError("Updated row count differs from the locked inventory")
                    cursor.execute(
                        sql.SQL("SELECT {} FROM {} WHERE id = %s").format(
                            sql.Identifier(column.column), sql.Identifier(column.table)
                        ),
                        (row_id,),
                    )
                    result = cursor.fetchone()
                    if result is None or not secrets.compare_digest(plaintext, decrypt(new_key, bytes(result[0]), aad)):
                        raise MigrationError("Stored replacement verification failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("--old-key-file", type=Path, required=True)
    parser.add_argument("--new-key-file", type=Path, required=True)
    parser.add_argument("--database-host", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--database-user-file", type=Path, required=True)
    parser.add_argument("--database-password-file", type=Path, required=True)
    parser.add_argument("--database-ca-file", type=Path, required=True)
    parser.add_argument("--expect-secrets", type=int, required=True)
    parser.add_argument("--expect-mirrors", type=int, required=True)
    args = parser.parse_args()
    try:
        old = read_key(args.old_key_file)
        new = read_key(args.new_key_file)
        with psycopg.connect(
            host=args.database_host,
            dbname=args.database_name,
            user=args.database_user_file.read_text().strip(),
            password=args.database_password_file.read_text(),
            sslmode="verify-full",
            sslrootcert=str(args.database_ca_file),
            connect_timeout=10,
            application_name="forgejo-key-migration",
            autocommit=True,
        ) as connection:
            migrate(
                connection,
                old=old,
                new=new,
                expected=(args.expect_secrets, args.expect_mirrors),
                apply=args.mode == "apply",
            )
    except MigrationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except psycopg.Error as error:
        # PostgreSQL diagnostics can contain row values. Never print them.
        print(f"PostgreSQL operation failed (SQLSTATE {error.sqlstate}); no success confirmed", file=sys.stderr)
        raise SystemExit(1) from None
    except OSError:
        print("Could not read a mounted credential file", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"{args.mode} succeeded: {args.expect_secrets} Actions secrets, {args.expect_mirrors} mirror addresses")


if __name__ == "__main__":
    main()
