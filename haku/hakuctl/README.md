# hakuctl

A small MCP client CLI for the Haku console's `/mcp` endpoint. It speaks
streamable-HTTP MCP with a static Agent bearer and exposes the generic
`tools/list` / `tools/call` surface, so console tool calls can be built and run
from a shell. It is a client of the deployed console, not part of the
`haku/console` server package.

The console keeps the approval and audit boundary; `hakuctl` only builds and
fires the requests.

## Auth and endpoint

- **Bearer**: `$HAKU_AGENT_TOKEN` — the reflected `haku-console-agent-api`
  secret. Read from the environment only (never a flag), so it stays out of
  shell history and process listings.
- **Endpoint**: `--url` / `$HAKU_MCP_URL`, defaulting to
  `https://haku.allegedly.works/mcp`.

TLS and proxy trust come from the environment: the underlying httpx client runs
with `trust_env` on, so it honors `HTTPS_PROXY` and the `SSL_CERT_FILE` CA
bundle the session exports. No CA path is hardcoded and verification is never
disabled.

## Commands

```bash
hakuctl list [--server <substr>]     # tools as name<TAB>first line of description
hakuctl schema <tool>                # a tool's input JSON schema
hakuctl call <tool> '<json>' [--json]  # call a tool; --json prints the raw result
```

`--server` filters `list` to tools whose name contains the substring (the
console names proxied tools `<server-id>__<tool>`, so this filters by upstream
server).

## Build and run

```bash
bazel run //haku/hakuctl:cli_bin -- list
```

Released as the `hakuctl` wheel (`//haku/hakuctl:wheel`) and shipped on PATH in the
Nix dev-tools closure (`nix/packages/default.nix`, `flake.nix` `devToolsCommon`).
