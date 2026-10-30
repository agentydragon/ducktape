"""cdk8s apps and charts for tests, with outdirs under pytest's temp dir.

These replace `cdk8s.Testing.app()`/`Testing.chart()` (banned in `ruff.toml`), which mkdtemp a
`cdk8s.outdir.*` in the system temp dir on every call and never remove it.
`cluster/cdk8s/conftest.py` exposes them.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from cdk8s import App, Chart


@pytest.fixture
def scratch_app(tmp_path: Path) -> App:
    return App(outdir=str(tmp_path))


@pytest.fixture
def scratch_chart(scratch_app: App) -> Chart:
    return Chart(scratch_app, "test")


@pytest.fixture(scope="session")
def scratch_app_factory(tmp_path_factory: pytest.TempPathFactory) -> Callable[[], App]:
    """Fresh apps for module- and session-scoped fixtures, which can't request `scratch_app`.

    Tests and function-scoped fixtures use `scratch_app`/`scratch_chart`.
    """
    return lambda: App(outdir=str(tmp_path_factory.mktemp("cdk8s")))
