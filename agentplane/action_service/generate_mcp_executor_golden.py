"""Golden vectors that pin the Go MCP executor (`agentplane/action_service/mcpexec`) to this service's.

Each `configs` vector is an MCP binding's `config` and whether `McpServerConfig` accepts it. Each `tools` vector is
a `tools/list` answer as the MCP wire carries it, and the Actions `McpActionGroupExecutor` mirrors from it, or
`invalid` where it refuses the list. Each `results` vector is a `tools/call` answer as the wire carries it, the
result an Execution keeps from it, and whether it reports an unknown outcome.

Regenerate from the checkout's root with
`bb run //agentplane/action_service:generate_mcp_executor_golden_bin -- "$PWD/agentplane/action_service/mcpexec/golden.json"`.
"""

import sys
from pathlib import Path

from mcp.types import CallToolResult, ListToolsResult
from pydantic import JsonValue, ValidationError

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.generate_golden import serialize
from agentplane.action_service.mcp_executor import (
    _SERVER_CONFIG_ADAPTER,
    McpActionGroupExecutor,
    _InvalidMcpCatalogError,
    _mcp_error_kind,
)

Json = dict[str, JsonValue]

URL = "https://mcp.test.example/mcp"


def http(**fields: JsonValue) -> Json:
    return {"transport": "streamable-http", "url": URL, "auth": "none"} | fields


def stdio(**fields: JsonValue) -> Json:
    return {"transport": "stdio", "command": "test-mcp-server"} | fields


def configs() -> dict[str, JsonValue]:
    return {
        "stdio": stdio(),
        "stdio with every field": stdio(args=["--test-flag"], env={"TEST_NAME": "test-value"}, cwd="/test/dir"),
        "stdio with a null cwd": stdio(cwd=None),
        "stdio with an empty command": stdio(command=""),
        "stdio without a command": {"transport": "stdio"},
        "stdio with null args": stdio(args=None),
        "stdio with args that are not text": stdio(args=[1]),
        "stdio with an unknown field": stdio(url=URL),
        "http without auth": {"transport": "streamable-http", "url": URL},
        "http with no auth": http(),
        "http with no auth and null credentials": http(server_id=None, bearer_file=None),
        "http with no auth and a server_id": http(server_id="test-server"),
        "http with headers": http(headers={"X-Test-Header": "test-value"}),
        "http with an Authorization header": http(headers={"authorization": "Bearer test-token"}),
        "http with null headers": http(headers=None),
        "http with headers that are not text": http(headers={"X-Test-Header": 1}),
        "http with an unknown auth": http(auth="test-unknown"),
        "http with an unknown field": http(command="test-mcp-server"),
        "http over plain http": http(url="http://mcp.test.example/mcp"),
        "http over another scheme": http(url="ftp://mcp.test.example/mcp"),
        "http without a host": http(url="https://"),
        "http with an empty authority": http(url="https:///mcp"),
        "http with credentials in the URL": http(url="https://test-user@mcp.test.example/mcp"),
        "http with a query": http(url="https://mcp.test.example/mcp?test=1"),
        "http with a fragment": http(url="https://mcp.test.example/mcp#test"),
        "http with a port": http(url="https://mcp.test.example:8443/mcp"),
        "http without a url": {"transport": "streamable-http", "auth": "none"},
        "oauth": http(auth="oauth", server_id="test-server"),
        "oauth without a server_id": http(auth="oauth"),
        "oauth with a server_id that is not a key": http(auth="oauth", server_id="Test Server"),
        "oauth with a bearer_file": http(auth="oauth", server_id="test-server", bearer_file="/test/token"),
        "static bearer": http(auth="static_bearer", bearer_file="/test/token"),
        "static bearer without a bearer_file": http(auth="static_bearer"),
        "static bearer with a server_id": http(
            auth="static_bearer", bearer_file="/test/token", server_id="test-server"
        ),
        "sse": {"transport": "sse", "url": URL},
        "no transport": {"command": "test-mcp-server"},
        "not an object": ["stdio"],
        "null": None,
    }


def tool(name: str = "test_tool", **fields: JsonValue) -> Json:
    wire: Json = {"name": name, "inputSchema": {"type": "object"}}
    return wire | fields


