# Personal Agents — Requirements

Status: draft, gathering requirements + mapping to existing/candidate infra. Not yet
actionable manifests. See `findings.md` for the requirements→implementation mapping and
`README.md` for how this directory is organized.

Requirements are grouped by scope: **cross-cutting** (applies to all agents), then
per-agent-function.

## Cross-cutting

- **C1. Reachable on the go.** Every agent function needs some web UI (or other
  remote-accessible interface) usable from a phone/browser away from a desk — not
  CLI-only.
- **C2. Runs in k8s.** All agents should run in the homelab k8s cluster (not laptop-local
  processes), consistent with existing `cluster/k8s/agents/` and `cluster/k8s/haku/`.
- **C3. Multi-provider LLM routing.** Must support OpenAI Codex **subscription** auth
  (not just pay-per-token API key) as one backend, but must not be Codex-only — Anthropic
  Claude and others need to keep working too. Preferred integration point is the
  existing LiteLLM proxy (already wired for other providers) since it's also the
  Langfuse logging path; a different integration is acceptable if LiteLLM can't do it,
  but single-provider lock-in is not.
- **C4. Langfuse-backed observability preferred.** Routing model calls through LiteLLM
  is preferred specifically because it already logs to Langfuse.
- **C5. No unrestricted network from agents holding personal data.** Any agent with
  access to personal data (mail, calendar, finance, etc.) must not have unrestricted
  outbound network access from its command-execution environment. At minimum a
  domain allowlist/egress proxy; full outbound denial-by-default is the target.
- **C6. Full execution traces/transcripts.** For agents handling personal data, the
  user wants **LLM-level rollouts** — full requests/responses, all API-level detail
  available (not just tool calls and their order/timing) — available to them
  directly, not mediated only through a hosted product UI that doesn't expose it.
  Resolved as a hard requirement, not just "the tool-call ledger is enough": current
  Claude-Code-Web-hosted Haku has no easy way to get this.
- **C7. Declarative provisioning that actually holds up.** Repeated experience: sketches
  fall apart once translated into real k8s manifests because some harness/sandbox
  component's operator story is incomplete or the workspace model doesn't fit
  multi-machine (harness pod vs. exec pod) topologies. Any chosen stack needs to be
  validated by actually writing manifests for it, not just read about.
- **C8. Persistence model must be understood before committing.** Need to know
  concretely how a harness persists state when the harness process and the
  command-execution environment are _not_ the same machine (relevant to OpenClaw
  specifically, see below). **Splitting the harness from the agent's shell is a
  soft want, not a hard requirement**: sandboxing the whole harness together with
  its execution (NemoClaw-style) is an acceptable outcome, just the less preferred
  one. So this question only has to be answered for whichever topology is actually
  chosen.
- **C9. Robust handling of overlong tool output.** Any harness used for these agents
  must bound tool/command output before it reaches the model — whether by
  truncation, pagination, or offloading to a file with a reference — rather than
  choking or wedging a session when a single command (or several in one turn)
  produces a large result. This is a hard requirement for harness selection, not a
  nice-to-have: it's the exact failure mode that got kagent retired from this
  cluster (see `findings.md`), and `docs/self_hosted_coding_agent_platforms.md`
  already surveys this dimension across candidate platforms.

## Wants (not requirements)

Things the user agrees are good and would take if they come cheap, but which are
not gating criteria on their own.

- **W1. Credential proxy.** Having agents hold placeholder credentials while a
  proxy substitutes the real secrets into outbound requests is generally a good
  design — it means a compromised harness/agent process never holds live keys.
  Recorded as a want rather than a requirement: C5 (egress restriction) is the
  hard bar; credential substitution is a strictly-better-if-available property
  on top of it. Note this is _not_ what Haku's current mitmproxy does (that is
  FQDN allowlisting only) — see `findings.md` for which components actually
  offer it.

## Agent: "public coder"

Handles only public repos (e.g. `ducktape` itself); no personal data.

- **P1. Simple, no sandboxing required.** OK to run with no network restrictions and
  no command-execution isolation, since it never touches personal data.
- **P2. Own GitHub bot identity** (e.g. `agentydragon-agent`) — sends PRs, responds to
  review comments, etc. on public repos.
- **P3. Candidate implementation: plain OpenClaw instance,** unsandboxed. Open to
  alternatives if research surfaces a better fit (see `docs/self_hosted_coding_agent_platforms.md`
  survey already in-repo, which was written for a near-identical desiderata set).

## Agent: personal-data agent(s) ("Haku, but self-hosted")

Currently: Haku runs hosted inside Claude Code Web. Wants a self-hosted-runtime
equivalent.

- **H1. Self-hosted runtime**, not dependent on Claude Code Web as the execution host —
  primarily to get H2 (traces).
- **H2. Full traces/transcripts available to the user** (= C6, called out again
  specifically against Haku). Specifically means **LLM-level request/response
  rollouts** (full prompts, model responses, all API-level detail available) — not
  just the tool-call ledger. Claude-Code-Web-hosted Haku doesn't have an easy way to
  get this today.
- **H3. Sandboxed command execution — network isolated.** The environment the agent
  executes shell/tool commands in must not have unrestricted internet access.
  Minimum bar: domain allowlist. Options the user is already aware of and weighing:
  a generic mitm proxy, "OpenShell", "OpenSandbox".
