# haku/runtime/x — experimental runtime pieces

In-flux runtime code with no stable API, in the sense <../../../README.md> § `x/` gives the
convention. The originating design note is <../../plans/agent_sdk_sandbox_runtime.md>; the shape
the runtime actually took is <../../console/channels/matrix/SPEC.md>, and where it is going is
<../../console/plans/conversation_layers.md>.

`bridge` is the sandbox side of the native-harness chat experiment: the wire protocol between
console and sandbox, the CLI protocol clients, the launch construction, the WebSocket transport,
and the `runner_bin` that runs inside the sandbox image. Its only production consumer is
`haku/console/x/session_runtime.py` — the two halves of one experiment, on both sides of the
WebSocket.

**Gotcha:** the provider-neutral image name is `haku-harness-runner`, set explicitly in
`.github/workflows/push-images.yml` rather than derived from this path. Its one OCI contains the
bridge, both native CLIs, git, kubectl and CA roots. The Claude SandboxTemplate selects the harness
at launch; adding another harness must not create another publication alias.
