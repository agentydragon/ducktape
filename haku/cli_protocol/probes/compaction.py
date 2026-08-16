"""What does a compaction look like on the wire, and does it invalidate a stored cursor?

The question with consequences. Haku's projection contract is that *a prefix of the frame log
determines the transcript* (<../../plans/chat_runtime_projection.md>), and compaction is the only
event that rewrites what the conversation **is**. Either the pre-compaction frames stay in the log
and a stored cursor still reproduces what the model saw, or they do not — and the durable cursor
has a hazard nobody has costed.

One session exercises three things at once, so the capture holds all of them:

- **Hooks handled in Python.** `hooks` on `initialize` with `hookCallbackIds`; the CLI asks over
  `hook_callback` and this process answers. That is exactly the wire the Agent SDK's `hooks=`
  option produces — the SDK is the typed layer above this, and probes stay stdlib-only, so the
  measurement is taken here. `--include-hook-events` is passed to see whether the CLI's own hook
  lifecycle also reaches the conversation channel; measured, it does not.
- **A tool implemented in-process.** `sdkMcpServers: ["probe"]` makes this client the MCP server;
  the same wire the SDK's in-process tool servers produce. Its one tool returns filler, which is
  also how the context grows.
- **A real compaction**, forced rather than waited for. `--autocompact` sets the window the CLI
  budgets against (its floor is 100k) and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` sets the fraction of
  it that trips auto-compact. Both are the CLI's own accounting, not the API's, so the session
  compacts against a window it would otherwise need most of a real one to reach. The
  `autocompact_state` frame reports what they resolved to, so the run asserts it is about to
  compact rather than hoping. **`CLAUDE_CODE_MAX_CONTEXT_TOKENS` is not the lever**: measured, it
  left `effective_window` at 180000 with `source: "auto"` — it is the model's assumed window, not
  the auto-compact one.

    python3 -m haku.cli_protocol.probes.compaction /tmp/capture.jsonl

Redact before committing anything it writes: `python3 -m haku.cli_protocol.probes.redact_capture`.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from haku.cli_protocol.probes.harness import Probe, allow_every_tool

SERVER = "probe"
FILLER_TOOL = "haku_filler"

# Planted before the compaction and asked for after it. Nothing in the model's training predicts
# it, so an answer that contains it is evidence the summary carried it across the boundary rather
# than evidence the model played along.
CANARY = "PLATYPUS-7731"

# The CLI's floor for `--autocompact`, from which it subtracts 20k of headroom: 100000 resolves
# to `effective_window: 80000`. The percentage then has to clear the ~26k the system prompt and
# tool schemas already cost, with enough room above it for several turns to precede the boundary —
# a capture whose whole pre-compaction history is one turn proves less about the prefix.
WINDOW_TOKENS = 100_000
THRESHOLD_PCT = 60
FILLER_WORDS = 4_000
MAX_ROUNDS = 12

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "PreCompact", "SessionStart", "Stop")

# A closed vocabulary, so the filler is bulk without being prose: nothing in it can be mistaken
# for content, and the redactor can recognise and elide it by shape.
VOCABULARY = ("alpha", "bravo", "delta", "echo", "gamma", "kilo", "lima", "mike", "oscar", "tango", "victor", "zulu")


def serve_mcp(message: dict[str, Any]) -> dict[str, Any]:
    match message.get("method"):
        case "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "0.1.0"},
            }
        case "tools/list":
            return {
                "tools": [
                    {
                        "name": FILLER_TOOL,
                        "description": "Returns a block of filler words. Call it when asked to.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"words": {"type": "integer"}},
                            "required": ["words"],
                        },
                    }
                ]
            }
        case "tools/call":
            words = int((message.get("params") or {}).get("arguments", {}).get("words", FILLER_WORDS))
            rng = random.Random(repr(message.get("id")))
            text = " ".join(rng.choice(VOCABULARY) for _ in range(words))
            return {"content": [{"type": "text", "text": text}], "isError": False}
        case _:
            # Notifications carry no `id` and still arrive as control requests, so they still need
            # an answer; an empty result is the convention.
            return {}


def compact_boundaries(probe: Probe) -> list[dict[str, Any]]:
    return [frame for frame in probe.of_type("system") if frame.get("subtype") == "compact_boundary"]


def frame_classes(probe: Probe) -> dict[str, int]:
    """`type`, or `type/subtype` where there is one — the unit <../../console/debug/> counts in."""
    counts = Counter(
        f"{frame.get('type')}/{subtype}" if (subtype := frame.get("subtype")) is not None else str(frame.get("type"))
        for frame in probe.frames
    )
    return dict(sorted(counts.items()))


async def main() -> None:
    capture = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("compaction_capture.jsonl")
    probe = Probe(
        "--permission-prompt-tool",
        "stdio",
        # A fresh id, so nothing in the capture is the capturing session's identity even if the
        # environment scrub in `harness` ever misses a variable.
        "--session-id",
        str(uuid.uuid4()),
        # The operator's own MCP servers and skills would otherwise be in the system prompt and
        # in `system/init` — identifying, and thousands of tokens of budget spent on nothing the
        # probe measures.
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": {}}),
        "--disable-slash-commands",
        "--include-hook-events",
        "--autocompact",
        str(WINDOW_TOKENS),
        "--model",
        "haiku",
        capture=capture,
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(THRESHOLD_PCT)},
    )
    await probe.start()

    async def inbound(frame: dict[str, Any]) -> dict[str, Any] | None:
        request = frame.get("request") or {}
        match request.get("subtype"):
            case "hook_callback":
                print(f"  hook {request.get('callback_id')}: {json.dumps(request.get('input'))[:200]}", flush=True)
                # Observe only: a hook that blocked anything would stop the context from growing.
                return {}
            case "mcp_message":
                message = request["message"]
                print(f"  mcp {request['server_name']} {message.get('method')}", flush=True)
                return {"mcp_response": {"jsonrpc": "2.0", "id": message.get("id"), "result": serve_mcp(message)}}
            case _:
                return await allow_every_tool(frame)

    probe.inbound = inbound
    try:
        await run(probe)
    finally:
        # A timeout mid-run would otherwise leave the CLI process alive and the capture unflushed,
        # which is the run you most want the partial capture from.
        await probe.stop()
    print(f"  capture written to {capture} ({capture.stat().st_size} bytes)", flush=True)


async def run(probe: Probe) -> None:
    await probe.control(
        {
            "subtype": "initialize",
            "hooks": {event: [{"hookCallbackIds": [f"cb_{event}"]}] for event in HOOK_EVENTS},
            "sdkMcpServers": [SERVER],
        }
    )

    await probe.prompt(f"Remember this code word exactly: {CANARY}. Reply with just: OK")
    await probe.wait_for("result", seconds=180)

    for state in probe.of_type("autocompact_state"):
        print(f"  autocompact_state: {json.dumps(state)}", flush=True)

    for round_index in range(1, MAX_ROUNDS + 1):
        mark = len(probe.frames)
        await probe.prompt(
            f"Call {FILLER_TOOL} with words={FILLER_WORDS}, then reply with just the last word it returned."
        )
        await probe.wait_for("result", seconds=300, after=mark)
        usage = await probe.control({"subtype": "get_context_usage"}, seconds=60)
        print(f"  round {round_index}: context {json.dumps(usage.get('response'))[:400]}", flush=True)
        if compact_boundaries(probe):
            break

    print(f"  frame classes: {json.dumps(frame_classes(probe))}", flush=True)
    if not (boundaries := compact_boundaries(probe)):
        print(f"  NO COMPACTION in {MAX_ROUNDS} rounds — report this rather than the capture", flush=True)
        return

    boundary_at = probe.frames.index(boundaries[0])
    print(f"  compact_boundary at frame {boundary_at} of {len(probe.frames)}: {json.dumps(boundaries[0])}", flush=True)
    print(f"  frames still in the log ahead of the boundary: {boundary_at}", flush=True)

    mark = len(probe.frames)
    await probe.prompt("What was the code word I asked you to remember? Reply with just the word.")
    answer = str((await probe.wait_for("result", seconds=180, after=mark)).get("result"))
    print(f"  the code word survived the compaction: {CANARY in answer}", flush=True)
    print(f"  post-compaction answer: {answer[:200]!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
