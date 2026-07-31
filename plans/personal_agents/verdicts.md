# Verdicts — what we evaluated, what we concluded, and why

The point of this file is to stop us re-litigating settled questions. Every row is
something that was considered and closed, with the reason it closed and where the
working lives. If you find yourself about to propose one of these, read the reason
first — and if the reason no longer holds, say so explicitly rather than
re-deriving from scratch.

Two kinds of evidence, and they are not interchangeable:

- **Measured** — an `F` number. Someone ran it and recorded what happened.
  [findings/](findings/README.md).
- **Sourced** — read out of upstream code, CRD schemas read live off the cluster,
  or vendor documentation. [survey/](survey/README.md) carries the citations.

Where the two disagree the measurement wins, but most rows here were never
measured because the sourced answer was decisive enough to stop.

## Harnesses

| Option                                           | Verdict                                | Why                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------ | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **OpenClaw**                                     | **Adopted**                            | Already wired, emits per-turn transcripts, routes through the Langfuse-traced LiteLLM lane, and handles the single-oversized-tool-result case (C9). Its weaknesses are known and worked around rather than disqualifying.                                                              |
| **Hermes** (Nous Research)                       | Ruled out for now                      | Gateway process is genuinely pod-friendly — non-root, refuses a root gateway, single PVC-shaped volume. But of six exec backends only `ssh`/`modal`/`daytona` are k8s-viable; `docker` wants `/var/run/docker.sock` bind-mounted and `singularity` is HPC. No first-party k8s tooling. |
| **QwenPaw** (Alibaba AgentScope)                 | Ruled out as a self-sandboxing harness | Its isolation story does not survive reading the implementation — see the sandboxing table. Not ruled out as a _harness_; it was never evaluated on merit because the reason to reach for it was the sandbox.                                                                          |
| **OpenHarness/Ohmo, Letta, RustFox, Moltworker** | Ruled out                              | Surveyed against C2 (must run in k8s); none has a k8s story, and none had a property worth the switching cost. Moltworker is cloud-only by construction.                                                                                                                               |
| **kagent's own `Agent`/`SandboxAgent` runtime**  | Ruled out, the hard way                | No client-side tool-output budget; noisy `kubectl` output killed sessions. Already retired from this repo (`cluster/archive/2026_07_kagent/`). Note this failure is specific to kagent's runtime and does **not** transfer to its `AgentHarness` CRD, which bakes in no runtime.       |

**The generalisation worth keeping:** no harness in the survey ships k8s-native
tooling, so hand-rolling Deployment/Service/PVC is unavoidable whichever you pick.
That means harness choice and isolation choice are **mostly separable**, and a
harness should be chosen on tool-output robustness, rollout capture, and workspace
fit — not on the strength of its own built-in sandbox.

## Isolation and sandboxing

