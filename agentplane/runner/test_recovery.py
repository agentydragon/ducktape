"""Native evidence parsing and comparison; real model-request assertions live in app:test_bridge."""

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.protocol import event_pb2
from agentplane.runner import claude_history, codex_history
from agentplane.runner.journal import Journal
from agentplane.runner.recovery import ObservedItem, compare_item, observed_items
from agentplane.runner.testing import codex_rollout
from agentplane.runner.testing.claude_transcript import (
    assistant_message,
    redacted_thinking_block,
    text_block,
    thinking_block,
    tool_result_block,
    tool_use_block,
    user_message,
    write_transcript,
)

# gazelle:include_dep @pypi//protobuf


def test_codex_pending_call_uses_model_normalization_not_execution_success(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "rollout-session.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"type": "response_item", "payload": {"type": "function_call", "call_id": "tool"}}) + "\n"
    )
    observed = {
        "tool": ObservedItem("tool", event_pb2.ITEM_KIND_TOOL_CALL, arguments='{"command":"run"}', output="partial"),
        "text": ObservedItem("text", event_pb2.ITEM_KIND_ASSISTANT_TEXT, text="uncommitted stream"),
    }
    recovered = codex_history.read_history(tmp_path, "session", observed)
    assert recovered is not None
    assert "text" not in recovered
    decision = compare_item(observed["tool"], recovered["tool"])
    assert decision.disposition == event_pb2.RECOVERY_DISPOSITION_REVISED
    assert decision.replacement.output == "aborted"
    assert not recovered["tool"].completed
    assert compare_item(observed["text"], recovered.get("text")).disposition == event_pb2.RECOVERY_DISPOSITION_ABSENT


@pytest.mark.parametrize(
    ("observed_text", "disposition"),
    [("first thought", event_pb2.RECOVERY_DISPOSITION_RETAINED), ("first", event_pb2.RECOVERY_DISPOSITION_REVISED)],
)
def test_codex_reasoning_is_recovered_from_its_saved_record_by_id(
    tmp_path: Path, observed_text: str, disposition: event_pb2.RecoveryDisposition
) -> None:
    codex_rollout.write_rollout(tmp_path, codex_rollout.ROLLOUT)
    saved, unsaved = codex_rollout.FIRST_REASONING_ID, "rs_not_in_rollout"
    observed = {
        saved: ObservedItem(saved, event_pb2.ITEM_KIND_REASONING, text=observed_text, completed=True),
        unsaved: ObservedItem(unsaved, event_pb2.ITEM_KIND_REASONING, text="unsaved thought", completed=True),
    }
    recovered = codex_history.read_history(tmp_path, codex_rollout.THREAD_ID, observed)
    assert recovered is not None
    assert set(recovered) == {saved}
    assert recovered[saved].text == "first thought"
    assert compare_item(observed[saved], recovered[saved]).disposition == disposition
    assert compare_item(observed[unsaved], recovered.get(unsaved)).disposition == event_pb2.RECOVERY_DISPOSITION_ABSENT


@pytest.mark.parametrize(
    "record", [{"type": "compacted"}, {"type": "event_msg", "payload": {"type": "thread_rolled_back"}}]
)
def test_codex_unsupported_history_is_unknown_not_empty(tmp_path: Path, record: dict[str, object]) -> None:
    path = tmp_path / "sessions" / "rollout-session.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps(record) + "\n")
    assert codex_history.read_history(tmp_path, "session", {}) is None


def test_claude_main_chain_excludes_unresolved_tool_and_sidechain(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "workspace" / "session.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "assistant",
            "uuid": "a",
            "parentUuid": None,
            "message": {"role": "assistant", "id": "message", "content": [{"type": "text", "text": "retained"}]},
        },
        {
            "type": "assistant",
            "uuid": "b",
            "parentUuid": "a",
            "message": {
                "role": "assistant",
                "id": "message",
                "content": [{"type": "tool_use", "id": "call", "name": "Bash", "input": {"command": "run"}}],
            },
        },
        {
            "type": "assistant",
            "uuid": "side",
            "parentUuid": "a",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "id": "side-message",
                "content": [{"type": "text", "text": "other conversation"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    recovered = claude_history.read_history(tmp_path, "session")
    assert set(recovered) == {"message#0"}
    assert recovered["message#0"].text == "retained"
    with path.open("a") as output:
        output.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "c",
                    "parentUuid": "b",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call", "content": "done"}],
                    },
                }
            )
            + "\n"
        )
    recovered = claude_history.read_history(tmp_path, "session")
    assert recovered["call"].output == "done"
    assert recovered["call"].completed
    assert "side-message#0" not in recovered


