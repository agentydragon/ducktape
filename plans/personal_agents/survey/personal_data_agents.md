# Agent: personal-data agent(s) — B1-B4 sandboxing/harness questions

- **B1 (name-length bug)**: **closed.** Upstream PR #114177 is still a blocked
  draft, but ducktape's shim (`openshell-cli-compat.yaml`, mounted at
  `/opt/openclaw/bin/openshell-compat` and wired as the OpenShell plugin's
  `command`) works: OpenClaw now executes inside OpenShell in mirror mode against
  the live agent-scoped sandbox. B1 is no longer a blocker on anything downstream
  — it is a permanent-until-upstream-lands shim, not a risk under test.
- **B2 (NemoClaw isolation model)**: **confirmed, and the user's suspicion was
  correct.** Per NVIDIA's own architecture docs
  (`docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/architecture`), NemoClaw
  runs the _entire_ OpenClaw harness (including LLM/channel integration) inside one
  OpenShell sandbox, under **one shared** Landlock+seccomp+netns perimeter —
  explicitly stated as "not layered or nested sandboxing." There is **no second
  isolation layer** separating the harness process from agent-issued exec calls.
  Credential safety instead comes from network-layer indirection: an L7 proxy at the
  OpenShell/gateway boundary substitutes real provider secrets into outbound requests,
  so the harness process never _holds_ live keys — but a compromised harness can still
  act as the agent within that one shared boundary. **Conclusion: NemoClaw's design
  achieves credential redaction, not harness/agent isolation** — it does not answer
  the user's B2 question in the affirmative. If harness/agent isolation is a hard
  requirement, this specific product doesn't provide it and a genuinely separate
  exec-only sandbox (what ducktape's current OpenClaw+OpenShell wiring already does —
  gateway/credentials stay on the Gateway host, only the executed command runs
  remotely) is the safer existing shape to build on.
