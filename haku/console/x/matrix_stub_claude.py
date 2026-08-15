#!/usr/bin/env python3
"""A stand-in for the `claude` binary, driven by what the operator typed into the room.

Sibling of `stub_claude.py`, which answers a fixed script; this one answers *about* the prompt,
because the test it serves asserts that every message the operator sent has its own reply. The
prompt a Matrix turn carries is `[$event_id] $body` per message (`matrix_session._as_prompt`), so
the answer is `re: $body` and a reply can be matched to the message that earned it.

The body also carries the turn's stage directions, in trailing `[…]` markers the test writes and
this strips before answering:

- `[hold]` — wait for `release` in `HAKU_STUB_STATE` before finishing the turn, which is what
  leaves a turn open across a console going away.
- `[narrate=N]` — write N lines to stderr first. Each becomes a `setup_output` frame and then one
  paced notice in the room, so this is how a test fills the room's outbound queue: past
  `matrix_pacer.SEND_BURST` the queue drains at `SENDS_PER_SECOND`, and whatever is queued behind
  that has minutes rather than milliseconds to still be waiting when the console goes away.
- `[silent]` — answer with the `result` frame alone and no `assistant` message, which is the turn
  whose text exists only at the end (`_run_turn`'s `if not spoke` fallback).

Every frame carries the id the console dedupes replays by (`haku/cli_protocol/frame_identity.py`);
without one an adopted connection's replay would post every answer a second time.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_DIRECTIVE = re.compile(r"\s*\[(hold|silent|narrate=\d+)\]")


def send(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _prompt_text(frame: dict[str, Any]) -> str:
    content = frame["message"]["content"]
    if isinstance(content, str):
        return content
    return "".join(str(block.get("text", "")) for block in content)


def _answer(state: Path, prompt: str, answered: int) -> None:
    """Answer one prompt, obeying whatever directions its last line carried."""
    # The last line, because a batch the sync loop folded together is several messages and the
    # directions belong to the one that arrived last.
    body = prompt.strip().splitlines()[-1].split("] ", 1)[-1]
    directives = _DIRECTIVE.findall(body)
    text = f"re: {_DIRECTIVE.sub('', body).strip()}"

    for narrate in (int(each.split("=")[1]) for each in directives if each.startswith("narrate=")):
        for line in range(narrate):
            print(f"narration {answered}.{line}", file=sys.stderr, flush=True)

    if "silent" not in directives:
        send(
            {
                "type": "assistant",
                "message": {"id": f"msg_{answered}", "role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
    if "hold" in directives:
        (asked := state / "asked").write_text(text)
        while not (release := state / "release").exists():
            time.sleep(0.05)
        # Both consumed, so a later `[hold]` in the same run waits for its own release rather than
        # walking straight through this one's.
        release.unlink()
        asked.unlink()
    send(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "uuid": f"result_{answered}",
            "total_cost_usd": 0.01,
            "duration_ms": 12,
        }
    )


def main() -> None:
    state = Path(os.environ["HAKU_STUB_STATE"])
    answered = 0
    while line := sys.stdin.readline():
        frame = json.loads(line)
        if frame["type"] == "control_request":
            send(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": frame["request_id"], "response": {}},
                }
            )
            continue
        if frame["type"] != "user":
            continue
        answered += 1
        _answer(state, _prompt_text(frame), answered)


if __name__ == "__main__":
    main()
