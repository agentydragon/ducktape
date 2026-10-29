"""cdk8s apps and charts for tests, writing only under an outdir the test owns.

These replace `cdk8s.Testing.app()`/`Testing.chart()` (banned in `ruff.toml`), which mkdtemp a
`cdk8s.outdir.*` in the system temp dir on every call and never remove it. Pass pytest's
`tmp_path` (or `tmp_path_factory.mktemp(...)` in a wider-scoped fixture), which pytest cleans.
"""

from pathlib import Path

from cdk8s import App, Chart


def scratch_app(outdir: Path) -> App:
    return App(outdir=str(outdir))


def scratch_chart(outdir: Path) -> Chart:
    return Chart(scratch_app(outdir), "test")
