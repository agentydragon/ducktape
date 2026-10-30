"""cdk8s app and chart factories for tests, each with a fresh outdir under pytest's temp dir.

These replace `cdk8s.Testing.app()`/`Testing.chart()` (banned in `ruff.toml`), which mkdtemp a
`cdk8s.outdir.*` in the system temp dir on every call and never remove it. Session-scoped, so any
fixture scope can request them; `cluster/cdk8s/conftest.py` exposes them.
"""

from collections.abc import Callable

import pytest
from cdk8s import App, Chart


@pytest.fixture(scope="session")
def scratch_app(tmp_path_factory: pytest.TempPathFactory) -> Callable[[], App]:
    return lambda: App(outdir=str(tmp_path_factory.mktemp("cdk8s")))


@pytest.fixture(scope="session")
def scratch_chart(tmp_path_factory: pytest.TempPathFactory) -> Callable[[], Chart]:
    return lambda: Chart(App(outdir=str(tmp_path_factory.mktemp("cdk8s"))), "test")