def tool_lists() -> dict[str, list[Json]]:
    return {
        "a bare tool": [tool()],
        "every field": [
            tool(
                description="test description",
                title="Test title",
                inputSchema={
                    "type": "object",
                    "properties": {"testName": {"type": "string", "minLength": 1}},
                    "required": ["testName"],
                    "additionalProperties": False,
                },
                annotations={
                    "title": "Test annotation title",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            )
        ],
        "the annotations' title names it": [tool(annotations={"title": "Test annotation title"})],
        "empty annotations": [tool(annotations={})],
        "hints that are false": [tool(annotations={"readOnlyHint": False, "idempotentHint": False})],
        "an empty description": [tool(description="")],
        "an empty title": [tool(title="", annotations={"title": "Test annotation title"})],
        "a name that is not a key": [tool("Test Tool"), tool("test_kept")],
        "a draft-07 schema": [
            tool(inputSchema={"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"})
        ],
        "a schema that is not a JSON Schema": [
            tool(inputSchema={"type": "object", "properties": {"test": {"type": 1}}})
        ],
        "an input schema that is not an object": [tool(inputSchema=True)],
        "a name listed twice": [tool(), tool(description="test second")],
        "a description at the limit": [tool(description="d" * 20_000)],
        "a description past the limit": [tool(description="d" * 20_001)],
        "no tools": [],
    }


def block(kind: str, **fields: JsonValue) -> Json:
    wire: Json = {"type": kind}
    return wire | fields


def text(value: str = "test text", **fields: JsonValue) -> Json:
    return block("text", text=value, **fields)


def result(*blocks: Json, **fields: JsonValue) -> Json:
    content: list[JsonValue] = [*blocks]
    wire: Json = {"content": content}
    return wire | fields


UNKNOWN = '{"kind": "execution_unknown"}'


def results() -> dict[str, Json]:
    image = block("image", data="dGVzdA==", mimeType="image/png")
    return {
        "text": result(text()),
        "every content block": result(
            text(annotations={"audience": ["user"], "priority": 0.5}, _meta={"test": True}),
            image,
            block("audio", data="dGVzdA==", mimeType="audio/wav"),
            block("resource_link", uri="test://resource", name="test-resource", mimeType="text/plain"),
            block("resource", resource={"uri": "test://text", "mimeType": "text/plain", "text": "test"}),
            block("resource", resource={"uri": "test://blob", "blob": "dGVzdA=="}),
        ),
        "structured content": result(text(), structuredContent={"test": [1, "two", None]}),
        "meta": result(text(), _meta={"test": "test-value"}),
        "no content": result(),
        "an error": result(text(), isError=True),
        "an error not an error": result(text(), isError=False),
        "an unknown outcome": result(text(UNKNOWN), isError=True),
        "an unknown outcome after another block": result(
            image,
            text("not JSON"),
            text('["execution_unknown"]'),
            text('{"kind": "execution_unknown", "message": "test"}'),
            isError=True,
        ),
        "another kind first": result(text('{"kind": "test_kind"}'), text(UNKNOWN), isError=True),
        "a kind that is not text": result(text('{"kind": 1}'), isError=True),
        "an unknown outcome not an error": result(text(UNKNOWN)),
    }


def mirrored(tools: list[Json]) -> Json | None:
    group = ActionGroup(
        title="test group",
        description="test group description",
        executor=McpExecutorBinding(kind="mcp", description="test MCP executor"),
    )
    executor = McpActionGroupExecutor("test-group", group, transport=object())
    try:
        listed = ListToolsResult.model_validate({"tools": tools}).tools
        actions = executor._catalog_from_tools(listed)
    except _InvalidMcpCatalogError, ValidationError:
        return None
    return {name: action.model_dump(mode="json") for name, action in actions.items()}


def build() -> dict[str, object]:
    config_vectors: list[object] = []
    for name, config in configs().items():
        try:
            _SERVER_CONFIG_ADAPTER.validate_python(config)
        except ValidationError:
            valid = False
        else:
            valid = True
        config_vectors.append({"name": name, "config": config, "valid": valid})
    tool_vectors: list[object] = []
    for name, tools in tool_lists().items():
        actions = mirrored(tools)
        tool_vectors.append({"name": name, "tools": tools, "invalid": actions is None, "actions": actions})
    result_vectors: list[object] = []
    for name, wire in results().items():
        result = CallToolResult.model_validate(wire)
        result_vectors.append(
            {
                "name": name,
                "result": wire,
                "stored": result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"result_type"}),
                "unknown": result.is_error and _mcp_error_kind(result) == "execution_unknown",
            }
        )
    return {"configs": config_vectors, "tools": tool_vectors, "results": result_vectors}


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
