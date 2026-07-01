"""Tests for statusline models, usage API client, and output formatting."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel
from syrupy.assertion import SnapshotAssertion

from devinfra.claude.claude_api.credentials import read_credentials
from devinfra.claude.claude_api.statusline import ContextWindow, Input
from devinfra.claude.claude_api.usage import UsageBucket, UsageResponse
from devinfra.claude.statusline.statusline import _format_context, _format_quota, render
from devinfra.claude.statusline.usage_cache import CACHE_TTL, CachedUsage, UsageCache

# === statusline_models tests ===


FULL_INPUT_JSON = json.dumps(
    {
        "cwd": "/home/user/code",
        "session_id": "abc12345xyz",
        "transcript_path": "/tmp/transcript.jsonl",
        "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6 (1M context)"},
        "workspace": {"current_dir": "/home/user/code/ducktape", "project_dir": "/home/user/code/ducktape"},
        "version": "1.0.80",
        "output_style": {"name": "default"},
        "cost": {
            "total_cost_usd": 2.41,
            "total_duration_ms": 1320000,
            "total_api_duration_ms": 2300,
            "total_lines_added": 156,
            "total_lines_removed": 23,
        },
        "context_window": {
            "total_input_tokens": 15234,
            "total_output_tokens": 4521,
            "context_window_size": 200000,
            "used_percentage": 7,
            "remaining_percentage": 93,
            "current_usage": {
                "input_tokens": 8500,
                "output_tokens": 1200,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 2000,
            },
        },
        "exceeds_200k_tokens": False,
        "vim": {"mode": "NORMAL"},
        "agent": {"name": "test-agent"},
        "worktree": {
            "name": "my-feature",
            "path": "/home/user/.claude/worktrees/my-feature",
            "branch": "worktree-my-feature",
            "original_cwd": "/home/user/code",
            "original_branch": "main",
        },
    }
)


def test_parse_full_input():
    data = Input.model_validate_json(FULL_INPUT_JSON)
    assert data.session_id == "abc12345xyz"
    assert data.model is not None
    assert data.model.display_name == "Opus 4.6 (1M context)"
    assert data.model.id == "claude-opus-4-6"
    assert data.workspace is not None
    assert data.workspace.current_dir == "/home/user/code/ducktape"
    assert data.cost is not None
    assert data.cost.total_cost_usd == 2.41
    assert data.context_window is not None
    assert data.context_window.used_percentage == 7
    assert data.context_window.current_usage is not None
    assert data.context_window.current_usage.input_tokens == 8500
    assert data.vim is not None
    assert data.vim.mode == "NORMAL"
    assert data.agent is not None
    assert data.agent.name == "test-agent"
    assert data.worktree is not None
    assert data.worktree.name == "my-feature"
    assert data.worktree.path == "/home/user/.claude/worktrees/my-feature"
    assert data.worktree.branch == "worktree-my-feature"
    assert data.worktree.original_cwd == "/home/user/code"
    assert data.worktree.original_branch == "main"


def test_parse_minimal_input():
    data = Input.model_validate_json("{}")
    assert data.session_id == ""
    assert data.model is None
    assert data.cost is None


def test_extra_fields_ignored():
    raw = json.dumps({"session_id": "abc", "some_future_field": True, "nested": {"x": 1}})
    data = Input.model_validate_json(raw)
    assert data.session_id == "abc"


def test_null_context_usage():
    raw = json.dumps({"context_window": {"used_percentage": None, "remaining_percentage": None, "current_usage": None}})
    data = Input.model_validate_json(raw)
    assert data.context_window is not None
    assert data.context_window.used_percentage is None
    assert data.context_window.current_usage is None


# === usage_cache tests ===


def test_usage_response_parsing():
    raw = {
        "five_hour": {"utilization": 6.0, "resets_at": "2026-02-25T05:00:00+00:00"},
        "seven_day": {"utilization": 35.0, "resets_at": "2026-02-28T04:00:00+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 3.0, "resets_at": "2026-03-01T00:00:00+00:00"},
    }
    resp = UsageResponse.model_validate(raw)
    assert resp.five_hour is not None
    assert resp.five_hour.utilization == 6.0
    assert resp.seven_day is not None
    assert resp.seven_day.utilization == 35.0
    assert resp.seven_day_opus is None
    assert resp.seven_day_sonnet is not None


def test_usage_response_extra_fields():
    raw = {"five_hour": {"utilization": 10.0}, "unknown_bucket": {"utilization": 99.0}}
    resp = UsageResponse.model_validate(raw)
    assert resp.five_hour is not None
    assert resp.five_hour.utilization == 10.0


def test_usage_response_null_resets_at():
    raw = {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 100.0, "resets_at": "2026-05-15T16:00:00+00:00"},
    }
    resp = UsageResponse.model_validate(raw)
    assert resp.five_hour is not None
    assert resp.five_hour.utilization == 0.0
    assert resp.five_hour.resets_at is None
    assert resp.seven_day is not None
    assert resp.seven_day.utilization == 100.0
    assert resp.seven_day.resets_at is not None


def test_read_credentials(tmp_path: Path):
    creds = {"claudeAiOauth": {"accessToken": "test-token-123", "subscriptionType": "max"}}
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps(creds))

    with patch("devinfra.claude.claude_api.credentials.CREDENTIALS_PATH", creds_file):
        oauth = read_credentials()
        assert oauth is not None
        assert oauth.access_token == "test-token-123"
        assert oauth.subscription_type == "max"


def test_read_credentials_missing_file(tmp_path: Path):
    with patch("devinfra.claude.claude_api.credentials.CREDENTIALS_PATH", tmp_path / "nonexistent"):
        assert read_credentials() is None


def test_read_credentials_malformed(tmp_path: Path):
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text("not json")

    with patch("devinfra.claude.claude_api.credentials.CREDENTIALS_PATH", creds_file):
        assert read_credentials() is None


def test_usage_cache_fresh(tmp_path: Path):
    cache_file = tmp_path / "usage_cache.json"
    usage = UsageResponse(five_hour=UsageBucket(utilization=12.0), seven_day=UsageBucket(utilization=45.0))
    cached = CachedUsage(fetched_at=datetime.now(UTC), usage=usage)
    cache_file.write_text(cached.model_dump_json())

    result = UsageCache(path=cache_file).get(access_token=None)

    assert result is not None
    assert result.usage.five_hour is not None
    assert result.usage.five_hour.utilization == 12.0


def test_usage_cache_stale_no_token(tmp_path: Path):
    cache_file = tmp_path / "usage_cache.json"
    usage = UsageResponse(five_hour=UsageBucket(utilization=99.0))
    stale_time = datetime.now(UTC) - CACHE_TTL - timedelta(seconds=10)
    cached = CachedUsage(fetched_at=stale_time, usage=usage)
    cache_file.write_text(cached.model_dump_json())

    result = UsageCache(path=cache_file).get(access_token=None)

    # Falls back to stale cache
    assert result is not None
    assert result.usage.five_hour is not None
    assert result.usage.five_hour.utilization == 99.0


def test_usage_cache_missing_no_token(tmp_path: Path):
    assert UsageCache(path=tmp_path / "nonexistent").get(access_token=None) is None


# === statusline output tests ===


def _make_cached(usage: UsageResponse, age: timedelta = timedelta(seconds=0)) -> CachedUsage:
    return CachedUsage(fetched_at=datetime.now(UTC) - age, usage=usage)


def test_format_quota_none():
    assert _format_quota(None, now=datetime.now(UTC)) is None


def test_format_quota_empty_response():
    cached = _make_cached(UsageResponse())
    assert _format_quota(cached, now=datetime.now(UTC)) is None


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            UsageResponse(five_hour=UsageBucket(utilization=80.0), seven_day=UsageBucket(utilization=35.0)),
            "5h:80% 7d:35%",
            id="both_buckets",
        ),
        pytest.param(
            UsageResponse(five_hour=UsageBucket(utilization=6.0), seven_day=UsageBucket(utilization=35.0)),
            "7d:35%",
            id="five_hour_below_70_hidden",
        ),
        pytest.param(UsageResponse(five_hour=UsageBucket(utilization=85.0)), "5h:85%", id="five_hour_only_high"),
    ],
)
def test_format_quota_buckets(usage: UsageResponse, expected: str):
    now = datetime.now(UTC)
    cached = CachedUsage(fetched_at=now, usage=usage)
    result = _format_quota(cached, now=now)
    assert result is not None
    assert result.plain == expected


def test_format_quota_five_hour_low_hidden():
    now = datetime.now(UTC)
    cached = CachedUsage(fetched_at=now, usage=UsageResponse(five_hour=UsageBucket(utilization=12.5)))
    assert _format_quota(cached, now=now) is None


@pytest.mark.parametrize(
    ("age", "expected_suffix"),
    [
        pytest.param(timedelta(seconds=5), None, id="fresh"),
        pytest.param(timedelta(seconds=42), "(42s ago)", id="seconds"),
        pytest.param(timedelta(seconds=150), "(2m ago)", id="minutes"),
        pytest.param(timedelta(hours=1, minutes=5), "(1h05m ago)", id="hours"),
    ],
)
def test_format_quota_staleness(age: timedelta, expected_suffix: str | None):
    now = datetime.now(UTC)
    cached = CachedUsage(fetched_at=now - age, usage=UsageResponse(seven_day=UsageBucket(utilization=20.0)))
    result = _format_quota(cached, now=now)
    assert result is not None
    if expected_suffix is None:
        assert result.plain == "7d:20%"
    else:
        assert result.plain == f"7d:20% {expected_suffix}"


@pytest.mark.parametrize(
    ("resets_in", "expected_part"),
    [
        pytest.param(timedelta(hours=2, minutes=13), "7d:35% rst 2h13m", id="hours"),
        pytest.param(timedelta(minutes=45), "7d:35% rst 45m", id="minutes"),
        pytest.param(timedelta(days=3, hours=5), "7d:35% rst 3d05h", id="days"),
        pytest.param(timedelta(minutes=-5), "7d:35%", id="past_no_reset"),
    ],
)
def test_format_quota_seven_day_reset(resets_in: timedelta, expected_part: str):
    now = datetime.now(UTC)
    resets_at = now + resets_in
    cached = CachedUsage(
        fetched_at=now, usage=UsageResponse(seven_day=UsageBucket(utilization=35.0, resets_at=resets_at))
    )
    result = _format_quota(cached, now=now)
    assert result is not None
    assert result.plain == expected_part


@pytest.mark.parametrize(
    ("utilization", "resets_in", "expected_dry"),
    [
        # 80% used with 2d remaining (elapsed=5d) → exhaust in (20/80)*5d = 1.25d → < 2d → show
        pytest.param(80.0, timedelta(days=2), "dry 1d06h", id="will_exhaust"),
        # 30% used with 4d remaining (elapsed=3d) → exhaust in (70/30)*3d = 7d → > 4d → no show
        pytest.param(30.0, timedelta(days=4), None, id="sustainable"),
        # 90% used with 6d remaining (elapsed=1d) → exhaust in (10/90)*1d = 2.7h → < 6d → show
        pytest.param(90.0, timedelta(days=6), "dry 2h40m", id="burning_fast"),
    ],
)
def test_format_quota_exhaustion_projection(utilization: float, resets_in: timedelta, expected_dry: str | None):
    now = datetime.now(UTC)
    resets_at = now + resets_in
    cached = CachedUsage(
        fetched_at=now, usage=UsageResponse(seven_day=UsageBucket(utilization=utilization, resets_at=resets_at))
    )
    result = _format_quota(cached, now=now)
    assert result is not None
    if expected_dry is None:
        assert "dry" not in result.plain
    else:
        assert result.plain.endswith(expected_dry)


def test_format_quota_spend_extra_usage():
    now = datetime.now(UTC)
    usage = UsageResponse.model_validate(
        {
            "seven_day": {"utilization": 100.0},
            "spend": {
                "enabled": True,
                "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
                "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
                "percent": 4.94,
            },
        }
    )
    cached = CachedUsage(fetched_at=now, usage=usage)
    result = _format_quota(cached, now=now)
    assert result is not None
    assert "7d:100%" in result.plain
    assert "extra $123/$2500 (5%)" in result.plain


def test_format_quota_disabled_spend_not_shown():
    now = datetime.now(UTC)
    usage = UsageResponse.model_validate(
        {"seven_day": {"utilization": 30.0}, "spend": {"enabled": False, "disabled_reason": "payment_method_required"}}
    )
    cached = CachedUsage(fetched_at=now, usage=usage)
    result = _format_quota(cached, now=now)
    assert result is not None
    assert "extra" not in result.plain


def test_format_quota_null_resets_at():
    now = datetime.now(UTC)
    usage = UsageResponse(
        five_hour=UsageBucket(utilization=0.0),
        seven_day=UsageBucket(utilization=100.0, resets_at=now + timedelta(days=1)),
    )
    cached = CachedUsage(fetched_at=now, usage=usage)
    result = _format_quota(cached, now=now)
    assert result is not None
    # 5h at 0% is below 70 threshold so hidden, but 7d at 100% shown
    assert "7d:100%" in result.plain
    assert "5h" not in result.plain


# === context window tests ===


def test_format_context_none():
    assert _format_context(None) is None


def test_format_context_no_percentage():
    ctx = ContextWindow(used_percentage=None)
    assert _format_context(ctx) is None


@pytest.mark.parametrize(
    ("pct", "expected_text", "expected_style"),
    [
        pytest.param(8, "ctx:8%", "green", id="low"),
        pytest.param(42, "ctx:42%", "green", id="mid_green"),
        pytest.param(59.9, "ctx:60%", "green", id="boundary_green"),
        pytest.param(60, "ctx:60%", "yellow", id="boundary_yellow"),
        pytest.param(75, "ctx:75%", "yellow", id="mid_yellow"),
        pytest.param(89.9, "ctx:90%", "yellow", id="boundary_yellow_high"),
        pytest.param(90, "ctx:90%", "bold red", id="boundary_red"),
        pytest.param(99, "ctx:99%", "bold red", id="high_red"),
    ],
)
def test_format_context_colors(pct: float, expected_text: str, expected_style: str):
    ctx = ContextWindow(used_percentage=pct)
    result = _format_context(ctx)
    assert result is not None
    assert result.plain == expected_text
    assert result.style == expected_style


# === render snapshot tests ===

# Fixed "now" for deterministic quota formatting
_NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def full_input() -> Input:
    return Input.model_validate_json(FULL_INPUT_JSON)


def _make_usage(
    *,
    seven_day_util: float = 30.0,
    seven_day_resets_in: timedelta = timedelta(days=4, hours=2),
    five_hour_util: float | None = None,
) -> CachedUsage:
    seven_day = UsageBucket(utilization=seven_day_util, resets_at=_NOW + seven_day_resets_in)
    five_hour = UsageBucket(utilization=five_hour_util) if five_hour_util is not None else None
    return CachedUsage(fetched_at=_NOW, usage=UsageResponse(five_hour=five_hour, seven_day=seven_day))


def test_render_subscription(full_input: Input, snapshot: SnapshotAssertion):
    """Subscription user: no cost, with quota, daemon healthy."""
    result = render(
        full_input,
        is_subscription=True,
        cached_usage=_make_usage(),
        home=Path("/home/user"),
        now=_NOW,
        daemon_healthy=True,
    )
    assert result == snapshot


def test_render_api_billing(full_input: Input, snapshot: SnapshotAssertion):
    """API billing user: shows dollar cost, no quota, daemon down."""
    result = render(
        full_input, is_subscription=False, cached_usage=None, home=Path("/home/user"), now=_NOW, daemon_healthy=False
    )
    assert result == snapshot


def test_render_subscription_high_usage(full_input: Input, snapshot: SnapshotAssertion):
    """Subscription user burning fast — shows dry warning."""
    result = render(
        full_input,
        is_subscription=True,
        cached_usage=_make_usage(seven_day_util=80.0, seven_day_resets_in=timedelta(days=2), five_hour_util=85.0),
        home=Path("/home/user"),
        now=_NOW,
        daemon_healthy=True,
    )
    assert result == snapshot


def test_render_minimal(snapshot: SnapshotAssertion):
    """Minimal input — no model, no cost, no context."""
    data = Input.model_validate_json("{}")
    result = render(data, is_subscription=False, cached_usage=None, home=None, now=_NOW, daemon_healthy=False)
    assert result == snapshot


if __name__ == "__main__":
    pytest_bazel.main()
