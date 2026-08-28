# Tana Export Toolkit

Utilities for transforming Tana JSON exports into Markdown or TanaPaste formats and for
materialising saved searches. The package bundles CLI helpers plus a reusable library of
parsers/renderers under the `tana.export` namespace.

## Development

See the repository root AGENTS.md for the standard Bazel workflow.

`bazel run //tana/export:convert_bin -- --help`

Key layout (`tana/`):

- `domain/` — data models, constants, and type definitions.
- `graph/` — `TanaGraph` workspace representation and structural helpers, including the
  JSON loader (`load_workspace`).
- `query/` — read/query helpers (filters, search parser/evaluator/materializer).
- `render/` — Markdown/TanaPaste formatting utilities.
- `export/` — CLI entry points and higher-level workflows.
- `mcp_server/` — container packaging for the internal desktop-backed Tana MCP server.
- `firebase_resigner/` — sidecar that re-signs the in-pod Tana Desktop's Firebase
  session when it goes stale.
- `firebase_session_extractor/` — Rust CLI that extracts a Firebase refresh token from a
  local Tana Desktop install's IndexedDB.
- `litellm_proxy/` — LiteLLM custom provider for Tana's internal `llmProxy` endpoint
  (development/demo integration).

The public Authentik-backed MCP OAuth facade is now the shared
`mcp-oauth-facade` image (`mcp_infra/oauth_facade/`); the per-tana facade was
removed when the facade was generalized.

Tests and golden fixtures are in `testdata/`.
