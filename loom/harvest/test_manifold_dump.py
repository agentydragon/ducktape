"""Hermetic tests for the dump readers, against fixture zips built from real record shapes."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_bazel

from loom.harvest.manifold_dump import (
    BetRecord,
    iter_bets,
    iter_comments,
    iter_contracts,
    iter_json_array,
    ms_to_datetime,
    prob_at,
    tiptap_plain_text,
)

# Field shapes mirror real records from the 2024-07-06 dump (truncated to the relevant fields).
RESOLVED_CONTRACT = {
    "id": "a3k1RgxAVYiAU1qeA5Su",
    "question": "January 2024: Will Bitcoin hit $50,000?",
    "outcomeType": "BINARY",
    "createdTime": 1704225194859,
    "resolution": "NO",
    "resolutionTime": 1706860865370,
    "uniqueBettorCount": 277,
    "isResolved": True,  # extra field: must be ignored
    "description": {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "If in January 2024 at least one 30min "},
                    {"type": "text", "text": "Coingecko", "marks": [{"type": "link", "attrs": {"href": "x"}}]},
                    {"type": "text", "text": " candle hits $50,000, this resolves YES."},
                ],
            }
        ],
    },
}
OPEN_CONTRACT = {
    "id": "will-bitcoin-be-worth-more",
    "question": "Will Bitcoin be worth more than $60,000?",
    "outcomeType": "BINARY",
    "createdTime": 1639779118231,
    "uniqueBettorCount": 37,
    "description": "Plain old string description",
}
TEXT_COMMENT = {
    "contractId": "a3k1RgxAVYiAU1qeA5Su",
    "createdTime": 1641280475468,
    "userName": "Austin",
    "text": "What a #meta question!",
    "betAmount": 10,  # extra field: must be ignored
}
TIPTAP_COMMENT = {
    "contractId": "a3k1RgxAVYiAU1qeA5Su",
    "createdTime": 1704630000000,
    "userName": "uair01",
    "content": {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Interesting critique,"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "feel free to disagree."}]},
        ],
    },
}


def _bet(created_time: int, prob_after: float) -> dict[str, object]:
    return {
        "contractId": "a3k1RgxAVYiAU1qeA5Su",
        "createdTime": created_time,
        "probAfter": prob_after,
        "probBefore": 0.5,  # extra field: must be ignored
        "amount": 100,
    }


def _fixture_zip(tmp_path: Path, member: str, records: list[dict[str, object]]) -> Path:
    zip_path = tmp_path / f"{member}.zip"
    with zipfile.ZipFile(zip_path, "w") as fixture:
        fixture.writestr(member, json.dumps(records, indent=2))
    return zip_path


def test_iter_json_array_streams_across_chunk_boundaries() -> None:
    records = [{"name": f"record-{n}", "value": n} for n in range(50)]
    # chunk_size far smaller than one record forces every refill path.
    assert list(iter_json_array(io.StringIO(json.dumps(records)), chunk_size=7)) == records


def test_iter_json_array_empty_and_whitespace() -> None:
    assert list(iter_json_array(io.StringIO("  [ ]  "), chunk_size=3)) == []


def test_iter_json_array_rejects_non_array() -> None:
    with pytest.raises(ValueError, match="not a JSON array"):
        list(iter_json_array(io.StringIO('{"a": 1}')))


def test_iter_json_array_rejects_truncation() -> None:
    with pytest.raises(ValueError, match="truncated"):
        list(iter_json_array(io.StringIO('[{"a": 1}, {"b": 2}'), chunk_size=5))
    with pytest.raises(json.JSONDecodeError):
        list(iter_json_array(io.StringIO('[{"a": 1}, {"b":'), chunk_size=5))


def test_iter_contracts_parses_resolved_and_open(tmp_path: Path) -> None:
    zip_path = _fixture_zip(tmp_path, "contracts.json", [RESOLVED_CONTRACT, OPEN_CONTRACT])
    resolved, opened = iter_contracts(zip_path)
    assert resolved.id == "a3k1RgxAVYiAU1qeA5Su"
    assert (resolved.outcome_type, resolved.resolution, resolved.unique_bettor_count) == ("BINARY", "NO", 277)
    assert ms_to_datetime(resolved.created_time) == datetime(2024, 1, 2, 19, 53, 14, 859000, tzinfo=UTC)
    assert resolved.resolution_time is not None
    assert ms_to_datetime(resolved.resolution_time).date() == datetime(2024, 2, 2, tzinfo=UTC).date()
    assert "Coingecko candle hits $50,000" in resolved.description_text()
    assert opened.resolution is None
    assert opened.resolution_time is None
    assert opened.description_text() == "Plain old string description"


def test_iter_comments_handles_text_and_tiptap_bodies(tmp_path: Path) -> None:
    zip_path = _fixture_zip(tmp_path, "comments.json", [TEXT_COMMENT, TIPTAP_COMMENT])
    plain, tiptap = iter_comments(zip_path)
    assert (plain.user_name, plain.body_text()) == ("Austin", "What a #meta question!")
    assert tiptap.user_name == "uair01"
    assert tiptap.body_text() == "Interesting critique,\nfeel free to disagree.\n"


def test_tiptap_plain_text_of_non_document() -> None:
    assert tiptap_plain_text(None) == ""


def test_iter_bets_and_prob_at(tmp_path: Path) -> None:
    zip_path = _fixture_zip(tmp_path, "bets.json", [_bet(1000, 0.3), _bet(3000, 0.7), _bet(2000, 0.5)])
    bets = list(iter_bets(zip_path))
    assert [bet.prob_after for bet in bets] == [0.3, 0.7, 0.5]
    assert all(bet.contract_id == "a3k1RgxAVYiAU1qeA5Su" for bet in bets)

    def at(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)

    # Last bet at/before the cutoff wins regardless of input order; an exact-time bet counts.
    assert prob_at(bets, at(2500)) == 0.5
    assert prob_at(bets, at(3000)) == 0.7
    assert prob_at(bets, at(999)) is None


def test_prob_at_empty() -> None:
    assert prob_at([], datetime(2024, 1, 15, tzinfo=UTC)) is None


def test_bet_record_round_trip() -> None:
    bet = BetRecord.model_validate({"contractId": "abc", "createdTime": 1000, "probAfter": 0.42})
    assert (bet.contract_id, bet.created_time, bet.prob_after) == ("abc", 1000, 0.42)


if __name__ == "__main__":
    pytest_bazel.main()
