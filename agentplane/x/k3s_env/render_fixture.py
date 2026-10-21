"""Render the fixture chart into `$K3S_FIXTURE_DIR`, at build time: `<backend>.json` for each model
backend, and `local-model-mcp-fixtures.json` with Dex, the MCP servers `test_mcp` calls and the Action
Service's OAuth server `test_dcr` registers with.

Takes no arguments: the app's `Settings`, which the chart code builds, parses `sys.argv` as its own.
"""

import os
from dataclasses import asdict
from pathlib import Path

from agentplane.x.k3s_env import fixture_chart
from agentplane.x.k3s_env.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    BrowserService,
    LocalModel,
    McpFixtures,
    McpInputs,
    PublicLiteLLM,
    RenderedFixture,
)


def _render(output: Path, backend: Backend, mcp: McpInputs | None) -> None:
    models = MODEL_PLACEHOLDERS[backend]
    fixture = RenderedFixture(
        backend=backend,
        namespace=fixture_chart.NAMESPACE,
        app_origin=fixture_chart.APP_ORIGIN,
        browser_services=[
            BrowserService(name=service.name, host=service.host, fqdn=service.fqdn)
            for service in fixture_chart.browser_services(mcp_fixtures=mcp is not None)
        ],
        mcp_fixtures=(
            McpFixtures(
                dex_host=fixture_chart.DEX_HOST,
                operator_secret=fixture_chart.OPERATOR_SECRET,
                actions_service=fixture_chart.ACTIONS_SERVICE.name,
                actions_port=fixture_chart.ACTIONS_SERVICE.port.number,
            )
            if mcp is not None
            else None
        ),
        objects=fixture_chart.objects(**asdict(PLACEHOLDERS), models=models, mcp=mcp),
    )
    text = fixture.model_dump_json()
    placeholders = list(asdict(PLACEHOLDERS).values())
    match models:
        case LocalModel(url=model_placeholder) | PublicLiteLLM(key=model_placeholder):
            placeholders.append(model_placeholder)
    if mcp is not None:
        placeholders.extend(asdict(mcp).values())
    # The chart transformed a placeholder (encoded it, say), so filling it in would miss.
    if missing := [placeholder for placeholder in placeholders if placeholder not in text]:
        raise ValueError(f"{backend=} renders without {missing=}")
    (output / f"{backend}{'-mcp-fixtures' if mcp is not None else ''}.json").write_text(text)


def main() -> None:
    output = Path(os.environ["K3S_FIXTURE_DIR"])
    for backend in Backend:
        _render(output, backend, None)
    _render(output, Backend.LOCAL_MODEL, MCP_PLACEHOLDERS)


if __name__ == "__main__":
    main()
