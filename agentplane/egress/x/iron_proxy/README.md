# iron-proxy reuse experiment

Executable comparisons against the central proxy proposed in
[PR #10816](https://github.com/agentydragon/ducktape/pull/10816).
[Findings and remaining gaps](../../debug/iron_proxy_reuse.md) are the decision document.

```bash
bbr test //agentplane/egress/x/iron_proxy/... //agentplane/egress/proxy:proxy_test
```

- `comparison_test` imports the pinned Iron packages and characterizes the original transport.
- `patched_comparison_test` runs the same sources with only the response-flush patch, requiring
  initial metadata and an interactive response frame before request EOF.
- `bridge:bridge_test` exercises a gRPC adapter using Agentplane's real policy and workload-auth
  packages, including an interoperability test that starts Iron's actual CLI from Bazel runfiles.
- `bridge/proto` generates Agentplane-side types from the pinned upstream schema.

Known stock limitations are asserted explicitly. Passing these tests does not establish that Iron
can replace the deployed gateway. The adapter is experimental and has no production runtime,
projected-audience support, policy-bound DNS dial integration, or tunnel-handle cleanup.
