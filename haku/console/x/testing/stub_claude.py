"""A stand-in for the `claude` binary, for the end-to-end tests that run a real runner.

Speaks enough of the CLI's newline-delimited JSON to be honest about the parts those tests are
actually about — the runner process, the transport, the websocket and the console either side of
it. It is deliberately **not** a model: what it answers is decided by what was asked, and what the
real CLI emits is pinned separately by the probes in `haku/cli_protocol/probes/`.

It answers `re: $body`, so a reply can be matched to the message that earned it — which is what
`../test_matrix_fullstack_e2e.py` asserts of every message the operator sent. The prompt a Matrix
turn carries is `[$event_id] $body` per message (`matrix_session._as_prompt`), so the event id is
stripped first. The body also carries the turn's stage directions, in trailing `[…]` markers the
test writes and this strips before answering:

- `[hold]` — wait for `release` in `HAKU_STUB_STATE` before finishing the turn, which is what
  leaves a turn open across a console going away.
- `[narrate=N]` — write N lines to stderr first. Each becomes a `setup_output` frame and then one
  paced notice in the room, so this is how a test fills the room's outbound queue: past
  `matrix_pacer.SEND_BURST` the queue drains at `SENDS_PER_SECOND`, and whatever is queued behind
  that has minutes rather than milliseconds to still be waiting when the console goes away.
- `[silent]` — answer with the `result` frame alone and no `assistant` message, which is the turn
  whose text exists only at the end (`_run_turn`'s `if not spoke` fallback).

Two more behaviours are load-bearing rather than decorative, and each exists because leaving it
out makes a test pass for the wrong reason:

- **It answers the `initialize` control request.** `ClaudeCli.connect()` sends one and waits for a
  correlated reply, so an echo loop hangs the console at connect rather than running a turn.
- **Every conversation frame carries the id the console dedupes replays by.** Without one, the
  console adopting a session would act on the runner's replay a second time and post the same
  answer twice — which is the bug the replay window exists to avoid, so a test would be asserting
  it away rather than exercising it. `_speak` puts each frame through the console's own reader
  (`haku/cli_protocol/frame_identity.py`) rather than through a rule copied by hand here.

`HAKU_STUB_GREETING`, when set, is written to stderr before anything else. Whatever the CLI writes
to stderr is the sandbox's narration, which the runner forwards as its own frame kind; it is the
console's one account of a session that never reached the model, so a test about that path
(`../test_claude_bridge_e2e.py`) needs a line printed before any turn.

The launch argv is **not acted on**: what the console passes is pinned by
`haku/runtime/x/claude_bridge/test_options.py`, and duplicating it here would be a second copy to
keep in step. One value is copied out of it rather than obeyed — `--append-system-prompt`, appended
to `system-prompts.jsonl` in `HAKU_STUB_STATE`, one line per CLI this run launched. It is the only
way a test can see what a session was woken with, and what a *replacement* session is woken with is
the whole of R3.3a.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from haku.cli_protocol.frame_identity import frame_uid
from haku.console.x.session_frames import frame_kind

_DIRECTIVE = re.compile(r"\s*\[(hold|silent|narrate=\d+)\]")


def _send(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _speak(frame: dict[str, Any]) -> None:
    """Send one conversation frame, refusing one the console could not recognise on a replay.

    The same pair the console records with (`SessionStore.record_frame`), so a frame this stub
    stamped in a way the dedupe cannot read fails here rather than in a test that would then be
    asserting the replay window away.
    """
    assert frame_uid(frame_kind(frame), frame) is not None, f"a frame with no identity to dedupe on: {frame=}"
    _send(frame)


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
        _speak(
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
    _speak(
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


def _record_system_prompt(state: Path) -> None:
    """Append what this CLI was launched with, for a test that asks how a session was woken.

    Appended rather than written, because the file spans every sandbox a test starts: a
    replacement session is a second launch, and telling it from the first is the point.
    """
    if "--append-system-prompt" not in sys.argv:
        return
    prompt = sys.argv[sys.argv.index("--append-system-prompt") + 1]
    with (state / "system-prompts.jsonl").open("a") as recorded:
        recorded.write(json.dumps(prompt) + "\n")


def main() -> None:
    state = Path(os.environ["HAKU_STUB_STATE"])
    _record_system_prompt(state)
    if (greeting := os.environ.get("HAKU_STUB_GREETING")) is not None:
        print(greeting, file=sys.stderr, flush=True)

    answered = 0
    while line := sys.stdin.readline():
        frame = json.loads(line)
        if frame["type"] == "control_request":
            _send(
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
