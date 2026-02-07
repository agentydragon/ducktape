# Claude MCP Ansible Role

This role manages Claude Code MCP (Model Context Protocol) server configuration.

## Requirements

- Claude Code CLI must be installed
- `jq` must be installed for JSON manipulation

## Role Variables

### Default Variables (defaults/main.yml)

```yaml
# MCP servers to configure
claude_mcp_servers:
  memory:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]
    env: {}

  firecrawl:
    command: npx
    args: ["-y", "firecrawl-mcp"]
    env:
      FIRECRAWL_API_URL: "http://localhost:3002"

  arxiv:
    command: uvx
    args: ["--from", "git+https://github.com/blazickjp/arxiv-mcp-server.git", "arxiv-mcp-server"]
    env: {}

  probe:
    command: uvx
    args: ["--from", "git+https://github.com/buger/probe.git", "probe-mcp-server"]
    env: {}

# Whether to automatically apply configuration
claude_mcp_auto_apply: false

# Claude config file location
claude_config_path: "{{ ansible_env.HOME }}/.claude.json"
```

## Example Playbook

### Check-only mode (default)

```yaml
- hosts: localhost
  roles:
    - claude-mcp
```

This will check if MCP servers are configured and fail if they're not.

### Auto-apply mode

```yaml
- hosts: localhost
  roles:
    - role: claude-mcp
      vars:
        claude_mcp_auto_apply: true
```

### Custom servers

```yaml
- hosts: localhost
  roles:
    - role: claude-mcp
      vars:
        claude_mcp_servers:
          memory:
            command: npx
            args: ["-y", "@modelcontextprotocol/server-memory"]
            env: {}
          custom-tool:
            command: node
            args: ["/path/to/custom-tool.js"]
            env:
              API_KEY: "{{ vault_custom_api_key }}"
```

## Manual Configuration

If you prefer to configure MCP servers manually:

```bash
~/code/ducktape/ansible/roles/legacy_claude_mcp/files/apply_mcp_config.py
```

This script reads the server configuration from the Ansible role's `defaults/main.yml` file, ensuring there's a single source of truth for the MCP server definitions.

## Tags

- `claude` - All Claude-related tasks
- `mcp` - MCP-specific tasks

## License

MIT
