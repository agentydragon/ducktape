# Tana Export Toolkit

Transforms Tana JSON exports into Markdown or TanaPaste and materialises saved searches: CLI
helpers plus a library under `tana.export`.

```bash
bazel run //tana/export:convert_bin -- --help
```

- `domain/` — data models, constants, types.
- `graph/` — `TanaGraph` workspace representation, structural helpers, JSON loader
  (`load_workspace`).
- `query/` — filters, search parser/evaluator/materializer.
- `render/` — Markdown/TanaPaste formatting.
- `export/` — CLI entry points and workflows.
- `mcp_server/` — container packaging for the internal desktop-backed Tana MCP server (its public
  OAuth facade is the shared `mcp-oauth-facade` image, <../x/mcp_oauth_facade/>).
- `firebase_resigner/` — sidecar re-signing the in-pod Tana Desktop's stale Firebase session.
- `firebase_session_extractor/` — Rust CLI extracting a Firebase refresh token from a local Tana
  Desktop install's IndexedDB.
- `litellm_proxy/` — LiteLLM custom provider for Tana's internal `llmProxy` endpoint
  (development/demo).
- `testdata/` — tests and golden fixtures.
