# BuildBuddy hosted remote-run authentication

Why the egress proxy's BuildBuddy support is local-client only, what a hosted `bb remote` run
would need, and what the smallest feasible workaround does and does not protect. The implemented
transport contract is [the egress specification](../egress/SPEC.md); whether to accept the
workaround is an open decision in the [task DAG](../plans/task_dag.md).

## Two products called "remote"

1. **Local Bazel client, remote BuildBuddy actions.** The Bazel process stays in the Agentplane
   Sandbox while Build Event Service, cache, and Remote Execution calls go to BuildBuddy. The
   existing proxy can keep the API key out of the Sandbox by replacing a whole
   `x-buildbuddy-api-key` HTTP/gRPC metadata value. This path is measured and needs no new target
   method.
2. **BuildBuddy-hosted `bb remote`.** The local CLI asks BuildBuddy to start a hosted runner, then a
   Bazel process on that runner makes its own BuildBuddy calls. The current header-only proxy can
   authenticate the first call but cannot affect the second call, which originates outside
   Agentplane.

The smallest useful question is not whether body rewriting is technically possible. It is:

> Is keeping the real key out of the local Sandbox sufficient when agent-controlled code on the
> BuildBuddy-hosted runner can recover it?

## Observed evidence

At BuildBuddy source commit
[`6fc01488`](https://github.com/buildbuddy-io/buildbuddy/tree/6fc01488a60d69832f86eff154ac985e1170653e):

- `bb remote` reads `x-buildbuddy-api-key` from its Bazel `--remote_header`, preserving that flag in
  the Bazel command it constructs.
- The CLI appends the same value to the outgoing gRPC context, authenticating its local
  `BuildBuddyService.Run` call.
- The Bazel command is serialized into `runner.RunRequest.steps[].run`.
- `BuildBuddyService.Run` is a unary RPC taking `runner.RunRequest`.

Consequently, with a placeholder the current wire shape is:

```text
outer gRPC metadata:
  x-buildbuddy-api-key: agentplane-credential-…

runner.RunRequest body:
  steps[0].run: bazel … --remote_header=x-buildbuddy-api-key=agentplane-credential-…
```

The Agentplane proxy substitutes the outer metadata. Under its current contract it deliberately
leaves body values inert, so BuildBuddy launches the hosted command with the placeholder and the
nested Bazel authentication fails.

## Candidate: narrow RunRequest rewrite

A BuildBuddy-specific request-body rewrite would make hosted remote builds work while keeping the
real key out of the local Sandbox:

1. Admit and authenticate the request under the ordinary EgressBinding and EgressPolicy decision.
2. Match only `remote.buildbuddy.io`, `POST`, and the exact
   `/buildbuddy.service.BuildBuddyService/Run` gRPC method.
3. Substitute the ordinary outer `x-buildbuddy-api-key` metadata target.
4. Read one bounded unary gRPC message, reject unsupported compression, and parse
   `runner.RunRequest` using the pinned BuildBuddy proto.
5. Find exactly one complete `--remote_header=x-buildbuddy-api-key=<expected placeholder>` option in
   the generated Bazel command. Reject zero candidates, multiple candidates, substring
   matches, malformed quoting, and every placeholder belonging to another credential.
6. Replace only that option value, reserialize the protobuf and gRPC frame, and forward it.
7. Record that body substitution occurred without logging the placeholder, real value, command, or
   body.

This is a special-purpose BuildBuddy presentation, not generic body search-and-replace: a generic
body target would make URL, JSON, protobuf, compression, framing, and substring semantics part of
the credential model and would reverse the property that body placeholders are inert.

## Security semantics

The rewrite provides **local-Sandbox credentiallessness**:

- the local Sandbox receives only the placeholder;
- the real key exists in the central proxy and in the request after it leaves that proxy;
- ordinary Sandbox files, environment, and process arguments contain no real key.

It does **not** provide credentiallessness from agent-controlled hosted code. BuildBuddy receives a
command containing the real key, and the hosted runner executes it. Code on that runner may be able
to recover the value from the shell command, Bazel process arguments, `/proc`, or another process
running as the same user, then print or exfiltrate it. BuildBuddy's redaction can reduce accidental
persistence in UI and logs; it cannot make the credential unavailable to the workload that uses it.

If this weaker boundary is accepted, use a dedicated BuildBuddy key with only the cache, execution,
and invocation permissions required by this lane. Do not use an org-admin, API-key-management, or
other broadly privileged key. Rotation limits persistence after recovery but does not prevent
recovery.

A stronger boundary needs a different hosted seam: a BuildBuddy-issued per-run credential, or a
run-scoped Agentplane gRPC gateway through which the hosted Bazel client sends placeholders. Both
keep the reusable key out of the hosted workload.

## Acceptance evidence required before the rewrite is called working

A focused fake-server integration must prove:

- the client sends only the placeholder in both outer metadata and `RunRequest.steps[].run`;
- the fake BuildBuddy service receives the real value in both locations;
- unrelated fields and command text are byte-for-byte semantically unchanged after protobuf
  decoding;
- wrong host, method, gRPC path, credential, field, quoting, zero/multiple flags, malformed frames,
  oversized bodies, and unsupported compression fail closed before an upstream dial or body
  forward;
- decisions and logs contain neither placeholder, real value, nor full command;
- the existing HTTP, unary gRPC, and bidirectional gRPC header-substitution tests remain green.

A live acceptance run must separately show that `bb remote` reaches the hosted Bazel invocation and
that the nested BES, cache, and Remote Execution calls succeed. Its report must state the known
hosted-runner exposure rather than treating successful execution as proof of the stronger boundary.

## Alternatives not taken

- **Generic request-body credential substitution**: reverses the inert-body property above and
  puts every body encoding into the credential model.
- **mTLS client-certificate presentation**: a separate BuildBuddy authentication mode, not header
  substitution; the proxy provisions and presents no client certificate.
- **Per-run BuildBuddy key minting and revocation** and **a run-scoped Agentplane gateway for
  hosted runners**: the stronger seams, unbuilt until the stronger guarantee is required.
