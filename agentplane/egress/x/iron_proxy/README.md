# iron-proxy reuse experiment

Executable comparisons against the current [central proxy](../../README.md).
[Findings and remaining gaps](findings.md) are the decision document.

```bash
bbr test //agentplane/egress/x/iron_proxy/... //agentplane/egress/internal/proxy:proxy_test
```

- `comparison_test` imports the pinned Iron packages and characterizes the original transport.
- `patched_comparison_test` runs the same sources with only the response-flush patch, requiring
  initial metadata and an interactive response frame before request EOF.
- `bridge/` preserves a historical bearer-auth adapter spike. Its Go source depends on the
  retired shared TokenReview resolver and is outside the active Bazel build graph.
- `bridge/proto` generates Agentplane-side types from the pinned upstream schema.

Known stock limitations are asserted explicitly. Passing these tests does not establish that Iron
can replace the deployed gateway. The adapter is experimental and has no production runtime,
projected-audience support, policy-bound DNS dial integration, or tunnel-handle cleanup.