def test_claude_thinking_is_loaded_under_the_id_of_its_block_in_its_message(tmp_path: Path) -> None:
    write_transcript(
        tmp_path,
        "session",
        user_message("first input"),
        assistant_message("msg_1", thinking_block("SEED_THOUGHT")),
        assistant_message("msg_1", text_block("SEED_TEXT")),
        user_message("second input"),
        assistant_message("msg_2", thinking_block("THOUGHT_BEFORE_TOOL")),
        assistant_message("msg_2", tool_use_block("call")),
        user_message([tool_result_block("call", "done")]),
        assistant_message("msg_3", thinking_block("THOUGHT_AFTER_TOOL")),
        assistant_message("msg_3", text_block("DONE")),
    )
    recovered = claude_history.read_history(tmp_path, "session")
    assert {
        key: (item.kind, item.text) for key, item in recovered.items() if item.kind != event_pb2.ITEM_KIND_TOOL_CALL
    } == {
        "msg_1#0": (event_pb2.ITEM_KIND_REASONING, "SEED_THOUGHT"),
        "msg_1#1": (event_pb2.ITEM_KIND_ASSISTANT_TEXT, "SEED_TEXT"),
        "msg_2#0": (event_pb2.ITEM_KIND_REASONING, "THOUGHT_BEFORE_TOOL"),
        "msg_3#0": (event_pb2.ITEM_KIND_REASONING, "THOUGHT_AFTER_TOOL"),
        "msg_3#1": (event_pb2.ITEM_KIND_ASSISTANT_TEXT, "DONE"),
    }


@pytest.mark.parametrize(
    "after_thinking",
    [
        pytest.param([assistant_message("msg", tool_use_block("call"))], id="unresolved-tool-call"),
        pytest.param(
            [assistant_message("msg", redacted_thinking_block()), assistant_message("msg", tool_use_block("call"))],
            id="redacted-thinking-and-unresolved-tool-call",
        ),
        pytest.param([], id="nothing"),
    ],
)
def test_claude_resume_drops_thinking_left_without_an_answer(
    tmp_path: Path, after_thinking: list[dict[str, Any]]
) -> None:
    """The pinned binary does not replay such thinking after a kill mid-tool or an interrupt before
    any text (`harness_tests/claude/test_turns.py`), and a redacted block counts as thinking."""
    write_transcript(
        tmp_path,
        "session",
        user_message("seed"),
        assistant_message("seed", text_block("SEED_TEXT")),
        user_message("input"),
        assistant_message("msg", thinking_block("ORPHANED_THOUGHT")),
        *after_thinking,
    )
    assert set(claude_history.read_history(tmp_path, "session")) == {"seed#0"}


async def test_recovery_turn_boundaries_survive_reopen_and_page_events(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    async with Journal.open(path, "source") as journal:
        for turn, status in [
            ("old", event_pb2.TURN_STATUS_COMPLETED),
            ("last", event_pb2.TURN_STATUS_COMPLETED),
            ("lost", event_pb2.TURN_STATUS_PROCESS_LOST),
        ]:
            await journal.append(event_pb2.TurnStarted(turn_id=turn), sources=[])
            await journal.append(
                event_pb2.ItemStarted(item_id=turn, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT), sources=[]
            )
            for _ in range(130):
                await journal.append(event_pb2.TextDelta(item_id=turn, text="x"), sources=[])
            await journal.append(event_pb2.TurnCompleted(turn_id=turn, status=status), sources=[])
    async with Journal.open(path, "source") as journal:
        assert await journal.recovery_turns() == ["last", "lost"]
        items = await observed_items(journal, "lost")
        assert set(items) == {"lost"}
        assert items["lost"].text == "x" * 130
        assert not items["lost"].completed


if __name__ == "__main__":
    pytest_bazel.main()
