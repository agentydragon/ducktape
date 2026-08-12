"""Does a second prompt reach the agent while it is already working on the first?

`haku/plans/matrix_chat_runtime.md` R2.2a defers mid-turn delivery, its design note claiming a
running turn **drops** stdin input. The bundled CLI describes the opposite — a `messageQueue`
with "mid-turn absorption" and a `command_lifecycle` state for a command "folded into an
already-in-flight turn", emitted "on the stdout stream in -p/SDK sessions". This settles which
is true by running it.

**Not a Bazel test.** It needs a real Claude credential and makes real model calls. Run it
wherever one exists — a `haku-claude` sandbox pod, where the egress proxy substitutes the
subscription token, or any box with a logged-in CLI:

    python3 mid_turn_steering_probe.py           # steer during a multi-step turn
    PROBE_CONTROL=1 python3 …                    # control: both prompts written up front

## Answer (2026-08-12): **it folds, at a tool boundary**

Run 2, first prompt spending its time in `Bash sleep 4` calls, steer written at 8.0s while the
first sleep was running:

    [  4.34s] assistant: tool_use(Bash {"command": "sleep 4"})
    --- [  8.00s] writing the second prompt ---
    [  9.69s] user: tool_result
    [ 11.41s] assistant: text('PIVOT')
    [ 11.45s] result   num_turns: 2

The agent never ran sleeps 2 through 5, and **there is no second `result`**: one turn answered
both prompts. So a prompt written mid-turn is absorbed at the next step boundary and the model
acts on it — which is exactly what R2.2a wants and what its design note says is impossible.

Two qualifications, both load-bearing:

- **The boundary is what makes it work** (see run 1 below). A turn generating continuous prose
  has no step to absorb at, and the steer waits for the turn to end.
- **No `command_lifecycle` frames appeared even here.** The folding is observable only by its
  effect, not by the lifecycle events the bundled CLI's schema documents — so a harness cannot
  currently tell "absorbed" from "queued" except by watching what the model does.

## Run 1 (2026-08-12): inconclusive, and worth keeping as a lesson

The first version asked the agent to "count slowly from 1 to 30". It got back one assistant
message with all thirty numbers, then `result`, then `PIVOT` as a second turn — so the steer
was neither dropped nor honoured, just answered next.

That proves less than it looks. **"Slowly" does not create a boundary.** One prompt answered
in continuous prose is a single generated message: no tool calls, no step boundaries, nothing
between the first token and the last at which anything could be injected. Every candidate
mechanism — the CLI's own folding, `PreToolUse`, appending to a tool result — lands at a step
boundary, so a turn with no steps is the one shape where they are all guaranteed to do nothing.

It also could not distinguish "the CLI received it during the turn and queued it" from "the
CLI did not read stdin until the turn ended". A pipe write succeeds into the OS buffer either
way, and the frames that would have separated them — a `command_lifecycle` with state
`queued` — never appeared at all.

## What this version does instead

The first prompt asks for **explicitly separate steps with a real wait in each**: five `Bash`
calls of `sleep 4`, one at a time, with a line of text between them. That produces four real
step boundaries spread over ~20s, so a mechanism that only fires at one has somewhere to fire,
and the tool-use and tool-result frames make the boundaries visible in the output.

Read off the result:

- **Does `PIVOT` appear before the first turn's `result` frame?** That is folding, and it means
  R2.2's hold-until-turn-end can become fold-into-turn.
- **Do the remaining `sleep` calls stop happening?** Folding that the model then acts on is the
  thing an operator actually wants from "skip the calendar part".
- **Any `command_lifecycle` frames, and in which state?** A `queued` state proves the CLI read
  the message during the turn even if it does not act on it — which separates "queued" from
  "not read yet", the ambiguity that made run 1 useless.
- **How many `result` frames.** One means the prompts shared a turn, which is also the case
  that breaks a turn model where a turn owns exactly one prompt.
- **The control arm.** If writing both prompts up front *also* yields two turns, the queue is
  unconditional and stdin is not a steering channel at all, whatever the boundaries look like.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Explicitly separate steps, each with a real wait in it, so the turn has boundaries rather
# than being one long generated message. `sleep` in Bash rather than a prompt asking for
# slowness: the wait has to be real time inside a tool call, not a request for pacing.
FIRST = (
    "Do exactly this, one step at a time, without combining steps: "
    "run `sleep 4` in Bash, then tell me 'step 1 done'; "
    "run `sleep 4` again, then tell me 'step 2 done'; "
    "and keep going the same way through step 5. "
    "Use a separate Bash call for each sleep and do not run them in one command."
)
STEER = "Stop after the current step. Do not run any more sleeps. Just reply with the word PIVOT."

# Into the second step's wait: late enough that the turn is unambiguously in flight, early
# enough that three boundaries are still ahead of it.
STEER_AFTER_SECONDS = 8.0
TOTAL_SECONDS = 180.0
# How long to keep reading after a `result` before calling the stream done. A folded prompt
# never produces a second one, so any "wait for two results" exit condition hangs.
QUIET_AFTER_RESULT = 5.0

# Frames worth seeing in full: the boundaries, the lifecycle events, and the turn's end.
_INTERESTING = ("assistant", "user", "result", "command_lifecycle")


def _frame(text: str) -> bytes:
    message = {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
    return (json.dumps(message) + "\n").encode()


def _summarize(frame: dict[str, object]) -> str:
    """One line per frame, keeping what marks a boundary rather than the prose around it."""
    kind = frame.get("type")
    message = frame.get("message")
    if kind in {"assistant", "user"} and isinstance(message, dict):
        parts = []
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            match block.get("type"):
                case "text":
                    parts.append(f"text({str(block.get('text', ''))[:120]!r})")
                case "tool_use":
                    parts.append(f"tool_use({block.get('name')} {json.dumps(block.get('input'))[:80]})")
                case "tool_result":
                    parts.append("tool_result")
                case other:
                    parts.append(str(other))
        return f"{kind}: {' | '.join(parts)}"
    return json.dumps(frame)[:400]


async def main() -> int:
    claude = os.environ.get("CLAUDE_BIN", "claude")
    control = os.environ.get("PROBE_CONTROL") == "1"
    process = await asyncio.create_subprocess_exec(
        claude,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    started = time.monotonic()

    async def steer() -> None:
        if control:
            return
        await asyncio.sleep(STEER_AFTER_SECONDS)
        print(f"--- [{time.monotonic() - started:6.2f}s] writing the second prompt ---", flush=True)
        assert process.stdin is not None
        process.stdin.write(_frame(STEER))
        await process.stdin.drain()

    process.stdin.write(_frame(FIRST))
    if control:
        # Both up front: if this also produces two turns, the queue is unconditional and no
        # arrival time would have changed the outcome.
        process.stdin.write(_frame(STEER))
    await process.stdin.drain()

    results = 0
    async with asyncio.TaskGroup() as group:
        group.create_task(steer())
        while time.monotonic() - started < TOTAL_SECONDS:
            # After a `result`, wait only a short grace period: a folded prompt produces no
            # second result, so waiting for one is waiting forever. (It did — the first version
            # of this script hung here rather than reporting the finding.)
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=QUIET_AFTER_RESULT if results else None
                )
            except TimeoutError:
                break
            if not line:
                break
            frame = json.loads(line)
            if frame.get("type") not in _INTERESTING:
                continue
            if frame.get("type") == "result":
                results += 1
            print(f"[{time.monotonic() - started:6.2f}s] {_summarize(frame)}", flush=True)

    process.stdin.close()
    process.terminate()
    folded = "folded into the running turn" if results == 1 else "answered as its own turn"
    print(f"--- {results} result frame(s): the second prompt was {folded} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
