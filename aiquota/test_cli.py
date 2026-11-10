from datetime import UTC, datetime

import pytest
import pytest_bazel
from typer.testing import CliRunner

from aiquota import cli
from aiquota.config import Config
from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render.view_model import AllQuotasView, to_view


def test_details_option_works_for_default_and_explicit_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    view = to_view(
        AllQuotas(
            fetched_at=now,
            providers=[
                ProviderQuota(
                    provider="codex",
                    last_output=ProviderFetch(
                        fetched_at=now,
                        result=FetchSuccess(
                            windows=[QuotaWindow(used_percent=60, reset_seconds=9000, window_seconds=18000)]
                        ),
                    ),
                )
            ],
        ),
        now,
    )

    class Service:
        async def fetch_view(self) -> AllQuotasView:
            return view

    monkeypatch.setattr(cli, "load_config", lambda _path: Config())
    monkeypatch.setattr(cli, "QuotaService", lambda **_kwargs: Service())
    runner = CliRunner()
    default = runner.invoke(cli.app, [])
    assert default.exit_code == 0, default.output
    assert "pace" not in default.output
    for args in (["--details"], ["--details", "fetch"], ["fetch", "--details"]):
        detailed = runner.invoke(cli.app, args)
        assert detailed.exit_code == 0, detailed.output
        assert "pace +10 pp" in detailed.output


if __name__ == "__main__":
    pytest_bazel.main()
