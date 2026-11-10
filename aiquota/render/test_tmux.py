from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from aiquota.models import AllQuotas, FetchError, FetchSuccess, PaidCredits, ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render.tmux import _AI_GLYPH, render, render_provider, render_view
from aiquota.render.view_model import to_view
from aiquota.testing.quota_fixtures import load_quota_fixture

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _provider(provider: str, result: FetchSuccess | FetchError, fetched_at: datetime = _NOW) -> ProviderQuota:
    return ProviderQuota(provider=provider, last_output=ProviderFetch(fetched_at=fetched_at, result=result))


def test_render_provider_uses_shared_summary_window() -> None:
    quotas = AllQuotas(
        providers=[
            _provider(
                "codex",
                FetchSuccess(
                    windows=[
                        QuotaWindow(used_percent=27, reset_seconds=3600, window_seconds=5 * 3600),
                        QuotaWindow(used_percent=92, reset_seconds=3600, window_seconds=7 * 86400),
                    ]
                ),
            )
        ],
        fetched_at=_NOW,
    )
    view = to_view(quotas, now=_NOW)
    provider = view.providers[0].model_copy(update={"summary_index": 0})

    rendered = render_view([provider])

    assert "O:27%" in rendered
    assert "92%" not in rendered


def test_paid_marker_is_hidden_when_reset_is_pending() -> None:
    quotas = AllQuotas(
        providers=[
            _provider(
                "codex",
                FetchSuccess(
                    windows=[
                        QuotaWindow(
                            used_percent=100,
                            reset_seconds=0,
                            window_seconds=5 * 3600,
                            reset_at=_NOW - timedelta(seconds=1),
                        )
                    ],
                    paid_credits=PaidCredits(balance="500"),
                ),
            )
        ],
        fetched_at=_NOW,
    )

    rendered = render_view(to_view(quotas, now=_NOW).providers)

    assert "O:↻?" in rendered
    assert "⚡" not in rendered


def test_paid_capacity_and_stale_markers_are_compact() -> None:
    pq = _provider(
        "claude",
        FetchSuccess(
            windows=[QuotaWindow(used_percent=100, reset_seconds=1800, window_seconds=5 * 3600)],
            paid_credits=PaidCredits(balance="500"),
        ),
        fetched_at=_NOW - timedelta(minutes=15),
    )

    rendered = render_view(to_view(AllQuotas(providers=[pq], fetched_at=pq.last_output.fetched_at), now=_NOW).providers)

    assert "A:↻15m*" in rendered
    assert "⚡" not in rendered
    assert "yellow" in rendered


def test_exhausted_included_quota_shows_only_compact_reset_countdown() -> None:
    pq = _provider(
        "claude",
        FetchSuccess(
            windows=[
                QuotaWindow(used_percent=100, reset_seconds=1800, window_seconds=5 * 3600),
                QuotaWindow(used_percent=100, reset_seconds=600, window_seconds=7 * 86400),
            ]
        ),
    )

    rendered = render_view(to_view(AllQuotas(providers=[pq], fetched_at=_NOW), now=_NOW).providers)

    assert "A:↻30m" in rendered
    assert "100%" not in rendered


def test_error_only_and_api_only_providers_have_no_quota_table_rows() -> None:
    providers = [
        _provider("codex", FetchError(error="no auth")),
        _provider("openai_platform", FetchSuccess()),
        _provider("anthropic_platform", FetchSuccess()),
    ]

    rendered = render_view(to_view(AllQuotas(providers=providers, fetched_at=_NOW), now=_NOW).providers)

    assert "O:!" in rendered
    assert "openai_platform" not in rendered
    assert "anthropic_platform" not in rendered
    assert "OpenAI" not in rendered
    assert "Anthropic" not in rendered


def test_render_multiple_and_empty() -> None:
    providers = [
        _provider(
            "claude", FetchSuccess(windows=[QuotaWindow(used_percent=50, reset_seconds=9000, window_seconds=18000)])
        ),
        _provider(
            "codex", FetchSuccess(windows=[QuotaWindow(used_percent=90, reset_seconds=9000, window_seconds=18000)])
        ),
    ]

    rendered = render_view(to_view(AllQuotas(providers=providers, fetched_at=_NOW), now=_NOW).providers)

    assert rendered.startswith(f"{_AI_GLYPH} ")
    assert rendered.count(_AI_GLYPH) == 1
    assert "A:50%" in rendered
    assert "O:90%" in rendered
    assert render([]) == ""
    assert render([_provider("openai_platform", FetchSuccess())]) == ""


def test_render_provider_error_only() -> None:
    rendered = render_provider(_provider("codex", FetchError(error="no auth")))

    assert "O:!" in rendered
    assert "red" in rendered


@pytest.mark.parametrize("fixture_name", ["multi_window_reset_pending", "stale_paid_signals"])
def test_uncertain_paid_signals_keep_pending_or_stale_markers(runfiles: Runfiles, fixture_name: str) -> None:
    quotas = load_quota_fixture(runfiles.root() / f"ducktape/aiquota/testing/fixtures/{fixture_name}.yaml")
    view = to_view(quotas, quotas.fetched_at)
    rendered = render_view(view.providers)
    assert "⚡" not in rendered
    for prefix in ("A:", "O:"):
        assert prefix in rendered
    if fixture_name == "multi_window_reset_pending":
        assert all(provider.summary_index == 1 for provider in view.providers)
        assert "A:↻?" in rendered
        assert "O:↻?" in rendered
    else:
        assert "A:↻1h45m*" in rendered
        assert "O:↻1h45m*" in rendered
