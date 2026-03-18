"""Unified Claude Code hook entry point.

Thin client that sends all hooks to the hook daemon over UDS. The daemon
handles dispatch, OTEL, and session start setup. If the daemon is unreachable,
the client starts a new one automatically.
"""

import os
import sys
import traceback

from pydantic import TypeAdapter

from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.hook_daemon.client import call_daemon
from devinfra.claude.session_paths import SessionPaths

_adapter: TypeAdapter[AnyHookInput] = TypeAdapter(AnyHookInput)


def main() -> None:
    raw = sys.stdin.buffer.read()
    parsed = _adapter.validate_json(raw)

    paths = SessionPaths.from_env(parsed.session_id, dict(os.environ))

    result = call_daemon(parsed, dict(os.environ), paths)
    if result is not None and result.output is not None:
        # exclude_none: Zod .optional() accepts undefined (absent) but NOT null.
        # Pydantic emits None as null by default; exclude_none omits those fields.
        sys.stdout.write(result.output.model_dump_json(by_alias=True, exclude_none=True))
    elif result is None:
        print("ERROR: hook daemon unavailable", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Hook dispatch failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
