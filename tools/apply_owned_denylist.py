from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    if old not in text:
        raise SystemExit(f'missing edit anchor in {path}: {old[:80]!r}')
    target.write_text(text.replace(old, new, 1))


replace_once(
    'haku/console/mcp_config.py',
    '    id: str\n    backend: McpBackend\n',
    '    id: str\n    backend: McpBackend\n    agent_tool_denylist: set[str] = Field(default_factory=set)\n',
)
replace_once(
    'haku/console/mcp_config.py',
    '    catalog_refresh_interval_seconds: float | None = Field(default=None, ge=5.0, le=900.0)\n',
    '''    catalog_refresh_interval_seconds: float | None = Field(default=None, ge=5.0, le=900.0)

    @field_validator("agent_tool_denylist")
    @classmethod
    def _require_named_agent_tools(cls, value: set[str]) -> set[str]:
        if any(not tool.strip() for tool in value):
            raise ValueError("Agent tool denylist must not contain blank tool names")
        return value

    def blocks_agent_tool(self, tool_name: str) -> bool:
        return tool_name in self.agent_tool_denylist
''',
)
replace_once(
    'haku/console/mcp/server.py',
    'from haku.console.tool_call_actor import OperatorActor, RuntimeActor',
    'from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor',
)
replace_once(
    'haku/console/mcp/server.py',
    '''def _exposed_metadata(
    metadata: ServerMetadata,
    *,
    policies: AutoApprovalPolicyRegistry,
''',
    '''def _is_agent_tool_blocked(server: McpServerEntry, actor: RuntimeActor, tool_name: str) -> bool:
    return isinstance(actor, AgentActor) and server.blocks_agent_tool(tool_name)


def _exposed_metadata(
    metadata: ServerMetadata,
    *,
    server: McpServerEntry,
    policies: AutoApprovalPolicyRegistry,
''',
)
replace_once(
    'haku/console/mcp/server.py',
    '    tools = [exposed(tool) for tool in metadata.state.tools]',
    '''    tools = [
        exposed(tool)
        for tool in metadata.state.tools
        if not _is_agent_tool_blocked(server, actor, tool.name)
    ]''',
)
replace_once(
    'haku/console/mcp/server.py',
    '''    # A browser Operator is always a direct caller, even if a stale or hand-built proxy supplied
''',
    '''    server = next((candidate for candidate in _load_servers(context.settings) if candidate.id == server_id), None)
    if server is not None and _is_agent_tool_blocked(server, actor, tool_name):
        raise ToolError(f"MCP tool {server_id!r}/{tool_name!r} is not available to Agents")

    # A browser Operator is always a direct caller, even if a stale or hand-built proxy supplied
''',
)
replace_once(
    'haku/console/mcp/server.py',
    '''            for tool in meta.tools
        ]
''',
    '''            for tool in meta.tools
            if not _is_agent_tool_blocked(server, actor, tool.name)
        ]
''',
)
replace_once(
    'haku/console/mcp/server.py',
    '''        for upstream_tool in meta.tools:
            tool = _build_proxy_tool(
''',
    '''        for upstream_tool in meta.tools:
            if _is_agent_tool_blocked(server, actor, upstream_tool.name):
                continue
            tool = _build_proxy_tool(
''',
)
replace_once(
    'haku/console/mcp/server.py',
    '''                server_metadata_response(server_id, reflection),
                policies=policies,
''',
    '''                server_metadata_response(server_id, reflection),
                server=server,
                policies=policies,
''',
)

replace_once(
    'haku/console/mcp/test_server.py',
    'from haku.console.mcp_config import ConsoleConfigFile, const_in_process_server',
    'from haku.console.mcp_config import ConsoleConfigFile, McpServerEntry, const_in_process_server',
)
Path('haku/console/mcp/test_server.py').open('a').write('''


def test_agent_tool_denylist_applies_only_to_agents() -> None:
    server = McpServerEntry(
        id="github",
        backend=_in_process_backend({"kind": "none"}),
        agent_tool_denylist={"create_pull_request_with_copilot"},
    )
    agent = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))
    operator = OperatorActor(operator_id=UUID(int=2))

    assert mcp_server_module._is_agent_tool_blocked(server, agent, "create_pull_request_with_copilot")
    assert not mcp_server_module._is_agent_tool_blocked(server, agent, "get_commit")
    assert not mcp_server_module._is_agent_tool_blocked(server, operator, "create_pull_request_with_copilot")


async def test_agent_tool_denylist_rejects_hand_built_dispatch(tmp_path: Path) -> None:
    config_file = _write_console_config(
        tmp_path / "denylist.yaml",
        {
            "mcp": {
                "servers": [
                    {
                        "id": "github",
                        "backend": _in_process_backend({"kind": "none"}),
                        "agent_tool_denylist": ["create_pull_request_with_copilot"],
                    }
                ]
            }
        },
    )
    context = Mock()
    context.settings.config_file = config_file
    agent = AgentActor(agent_id=UUID(int=1), operator_id=UUID(int=2), binding_id=UUID(int=3))

    with pytest.raises(ToolError, match="not available to Agents"):
        await mcp_server_module._dispatch(
            context,
            server_id="github",
            tool_name="create_pull_request_with_copilot",
            arguments={},
            passthrough=False,
            actor=agent,
        )
''')
replace_once(
    'haku/console/test_deployment_config.py',
    '    server_ids = {server.id for server in config.mcp.servers}\n',
    '''    server_ids = {server.id for server in config.mcp.servers}
    github_server = next(server for server in config.mcp.servers if server.id == "github")
    assert github_server.agent_tool_denylist == {
        "assign_copilot_to_issue",
        "create_pull_request_with_copilot",
        "get_copilot_job_status",
        "request_copilot_review",
    }
''',
)
replace_once(
    'cluster/k8s/haku/console/config.yaml',
    "    # the Console's per-call operator approval queue.\n",
    "    # the Console's per-call operator approval queue; tools in agent_tool_denylist are unavailable\n    # to Agents entirely.\n",
)
replace_once(
    'cluster/k8s/haku/console/config.yaml',
    '      catalog_refresh_interval_seconds: 900\n',
    '''      catalog_refresh_interval_seconds: 900
      agent_tool_denylist:
        - assign_copilot_to_issue
        - create_pull_request_with_copilot
        - get_copilot_job_status
        - request_copilot_review
''',
)
replace_once(
    'haku/console/README.md',
    'credential kind the implementation did not declare.\n',
    "credential kind the implementation did not declare.\nAn entry's `agent_tool_denylist` removes named upstream tools from every Agent's listing, status metadata, and execution path; Operators retain access unless the upstream server itself denies it.\n",
)
replace_once(
    'cluster/k8s/haku/console/README.md',
    'the reviewed read-only tool names for Haku. Every other GitHub tool remains per-call operator approval.\n',
    'the reviewed read-only tool names for Haku. The same entry denies the Copilot delegation tools to every Agent, including stale-schema and generic-dispatch calls. Other GitHub tools remain per-call operator approval.\n',
)
