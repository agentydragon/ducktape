"""What happens to a prompt written while a turn is already running?

Two scenarios, because there are two answers and the console needs both. `fold` measures where a
mid-turn prompt lands; `interrupt` measures what an abort does to one still queued.

**The first prompt must have real step boundaries.** A turn answered in continuous prose has
nothing between its first token and its last, so every candidate absorption point — the CLI's own
folding, `PreToolUse`, appending to a tool result — is guaranteed to do nothing, and the run
proves only that this shape of turn cannot be steered. Asking the model to work "slowly" does not
help: slow prose is still one message. Hence separate `Bash` calls with a real `sleep` in each.

Verdicts come from `command_lifecycle`, which is why every prompt here is uuid-stamped. Without
that, folding was visible only in what the model did next, and "the CLI queued it but has not
read it" could not be told apart from "the CLI read it and ignored it".
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

from haku.cli_protocol.probes.harness import Probe, allow_every_tool

STEPPED = (
    "Do exactly this, one step at a time, without combining steps: "
    "run `sleep 5` in Bash, then tell me 'step 1 done'; "
    "run `sleep 5` again, then tell me 'step 2 done'; "
    "and keep going the same way through step 5. "
    "Use a separate Bash call for each sleep and do not run them in one command."
)
STEER = "Stop after the current step. Do not run any more sleeps. Just reply with the word PIVOT."
FOLLOW_UP = "Afterwards, tell me a joke about ducks."

# Into the second step's wait: late enough that the turn is unambiguously in flight, early enough
# that three boundaries are still ahead of it.
STEER_AFTER_SECONDS = 8.0


async def _turn_with_second_prompt(second: str, delay: float) -> tuple[Probe, dict[str, str]]:
    probe = Probe("--permission-prompt-tool", "stdio")
    await probe.start()
    probe.inbound = allow_every_tool
    await probe.control({"subtype": "initialize"})

    first_uuid, second_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    await probe.prompt(STEPPED, command_uuid=first_uuid)
    await asyncio.sleep(delay)
    await probe.prompt(second, command_uuid=second_uuid)
    return probe, {first_uuid: "first", second_uuid: "second"}


def _lifecycle(probe: Probe, names: dict[str, str]) -> list[tuple[str, Any]]:
    return [
        (names.get(str(frame.get("command_uuid")), "?"), frame.get("state"))
        for frame in probe.of_type("command_lifecycle")
    ]


async def fold() -> None:
    """Does the second prompt join the running turn, or wait for the next one?"""
    probe, names = await _turn_with_second_prompt(STEER, STEER_AFTER_SECONDS)
    result = await probe.wait_for("result", seconds=180)
    before_result = probe.frames[: probe.frames.index(result)]
    folded = any(
        names.get(str(frame.get("command_uuid"))) == "second" and frame.get("state") == "completed"
        for frame in before_result
        if frame.get("type") == "command_lifecycle"
    )
    print(f"  lifecycle: {_lifecycle(probe, names)}", flush=True)
    print(f"  result frames: {len(probe.of_type('result'))}", flush=True)
    print(f"  VERDICT: the second prompt was {'folded into the running turn' if folded else 'left for its own turn'}")
    await probe.stop()


async def interrupt(cancel_queued: bool) -> None:
    """Does aborting the running turn also drop what is queued behind it?"""
    probe, names = await _turn_with_second_prompt(FOLLOW_UP, 3.0)
    await asyncio.sleep(4.0)
    request: dict[str, Any] = {"subtype": "interrupt", "reason": "user-cancel"}
    if cancel_queued:
        request["cancel_queued"] = True
    await probe.control(request, seconds=30)

    await asyncio.sleep(20.0)
    lifecycle = _lifecycle(probe, names)
    print(f"  lifecycle: {lifecycle}", flush=True)
    print(
        f"  VERDICT with cancel_queued={cancel_queued}: the queued prompt "
        f"{'ran anyway' if ('second', 'started') in lifecycle else 'was dropped'}"
    )
    await probe.stop()


async def main() -> None:
    match sys.argv[1:]:
        case ["interrupt"]:
            await interrupt(cancel_queued=False)
        case ["interrupt", "cancel-queued"]:
            await interrupt(cancel_queued=True)
        case _:
            await fold()


if __name__ == "__main__":
    asyncio.run(main())
