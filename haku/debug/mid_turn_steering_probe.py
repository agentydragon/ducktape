"""Does the CLI accept a second prompt while it is already answering one?

`haku/plans/matrix_chat_runtime.md` R2.2a defers mid-turn delivery as having "no native
mechanism". Reading the bundled CLI says otherwise, so this settles it by running it.

**Not a Bazel test.** It needs a real Claude credential and makes a real model call, and the
only place both exist is inside a `haku-claude` sandbox pod, where the egress proxy swaps the
placeholder for the subscription token. Run it there:

    kubectl -n haku-claude-sandbox cp mid_turn_steering_probe.py <pod>:/tmp/probe.py
    kubectl -n haku-claude-sandbox exec <pod> -- python3 /tmp/probe.py

What it does: asks for a slow, clearly-structured answer, waits until the first text is
streaming back, then writes a second `user` frame **without** waiting for `result`.

What to read off the output:

- `command_lifecycle` frames at all, and whether the second prompt's state goes
  `queued` → `started`. The bundled CLI's schema says these are emitted "on the stdout stream
  in -p/SDK sessions", and `claude_agent_sdk.types.Message` has no variant for them — so they
  are invisible to the SDK's typed layer and only a raw reader sees them.
- **Where `completed` lands relative to `result`.** The same schema says a command that starts
  a fresh turn completes *after* that turn's result frame, and one "folded into an
  already-in-flight turn" completes *before* it. That ordering is the answer: fold means the
  running turn absorbed it, and R2.2's hold-until-turn-end can become fold-into-turn.
- Whether the model's own answer reflects the steer, which is the part that actually matters
  to an operator typing "actually, skip the calendar part".
- How many `result` frames arrive. One means the prompts shared a turn — which is also the
  case that breaks a turn model where each turn owns exactly one prompt (R5.5 / the turn
  bracket discussion).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

FIRST = "Count slowly from 1 to 30, one number per line, with a short sentence about each."
STEER = "Actually, stop counting and just say the word PIVOT."

# Long enough that the first answer is unmistakably still in flight, short enough that a probe
# run stays interactive.
STEER_AFTER_SECONDS = 6.0
TOTAL_SECONDS = 120.0


def _frame(text: str) -> bytes:
    message = {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
    return (json.dumps(message) + "\n").encode()


async def main() -> int:
    claude = os.environ.get("CLAUDE_BIN", "claude")
    process = await asyncio.create_subprocess_exec(
        claude,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    started = time.monotonic()

    async def steer() -> None:
        await asyncio.sleep(STEER_AFTER_SECONDS)
        print(f"--- [{time.monotonic() - started:6.2f}s] writing the second prompt ---", flush=True)
        assert process.stdin is not None
        process.stdin.write(_frame(STEER))
        await process.stdin.drain()

    process.stdin.write(_frame(FIRST))
    await process.stdin.drain()

    results = 0
    async with asyncio.TaskGroup() as group:
        group.create_task(steer())
        while (line := await process.stdout.readline()) and time.monotonic() - started < TOTAL_SECONDS:
            frame = json.loads(line)
            kind = frame.get("type")
            elapsed = time.monotonic() - started
            # Deltas are the bulk of the stream and say nothing about ordering; everything else
            # is printed whole, since the point of the probe is which frames exist at all.
            if kind == "stream_event":
                continue
            if kind == "result":
                results += 1
            print(f"[{elapsed:6.2f}s] {json.dumps(frame)[:600]}", flush=True)
            if results == 2:
                break

    process.stdin.close()
    process.terminate()
    print(f"--- {results} result frame(s): {'separate turns' if results > 1 else 'one shared turn'} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
