"""Frame shapes copied from staging Claude Code 2.1.x and Codex app-server deltas (2026-10-10)."""

import pytest
import pytest_bazel

from agentplane.native.delta_templates import ClaudeDelta, CodexDelta, DeltaTemplate, match_delta, parse_strict


def claude(delta: dict[str, object], **overrides: object) -> dict[str, object]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 2, "delta": delta},
        "session_id": "test-session",
        "parent_tool_use_id": None,
        "uuid": "test-uuid",
        **overrides,
    }


def codex(method: str, **params: object) -> dict[str, object]:
    return {
        "method": method,
        "params": {"threadId": "test-thread", "turnId": "test-turn", "itemId": "test-item", "delta": "chunk", **params},
        "emittedAtMs": 1760000000000,
    }


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (
            claude({"type": "text_delta", "text": "chunk"}),
            ClaudeDelta(DeltaTemplate.CLAUDE_TEXT, "test-session", None, 2, "chunk"),
        ),
        (
            claude({"type": "thinking_delta", "thinking": "chunk", "estimated_tokens": 3}),
            ClaudeDelta(DeltaTemplate.CLAUDE_THINKING, "test-session", None, 2, "chunk"),
        ),
        (
            claude({"type": "input_json_delta", "partial_json": '{"a"'}, parent_tool_use_id="toolu_parent"),
            ClaudeDelta(DeltaTemplate.CLAUDE_INPUT_JSON, "test-session", "toolu_parent", 2, '{"a"'),
        ),
        (
            codex("item/agentMessage/delta"),
            CodexDelta(DeltaTemplate.CODEX_AGENT_MESSAGE, "test-thread", "test-turn", "test-item", "chunk", None),
        ),
        (
            codex("item/reasoning/summaryTextDelta", summaryIndex=1),
            CodexDelta(DeltaTemplate.CODEX_REASONING_SUMMARY, "test-thread", "test-turn", "test-item", "chunk", 1),
        ),
        (
            codex("item/commandExecution/outputDelta"),
            CodexDelta(DeltaTemplate.CODEX_COMMAND_OUTPUT, "test-thread", "test-turn", "test-item", "chunk", None),
        ),
    ],
)
def test_known_shapes_match(frame: dict[str, object], expected: ClaudeDelta | CodexDelta) -> None:
    assert match_delta(frame) == expected


@pytest.mark.parametrize(
    "frame",
    [
        claude({"type": "text_delta", "text": "chunk"}, extra="a key a newer harness adds"),
        claude({"type": "text_delta", "text": "chunk", "citations": []}),
        claude({"type": "thinking_delta", "thinking": "chunk", "estimated_tokens": True}),
        claude({"type": "signature_delta", "signature": "sig"}),
        claude({"type": "text_delta", "text": 7}),
        {
            **claude({"type": "text_delta", "text": "chunk"}),
            "event": {"type": "content_block_delta", "index": True, "delta": {"type": "text_delta", "text": "chunk"}},
        },
        codex("item/agentMessage/delta", summaryIndex=0),
        codex("item/reasoning/summaryTextDelta", summaryIndex=-1),
        codex("item/reasoning/summaryTextDelta"),
        codex("item/plan/delta"),
        {**codex("item/agentMessage/delta"), "emittedAtMs": "1760000000000"},
    ],
)
def test_other_shapes_do_not_match(frame: dict[str, object]) -> None:
    assert match_delta(frame) is None


@pytest.mark.parametrize("line", ['{"a": 1, "a": 2}', '{"a": {"b": 1, "b": 1}}', "[1]", "not json"])
def test_parse_strict_refuses_lossy_frames(line: str) -> None:
    assert parse_strict(line) is None


if __name__ == "__main__":
    pytest_bazel.main()
