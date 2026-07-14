"""Tests for statusline models, quota formatting, and output rendering."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel
from syrupy.assertion import SnapshotAssertion

from aiquota.models import ExtraSpend, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow
from devinfra.claude.claude_api.credentials import read_credentials
from devinfra.claude.claude_api.statusline import ContextWindow, Input
from devinfra.claude.statusline.statusline import (
    QuotaRoute,
    _detect_quota_route,
    _format_context,
    _format_quota,
    render,
)

_SHORT_WINDOW_SECS = 5 * 3600
_LONG_WINDOW_SECS = 7 * 86400
_LITELLM_ZAI_ROUTE = QuotaRoute(provider="zai", label="litellm→zai")
_UNKNOWN_LITELLM_ROUTE = QuotaRoute(provider=None, label="litellm→?")


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


def _make_quota(
    *,
    short: QuotaWindow | None = None,
    long: QuotaWindow | None = None,
    extra: ExtraSpend | None = None,
    fetched_at: datetime | None = None,
    provider: str = "claude",
) -> ProviderQuota:
    return ProviderQuota(
        provider=provider,
        last_output=ProviderFetch(
            fetched_at=fetched_at or datetime.now(UTC),
            result=FetchSuccess(windows=[window for window in (short, long) if window], extra_spend=extra),
        ),
    )


def test_format_quota_none():
    assert _format_quota(None, now=datetime.now(UTC)) is None


def test_format_quota_empty():
    assert _format_quota(_make_quota(), now=datetime.now(UTC)) is None


@pytest.mark.parametrize(
    ("short_util", "long_util", "expected"),
    [
        pytest.param(80.0, 35.0, "5h:80% 7d:35%", id="both_buckets"),
        pytest.param(6.0, 35.0, "5h:6% 7d:35%", id="both_buckets_low_short"),
        pytest.param(85.0, None, "5h:85%", id="five_hour_only_high"),
    ],
)
def test_format_quota_buckets(short_util: float, long_util: float | None, expected: str):
    now = datetime.now(UTC)
    short = QuotaWindow(used_percent=short_util, reset_seconds=0, window_seconds=_SHORT_WINDOW_SECS)
    long = (
        QuotaWindow(used_percent=long_util, reset_seconds=0, window_seconds=_LONG_WINDOW_SECS)
        if long_util is not None
        else None
    )
    result = _format_quota(_make_quota(short=short, long=long, fetched_at=now), now=now)
    assert result is not None
    assert result.plain == expected


def test_format_quota_uses_provider_window_name_and_duration():
    now = datetime.now(UTC)
    window = QuotaWindow(name="Monthly", used_percent=35, reset_seconds=0, window_seconds=30 * 86400)
    result = _format_quota(_make_quota(long=window, fetched_at=now), now=now)
    assert result is not None
    assert result.plain == "Monthly (30d):35%"


def test_format_quota_single_five_hour_window():
    now = datetime.now(UTC)
    short = QuotaWindow(used_percent=12.5, reset_seconds=0, window_seconds=_SHORT_WINDOW_SECS)
    result = _format_quota(_make_quota(short=short, fetched_at=now), now=now)
    assert result is not None
    assert result.plain == "5h:12%"


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
    long = QuotaWindow(used_percent=20.0, reset_seconds=0, window_seconds=_LONG_WINDOW_SECS)
    result = _format_quota(_make_quota(long=long, fetched_at=now - age), now=now)
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
    long = QuotaWindow(
        used_percent=35.0, reset_seconds=max(0, resets_in.total_seconds()), window_seconds=_LONG_WINDOW_SECS
    )
    result = _format_quota(_make_quota(long=long, fetched_at=now), now=now)
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
    long = QuotaWindow(
        used_percent=utilization, reset_seconds=resets_in.total_seconds(), window_seconds=_LONG_WINDOW_SECS
    )
    result = _format_quota(_make_quota(long=long, fetched_at=now), now=now)
    assert result is not None
    if expected_dry is None:
        assert "dry" not in result.plain
    else:
        assert result.plain.endswith(expected_dry)


def test_format_quota_spend_extra_usage():
    now = datetime.now(UTC)
    long = QuotaWindow(used_percent=100.0, reset_seconds=0, window_seconds=_LONG_WINDOW_SECS)
    extra = ExtraSpend(is_enabled=True, monthly_limit_usd=2500.0, used_usd=123.45, utilization=4.94)
    result = _format_quota(_make_quota(long=long, extra=extra, fetched_at=now), now=now)
    assert result is not None
    assert "7d:100%" in result.plain
    assert "extra $123/$2500 (5%)" in result.plain


def test_format_quota_no_extra_spend():
    now = datetime.now(UTC)
    long = QuotaWindow(used_percent=30.0, reset_seconds=0, window_seconds=_LONG_WINDOW_SECS)
    result = _format_quota(_make_quota(long=long, fetched_at=now), now=now)
    assert result is not None
    assert "extra" not in result.plain


def test_format_quota_displays_provider_visible_windows():
    now = datetime.now(UTC)
    short = QuotaWindow(used_percent=0.0, reset_seconds=0, window_seconds=_SHORT_WINDOW_SECS)
    long = QuotaWindow(used_percent=100.0, reset_seconds=0, window_seconds=_LONG_WINDOW_SECS)
    result = _format_quota(_make_quota(short=short, long=long, fetched_at=now), now=now)
    assert result is not None
    assert result.plain == "5h:0% 7d:100%"


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


@pytest.mark.parametrize(
    ("base_url", "model_id", "expected"),
    [
        pytest.param(
            "https://api.z.ai/api/anthropic", "glm-5.2", QuotaRoute(provider="zai", label="zai"), id="direct_zai"
        ),
        pytest.param(
            "https://litellm.allegedly.works", "glm-5.2-anthropic", _LITELLM_ZAI_ROUTE, id="litellm_glm_fallback"
        ),
        pytest.param(
            "https://litellm.allegedly.works", "some-model-alias", _UNKNOWN_LITELLM_ROUTE, id="ambiguous_litellm"
        ),
        pytest.param(
            "https://unknown-proxy.example",
            "claude-opus-4-6",
            QuotaRoute(provider=None, label="proxy→?"),
            id="ambiguous_proxy",
        ),
        pytest.param(
            "https://api.anthropic.com", "claude-opus-4-6", QuotaRoute(provider="claude"), id="default_anthropic"
        ),
        pytest.param("", "claude-opus-4-6", QuotaRoute(provider="claude"), id="unset"),
    ],
)
def test_detect_quota_route(base_url: str, model_id: str, expected: QuotaRoute):
    assert _detect_quota_route(base_url=base_url, model_id=model_id) == expected


# Fixed "now" for deterministic quota formatting
_NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def full_input() -> Input:
    return Input.model_validate_json(FULL_INPUT_JSON)


def _render_quota(
    *,
    seven_day_util: float = 30.0,
    seven_day_resets_in: timedelta = timedelta(days=4, hours=2),
    five_hour_util: float | None = None,
) -> ProviderQuota:
    long = QuotaWindow(
        used_percent=seven_day_util, reset_seconds=seven_day_resets_in.total_seconds(), window_seconds=_LONG_WINDOW_SECS
    )
    short = (
        QuotaWindow(used_percent=five_hour_util, reset_seconds=0, window_seconds=_SHORT_WINDOW_SECS)
        if five_hour_util is not None
        else None
    )
    return _make_quota(short=short, long=long, fetched_at=_NOW)


def test_render_subscription(full_input: Input, snapshot: SnapshotAssertion):
    """Subscription user: no cost, with quota, daemon healthy."""
    result = render(
        full_input, is_subscription=True, quota=_render_quota(), home=Path("/home/user"), now=_NOW, daemon_healthy=True
    )
    assert result == snapshot


def test_render_api_billing(full_input: Input, snapshot: SnapshotAssertion):
    """API billing user: shows dollar cost, no quota, daemon down."""
    result = render(
        full_input, is_subscription=False, quota=None, home=Path("/home/user"), now=_NOW, daemon_healthy=False
    )
    assert result == snapshot


def test_render_subscription_high_usage(full_input: Input, snapshot: SnapshotAssertion):
    """Subscription user burning fast — shows dry warning."""
    result = render(
        full_input,
        is_subscription=True,
        quota=_render_quota(seven_day_util=80.0, seven_day_resets_in=timedelta(days=2), five_hour_util=85.0),
        home=Path("/home/user"),
        now=_NOW,
        daemon_healthy=True,
    )
    assert result == snapshot


def test_render_minimal(snapshot: SnapshotAssertion):
    """Minimal input — no model, no cost, no context."""
    data = Input.model_validate_json("{}")
    result = render(data, is_subscription=False, quota=None, home=None, now=_NOW, daemon_healthy=False)
    assert result == snapshot


@pytest.mark.parametrize(
    ("quota_route", "include_quota", "quota_error", "expected_parts", "unexpected"),
    [
        pytest.param(_LITELLM_ZAI_ROUTE, True, None, ("litellm→zai", "5h:24%", "7d:61%"), None, id="known_with_quota"),
        pytest.param(
            _LITELLM_ZAI_ROUTE, False, None, ("litellm→zai quota unavailable",), None, id="known_without_quota"
        ),
        pytest.param(
            _UNKNOWN_LITELLM_ROUTE,
            True,
            None,
            ("litellm→? quota unknown",),
            "7d:61%",
            id="unknown_suppresses_mismatched_quota",
        ),
        pytest.param(
            _LITELLM_ZAI_ROUTE,
            False,
            "aiquota config error",
            ("litellm→zai aiquota config error",),
            "quota unavailable",
            id="config_error",
        ),
    ],
)
def test_render_quota_route_status(
    full_input: Input,
    quota_route: QuotaRoute,
    include_quota: bool,
    quota_error: str | None,
    expected_parts: tuple[str, ...],
    unexpected: str | None,
):
    result = render(
        full_input,
        is_subscription=True,
        quota=_render_quota(seven_day_util=61.0, five_hour_util=24.0) if include_quota else None,
        home=Path("/home/user"),
        now=_NOW,
        daemon_healthy=True,
        quota_route=quota_route,
        quota_error=quota_error,
    )
    assert all(part in result for part in expected_parts)
    if unexpected is not None:
        assert unexpected not in result


if __name__ == "__main__":
    pytest_bazel.main()
