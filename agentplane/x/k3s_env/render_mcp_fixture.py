"""Render the local-model fixture with Dex and MCP acceptance servers."""

import os
from dataclasses import asdict
from pathlib import Path

from agentplane.x.k3s_env import fixture_chart, mcp_fixture_chart
from agentplane.x.k3s_env.render_fixture import write_fixture
from agentplane.x.k3s_env.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    BrowserService,
    McpFixtures,
    RenderedFixture,
)
from cluster.cdk8s.agentplane import dex


def render(output: Path) -> None:
    mcp = MCP_PLACEHOLDERS
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    app_service = fixture_chart.APP_SERVICE
    edge_service = fixture_chart.MCP_EDGE_SERVICE
    dex_service = dex.service()
    fixture = RenderedFixture(
        backend=Backend.LOCAL_MODEL,
        vm_sandboxes=False,
        namespace=fixture_chart.NAMESPACE,
        app_origin=mcp.app_origin,
        browser_services=[
            BrowserService(name=service.name, host=service.host, fqdn=service.fqdn)
            for service in (app_service, dex_service)
        ],
        mcp_fixtures=McpFixtures(
            dex_host=fixture_chart.DEX_HOST,
            operator_secret=mcp_fixture_chart.OPERATOR_SECRET,
            mcp_edge_service=edge_service.name,
            mcp_edge_port=edge_service.port.number,
            app_service=app_service.name,
            app_port=app_service.port.number,
        ),
        objects=mcp_fixture_chart.objects(**asdict(PLACEHOLDERS), models=models, mcp=mcp),
    )
    write_fixture(output / "local-model-mcp-fixtures.json", fixture, models=models, mcp=mcp)


def main() -> None:
    render(Path(os.environ["K3S_FIXTURE_DIR"]))


if __name__ == "__main__":
    main()
