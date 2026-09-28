# Single Source of Truth for remote, OAuth-based MCP servers wired into every
# agent harness.
#
# Used by:
#   - nix/home/claude_code/default.nix
#   - nix/home/codex/default.nix
#   - nix/home/opencode/default.nix
#   - nix/home/gemini_cli.nix
#
# Each harness module maps this attrset into its own native mcpServers/mcp_servers
# schema; this file only carries what's common to a plain-URL OAuth MCP server.
#
# Deliberately out of scope: stdio/local-command servers (e.g. Codex's ChatGPT
# desktop bridge) and per-harness auth/approval knobs, which vary per harness and
# are set where each entry is mapped.
{
  agentplane-staging = {
    url = "https://agentplane-actions-staging.allegedly.works/mcp";
  };
}
