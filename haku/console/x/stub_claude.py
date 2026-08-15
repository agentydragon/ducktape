#!/usr/bin/env python3
"""A stand-in for the `claude` binary, for the bridge end-to-end test.

Speaks enough of the CLI's newline-delimited JSON to be honest about the parts the test is
actually about — the runner process, the transport, the websocket and the console either side of
it. It is deliberately **not** a model: what it answers is fixed, and what the real CLI emits is
pinned separately by the probes in `haku/cli_protocol/probes/`.

Three behaviours here are load-bearing rather than decorative, and each exists because leaving it
out makes the test pass for the wrong reason:

- **It answers the `initialize` control request.** `ClaudeCli.connect()` sends one and waits for a
  correlated reply, so an echo loop hangs the console at connect rather than running a turn.
- **Every frame carries the id the console dedupes replays by** (`haku/cli_protocol/frame_identity.py`).
  Without one, the console adopting a session would act on the runner's replay a second time and
  post the same answer twice — which is the bug the replay window exists to avoid, so the test
  would be asserting it away rather than exercising it.
- **The second answer is held until released.** That is what strands a turn in flight while the
  console goes away, which is the whole subject of the adoption case.

The launch argv is ignored on purpose: what the console passes is pinned by
`haku/runtime/x/claude_bridge/test_options.py`, and duplicating it here would be a second copy to
keep in step.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def send(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def main() -> None:
    state = Path(os.environ["HAKU_STUB_STATE"])
    # Whatever the CLI writes to stderr is the sandbox's narration, which the runner forwards as
    # its own frame kind; it is the console's one account of a session that never reached the model.
    print("the sandbox says hello", file=sys.stderr, flush=True)

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
        text = f"answer {answered}"
        send(
            {
                "type": "assistant",
                "message": {"id": f"msg_{answered}", "role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
        if answered == 2:
            (state / "asked").write_text("")
            while not (state / "release").exists():
                time.sleep(0.05)
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


if __name__ == "__main__":
    main()