| Option                                                    | Verdict                                   | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Plain Deployment + NetworkPolicy + intercepting proxy** | **Adopted**                               | What `public-coder-agent` runs. The NetworkPolicy is the unbypassable fence; the proxy is where policy and credentials live.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| **OpenClaw operator (`OpenClawInstance`)**                | **Ruled out, decisive**                   | Its NetworkPolicy always emits an unconditional 443-anywhere egress rule, and NetworkPolicies are unions of allows — so it cannot be subtracted. Egress confinement is impossible under it, full stop. Measured: **F3**. This is why the agent is a plain Deployment.                                                                                                                                                                                                                                                                                                                           |
| **NemoClaw**                                              | **Ruled out on both counts**              | (1) It runs the _entire_ harness inside **one shared** Landlock+seccomp+netns perimeter, explicitly documented as "not layered or nested sandboxing" — so it gives credential redaction, not harness/agent isolation, and does not answer W2. (2) It is a single-host installer (`curl \| bash` → systemd user service + plain Docker container), not a cluster artifact. Its own docs describe the k8s "remote driver" as degraded. The only k8s wrapper is a community repo running a **privileged** pod with a Docker-socket hostPath.                                                       |
| **QwenPaw's built-in sandbox**                            | Ruled out as a sole layer                 | It probes Bubblewrap first and falls back to Landlock — and Landlock is the mode that actually engages in a stock unprivileged pod. In that mode: the filesystem denylist is real but **does not cover QwenPaw's own credential directory**, so a plain shell command can read the harness's secrets; network restriction is zero by default (`network_allow=["*"]`, and the ABI v4 port-gating needs kernel 6.7+); and there is no process isolation at all — shared `/proc`, shared PID namespace.                                                                                            |
| **Whole harness inside an OpenShell sandbox**             | Blocked on a milestone, not a design flaw | `OpenShellSandbox` exposes `image`, `policy`, `providers`, `volumes` with retention, and `runtimeClassName` — everything needed. But the CRD states plainly that **entrypoint** "arrives in later milestones", so you can set the image but not what it runs. Expressible only with a purpose-built image whose default entrypoint is already right.                                                                                                                                                                                                                                            |
| **OpenShell as the exec backend**                         | **Ruled out for unattended use**          | Runs (**F13**), but wedges silently and permanently from something ordinary we never identified, and its egress policy is per-process rather than per-pod (**F2**) — a `kubectl exec`-launched process bypasses it. The only deployment on it is the main `openclaw` gateway, which the operator reports as unused and believed broken: three sandboxes alive 39h–2d11h with zero restarts and a gateway log carrying nothing but config polling, which is the orphan shape **F1** describes. So it is installed and running, **not in service** — do not read it as a working counter-example. |
| **Landlock as a substitute for a domain proxy**           | Category error, worth stating once        | They answer different questions — "what can this process touch" vs "what domains can anything here reach" — and Landlock's network scoping is **port-number-only, never FQDN**. You need both; neither replaces the other.                                                                                                                                                                                                                                                                                                                                                                      |

## Control planes

| Option                                                                           | Verdict                     | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **kagent `AgentHarness` CRD**                                                    | **Right shape, wrong cost** | `backend: openshell\|openclaw\|nemoclaw`, declarative `network.allowedDomains`, channel bindings, and it writes OpenClaw's config inside the VM and starts the gateway — the NemoClaw shape expressed declaratively, which is the thing NemoClaw itself cannot give you. The obstacle is purely operational: only the CRDs survived kagent's retirement, the namespace runs no pods, and a CRD with no controller is inert YAML. Using it means reinstating a pre-1.0 control plane. |
| **Composing `OpenClawInstance` + `OpenShellSandbox`/`OpenShellPolicy` directly** | Superseded by F3            | Was the recommended alternative to reinstating kagent. Then F3 ruled out the OpenClaw operator entirely, so the composition lost one of its two halves.                                                                                                                                                                                                                                                                                                                              |

**Loose end:** the orphaned `kagent.dev` CRDs are still installed with no
controller. They should be cleaned up or consciously kept; right now they are
neither.

## Credentials

| Option                                               | Verdict                | Why                                                                                                                                                                                                                                                                                                                            |
| ---------------------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **iron-proxy `replace` mode**                        | **Adopted**            | The agent holds a placeholder; the proxy substitutes on scoped hosts. Chosen over `inject` deliberately: it leaves the agent aware a credential exists while unable to read it. Measured end to end, including a 3.9 MB `git push` — **F15**, **F16**.                                                                         |
| **Our own mitmproxy addon**                          | Ruled out, narrowly    | It worked and was smaller. Lost on two things: iron-proxy expresses the whole policy as YAML from a maintained project rather than forty lines of ours, and it handles the base64 `Basic` shape git-over-HTTPS uses, which our addon did not.                                                                                  |
| **Token in the agent's environment**                 | Ruled out              | It is also simply _broken_ under OpenClaw: the exec tool strips `GITHUB_TOKEN` by exact-name denylist, so the agent could not authenticate for weeks while the variable was plainly set in the pod (**F7**). The fix that supersedes it is not a rename — it is that the credential need not be in the agent at all (**F10**). |
| **Token in a file the agent reads**                  | Ruled out              | Session memory persists to the PVC, so a credential in context becomes a stored credential, exfiltratable by prompt injection from any public repo the agent clones.                                                                                                                                                           |
| **Bespoke Google OAuth proxy**                       | Dominated              | iron-proxy's `oauth_token` transform already performs the refresh-token exchange, caches, and refreshes in process. See [personal_data_agent.md](personal_data_agent.md).                                                                                                                                                      |
| **ESO-mirroring a live access token into the agent** | Ruled out for new work | This is what `google-access-token-eso.yaml` does today for `claude-sandbox` and `haku-sandbox`. It is the exposure the proxy design exists to remove.                                                                                                                                                                          |

