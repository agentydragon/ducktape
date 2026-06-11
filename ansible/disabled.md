- Anki mini-format-pack: disabled - seems I deleted my fork (`addon_id: 295889520`, `https://github.com/agentydragon/mini-format-pack`)
- Chrome remote desktop
- disable screensaver and desktop effects on VMs
- GitLab runner

## Legacy Claude MCP Servers

MCP servers previously managed by the `legacy_claude_mcp` Ansible role (removed 2026-03).

| Server    | Command | Package                                                          |
| --------- | ------- | ---------------------------------------------------------------- |
| memory    | `npx`   | `@modelcontextprotocol/server-memory`                            |
| firecrawl | `npx`   | `firecrawl-mcp` (env: `FIRECRAWL_API_URL=http://localhost:3002`) |
| arxiv     | `uvx`   | `git+https://github.com/blazickjp/arxiv-mcp-server.git`          |
| probe     | `npx`   | `@buger/probe-mcp`                                               |
