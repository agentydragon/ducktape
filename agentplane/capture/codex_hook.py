"""The shell command a hooks capture registers with Codex for every event: records the hook input
Codex writes to its stdin and answers on stdout.

Stdlib only, since Codex runs it as a bare interpreter script: `codex_hook.py LOG allow|deny`. A
PreToolUse firing is answered with that decision; every other event gets `{}`, no opinion.
"""

import json
import sys
import time
from pathlib import Path

DENIAL = "agentplane capture: this tool call is denied by a PreToolUse hook"


def main() -> None:
    log, decision = sys.argv[1], sys.argv[2]
    payload = sys.stdin.read()
    with Path(log).open("a", encoding="utf-8") as output:
        output.write(json.dumps({"time_ns": time.monotonic_ns(), "text": payload}) + "\n")
    answer: dict[str, object] = {}
    if json.loads(payload).get("hook_event_name") == "PreToolUse":
        answer = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": DENIAL if decision == "deny" else "agentplane capture allows",
            }
        }
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
