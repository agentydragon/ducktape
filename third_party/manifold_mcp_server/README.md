# manifold-mcp-server image

Sidecar container image for the Manifold Markets MCP. Wraps the stdio-only
[bmorphism/manifold-mcp-server](https://github.com/bmorphism/manifold-mcp-server)
npm package as a Streamable HTTP MCP endpoint (`http://localhost:8080/mcp`)
using [supergateway](https://github.com/supercorp-ai/supergateway).

The image runs as a long-lived sidecar in the `manifold-mcp` Pod alongside the
`mcp-oauth-facade` container. The facade reaches it as a plain HTTP upstream —
no stdio across container boundaries.

`MANIFOLD_API_KEY` is mounted into this container from the SOPS-encrypted
`manifold-mcp-api-key` Secret and inherited by the spawned manifold-mcp-server
child process.

Both npm packages are vendored via `aspect_rules_js`; bumping their versions
means editing the root `package.json` and letting Bazel regenerate
`pnpm-lock.yaml`.
