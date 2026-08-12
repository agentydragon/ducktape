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

**Every frame is printed, both directions, verbatim** — `>>` for what we write to the CLI,
`<<` for what it writes back — with a headline above each line for readability and a tally at
the end that names the frame types it saw *zero* of. Nothing is filtered: partial messages are
simply not requested (no `--include-partial-messages`), so what is printed is the whole
conversation on the wire rather than a selection from it. Earlier versions printed a curated
subset, which is how two runs in a row managed to be inconclusive — a frame missing from the
output could not be told apart from a frame missing from the wire.

## Answer (2026-08-12): **it folds, at a tool boundary**

Run 3, printing all traffic. First prompt spending its time in `Bash sleep 4` calls; steer
written at 8.0s, while the step-1 sleep was running:

    [  3.98s] << assistant       tool_use(Bash "sleep 4", "Sleep 4 seconds (step 1)")
    [  7.18s] << system/task_started      task_id, tool_use_id, task_type: local_bash
    [  8.00s] >> user            "Stop after the current step … reply with the word PIVOT."
    [  8.32s] << system/task_notification status: completed
    [  8.32s] << user            tool_result
    [ 10.61s] << assistant       text('PIVOT')
    [ 10.61s] << result/success  num_turns: 2, result: "PIVOT", terminal_reason: completed

Sleeps 2 through 5 never ran and **there is no second `result`**: one turn answered both
prompts. A prompt written mid-turn is absorbed at the next step boundary and the model acts on
it — exactly what R2.2a wants and what its design note says is impossible.

Three things the full-traffic view added that a filtered one had hidden:

- **`system/init` advertises `capabilities`:** `interrupt_receipt_v1`,
  `interrupt_cancel_queued_v1`, **`msg_lifecycle_v1`**. So `command_lifecycle` is not missing,
  it is **behind a capability nobody negotiated** — and the Python SDK's `initialize` request
  sends hooks, agents and skills but no client capabilities at all, so there is currently no
  way to ask for it. (In the CLI the emitter is installed conditionally on the input source's
  type, so the gate may not be the capability alone.) Worth chasing before building anything
  that needs to *confirm* a steer landed rather than infer it from behaviour.
- **`interrupt_cancel_queued_v1` is a capability**, which means interrupt and queued messages
  interact — relevant to our abort path, which today knows nothing about a prompt that may be
  sitting in the CLI's queue.
- **`system/task_started` / `task_notification` carry `tool_use_id`, a `task_type`, and a
  human-readable `description`** ("Sleep 4 seconds (step 1)"). That is a ready-made "what is
  Haku doing right now" signal for R6's status line, it is already in our frame store, and the
  SDK's `Message` union has no variant for it — so the typed layer drops it.

One qualification stands: **the boundary is what makes it work** (see run 1). A turn generating
continuous prose has no step to absorb at, and there the steer waits for the turn to end.

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
from collections import Counter

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

# Named so their *absence* is reported rather than inferred. `command_lifecycle` is the one
# the bundled CLI's schema promises for folding; a run that shows `command_lifecycle=0` has
# said something, where a run that simply never mentions it has not.
_ALWAYS_TALLIED = ("assistant", "user", "system", "result", "command_lifecycle", "stream_event")


def _frame(text: str) -> bytes:
    message = {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
    return (json.dumps(message) + "\n").encode()


def _headline(frame: dict[str, object]) -> str:
    """A short label for a frame, above the verbatim JSON that follows it."""
    kind = str(frame.get("type"))
    if subtype := frame.get("subtype"):
        kind = f"{kind}/{subtype}"
    message = frame.get("message")
    if not isinstance(message, dict):
        return kind
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
    return f"{kind}  {' | '.join(parts)}" if parts else kind


def _log(started: float, arrow: str, raw: str) -> None:
    """Every frame, both directions, headline then the line exactly as it crossed.

    Verbatim and unfiltered on purpose. The first run of this probe printed a curated subset,
    which is how it managed to be inconclusive twice: what was missing from the output could
    not be distinguished from what was missing from the wire.
    """
    frame = json.loads(raw)
    print(f"[{time.monotonic() - started:6.2f}s] {arrow} {_headline(frame)}", flush=True)
    print(f"           {raw.strip()}", flush=True)


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

    async def write(text: str) -> None:
        assert process.stdin is not None
        raw = _frame(text)
        process.stdin.write(raw)
        await process.stdin.drain()
        _log(started, ">>", raw.decode())

    steered_at: float | None = None

    async def steer() -> None:
        nonlocal steered_at
        if control:
            return
        await asyncio.sleep(STEER_AFTER_SECONDS)
        steered_at = time.monotonic() - started
        await write(STEER)

    await write(FIRST)
    if control:
        # Both up front: if this also produces two turns, the queue is unconditional and no
        # arrival time would have changed the outcome.
        await write(STEER)

    results = 0
    first_result_at: float | None = None
    seen: Counter[str] = Counter()
    async with asyncio.TaskGroup() as group:
        steering = group.create_task(steer())
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
            raw = line.decode()
            kind = str(json.loads(raw).get("type"))
            seen[kind] += 1
            if kind == "result":
                results += 1
                first_result_at = first_result_at if first_result_at is not None else time.monotonic() - started
            _log(started, "<<", raw)
        # Otherwise it writes into a finished conversation and the run reports a verdict about
        # a steer that was never in flight.
        steering.cancel()

    process.stdin.close()
    process.terminate()
    tally = " ".join(f"{kind}={seen[kind]}" for kind in dict.fromkeys((*_ALWAYS_TALLIED, *seen)))
    print(f"--- frames in: {tally}")

    # A steer that went in after the turn already ended tests nothing, and the frame counts
    # would read exactly like a real result. Say so instead of reporting a verdict: the first
    # smoke run of this script did precisely that against a CLI that answered in 1.6s.
    if not control and (steered_at is None or (first_result_at is not None and steered_at > first_result_at)):
        print("--- VOID: the turn ended before the second prompt went in; nothing was steered ---")
        return 1
    folded = "folded into the running turn" if results == 1 else "answered as its own turn"
    print(f"--- {results} result frame(s): the second prompt was {folded} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