- **B2a (is NemoClaw even k8s-deployable?): no, not natively — this cluster's own
  OpenClaw+OpenShell wiring is the better k8s starting point regardless.** NemoClaw is
  a single-host installer, not a cluster artifact: `curl ... nemoclaw.sh | bash`
  installs a Node-based CLI, which then installs OpenShell's gateway as a **systemd
  user service** and runs the sandbox as a **plain Docker container** on the same
  host — confirmed via NVIDIA's own docs
  (`docs.nvidia.com/nemoclaw/user-guide/openclaw/home`,
  `docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/commands`) and the
  [NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw) repo (`install.sh`,
  `Dockerfile`, no Helm chart or manifests). The CLI's own docs list a Kubernetes
  "remote driver" as an option but describe it as degraded — several lifecycle
  commands (`stop`, gateway `recover`) are documented as failing under it because
  they need direct local-container control. No first-party Helm chart/operator
  exists; the closest thing is a community repo (`kubespark/nemoclaw-k8s`,
  explicitly "not officially supported by NVIDIA") that wraps NemoClaw's sandbox
  container in a **privileged** Pod with a Docker-socket hostPath mount (because the
  sandbox itself runs nested containers) — flagged in
  [NVIDIA/NemoClaw#1442](https://github.com/NVIDIA/NemoClaw) as shipping without
  security warnings. Confirms the mental model: NemoClaw is a management/installer
  layer wiring up OpenShell + an agent harness with NVIDIA config/scripts, not its
  own sandboxing technology — and it's a family name, explicitly supporting three
  harnesses (**OpenClaw** default, **Hermes**, **LangChain Deep Agents Code**), not
  an OpenClaw-specific product. OpenShell itself (the thing NemoClaw wraps) does
  have a real k8s path independent of NemoClaw
  (`helm install openshell oci://ghcr.io/nvidia/openshell/helm-chart` per
  [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell)) — this is presumably the
  same operator ducktape already runs (`cluster/k8s/agents/openshell/operator/`),
  independent of NemoClaw's single-host installer path. **Bottom line: don't chase
  NemoClaw for this cluster** — it doesn't productize anything k8s-native beyond
  what's already running, and getting it onto k8s at all means either a privileged,
  security-compromised community wrapper or hand-rolling the same OpenShell-operator
  wiring ducktape already has directly.
- **B3 (workspace model)**: resolved above under C8. Mirror mode is running and
  working; its one sharp edge is the background-`exec` retention bug, which has a
  `/tmp`-based workaround already documented in-repo. The shared-vs-private
  question resolves to `/sandbox` (shared, mirrored, persistent) vs `/tmp`
  (disposable, pod-lifetime), with git as the durable propagation mechanism.
- **B4 (declarative provisioning maturity)**: resolved above under C7 — OpenClaw's
  operator provenance needs a direct confirmation of source repo; OpenShell's is
  solid via `agent-sandbox`; **kagent's "agent harness" angle does not deliver what
  was hoped** (next section).

### kagent as a possible "agent harness CRD"

Investigated as a candidate for productizing "run an arbitrary harness declaratively."
kagent has **two** relevant CRDs and they answer very differently: `AgentHarness`
(on-target, see below) and `Agent` (not on-target).
Sources: [kagent.dev docs](https://kagent.dev/), [github.com/kagent-dev/kagent](https://github.com/kagent-dev/kagent).

#### `Agent` (BYO mode) — not the tool it sounds like

- kagent's `Agent` CRD has a "BYO" (bring-your-own) mode for arbitrary containers, but
  it **requires the container to speak the A2A protocol** (JSON-RPC/SSE,
  `.well-known/agent.json`) — all published BYO examples wrap agent _frameworks_
  (LangGraph, CrewAI, Google ADK) that already speak or can be adapted to speak A2A.
  There's no adapter for wrapping a raw CLI harness like OpenClaw or Claude Code
  directly; you'd own writing that A2A shim yourself. Not the "point at an arbitrary
  binary, get sandboxing for free" tool it might sound like.
- Sandboxing/egress is not kagent-core's job — it's delegated to two young,
  Solo.io-led adjacent projects: **agentgateway** (policy-driven egress/mTLS for
  agent traffic) and **Agent Substrate** (Bubblewrap/Landlock/seccomp/Firecracker
  sandboxing, Google-originated). Both read as newer additions layered onto kagent by
  its commercial backer, not core-project maturity.
- LiteLLM routing works (`ModelConfig.baseUrl`), and OTel tracing is first-class, but
  the richer Langfuse pipeline goes through agentgateway's OTel collector, and the
  fuller enterprise observability stack is a **paid** Solo.io product.
- This repo tried and retired kagent (`cluster/archive/2026_07_kagent/`) because
  **kagent's own agent runtime** had no tool-output-truncation budget and killed
  sessions on noisy `kubectl` output. That failure belongs to the `Agent`/
  `SandboxAgent` loop — it does not transfer to `AgentHarness` (see below).
- Maturity: active (~3.4k★, tight beta release cadence into late July 2026) but
  pre-1.0, with Solo.io pushing enterprise features somewhat ahead of core OSS
  maturity.

#### `AgentHarness` — on-target

`agentharnesses.kagent.dev` is a **separate CRD, still installed on this cluster**.
Its schema (read live via `kubectl get crd agentharnesses.kagent.dev -o jsonpath=…`):

> "AgentHarnessSpec describes a generic remote execution environment that agents (or
> human operators) can attach to via exec or SSH. An AgentHarness is distinct from a
> SandboxAgent: it has no agent runtime baked in."

Fields that matter here:

- **`backend`** (required) — an enum of exactly `openshell` | `openclaw` |
  `nemoclaw`. Not a generic "any container" escape hatch, but the three backends
  it does support are the three this project cares about.
- **`modelConfigRef`** — "when set with backend `openclaw` or `nemoclaw`, the
  controller registers the gateway provider and, after the harness is Ready,
  **writes OpenClaw config inside the VM (`~/.openclaw/openclaw.json`) and starts
  the gateway**." That is the NemoClaw shape (whole harness inside the sandbox)
  expressed declaratively as a k8s object — the thing B2a concluded NemoClaw
  itself cannot give you.
- **`network.allowedDomains`** — "a list of DNS names the harness may reach",
  declarative, at the harness level.
- **`channels`** — Telegram/Slack bindings (bot/app tokens via `valueFrom`
  Secret refs, `channelAccess: allowlist|open|disabled`, allowlisted channel
  lists, `allowedUserIDs` for Telegram), "only supported when backend is
  `openclaw` or `nemoclaw`".
- **`image`** — "backends `openclaw` and `nemoclaw` pin the image to the NemoClaw
  sandbox base; `openshell` uses `spec.image` when set."

For hosting OpenClaw declaratively with egress control, this is squarely on-target
— closer to a packaged answer than anything else surveyed.

**C9 does not apply to it.** `AgentHarness` has **"no agent runtime baked in"**: with
`backend: openclaw` the model conversation is driven by OpenClaw's loop inside the
harness, so tool results pass through OpenClaw's `session-tool-result-guard`
truncation (see "public coder" above), not through anything kagent wrote. The
output budget belongs to the harness, not the control plane — so kagent's own
truncation history is irrelevant here.

**The obstacle is purely operational**: only the CRDs survive kagent's retirement.
The `kagent` namespace still exists (80d) but runs **no pods**, and there is no
kagent HelmRelease (only `openclaw-operator` and `openshell` are installed). A CRD
with no controller is inert YAML, so using `AgentHarness` means reinstating
kagent's control plane — another pre-1.0, Solo.io-backed operator to run.

**Verdict**: right shape; the reason to hesitate is deployment weight, not a
defect. Three options: (a) reinstall kagent's controller for `AgentHarness` only,
ignoring its `Agent`/`SandboxAgent` runtime entirely; (b) treat `AgentHarness` as a
design reference and keep composing `OpenClawInstance` +
`OpenShellSandbox`/`OpenShellPolicy` directly, which covers the same ground with
controllers already running; (c) defer until the B1 shim verification lands, since
that determines how much of (b) actually works today. **Separately: the orphaned
`kagent.dev` CRDs should either be cleaned up or consciously kept** — right now
they are neither.

### Alternative harnesses to OpenClaw — landscape survey, evaluated against C2 (k8s)

Surveyed the niche of chat-channel-connected, tool-executing personal-assistant
harnesses (distinct from generic coding-agent-for-a-team platforms, which
`docs/self_hosted_coding_agent_platforms.md` already covers) to see if anything
sidesteps the OpenClaw+OpenShell pain points above.

| Harness                                                                                                               | Maintained by   | Unsandboxed local          | Docker                     | OpenShell                                        | Other                                                                                               |
| --------------------------------------------------------------------------------------------------------------------- | --------------- | -------------------------- | -------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| **OpenClaw**                                                                                                          | community       | Yes                        | Yes                        | Yes (current default here)                       | —                                                                                                   |
| **Hermes** (Nous Research, real independent project — not NVIDIA — NemoClaw just added it as a 2nd supported harness) | active          | Yes (`local`)              | Yes (persistent container) | Only via NemoClaw's external wrapper, not native | ssh, Singularity (HPC), Modal/Daytona (cloud); no gVisor/Kata/Landlock (open feature requests only) |
| OpenHarness/Ohmo (HKUDS, 15k★)                                                                                        | active          | Yes (undocumented default) | not documented             | No                                               | not documented                                                                                      |
| Letta (formerly MemGPT)                                                                                               | active, large   | Yes                        | not primary                | No                                               | E2B cloud sandbox                                                                                   |
| **QwenPaw** (Alibaba AgentScope, 30k★)                                                                                | active          | No — sandboxed by default  | not used                   | No                                               | **Bubblewrap→Landlock fallback (Linux), Seatbelt (macOS), AppContainer (Windows)**                  |
| RustFox (6★)                                                                                                          | tiny            | directory-scoped only      | not documented             | No                                               | —                                                                                                   |
| Moltworker (Cloudflare PoC)                                                                                           | Cloudflare, PoC | No (cloud-only)            | N/A                        | No                                               | Cloudflare Sandbox SDK                                                                              |

**Checked the two most promising ones (Hermes, QwenPaw) against the hard C2
requirement (must run in k8s) — neither has first-party k8s tooling, but the gap
is not equally bad:**

- **Hermes**: no Helm chart/operator/k8s manifests in
  [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
  (only `Dockerfile` + `docker-compose.yml`), no k8s page in its docs site. The
  **gateway process itself is pod-friendly** — runs non-root (UID 10000 by
  default, root gateway actively refused unless `HERMES_ALLOW_ROOT_GATEWAY=1`),
  single mounted volume maps directly to a PVC, no privileged/hostPath
  requirement; code comments even discuss cgroup-aware memory limits "in a
  Docker/k8s container." But of its 6 exec backends, only `ssh`/`modal`/`daytona`
  (all external) are k8s-viable: the `docker` backend docs explicitly say to
  bind-mount `/var/run/docker.sock` — the same DinD/socket-mount wall hit
  elsewhere in this doc — and `singularity` is HPC-specific, not k8s at all.
  Third-party Helm charts surfaced in search (`jyje/hermes-agent`,
  `ultraworkers/hermes-agent-helm-chart`) are unofficial and unverified.
- **QwenPaw**: also no Helm/operator/k8s manifests in
  [github.com/agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw)
  (only `deploy/Dockerfile` + `docker-compose.yml`). It probes **bubblewrap
  first, falls back to Landlock** if unprivileged user namespaces aren't
  available — which most stock k8s pods won't have, same as Docker.
  **Per the sandbox implementation itself
  (`src/qwenpaw/sandbox/linux_sandbox.py`,
  `src/qwenpaw/security/tool_guard/guardians/file_guardian.py`,
  `src/qwenpaw/governance/resource_governor.py` / `policy.py`) — the docs
  oversell it — the Landlock fallback has three real gaps:**
  - **Filesystem**: real denylist, blocking well-known credential dirs
    (`~/.ssh`, `~/.aws`, `~/.kube`, `~/.gitconfig`, etc. —
    `DEFAULT_SANDBOX_DENY_PATHS` in `policy.py`) — but it does **not** cover
    QwenPaw's own credential directory (`~/.qwenpaw.secret`). That's only
    excluded by a separate "Tool Guard" layer doing semantic checks on
    _structured_ tool calls (`read_file` etc.), not a kernel-enforced rule, and
    not applied to raw shell exec. **A plain shell command run under the
    Landlock fallback can read the harness's own secrets** — directly
    undercutting "the agent can't touch the harness's credentials" for this
    specific path.
  - **Network**: confirmed zero restriction by default. The code implements
    Landlock ABI v4 port-gating (`LANDLOCK_ACCESS_NET_BIND_TCP/CONNECT_TCP`)
    but ships with `network_allow=["*"]`, with the authors' own comment noting
    ABI v4 (kernel 6.7+) "is not yet widely available in production" — so the
    net hooks aren't even engaged out of the box. And regardless of whether
    it's engaged: Landlock's network scoping is port-number-only, never
    domain/FQDN — confirmed no L7/domain layer exists in QwenPaw at all
    (matching the general Landlock-vs-domain-proxy distinction: they answer
    different questions — "what can this process touch" vs. "what domains can
    any process here reach" — and you need both, not one instead of the
    other, for a personal-data agent).
  - **Process isolation**: none under the Landlock fallback. It shares the
    harness's real `/proc` and PID namespace (no `unshare`, no ptrace
    mediation) — only the _preferred_ Bubblewrap path gets real PID-namespace
    isolation (`--unshare-pid --unshare-user`), which is exactly the privilege
    class typically unavailable in the stock k8s pods that trigger the
    Landlock fallback in the first place.

  **Verdict**: the Landlock fallback — the mode that actually engages in an
  ordinary unprivileged pod — gives a real-but-incomplete filesystem denylist
  (with a gap on its own secrets) and **no** network or process isolation by
  default. So QwenPaw's built-in isolation can't be relied on as the _only_
  layer. This is not an argument against QwenPaw as a harness: its sandboxing and
  OpenShell aren't competing alternatives, they compose (next section).

### Can a whole harness be run _under_ OpenShell, declaratively?

This reframes the survey above: a harness's own sandboxing story matters much less
if the harness itself is wrapped. **Partially yes — the CRD supports it, with one
blocking gap.** `OpenShellSandbox` (`openshell.lenshq.io/v1alpha1`, schema read
live) exposes:

- **`image`** — "container image the sandbox runs. Empty defers to the gateway
  default." So a QwenPaw or Hermes image can be named directly.
- **`policy` / `policyRef`** — the full L7 network + filesystem + landlock +
  process policy described under C5, applied at creation.
- **`providers`** — gateway credential providers (e.g. the existing
  `agentydragon-github` `OpenShellProvider`) attached by name; **converged in
  place** on a running sandbox, so attaching/detaching doesn't recreate it.
- **`volumes` + `volumeRetention`** — "persistent volumes provisioned and owned by
  this sandbox… Because the PVC is anchored to this resource — not the disposable
  gateway sandbox — its data survives the delete+recreate the gateway requires to
  change an otherwise-immutable field." **This is a direct answer to H4/C8**: it is
  the operator's designed escape from the immutable-field-recreate problem.
- **`runtimeClassName`** — `gvisor`, `kata`, etc., so a second isolation layer under
  the sandbox is one field.
- **`environment`**, `resources`, `gpu`/`gpuCount`, `labels`/`annotations`,
  `workspace`.

**The blocking gap**: the CRD description states plainly that "gateway selection,
**entrypoint**, and TTL/cleanup arrive in later milestones." Without an entrypoint
field you can set the image but not what it runs, so "run Hermes/QwenPaw as a
long-lived process under OpenShell" is **not** expressible today unless the image's
own default entrypoint already does the right thing (i.e. you build a purpose-built
image). That is a real constraint but a much smaller one than "no declarative path
exists" — and it's a milestone gap, not a design objection.

Two further **immutability gotchas** to design around, both from the CRD text:
`workspace` is immutable because the gateway names objects `{workspace}--{name}`
(changing it orphans rather than moves — the same naming-scheme hazard as B1), and
`labels`/`annotations`/`resources`/`runtimeClassName`/`logLevel` changes all
recreate the sandbox.

**Net**: no harness in this survey ships k8s-native tooling out of the box —
hand-rolling Deployment/Service/PVC manifests is unavoidable regardless of choice.
But the more useful conclusion is that **harness choice and isolation choice are
mostly separable here**: OpenShell can in principle wrap an arbitrary image with
L7 egress policy, credential substitution, provider attachment, persistent volumes,
and a gVisor/Kata runtime class, all declaratively. So a harness should be picked
on C9 (tool-output robustness), rollout capture, and workspace fit — not on the
strength of its own built-in sandbox. On those grounds OpenClaw remains the
front-runner, mostly because it is already wired, already emits per-turn
transcripts, and already routes through a Langfuse-traced LiteLLM lane.
