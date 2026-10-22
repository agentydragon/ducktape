# iron-proxy reuse experiment

Executable comparisons against the current [central proxy](../../README.md).
[Findings and remaining gaps](findings.md) are the decision document.

```bash
bbr test //agentplane/egress/x/iron_proxy/... //agentplane/egress/proxy:proxy_test
```

- `comparison_test` imports the pinned Iron packages and characterizes the original transport.
- `patched_comparison_test` runs the same sources with only the response-flush patch, requiring
  initial metadata and an interactive response frame before request EOF.
- `bridge:bridge_test` exercises a gRPC adapter using Agentplane's real policy and workload-auth
  packages. It drives both CLIs from Bazel runfiles: exact header/JSON substitution, plus real
  bidirectional TLS gRPC through the patched transport and a header-only transform. It checks
  metadata, messages before EOF, trailers, cancellation, deadlines and refusal after revocation;
  stock flushing, body buffering and gRPC refusal framing remain characterized limitations.
- `bridge/proto` generates Agentplane-side types from the pinned upstream schema.

Known stock limitations are asserted explicitly. Passing these tests does not establish that Iron
can replace the deployed gateway. The adapter is experimental and has no production runtime,
projected-audience support, policy-bound DNS dial integration, or tunnel-handle cleanup.
