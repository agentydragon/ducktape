"""Unified Claude Code hook entry point.

Thin client that sends all hooks to the hook daemon over UDS. The daemon
handles dispatch, OTEL, and session start setup. If the daemon is unreachable,
the client starts a new one automatically.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

from pydantic import TypeAdapter

from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.hook_daemon.client import call_daemon
from devinfra.claude.session_paths import SessionPaths

_adapter: TypeAdapter[AnyHookInput] = TypeAdapter(AnyHookInput)

_HOOK_EVENT_LOG = Path("/tmp/claude-hook-events.jsonl")


def _log_event(direction: str, raw_json: str | None, *, hook_name: str = "", session_id: str = "") -> None:
    """Append a hook event entry to the machine-wide event log."""
    entry = {
        "ts": time.time(),
        "pid": os.getpid(),
        "dir": direction,
        "hook": hook_name,
        "session_id": session_id,
        "data": raw_json,
    }
    with _HOOK_EVENT_LOG.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def main() -> None:
    raw = sys.stdin.buffer.read()
    raw_str = raw.decode("utf-8", errors="replace")

    try:
        parsed = _adapter.validate_json(raw)
    except Exception:
        _log_event("input_parse_error", raw_str)
        raise

    _log_event("input", raw_str, hook_name=parsed.hook_event_name, session_id=parsed.session_id)

    paths = SessionPaths.from_env(parsed.session_id, dict(os.environ))

    result = call_daemon(parsed, dict(os.environ), paths)
    if result is not None and result.output is not None:
        output_json = result.output.model_dump_json(by_alias=True, exclude_none=True)
        _log_event("output", output_json, hook_name=parsed.hook_event_name, session_id=parsed.session_id)
        # exclude_none: Zod .optional() accepts undefined (absent) but NOT null.
        # Pydantic emits None as null by default; exclude_none omits those fields.
        sys.stdout.write(output_json)
    elif result is None:
        _log_event("error", "daemon_unavailable", hook_name=parsed.hook_event_name, session_id=parsed.session_id)
        print("ERROR: hook daemon unavailable", file=sys.stderr)
        sys.exit(1)
    else:
        _log_event("output", None, hook_name=parsed.hook_event_name, session_id=parsed.session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_event("crash", traceback.format_exc())
        print(f"Hook dispatch failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
