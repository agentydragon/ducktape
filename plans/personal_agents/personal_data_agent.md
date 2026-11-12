# Personal-data agent

Requirements H1–H4 and W2 are in <../../docs/personal_agents/requirements.md>. The agent is
not deployed. Select a runtime and verify self-hosting, user-accessible traces, network-isolated
command execution and durable memory together; execution outside the harness container is a
preference conditional on preserving memory.

The earlier proposal used the `public-coder-agent` OpenClaw deployment and iron-proxy as its
baseline. Public Coder is now parked and its Iron proxy is retired; its current status and revival
conditions are in <../../cluster/parked/public-coder-agent/README.md>. The prior measurements in
<../../docs/personal_agents/findings/README.md> are useful evidence for that tested setup, not proof
that a new personal-data deployment meets these requirements. The Console's former tool-call
execution and approval path is also retired (<../../haku/console/README.md>).

## Runtime and execution boundary

- Compare the current Agentplane execution and egress paths with the parked OpenClaw design before
  choosing a runtime. Specify which component owns session recovery, durable memory, commands and
  their output, and verify the selected boundary end to end.
- Enforce deny-by-default outbound access for command execution, including when proxy environment
  variables are absent. Prove intended destinations work and unauthorized routes fail. Distinguish
  direct egress from writes through allowed destinations; personal data in agent context can leak
  through either path.
- Verify traces include the intended model turns and tool interactions, access to them is scoped,
  and retention is appropriate for personal data. A model gateway alone does not prove all of these.
- Investigate execution outside the harness as W2, without assuming it keeps tool output away from
  the model: the agent may legitimately receive that output. Preserve workspace data and define
  reconciliation for interrupted commands before adopting a split. The `WorkerProvider` evaluation
  and OpenShell caveats are tracked in <TODO.md>.

## Credentials and data access

Keep provider refresh tokens and access tokens outside the agent's execution environment. Scope
upstream credentials to the minimum per surface; a proxy path rule is defense in depth, not a
substitute for a read-only grant. Evaluate which existing MCP servers provide the needed narrowly
scoped operations and which other APIs need a credential broker. The old Console approval queue is
not an available implementation; name and verify the policy and approval owner for every write.

The earlier investigation compared server-held MCP credentials with iron-proxy's `oauth_token`
transform and rejected mirroring a live access token into the agent. Iron-proxy is a candidate to
re-evaluate, not a deployed dependency. Before selecting any broker, verify refresh and rotation,
restart behavior, host/path matching, CONNECT handling, token custody, and network policy for both
agent and MCP-server processes. The concrete iron-proxy probes remain in <TODO.md>. Re-check the
current Google consent and token lifetime for the chosen OAuth clients rather than treating the
prior weekly expiry as a permanent contract.

## Acceptance

1. A representative personal-data workflow runs on the chosen self-hosted runtime; after restart it
   recovers authorized memory and does not silently replay an uncertain external effect.
2. An operator can inspect the required trace and tool activity under the chosen access and retention
   policy, including after a restart.
3. The command environment cannot reach an unapproved public destination, even with proxy settings
   removed; separately test allowed services and credential substitutions.
4. The agent cannot read refresh or access tokens from its environment, and denied operations fail at
   the relevant upstream credential, policy or approval boundary.
5. Test exfiltration through allowed write destinations as well as direct networking. Describe the
   residual risk of data already present in model context.
