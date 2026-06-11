# Tana Export Toolkit

Utilities for transforming Tana JSON exports into Markdown or TanaPaste formats and for
materialising saved searches. The package bundles CLI helpers plus a reusable library of
parsers/renderers under the `tana.export` namespace.

## Development

See the repository root AGENTS.md for the standard Bazel workflow.

`bazel run //tana:tana-export-convert -- --help`

Key layout (`tana/`):

- `domain/` — data models, constants, and type definitions.
- `graph/` — `TanaGraph` workspace representation and structural helpers.
- `query/` — read/query helpers (filters, search parser/evaluator/materializer).
- `render/` — Markdown/TanaPaste formatting utilities.
- `io/` — JSON loaders (`load_workspace`).
- `export/` — CLI entry points and higher-level workflows.
- `mcp_server/` — container packaging for the internal desktop-backed Tana MCP server.

The public Authentik-backed MCP OAuth facade is now the shared
`mcp-oauth-facade` image (`mcp_infra/oauth_facade/`); the per-tana facade was
removed when the facade was generalized.

Tests and golden fixtures are in `testdata/`.
