"""Native request identity and source-counter semantics."""

import json
from pathlib import Path

import pytest_bazel

from x.ai_sub_value.native_accounting import aggregate, counters, database, scan


def write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_native_accounting_deduplicates_forks_and_direct_snapshots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = {
        "type": "assistant",
        "timestamp": "2026-10-30T12:02:00Z",
        "message": {
            "id": "msg-1",
            "model": "claude-sonnet",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 2,
                "output_tokens": 3,
            },
        },
    }
    write(home / ".claude/projects/a/one.jsonl", [claude])
    write(home / ".claude/projects/a/subagents/two.jsonl", [claude])
    write(
        home / ".codex/sessions/one.jsonl",
        [
            {
                "type": "turn_context",
                "timestamp": "2026-10-30T12:03:00Z",
                "payload": {"turn_id": "turn-1", "model": "gpt-6-sol"},
            },
            {
                "type": "token_usage_record",
                "timestamp": "2026-10-30T12:04:00Z",
                "payload": {
                    "turn_id": "turn-1",
                    "response_id": "resp-1",
                    "usage": {
                        "input_tokens": 20,
                        "cached_input_tokens": 8,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 2,
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-10-30T12:04:00Z",
                "payload": {
                    "type": "token_count",
                    "turn_id": "turn-1",
                    "info": {
                        "last_token_usage": {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 4},
                        "total_token_usage": {"input_tokens": 20},
                    },
                },
            },
        ],
    )
    write(
        home / ".codex/sessions/legacy.jsonl",
        [
            {"type": "turn_context", "timestamp": "2026-10-30T12:05:00Z", "payload": {"model": "gpt-5.5"}},
            *[
                {
                    "type": "event_msg",
                    "timestamp": f"2026-10-30T12:05:0{index}Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 20,
                                "cached_input_tokens": 8,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 4,
                                "reasoning_output_tokens": 2,
                                "total_tokens": 24,
                            },
                            "total_token_usage": {
                                "input_tokens": total_input,
                                "cached_input_tokens": total_input * 2 // 5,
                                "cache_write_input_tokens": 0,
                                "output_tokens": total_input // 5,
                                "reasoning_output_tokens": total_input // 10,
                                "total_tokens": total_input * 6 // 5,
                            },
                        },
                    },
                }
                for index, total_input in enumerate((20, 20, 40, 10))
            ],
        ],
    )
    with database(tmp_path / "ledger.sqlite") as db:
        audit, _ = scan("host", home, db)
        aggregate(db, tmp_path)
        assert (
            db.execute("SELECT COUNT(*) FROM requests WHERE source != 'codex_last_response_candidate'").fetchone()[0]
            == 3
        )
        assert db.execute('SELECT uncached, read, write FROM requests WHERE provider="anthropic"').fetchone() == (
            10,
            5,
            2,
        )
        assert db.execute(
            'SELECT uncached, read, output, reasoning, model FROM requests WHERE source="codex_response"'
        ).fetchone() == (12, 8, 4, 2, "gpt-6-sol")
    assert audit["duplicate_identity"] == 1
    assert audit["last_response_paired_direct"] == 1
    assert audit["codex_cumulative_snapshots"] == 5
    assert audit["last_response_repeated_cumulative"] == 1
    assert audit["last_response_cumulative_reset"] == 1
    assert sum(row["requests"] for row in json.loads((tmp_path / "native-usage-5min.json").read_text())) == 3
    candidates = json.loads((tmp_path / "native-last-response-candidates-5min.json").read_text())
    assert sum(row["requests"] for row in candidates) == 3
    assert {row["quality"] for row in candidates} == {"unvalidated_snapshot_candidate"}


def test_openai_cached_input_is_subset_and_invalid_counts_do_not_enter_ledger() -> None:
    assert counters({"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}, "openai") == (
        6,
        4,
        None,
        3,
        None,
        None,
    )
    assert counters({"input_tokens": 3, "cached_input_tokens": 4}, "openai") is None


if __name__ == "__main__":
    pytest_bazel.main()
