"""Render a private Squid parent configuration from runtime-only credentials."""

import argparse
import json
import os
import re
from pathlib import Path
from textwrap import dedent


def render(*, host: str, port: int, listen_port: int, credentials_file: Path, ca_bundle: Path) -> str:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not all(1 <= value <= 65535 for value in (port, listen_port)):
        raise ValueError("Invalid relay endpoint")
    if re.search(r"[\s\"\\]", str(ca_bundle)):
        raise ValueError("Invalid CA bundle path")
    if credentials_file.stat().st_mode & 0o077:
        raise ValueError("Credentials must be owner-only")
    credentials = json.loads(credentials_file.read_text())
    if not isinstance(credentials, dict) or len(credentials) != 1:
        raise ValueError("Expected one credential entry")
    username, password = next(iter(credentials.items()))
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username) or not isinstance(password, str):
        raise ValueError("Invalid credentials")
    if not re.fullmatch(r"[0-9a-f]{64}", password):
        raise ValueError("Expected a 32-byte lowercase hexadecimal password")
    return dedent(f"""\
        http_port 127.0.0.1:{listen_port}
        visible_hostname github-api-relay
        http_access deny manager
        http_access allow localhost
        http_access deny all
        cache_peer {host} parent {port} 0 no-query no-digest default tls tls-min-version=1.2 tls-default-ca=off tls-cafile={ca_bundle} ssldomain={host} login={username}:{password}
        never_direct allow all
        cache deny all
        cache_mem 0 MB
        access_log none
        cache_store_log none
        cache_log /dev/null
        pid_filename none
        pinger_enable off
        shutdown_lifetime 1 seconds
        connect_timeout 5 seconds
        peer_connect_timeout 5 seconds
        forwarded_for delete
        via off
        """)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--ca-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        content = render(
            host=args.host,
            port=args.port,
            listen_port=args.listen_port,
            credentials_file=args.credentials_file,
            ca_bundle=args.ca_bundle,
        )
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
    except (OSError, ValueError):
        # Neither JSON parser diagnostics nor Squid configuration lines may log credentials.
        parser.exit(1, "github-api-relay: could not prepare private configuration\n")


if __name__ == "__main__":
    main()
