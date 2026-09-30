"""Claude Code's public OAuth client, shared by everything that pairs or refreshes a Claude grant."""

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # public identifier, shipped in the Claude Code CLI
FULL_SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers", "user:file_upload")
