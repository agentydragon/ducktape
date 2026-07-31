# Requirements status — `public-coder-agent`

What [requirements.md](requirements.md) asked for, against what is deployed, with the
evidence. Only the public coder is assessed: the personal-data agent and the
knowledge-garden function are unbuilt, and their hard bars (C5, C6) are the two
this agent does not demonstrate. The personal-data agent's H1–H4 are assessed
against a proposed design instead, in
[personal_data_agent.md](personal_data_agent.md).

Findings are cited by number; see [findings/](findings/README.md).

## Cross-cutting

| #      | Requirement                              | Status                | Evidence                                                                                                                                                                         |
| ------ | ---------------------------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C1** | Reachable on the go                      | met                   | Web UI behind the Authentik outpost, restricted to one user                                                                                                                      |
| **C2** | Runs in k8s                              | met                   | `cluster/k8s/agents/public-coder-agent/`                                                                                                                                         |
| **C3** | Multi-provider, Codex **subscription**   | met                   | `litellm-subscription/codex-gpt-5.6-luna` plus the z.ai lane — not single-provider                                                                                               |
| **C4** | Langfuse observability                   | **met, verified**     | Traces read back: `gemini-embedding-2` ×10, `gpt-5.6-luna` ×7, `gpt-5.6-sol` ×25 in a day, `input` and `output` both populated                                                   |
| **C5** | No unrestricted network w/ personal data | n/a here              | Scoped to agents holding personal data; this one holds none, and egress is deliberately open                                                                                     |
| **C6** | Full LLM-level rollouts                  | **likely, unchecked** | C4’s traces carry full `input` and `output`, which is the substance C6 wants — but no real transcript has been reconstructed from them, and C6 is scoped to personal-data agents |
| **C7** | Declarative provisioning that holds up   | met, the hard way     | F19 is precisely this requirement failing silently, and the fix                                                                                                                  |
| **C8** | Persistence model understood             | met                   | One container, state on a PVC; harness and shell share a machine — the outcome C8 calls acceptable                                                                               |
| **C9** | Bounds overlong tool output              | **met, tested**       | 20 MB through the real agent → truncated with an explicit marker, 1 tool call, session intact                                                                                    |

## Wants

| #      | Want             | Status        | Evidence                                                                                       |
| ------ | ---------------- | ------------- | ---------------------------------------------------------------------------------------------- |
| **W1** | Credential proxy | met, exceeded | Placeholders for **both** the GitHub PAT and the BuildBuddy key; neither readable by the agent |

## Public coder

| #      | Requirement                    | Status        | Evidence                                                                              |
| ------ | ------------------------------ | ------------- | ------------------------------------------------------------------------------------- |
| **P1** | Simple, no sandboxing required | met, exceeded | P1 permitted no restrictions; it got forced-proxy egress and an unreadable credential |
| **P2** | Own GitHub bot identity        | met           | `agentydragon-agent`; PR opened end to end, F16                                       |
| **P3** | Plain OpenClaw instance        | met           | Plain Deployment rather than the operator, for the reason in F3                       |

## What is knowingly not there

**The harness/shell split.** The agent's commands run in the same container as the
gateway. C8 states outright that splitting them is "a soft want, not a hard
requirement" and that sandboxing them together is "an acceptable outcome, just the
less preferred one" — so this is the documented second choice, not a shortfall.
What it costs: a prompt injection that reaches the shell reaches the harness, and
the credential-holding boundary is the proxy rather than the process.

**Deny-by-default egress.** Switched off deliberately for this agent. The confined
configuration is kept verbatim in comments in both `cnp-egress.yaml` and
`iron.yaml` and must be re-enabled together. Rationale and scope in
[success_criteria.md](success_criteria.md) § S4 waiver. **Still a hard requirement
for anything holding personal data.**

## Rough edges outside the requirements list

- **The credential path now auto-updates.** The proxy was pinned to
  `ironsh/iron-proxy:0.49.0` deliberately, because that process holds the tokens.
  It is now a self-built image under Flux image automation, so the container
  holding the GitHub and BuildBuddy credentials tracks a moving tag. Defensible
  since the build is ours; worth being deliberate about rather than incidental.
- **The PAT still needs rotating** — see [TODO.md](TODO.md). Cheaper now: the token
  exists only in the proxy's Secret.
- **Every config-driven restart costs one prompt** on whatever session was live
  (F14). Upstream behaviour, self-healing, recurring.
