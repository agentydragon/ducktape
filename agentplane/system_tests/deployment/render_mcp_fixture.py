"""Render the local-model fixture with Dex and MCP acceptance servers."""

import os
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

from agentplane.system_tests.deployment import dex, fixture_chart, mcp_fixture_chart
from agentplane.system_tests.deployment.render_fixture import write_fixture
from agentplane.system_tests.deployment.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    BrowserService,
    McpFixtures,
    RenderedFixture,
)
from agentplane.system_tests.k3s.namespaces import NAMESPACE


class FixtureVariant(StrEnum):
    LOCAL_MODEL_MCP_FIXTURES = "local-model-mcp-fixtures"
    LOCAL_MODEL_MCP_SERVICES = "local-model-mcp-services"

    @property
    def central_sampler(self) -> bool:
        return self == FixtureVariant.LOCAL_MODEL_MCP_FIXTURES


def render(output: Path, variant: FixtureVariant) -> None:
    mcp = MCP_PLACEHOLDERS
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    app_service = fixture_chart.APP_SERVICE
    edge_service = fixture_chart.MCP_EDGE_HTTP_SERVICE
    dex_service = dex.service()
    fixture = RenderedFixture(
        backend=Backend.LOCAL_MODEL,
        vm_sandboxes=False,
        central_sampler=variant.central_sampler,
        namespace=NAMESPACE,
        app_origin=mcp.app_origin,
        browser_services=[
            BrowserService(name=service.name, host=service.host, fqdn=service.fqdn)
            for service in (app_service, dex_service)
        ],
        mcp_fixtures=McpFixtures(
            dex_host=fixture_chart.DEX_HOST,
            operator_secret=mcp_fixture_chart.OPERATOR_SECRET,
            operator_token_grant=mcp_fixture_chart.OPERATOR_TOKEN_GRANT,
            mcp_edge_service=edge_service.name,
            mcp_edge_port=edge_service.port.number,
            app_service=app_service.name,
            app_port=app_service.port.number,
        ),
        objects=mcp_fixture_chart.objects(
            **asdict(PLACEHOLDERS), models=models, mcp=mcp, central_sampler=variant.central_sampler
        ),
    )
    write_fixture(output / f"{variant}.json", fixture, models=models, mcp=mcp)


def main() -> None:
    render(Path(os.environ["K3S_FIXTURE_DIR"]), FixtureVariant(os.environ["K3S_MCP_FIXTURE_VARIANT"]))


if __name__ == "__main__":
    main()
