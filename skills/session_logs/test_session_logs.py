import json

import pytest
import pytest_bazel

from skills.session_logs import session_logs


def test_iter_entries_accepts_unescaped_controls_and_skips_truncated_records(tmp_path) -> None:
    transcript = tmp_path / "broken.jsonl"
    valid_control = '{"type":"session_meta","payload":{"value":"before\x00after"}}'
    truncated = '{"type":"response_item","payload":{"type":"message","role":"user"'
    valid_after = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "still visible"}],
            },
        }
    )
    transcript.write_text(f"{valid_control}\n{truncated}\n{valid_after}\n")

    stats = session_logs.ParseStats()
    entries = list(session_logs.iter_entries(transcript, stats))

    assert len(entries) == 2
    assert entries[0]["payload"]["value"] == "before\x00after"
    assert session_logs._codex_user_text(entries[1]) == "still visible"
    assert stats.malformed_records == 1
    assert stats.issues[0].line_number == 2


def test_display_text_preserves_both_ends() -> None:
    rendered = session_logs.display_text("abcdefghij", 100)
    assert rendered == "abcdefghij"

    rendered = session_logs.display_text("a" * 120, 100)
    assert rendered.startswith("a")
    assert rendered.endswith("a")
    assert len(rendered) == 100
    assert "--max-display-text-length" in rendered


def test_codex_harness_detection_uses_session_meta(tmp_path) -> None:
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "test"}}) + "\n")

    assert session_logs.detect_harness(transcript) == "codex"


def test_codex_tool_calls_are_counted_outside_assistant_messages(tmp_path) -> None:
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"session_id": "test"}}),
                json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "tool"}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    summary, _ = session_logs.analyze_transcript(transcript, "codex")

    assert summary["tool_uses"] == 1


def _assert_compaction_marker_count(tmp_path, harness, marker, near_miss) -> None:
    transcript = tmp_path / f"{harness}.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in (marker, near_miss)) + "\n")
    summary, stats = session_logs.analyze_transcript(transcript, harness)
    assert summary["compactions"] == 1
    assert stats.malformed_records == 0


def test_claude_compaction_markers_are_counted(tmp_path) -> None:
    _assert_compaction_marker_count(
        tmp_path, "claude", {"type": "system", "subtype": "compact_boundary"}, {"type": "system", "subtype": "other"}
    )


def test_codex_compaction_markers_are_counted(tmp_path) -> None:
    _assert_compaction_marker_count(
        tmp_path,
        "codex",
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
        {"type": "event_msg", "payload": {"type": "user_message"}},
    )


@pytest.mark.parametrize(
    ("compactions", "malformed_records", "expected"),
    [
        (0, 0, session_logs.ReplayGuidance.SKIP),
        (2, 0, session_logs.ReplayGuidance.REPLAY),
        (0, 1, session_logs.ReplayGuidance.UNCERTAIN),
        # A scan that skipped records cannot vouch for its compaction count either way.
        (2, 1, session_logs.ReplayGuidance.UNCERTAIN),
    ],
)
def test_followups_replay_guidance_requires_clean_scan_for_fast_path(
    compactions: int, malformed_records: int, expected: session_logs.ReplayGuidance
) -> None:
    assert session_logs.followups_replay_guidance(compactions, malformed_records) == expected


if __name__ == "__main__":
    pytest_bazel.main()