- **H4. Durable memory across sessions.** The agent must be able to _make_ a memory
  ("remember that X") and _recall_ it in a later session. Stated deliberately in
  terms of the capability rather than the mechanism: a persistent "workspace" is
  simply how OpenClaw happens to implement it, and a harness that achieves recall
  some other way (a database, a git-backed notes repo, a memory service) satisfies
  this equally well. How the chosen mechanism behaves across the harness/execution
  split is C8.
- **W2 (want). Agent execution off the harness container.** Preferably the agent's
  commands run somewhere other than the harness's own pod — its own execution pod —
  rather than harness and shell sharing one container. Conditional on memory (H4)
  still working in that topology; if splitting them breaks recall, the split loses.

### Known blockers / open questions on the sandboxing options (as stated by user)

- **B1.** OpenClaw's current OpenShell **exec plugin is broken** against current
  OpenShell because OpenShell now requires short sandbox names and the plugin
  doesn't comply. **Sourced and confirmed** (no longer "reportedly"): the real fix
  is openclaw/openclaw PR #114177, a **draft** blocked because the new naming
  scheme has no legacy lookup or migration and would orphan existing sandboxes in
  remote-mode deployments; issue
  [#115057](https://github.com/openclaw/openclaw/issues/115057) is a thinner
  duplicate, closed as `needs-info`/`needs-product-decision`, and is not where the
  fix lives. Ducktape already ships a CLI-boundary shim
  (`cluster/k8s/agents/openclaw/gateway/openshell-cli-compat.yaml`) that tracks
  that same PR. Detail in `findings.md`.
- **B2 — an option, not a blocker.** Nvidia's "NemoClaw" setup reportedly runs the
  _entire_ agent harness (not just the exec step) _inside_ OpenShell. This is an
  **acceptable outcome**, merely the less preferred one: a functioning harness that
  sandboxes everything together and can still send PRs and keep memories satisfies
  the hard requirements. Upside: harness never needs inference/channel
  (e.g. Telegram) credentials outside the sandbox. Open question: does this actually
  isolate the agent from its own harness process, or does the unsandboxed-equivalent
  harness simply move inside the same boundary with no isolation _within_ it? Is there
  a second isolation layer (OpenClaw's own Landlock-based sandboxing, or a
  Docker-in-Docker sidecar) that separates agent-issued commands from the harness
  process itself?
- **B3 — confirmed, and worse than "assumes".** OpenClaw's persistence model does
  assume harness and command-execution share a machine, and the seams show. The
  mirror sync fires when `exec` _returns_, which is on yield, so a command that
  outruns `yieldMs` gets snapshotted mid-write and the next `exec` restores that
  partial state over it — the vanishing-clone incident of 2026-07-28
  (`cluster/docs/lessons_learned/2026_07_28_openclaw_sandbox_clone_loss_and_ssh_orphan.md`).
  Read as a signal about maturity rather than as one bug: the "execute elsewhere"
  path has a sync that is only correct when nothing is still running, gateway-side
  file tools that had to be disabled outright (#3556), and a sandbox that wedged
  itself within seven minutes of first use. **This is the main evidence that
  OpenClaw's split-execution model is under-tested**, and the main argument for
  preferring a harness where the split is native — or for accepting B2's
  everything-in-one-sandbox topology, where the seam does not exist.
- **B4 — resolved, both halves.** Declarative provisioning is better than feared.
  OpenShell _does_ have an operator, though not a first-party one: the
  `openshell.lenshq.io` CRDs come from a **Lens-authored** chart wrapping NVIDIA's
  OpenShell and delegating runtime pods to `kubernetes-sigs/agent-sandbox`;
  upstream NVIDIA has only an open design discussion
  ([NVIDIA/OpenShell#1719](https://github.com/NVIDIA/OpenShell/issues/1719)).
  kagent's `AgentHarness` CRD **is** the generic kind: it targets
  `openshell`/`openclaw`/`nemoclaw` with declarative `allowedDomains`, distinct
  from the `Agent` BYO/A2A path, so it can host a third-party harness rather than
  only kagent's own framework. Both are installed in this cluster today. Detail
  and citations in `findings.md`.

## Agent: knowledge-garden maintainer

One of the personal-data agents' main jobs: maintain a "knowledge garden" — tasks,
plans, finances, general notes — kept current.

- **K1. Git-backed.** The garden's content should live in a git repo (plain files),
  not a proprietary DB, so agents can read/write it with normal git tooling and get
  history/diffs for free.
- **K2. Existing, not bespoke, garden software.** Current Haku approach (garden living
  in an ad hoc "haku-state" repo) feels brittle and overly custom; prefer adopting an
  existing knowledge-garden tool rather than continuing to hand-roll one.
- **K3. Supports genuinely dynamic embedded components,** not just static markdown —
  e.g. a live chart of actual spending pulled from Plaid, rendered inside a garden
  page — and the agent itself should be able to author/update both the surrounding
  note content and the dynamic component's data/definition via normal file writes.
- **K4. Decent self-hosted web UI** for browsing the garden (rendering, ideally
  backlinks/graph/tags — standard digital-garden features), reachable per C1.
- **K5. Open modeling question (not yet a hard requirement):** where's the boundary
  between "agent definition/memory" (stuff a harness like OpenClaw puts in its own
  workspace, awkward to track in git) and "the knowledge garden proper" (K1–K4)? This
  needs its own think-through and may influence harness choice (whether the harness's
  workspace model can itself be git-native). Tracked as an open question, not a hard
  requirement, for now.

## Non-goals / explicitly deprioritized

- Not asking for a fully automated/pre-baked product recommendation without
  hands-on validation — user wants to actually draft k8s manifests to pressure-test
  any shortlisted stack before committing (see C7).
