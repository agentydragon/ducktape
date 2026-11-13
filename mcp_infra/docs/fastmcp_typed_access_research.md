# FastMCP typed tool access

> **Status:** This pattern is used by `ContainerExecServer`; no custom FastMCP base
> class is needed.

FastMCP's `tool` decorator registers the MCP tool and returns its Python callable.
The registered name remains the protocol ID. When code needs typed access to that
callable, retain it on an annotated server attribute; keep protocol names and resource
URIs in class constants when callers need stable identifiers.

[`ContainerExecServer`](../exec/docker/server.py) demonstrates both: `exec_tool`
holds the returned callable, while `EXEC_TOOL_NAME` and `CONTAINER_INFO_URI` identify
the registered tool and resource.
