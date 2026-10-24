"""Render every fixture variant in one test process to keep the determinism check cheap."""

import os
from pathlib import Path

from agentplane.system_tests.k3s import render_fixture, render_mcp_fixture


def main() -> None:
    output = Path(os.environ["K3S_FIXTURE_DIR"])
    for variant in render_fixture.FixtureVariant:
        render_fixture.render_variant(output, variant)
    render_mcp_fixture.render(output)


if __name__ == "__main__":
    main()
