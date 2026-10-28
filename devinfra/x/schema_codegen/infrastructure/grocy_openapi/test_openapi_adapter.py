from __future__ import annotations

import json
from dataclasses import dataclass

import httpx2
import pytest
import pytest_bazel
from fastmcp import Client
from jsonschema import Draft202012Validator, ValidationError

from grocy_mcp import server as grocy_server
from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from mcp_infra.request_scoped_openapi import borrowed_http_client_provider
from util.bazel.runfiles import get_required_path


@dataclass(frozen=True)
class _FixtureOverride:
    name: str
    extra_description: str | None = None
    enabled: bool = True


async def test_openapi_tool_adapter_advertises_and_invokes_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = json.loads(
        get_required_path(
            "_main/devinfra/x/schema_codegen/infrastructure/grocy_openapi/sample_openapi.json"
        ).read_text()
    )
    monkeypatch.setattr(grocy_server, "_load_openapi_spec", lambda: spec)
    monkeypatch.setattr(
        grocy_server, "TOOL_OVERRIDES", {("POST", "/widgets/{widgetId}"): _FixtureOverride("fixture_update_widget")}
    )
    monkeypatch.setattr(grocy_server, "register_batch_tools", lambda *_args, **_kwargs: None)

    requests: list[httpx2.Request] = []

    async def backend(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"id": "widget-7", "preview": True})

    async with GrocyClient(
        base_url="https://fixture.example.test/api", transport=httpx2.MockTransport(backend)
    ) as grocy_client:
        mcp = grocy_server.build_mcp(
            ServerSettings(grocy_url="https://fixture.example.test"),
            client_provider=borrowed_http_client_provider(grocy_client),
        )

        async with Client(mcp) as mcp_client:
            [tool] = await mcp_client.list_tools()
            schema = tool.input_schema
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            assert tool.output_schema is None

            valid_arguments: dict[str, object] = {
                "widgetId": "widget-7",
                "preview": True,
                "label": "demo",
                "enabled": True,
            }
            validator.validate(valid_arguments)
            for invalid_arguments in (
                {"widgetId": "widget-7", "preview": True, "enabled": True},
                {"widgetId": "widget-7", "preview": "true", "label": "demo", "enabled": True},
                {"widgetId": "widget-7", "preview": True, "label": "", "enabled": True},
            ):
                with pytest.raises(ValidationError):
                    validator.validate(invalid_arguments)

            assert tool.name == "fixture_update_widget"
            result = await mcp_client.call_tool_mcp(tool.name, valid_arguments)

    assert result.is_error is False
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/widgets/widget-7"
    assert requests[0].url.params["preview"] == "true"
    assert json.loads(requests[0].content) == {"label": "demo", "enabled": True}


if __name__ == "__main__":
    pytest_bazel.main()
