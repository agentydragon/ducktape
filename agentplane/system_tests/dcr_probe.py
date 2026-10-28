"""Run the real registration-only SDK client against a test-owned HTTP server."""

import asyncio
import sys

from agentplane.system_tests.dcr import DcrError, register_client


async def async_main() -> None:
    for redirect_uri in ("http://127.0.0.1:49152/callback", "https://client.example.test/callback"):
        await register_client(sys.argv[1], redirect_uri)


def main() -> None:
    try:
        asyncio.run(async_main())
    except DcrError as error:
        # The Go test reads the process status; only the sanitized reason is rendered.
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
