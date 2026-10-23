"""Render the fixture chart into `$K3S_FIXTURE_DIR`, at build time: `<backend>.json` for each model
backend, `local-model-mcp-fixtures.json` with Dex and the MCP servers `test_mcp` calls, and
`local-model-vm.json` with VM Sandboxes.

Takes no arguments: the app's `Settings`, which the chart code builds, parses `sys.argv` as its own.
"""

import os
from dataclasses import asdict
from pathlib import Path

from agentplane.x.k3s_env import fixture_chart, fixture_credentials
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


def _render(output: Path, backend: Backend, mcp: McpInputs | None, *, vm_sandboxes: bool) -> None:
    models = MODEL_PLACEHOLDERS[backend]
    fixture = RenderedFixture(
        backend=backend,
        vm_sandboxes=vm_sandboxes,
        namespace=fixture_chart.NAMESPACE,
        app_origin=mcp.app_origin if mcp is not None else fixture_chart.APP_ORIGIN,
        browser_services=[
            BrowserService(name=service.name, host=service.host, fqdn=service.fqdn)
            for service in fixture_chart.browser_services(mcp_fixtures=mcp is not None)
        ],
        mcp_fixtures=(
            McpFixtures(
                dex_host=fixture_chart.DEX_HOST,
                operator_secret=fixture_chart.OPERATOR_SECRET,
                mcp_edge_service=fixture_chart.MCP_EDGE_SERVICE.name,
                mcp_edge_port=fixture_chart.MCP_EDGE_SERVICE.port.number,
                app_service=fixture_chart.APP_SERVICE.name,
                app_port=fixture_chart.APP_SERVICE.port.number,
            )
            if mcp is not None
            else None
        ),
        objects=fixture_chart.objects(
            **asdict(PLACEHOLDERS),
            models=models,
            mcp=mcp,
            vm_sandboxes=vm_sandboxes,
            credentials=fixture_credentials.PLACEHOLDERS,
            mcp_credentials=fixture_credentials.MCP_PLACEHOLDERS if mcp is not None else None,
        ),
    )
    text = fixture.model_dump_json()
    placeholders = list(asdict(PLACEHOLDERS).values())
    placeholders.extend(asdict(fixture_credentials.PLACEHOLDERS).values())
    match models:
        case LocalModel(url=model_placeholder) | PublicLiteLLM(key=model_placeholder):
            placeholders.append(model_placeholder)
    if mcp is not None:
        placeholders.extend(asdict(mcp).values())
        placeholders.extend(asdict(fixture_credentials.MCP_PLACEHOLDERS).values())
    # The chart transformed a placeholder (encoded it, say), so filling it in would miss.
    if missing := [placeholder for placeholder in placeholders if placeholder not in text]:
        raise ValueError(f"{backend=} renders without {missing=}")
    variant = f"{'-mcp-fixtures' if mcp is not None else ''}{'-vm' if vm_sandboxes else ''}"
    (output / f"{backend}{variant}.json").write_text(text)


def main() -> None:
    output = Path(os.environ["K3S_FIXTURE_DIR"])
    for backend in Backend:
        _render(output, backend, None, vm_sandboxes=False)
    _render(output, Backend.LOCAL_MODEL, MCP_PLACEHOLDERS, vm_sandboxes=False)
    _render(output, Backend.LOCAL_MODEL, None, vm_sandboxes=True)


if __name__ == "__main__":
    main()