**Two anti-patterns with teeth**, both learned by being bitten:

- `require: true` on an iron-proxy `secrets` transform is evaluated against the
  header-less `CONNECT` and rejects **every** HTTPS request with 403 in
  explicit-proxy mode (**F15**).
- Scoping allowlist rules by method or path blocks their own `CONNECT` preflight
  unless you pair each with a `methods: ["CONNECT"]` rule — the host becomes
  unreachable while the config looks correct (**F15**).

## Egress and TLS

| Option                                  | Verdict     | Why                                                                                                                                                                                                                                                                                                           |
| --------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Proxy allowlist + NetworkPolicy**     | **Adopted** | Works without Cilium FQDN rules or the shared mitmproxy (**F4**). `HTTP_PROXY` is convenience; the NetworkPolicy is enforcement — an agent that unsets it loses its only route out rather than gaining freedom.                                                                                               |
| **CA by per-tool environment variable** | Ruled out   | `GIT_SSL_CAINFO` is stripped from the exec environment by name, and git links GnuTLS so neither `SSL_CERT_FILE` nor `CURL_CA_BUNDLE` reaches it (**F17**). Put the CA in the **system trust store** instead; it covers git, curl and Python at once, and removes the variable name the denylist was catching. |
| **Self-generated mitmproxy CA**         | Ruled out   | It re-keys on restart, and the agent responded to the broken trust chain by turning TLS verification off and carrying on silently (**F8**). cert-manager owns the keypair now.                                                                                                                                |

**The `NODE_EXTRA_CA_CERTS` trap** is worth its own line because it costs a
deploy cycle every time: Node ignores a **missing** file silently, falls back to
its bundled roots, and fails with `SELF_SIGNED_CERT_IN_CHAIN` — which reads like a
trust problem rather than the typo it is (**F18**).

## Still open — shapes worth costing, not settled

**A Kubernetes `WorkerProvider` is the most promising unexplored path to W2**
(execution off the harness container), and it is the strongest reason not to treat
this research as finished.

First, what nothing here currently runs. `public-coder-agent` is
`sandbox.mode: "off"` — no execution split at all, exec in the harness container.
The main `openclaw` gateway is configured for the OpenShell mirror but is unused
and believed broken. So **we have no working split-execution deployment of any
kind**, and W2 is unattempted rather than tried and failed.

That makes the comparison below a code comparison between two upstream
mechanisms, not a report on something running here.

OpenClaw already implements git-backed workspace synchronisation for its **cloud
workers** feature, and it is markedly better engineered than the OpenShell mirror.
Outbound it ships a git pack and the worker reconstructs a pinned shallow
repo. Inbound, results arrive as a git ref staged under
`refs/openclaw/worker-results/` _before_ being applied, so it stays recoverable if
the gateway dies mid-apply, and the apply is a three-way merge against the
dispatch-time manifest — cloud-only changes applied, local-only left alone,
conflicts resolved keep-local with the staged ref named for inspection. Compare
the OpenShell mirror: tar upload, whole-tree destructive replace, no merge base,
no conflict handling, no staging ref, and a sync that fires on yield.

That contrast is the strongest available evidence for "the execute-elsewhere path
is under-tested" — it is under-tested **in the OpenShell plugin**, while the
cloud-worker path got the careful design.

Two properties make it more than an implementation detail:

- **Credential placement is inverted, in the good direction.** Workspace git
  history is authored on the box credential-free; the gateway adopts commits and
  owns push/PR, with no standing model, forge, or cloud credentials on the box.
  That is strictly better than what we run now, and it makes opening a PR a
  first-class supported path rather than something the agent improvises with `gh`.
- **The provider is pluggable.** `WorkerProvider` is a public plugin-SDK type with
  `registerWorkerProvider`. The bundled `crabbox` provider leases cloud VMs, which
  is wrong for personal-data agents, but the interface is not cloud-specific.

Unknowns to resolve before committing: how much of the provider contract is
VM-shaped, whether a pod can satisfy the setup/lease lifecycle, and whether
sandboxing would then have to come from the pod spec rather than from OpenShell.
