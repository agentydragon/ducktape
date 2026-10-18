# Pinned iron-proxy packages

Imports [iron-proxy](https://github.com/paradigmxyz/iron-proxy) at
`7c6a6e9261f09aa4f4efdb0bc20bdbbfb306603d` for the
[Agentplane comparison](../../agentplane/egress/x/iron_proxy/README.md).
There is no deployment or image-publication entry.

`go.mod` and `go.sum` are the upstream dependency inventory. Isolated `go_deps` usages prevent those
versions from changing the primary Go graph. BUILD aliases expose selected internal packages;
Gazelle generates their targets with explicit visibility. `schema.patch` exports the upstream
transform proto as a source file. Original Go sources are unchanged in that graph.

The second graph applies `streaming.patch` and exposes `patched_*` aliases. Separate test binaries
compare the same scenarios without linking two definitions of one Go package. The patch changes
response flushing only. The source revision and archive digest are pinned.

The CLI wrapper and transport tests use `pure = "on"`, matching upstream releases. Its PostgreSQL
feature parses through wasilibs and imports the original parser module for generated AST types;
the cgo-only implementation is not needed for that build mode.
