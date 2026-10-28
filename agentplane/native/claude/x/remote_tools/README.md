# Native remote tools

A versioned experiment for Claude Code 2.1.296's gated native remote-tools feature.
Its npm executable and Bun source-entry runtime have independent Bazel pins; production pins are unchanged.

```sh
bbr test //agentplane/native/claude/x/remote_tools:test_native_serving //agentplane/native/claude/x/remote_tools:test_characterization
```

`test_native_serving` covers the supported cloud CLI startup without an account and exercises the
native serving source against a loopback WebSocket bridge. It extracts modules from the pinned
executable, relocates imports, adds three logging taps, initializes native config and supplies an
already-ready fixture. It first captures default gates, then explicitly **simulates** entitlement
and stored account state in temporary config. Eligibility, egress, account and trust checks then run
their native implementations. No real eligibility or account is changed.

The bridge peer records registration, the actual native tool roster/schemas, forwarding envelopes,
approval legs and native Read/Edit results. It does not implement file tools or inject a replacement
provider. Synthetic token, hostname, session/account/org IDs, approvals and config are labeled in the
source. Source provenance, native logs and wire captures go to Bazel undeclared outputs.

`test_characterization` is one unmodified local CLI comparator. A scripted loopback Messages endpoint
captures the next model request after native Read, including root/nested instruction markers.
Generic MCP and ordinary local state tests are omitted from this feature experiment.

The source-entry result does not establish supported account availability or a full cloud caller's
instruction behavior. See the [investigation](../../../../debug/claude_remote_tools_20261027.md) for
exact predicates, evidence and remaining gaps. No live inference, hosted bridge or deployment is used.
