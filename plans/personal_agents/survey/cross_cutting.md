# Cross-cutting

### C1 — web UI reachable on the go

Reuse: `haku/console/` is a FastAPI+Postgres "operator console" with an embedded React
UI (`haku_ui_embed.tsx`) already serving as Haku's approval-queue + tool-call-history
front end (`haku/console/frontend/haku_ui_embed.tsx`; ledger model in
`haku/shared/haku/console/tool_calls.py:72-89`). The retired OpenClaw gateway shipped
its own custom UI (see <../../../cluster/archive/2026_08_openclaw_namespace_retirement.md>).
kagent ships a built-in
Next.js+Nginx chat UI, but kagent itself is retired here (see kagent section below). For
the public-coder function specifically, `docs/self_hosted_coding_agent_platforms.md`
already ranks web-UI options — no new research needed there.

### C2 — runs in k8s

Already the norm; no gap.

### C3/C4 — multi-provider LLM routing via LiteLLM + Langfuse

**Reuse, with known rough edges, not a clean win.** Already wired
(`cluster/k8s/litellm/app/test_litellm_config.py`): Ollama, z.ai/GLM, plain Anthropic,
Groq, Gemini, and **two** Codex/ChatGPT-subscription paths:

1. Native LiteLLM `chatgpt/*` provider — OAuth device-code flow, tokens cached on a
   PVC seeded from `litellm-chatgpt-auth-seed` (`test_litellm_config.py:70-75`;
   `chatgpt-deployment.yaml:116`). **Known bugs, still open as of this research:**
   - Usable only via streaming (`/v1/responses`) due to unfixed upstream
     [BerriAI/litellm#25429](https://github.com/BerriAI/litellm/issues/25429).
   - Cloudflare 403/challenge against `chatgpt.com/backend-api/codex/chat/completions`
     even with valid tokens, reported in
     [BerriAI/litellm#27175](https://github.com/BerriAI/litellm/issues/27175) (opened
     May 2026, still open, no maintainer response as of research date) — traced to a
     missing Cookie header, i.e. bearer-token-only auth isn't always sufficient against
     Cloudflare's edge policy.
   - **Login/renewal is handled**, unlike the wider ecosystem (where community
     proxies generally require re-running interactive `codex login` on expiry).
     `litellm-chatgpt` mounts a dedicated PVC at `CHATGPT_TOKEN_DIR=/data/chatgpt`
     and **LiteLLM rotates the live OAuth token on that PVC**; the SOPS
     `litellm-chatgpt-auth-seed` is disaster-recovery bootstrap only, and the init
     container preserves a live token over the seed
     (`grep -q '"refresh_token"' … || cp /seed/auth.json`). The Deployment is
     pinned to `replicas: 1` + `strategy: Recreate` so "rollouts never overlap
     token writers" (`cluster/k8s/litellm/app/chatgpt-deployment.yaml`). The
     CLIProxyAPI lane refreshes its ChatGPT/Codex OAuth session independently.
     **The operational constraint is single-writer discipline, not interactive
     re-login.**
   - LiteLLM 1.82.7/1.82.8 had a credential-stealing supply-chain incident (fixed in
     1.83.0+) — worth remembering given this proxy holds OAuth session tokens.
2. **CLIProxyAPI** (`cli-proxy-api` in-cluster service) — correctly translates
   `function_call`→`tool_use`, exposed as `anthropic/`-shaped `codex-gpt-*` model
   entries (`test_litellm_config.py:145-159`). This is the **more robust of the two
   paths** and should likely be the default rather than the native `chatgpt/`
   provider, given the open bugs above.

Community consensus
([BerriAI/litellm discussion #26010](https://github.com/BerriAI/litellm/discussions/26010))
is explicit that ChatGPT-subscription auth through LiteLLM is a workaround, not a
first-class supported path — "can be unstable, potentially causing restart loops in
containerized environments." Everything else (Anthropic, standard OpenAI key,
Bedrock/Vertex/Azure) is mature and well-trodden — Codex subscription auth is
genuinely the one fragile leg, matching the user's own instinct.

**Alternative worth keeping in reserve** if LiteLLM's Codex path proves too flaky:
invoke the actual `codex` CLI headlessly (`codex exec`) as the agent itself, sidestepping
API-shape/auth-proxying problems entirely — [Codex headless docs](https://developers.openai.com/codex/noninteractive).

### C4 — Langfuse

Reuse: `callbacks: ["langfuse_otel", "prometheus"]` already set
(`test_litellm_config.py:299`). **Known gap**: `/v1/responses` calls — i.e. exactly the
Codex/ChatGPT-subscription lane — do **not** produce Langfuse traces, root-caused to
LiteLLM 1.86.3's Responses-to-chat bridge returning before the Responses-specific
logging hook runs (`cluster/debug/2026-06-05-litellm-responses-langfuse-otel.md`,
confirmed still present in 1.87.1/1.88.0-rc.3 per that doc). Haku's second-layer
worker LiteLLM only wires `prometheus` with a `TODO(langfuse)` pending a dedicated
project (`cluster/k8s/x/haku/dispatch/litellm/generate_workers_litellm.py:37-39`).

### C5 — no unrestricted network for personal-data agents

**Largely solved, reuse directly for Haku's own sandbox exec.** mitmproxy-based FQDN
allowlist: cert-manager mints a CA, trust-manager distributes it, Kyverno injects the
CA + proxy env into every sandbox pod
(`cluster/k8s/agents/haku-egress-proxy/README.md:6-9`). `cnp-haku-cloud-api-egress.yaml`
allowlists ~20 named FQDNs with per-line justification comments; the tighter
worker-zone variant (`haku/zones/README.md:31-36`) allows only github.com family +
pypi/npm — explicitly "no Google, no direct LLM provider... no image registries."
Deliberately implementation-neutral ("could swap to Squid").

**OpenClaw is covered too**, by two separate mechanisms:

1. **OpenClaw's former gateway pod** was forced through the shared mitmproxy:
   `security.networkPolicy.additionalEgress` allows exactly three destinations —
   the `agents-mitmproxy` namespace (":8080 — Internet egress is forced through
   the shared mitmproxy"), the OpenShell gateway, and LiteLLM
   (the retired `OpenClawInstance` `security:` block). Same mitmproxy Haku uses;
   not a Haku-only mechanism.
2. **OpenShell's sandboxes do their own L7 interception, and now own their egress
   outright.** The sandbox proxy "auto-detects TLS by peeking the first bytes of
   each connection and terminates it for inspected HTTPS traffic", issuing
   per-sandbox certificates from an **ephemeral CA generated at sandbox startup**.
   Policy rules are keyed on `host` (DNS wildcards like `*.example.com` supported),
   `port`, `path` glob, `protocol` (`rest`/`websocket`/`graphql`/`mcp`/`json-rpc`,
   or omitted for TCP passthrough), plus `access` presets and `rules`/`deny_rules`.
   Enforcement is default-deny; a blocked endpoint surfaces in the TUI for operator
   review and can be merged into the policy as a new durable revision.
   (<https://docs.nvidia.com/openshell/reference/policy-schema>,
   <https://docs.nvidia.com/openshell/sandboxes/policies>.)

   **Gotcha — the two mechanisms are deliberately _not_ chained.** The
   `openshell-sandboxes` namespace is **excluded** from the generic mitmproxy
   injection and the force-proxy Cilium policy, precisely because routing an
   OpenShell sandbox through the generic proxy as well breaks it. The retired
   `openclaw-sandbox` namespace used the generic policy instead. Thus "which proxy
   governs this pod" depended on its namespace; mitmproxy did not cover everything
   under `agents/`.

   The effective sandbox policy is **GitOps-owned**, not inherited from the
   image: `cluster/k8s/agents/openshell/openclaw/policy.yaml` is packaged into an
   `openshell-sandbox-policy` ConfigMap and passed explicitly as `--policy`,
   because the mutable community `openclaw:latest` image ships an
   `/etc/openshell/policy.yaml` that "has historically granted unrelated Claude,
   GitLab, and NVIDIA egress." Base `network_policies: {}` is default-deny;
   **provider v2 composes the attached `agentydragon-github` provider's
   executable- and request-scoped rules into the effective policy at runtime** —
   i.e. W1 credential-proxying is not just a feature that exists, it is the
   mechanism actually in use for GitHub access here.

**And it satisfies W1 (credential proxy) natively**, which no other component here
does: the same TLS termination enables `request_body_credential_rewrite` (rewrites
credential placeholders in JSON/form/text bodies, buffers to 256 KiB),
`websocket_credential_rewrite`, and `credential_signing` (proxy-side AWS SigV4
re-signing with the real credentials, stripping the sandbox's own auth headers).
For inference endpoints the agent only ever sees placeholders. **This is the
sharpest distinction to keep straight**: Haku's mitmproxy = FQDN allowlist only;
OpenShell = FQDN + path/method/protocol + credential substitution.

This is declarative in-cluster, not just a CLI feature: `OpenShellPolicy`
(`openshell.lenshq.io/v1alpha1`) carries typed `filesystem`/`landlock`/`process`
blocks plus an opaque pass-through `networkPolicies` map keyed by rule name (the
CRD deliberately does not mirror the gateway's L7 schema — it delegates validation
to the gateway's own parser at reconcile time). Policies attach via
`OpenShellSandbox.spec.policyRef` or inline `spec.policy`. **Gotcha**: `filesystem`,
`landlock`, and `process` are immutable on a running sandbox — editing an
`OpenShellPolicy` only affects sandboxes created afterwards.

### C6 — full traces/transcripts

**Resolved with the user: the want is LLM-level rollouts, not just the tool-call
ledger — this is a real gap, not already satisfied.** Two different things could be
meant by "traces", and only one is built:

1. **Tool-call sequence/ledger** (what tool ran, args, result, order, timing) — **this
   already exists** via the Haku console's `ToolCallRecord`
   (`haku/shared/haku/console/tool_calls.py:72-89`), with a full "Past tool calls"
   history view (`/tool-calls`, `GET /api/tool-calls`). This is exactly the MCP
   tool-call ledger visible through the `mcp__Haku__*` tools in this very session.
   **Not what's being asked for.**
2. **LLM-level turn traces** (full prompts, full model responses, all API-level
   detail — token usage, stop reasons, etc.) — **this is the actual requirement**,
   and it's **not** currently available for Haku's primary loop, because that loop
   runs as an Anthropic-managed Claude Code Web routine (Runtime A,
   `haku/plans/runtime_options.md`), which doesn't go through LiteLLM/Langfuse and
   has no easy export path. Confirmed: this is a real, unresolved gap for the
   Claude-Code-Web-hosted setup, not a documentation/visibility problem.

**A self-hosted harness gets most of this for free, from two independent
directions.** The gap is real _for Claude-Code-Web-hosted Haku_, but it is not a
gap for the OpenClaw-based shape already running here:

- **OpenClaw records per-turn session transcripts itself.** Session logs land in
  `~/.openclaw/workspace/<job-name>/logs/` as JSONL, one record per turn covering
  the user message, **the system prompt**, the model's response, tool calls the
  model made, and tool results (<https://docs.openclaw.ai/logging>,
  <https://github.com/openclaw/openclaw/blob/main/docs/logging.md>). That is
  LLM-level rollout content, not just a tool ledger. Caveats to verify hands-on:
  (a) matching secret values are **masked before the line is written**, so it's a
  redacted rollout, not a byte-exact wire capture; (b) it lives on the gateway
  PVC (`storage.persistence`, 20Gi `local-path-proxmox`,
  `openclawinstance.yaml`), so retention is a backup question, not a plumbing one.
- **For byte-level detail there are opt-in knobs**, none on by default:
  `OPENCLAW_DEBUG_MODEL_TRANSPORT=1` (request start, fetch response, SDK headers,
  first streaming event, stream completion, transport errors at `info`);
  `OPENCLAW_DEBUG_MODEL_PAYLOAD=summary|tools|full-redacted` (the last being a
  redacted, capped JSON payload snapshot); `OPENCLAW_DEBUG_SSE=events|peek`; and
  `trace` log level adds raw HTTP request/response headers and full payloads.
  Payload bytes / response bytes / latency are logged at `info` regardless.
  Everything with "redacted" in the name is exactly that — if a truly unredacted
  rollout is wanted, the LiteLLM/Langfuse side is the better capture point.
- **The Langfuse `/v1/responses` gap does not apply to the lane actually in use.**
  The live OpenClaw instance routes `codex-gpt-5.6-luna` through LiteLLM as
  `api: anthropic-messages` → CLIProxyAPI, explicitly noting "the `/v1/responses`
  bridge cannot front this lane" (`openclawinstance.yaml`, `models.providers`).
  Anthropic-Messages-shaped calls already trace to Langfuse correctly, so the C4
  bug is a reason to _prefer CLIProxyAPI over the native `chatgpt/` provider_ —
  which this cluster already does — rather than an open blocker on C6.

Still genuinely open: Haku's own loop specifically (Runtime B — files at
`haku/runtime/managed_agent/self_hosted/`, marked `# TEMP(debugging)`, not
production-trusted per `haku.agent.yaml:13-14`; or Runtime C — drafted only, per
`haku/plans/runtime_c_artifacts.md`). But "get LLM-level rollouts at all" is no
longer the hard part; **picking a self-hosted harness is the thing that delivers
it**, and the harness already running here delivers it twice over.

### C7/C8 — declarative provisioning holds up / workspace model

This is where the user's frustration is most validated by research, not resolved by
it:

- **OpenClaw operator**: chart source was `oci://ghcr.io/paperclipinc/charts`,
  chart `openclaw-operator` v0.38.1
  (see <../../../cluster/archive/2026_08_openclaw_namespace_retirement.md>); the CRDs it
  installs are in the **`openclaw.rocks`** API group — `openclawinstances`,
  `openclawclusterdefaults`, `openclawselfconfigs` (verified live via
  `kubectl get crds`). So it is the `openclaw-rocks` operator project, with the
  chart published under the `paperclipinc` GHCR org — one lineage, not two
  competing projects. **Third-party**: there is no operator in the
  `openclaw/openclaw` org itself, so this is a maintenance risk to keep an eye
  on. Release cadence (0.38.1, installed 33h ago) reads active.
- **OpenShell operator — also third-party.**
  Chart is `oci://ghcr.io/lensapp/charts` / `openshell-operator` v0.4.0, CRD API
  group **`openshell.lenshq.io`** (`openshellsandboxes`, `openshellworkspaces`,
  `openshellpolicies`, `openshellproviders`, `openshellproviderprofiles`). That
  is a **Lens-authored operator wrapping NVIDIA's OpenShell**, not an NVIDIA
  first-party artifact — it bundles OpenShell gateway 0.0.90 and delegates its
  runtime pods to `kubernetes-sigs/agent-sandbox`. Upstream NVIDIA has a
  Kubernetes Operator only as an open design discussion
  ([NVIDIA/OpenShell#1719](https://github.com/NVIDIA/OpenShell/issues/1719)),
  which explicitly flags that an operator and the existing gateway would have
  overlapping responsibility for sandbox lifecycle/policy — i.e. the layering the
  Lens chart has already picked a side on. **Version-pinning gotcha, already
  documented in-repo**: the gateway is stateful and its SQLite database has
  applied an incompatible workspace migration, so downgrading below 0.0.90 is a
  one-way door (`helmrelease.yaml` chart comment).
- **Workspace model / harness-exec split (B3/C8)**: OpenClaw's OpenShell plugin
  has two sync modes precisely because harness and exec run on different
  hosts — **mirror** (gateway-local canonical, synced before/after each `exec`,
  per-call sync overhead) and **remote** (seeded once, then local edits are
  invisible until a destructive `sandbox recreate`)
  (`github.com/openclaw/openclaw/blob/main/docs/gateway/openshell.md`). Neither
  gives a live bidirectional filesystem.

  **Running config: `mode: mirror`, `scope: agent`, `workspaceAccess: rw`** —
  verified working end to end. `/sandbox` is the mirrored tree; every session for
  the agent shares one sandbox.

  **Mirror-mode retention bug (confirmed in production, not just theory).** The
  plugin syncs `/sandbox` back **when the initial `exec` call returns** — but
  `exec` returns as soon as the command _yields_, either explicitly
  (`background: true`) or automatically once it outruns `yieldMs`. Work the
  command does after that yield, continuing under the `process` tool, is never
  synced back, and **the next `exec` can overwrite it from the stale
  gateway-local workspace**. The trigger is duration, not intent: any command
  slower than the yield window silently becomes a candidate. Compounding it,
  background sessions are in-memory only and lost on gateway restart, so a
  restart can leave a process still writing remotely with no session to poll.

  **Workaround in place**: put disposable clones, builds, and other long-running
  work under a uniquely named directory in `/tmp`, which is outside the mirrored
  `/sandbox` tree and therefore never clobbered by a local→remote sync. `/tmp` is
  pod-lifetime and shared by concurrent sessions of the agent, so unique paths
  matter and anything that must survive sandbox recreation has to be committed
  and pushed. OpenClaw only accepts tool `workdir` under its managed workspace
  roots, so keep `workdir` under `/sandbox` and `cd` inside the shell command.
  Full note: `cluster/k8s/agents/openshell/openclaw/README.md`.

  **The gateway/sandbox split is one seam, and it is mode-dependent.** Everything
  below is read from OpenClaw at tag `v2026.7.1` and the deployed
  `@openclaw/openshell-sandbox@2026.7.1` npm bundle — the exact artifacts
  `openclaw/Dockerfile` pins — not inferred from behavior. The seam is a single
  optional hook:

  ```js
  sandboxContext.fsBridge =
    backend.createFsBridge?.({ sandbox: sandboxContext }) ?? createSandboxFsBridge({ sandbox: sandboxContext });
  ```

  (`src/agents/sandbox/context.ts:304`). Anything routed through the
  `SandboxFsBridge` follows execution off the gateway; anything importing
  `node:fs` directly is pinned to the gateway permanently. That single line is
  the whole taxonomy.

  **The OpenShell plugin implements `createFsBridge` in _both_ modes**
  (`dist/index.js:880`), so the file tools are never simply "gateway tools":
  - `mode: remote` → `createRemoteShellSandboxFsBridge`: every operation is a
    guarded shell command in the sandbox. Fully remote.
  - `mode: mirror` (what runs today) → the plugin's own `OpenShellFsBridge`,
    which is **write-through, read-local**: `readFile`/`stat` touch only the
    gateway-local mirror, `writeFile` writes locally and then calls
    `syncLocalPathToRemote` for that one file, and `mkdirp`/`remove`/`rename`
    execute **remotely first** and then mirror locally.

  Two corrections follow, both retracting earlier claims in this doc:
  - **Upstream docs are right, not contradictory.** The OpenShell page's "in
    remote mode `exec`, `read`, `write`, `edit`, and `apply_patch` operate
    directly on the remote workspace" is accurate; the sentence is scoped to
    remote mode, and this cluster runs mirror. There is no docs bug and no
    implementation bug to chase, and `remote` mode is not blocked on this.
  - **The `/tmp/ducktape` symptom was an unmounted path, not a wrong machine.**
    The mirror bridge throws `OpenShell mirror bridge requires a local host
path` whenever a target resolves outside a mount, and `/tmp` is not one
    (`/sandbox` and `/agent` are). Structured editing under `/tmp` was never
    reachable, in either mode.

  What survives from the original point is the direction asymmetry:
  gateway→sandbox propagates per write, sandbox→gateway only at the `exec`
  boundary. That is what the retention bug breaks, and it is unchanged.

  **Mirror sync semantics, now read rather than assumed.** This answers what was
  previously recorded as an open question — the transfer is a **whole-tree
  destructive replace, not an mtime/delta sync**:
  - _Pre-`exec`_: `find "$1" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +` wipes
    `/sandbox`, then the staged gateway workspace is pushed with
    `openshell sandbox upload --no-git-ignore`. Staging for upload applies **no**
    exclusions.
  - _Post-`exec`_: `openshell sandbox download` into a temp dir, then
    `replaceDirectoryContents` deletes every top-level entry of the gateway
    workspace and copies the downloaded tree over. Exclusions apply here only:
    `hooks`, `git-hooks`, `.git`.
  - **The exclusions match top-level entry names only.** `replaceDirectoryContents`
    filters the non-recursive `readdir` of each root; the recursive copy beneath
    it (`copyTreeWithoutSymlinks`) applies no exclusions at all. So `.git` is
    protected **only at the workspace root** — a clone at `/sandbox/<repo>/.git`
    is synced like any other directory, in both directions.
  - Symlinks are skipped outright in both directions (`copyTreeWithoutSymlinks`),
    so they never survive a sync.

  Consequences:
  - **A repo cloned into a subdirectory does round-trip, `.git` included.**
    Multi-turn work on a checkout under `/sandbox/<repo>` is supported in
    principle: working tree and history both survive the sync in both
    directions. The root-level `.git` exclusion only matters if the agent
    workspace root is itself a repo.
  - **What breaks multi-turn repo work is the retention bug, not `.git`.** A
    `git clone` large enough to outrun `yieldMs` returns on yield;
    `finalizeExec` then runs `syncWorkspaceFromRemote` **while the clone is
    still writing**, so the gateway captures a partial tree. The next `exec`
    wipes `/sandbox` and re-uploads that partial tree — from the agent's
    perspective the repo it just cloned is truncated or gone.

    **Confirmed against the live transcript, not inferred.** In the 2026-07-28
    session the fingerprint is unmistakable: the clone yields at 19:56:35, the
    poll at 19:57:30 shows `Updating files: 23%…27%` (objects written, checkout
    still running), the next `exec` at 19:57:59 reports
    `fatal: cannot change to 'ducktape': No such file or directory`, and two
    later attempts leave a directory containing **only `.git`** — exactly what a
    snapshot taken between "objects fetched" and "working tree written"
    produces. Full write-up, including the separate SSH failure it gets confused
    with: [../findings/openshell.md](../findings/openshell.md) F1.

  - **Every `exec` pays a full-tree upload and download**, `.git` objects
    included, so per-turn sync cost scales with repo size — a second, quieter
    reason `/sandbox` is a poor home for a large checkout.
  - **The lost-update hazard resolves to the pessimistic branch.** A whole-tree
    replace loses a concurrent gateway-side write unconditionally; there is no
    mtime comparison that might let a newer gateway file survive.
  - The `/tmp` carve-out is untouched by all of this: it is outside both roots,
    so it is neither wiped nor synced.

  **Resolution, landed in #3556: the gateway-fixed file tools are disabled.**
  `read`, `write`, `edit`, and `apply_patch` are out of `tools.allow`, leaving
  `exec` and `process` (plus `memory_*`, `session_status`) as the filesystem
  surface. The stated trigger was concrete: sandbox paths such as
  `/tmp/ducktape` were being interpreted as gateway paths. Every path now
  resolves on one machine for the shell tools, and most of the class above
  dissolves rather than being worked around:
  - No split-brain — there is only the sandbox's view.
  - **The mirror sync stops being load-bearing**, which makes `remote` mode the
    natural choice rather than a trap: seed once, operate on the remote workspace,
    no per-turn sync, and therefore no post-yield retention bug and no need for
    the `/tmp` carve-out. `/tmp` vs `/sandbox` reverts to being purely about
    persistence.
  - `fs.workspaceOnly: true` becomes moot; it constrains only the file tools.

  Costs and things to watch, none blocking:
  - **Structured editing goes away.** Edits become heredocs, `sed`, and
    redirects. More tokens and more room for the model to corrupt a file than
    `apply_patch`, which has real patch-application semantics and retry
    behavior.
  - **Ranged reads go away.** `cat` returns whole files into the tool-output
    truncation guard rather than being paged by tool arguments; the agent has to
    reach for `sed -n '100,200p'`. Mildly _good_ for C9 (everything is now
    bounded by the same 16K/32K/64K guard) but worse ergonomically.
  - Skills or prompt scaffolding that assume `read`/`write` exist will need
    rewording.

  **Correction from the live gateway: `memory_*` is _not actually reaching the
  agent_.** `openclawinstance.yaml` lists `memory_get`/`memory_search` in
  `tools.allow`, but a **second, independent policy** — the sandbox tool
  policy — strips them again. The running gateway logs both passes explicitly:

  ```text
  [agents/tool-policy] tool policy removed 22 tool(s) via tools.allow: …
  [agents/tool-policy] tool policy removed 2 tool(s) via sandbox tools.allow: memory_get, memory_search
  ```

  The cause is a default, not a typo: with `sandbox.mode: all` and no
  `tools.sandbox.tools.allow` set, the sandbox policy falls back to
  `DEFAULT_TOOL_ALLOW` (`src/agents/sandbox/constants.ts:18`), which lists
  `exec`, `process`, `read`, `write`, `edit`, `apply_patch`, `image`,
  `sessions_*`, `subagents`, `session_status` — and **no memory tools**. So the
  deployed agent's effective surface is `exec`, `process`, `session_status`, full
  stop.

  **This does _not_ block cross-session memory (S3), which is the thing that
  matters.** Recall does not depend on the memory tools at all: workspace context
  files are loaded with their **content inlined into the system prompt** —
  `lines.push("## " + file.path, "", sanitizeContextFileContentForPrompt(file.content))`
  (`src/agents/system-prompt.ts:251`) — and `MEMORY.md` is one of them, announced
  to the model as "durable user preferences and behavior guidance. Keep following
  it throughout the session" (`:246`). So the working loop needs only `exec`:
  1. the agent appends to `MEMORY.md` with a shell redirect inside an `exec`;
  2. the post-`exec` sync carries it to the gateway workspace — `MEMORY.md` is at
     the workspace root but is **not** on the exclusion list (`hooks`,
     `git-hooks`, `.git`), so it round-trips;
  3. the next session's bootstrap loads it and inlines it into the system prompt,
     with no tool call and no retrieval step.

  Sandbox recreation is also safe: `/sandbox` is re-seeded from the gateway copy.
  The one real hazard is the retention bug — a memory write that happens after a
  yield is lost like any other post-yield work.

  What the missing tools actually cost is **retrieval, not persistence**:
  `memory_search`'s QMD index and `memory_get`'s targeted reads. Worth restoring
  anyway, and the fix belongs in the manifest — `tools.sandbox.tools.alsoAllow`
  extends the default list rather than replacing it, so adding
  `memory_get`/`memory_search` there restores them without hand-copying
  `DEFAULT_TOOL_ALLOW`. **Verify against the gateway log after any tool-policy
  change**: the agent-level `tools.allow` is not the last word, and the second
  pass is silent unless you read the log.

  Everything below describes `memory_*` as it behaves when it _is_ enabled.

  **`memory_*` is gateway-fixed too, and was nominally retained in #3556 — so
  the gateway is not fully escaped.** OpenClaw's memory is plain Markdown in the agent
  workspace on the gateway: `MEMORY.md` for curated long-term facts and
  `memory/YYYY-MM-DD.md` daily logs. `memory_get` is documented as "a targeted
  read **by file and line range**" — a file reader by another name — and
  `memory_search` queries a gateway-side QMD index (sqlite + embeddings under
  `~/.openclaw/agents/<agentId>/qmd/`) built by `qmd update`/`qmd embed` on boot
  and on an interval, over gateway-side files only
  (<https://docs.openclaw.ai/concepts/memory>,
  <https://docs.openclaw.ai/reference/memory-config>). Three consequences:
  - Removing `read`/`write` but keeping `memory_*` leaves **read-only
    gateway-side file access still in the tool surface**. The split-brain is
    narrowed, not eliminated.
  - **The agent can no longer curate `MEMORY.md`.** Long-term memory was
    authored by editing that file; with `write`/`edit` gone, only the
    `session-memory` hook still writes (daily logs, harness-side). Whether that
    is acceptable is a product decision, not an accident — but it should be a
    decision.
  - **This is the real objection to `remote` mode**, and it partly walks back the
    point above. Memory files written from the sandbox via `exec` land at
    `/sandbox/...`, not in the gateway's agent workspace. Under mirror mode the
    post-`exec` sync carries them back so QMD indexes them; under `remote` mode
    they never reach the gateway and **memory silently stops growing**. Mirror
    mode's sync is what keeps the sandbox's filesystem and the gateway's memory
    index coherent.

  **Lost-update hazard: the memory hook and the mirror sync are two independent
  writers to the same gateway files.** Memory files live _inside_ the agent
  workspace, which is exactly the tree mirror mode syncs. So the sequence is:
  pre-`exec` sync copies gateway→sandbox (snapshot at T0), the command runs, and
  the post-`exec` sync copies sandbox→gateway. Any `session-memory` hook write
  that landed gateway-side **during** that window is written from a stale
  snapshot on the way back. The agent never has to touch `MEMORY.md` for this to
  happen — the sync alone is the second writer.

  Two things make it more than theoretical:
  - **`scope: agent` means all sessions share one sandbox.** Session A's
    post-`exec` sync can roll back a hook write triggered by session B, and the
    two sessions' pre/post syncs interleave arbitrarily. Concurrency here is the
    normal case, not an edge case.
  - **It compounds with the retention bug.** A long or backgrounded `exec`
    widens the window in which a hook write can be clobbered.

  **Severity is now determined, and it is the bad branch.** The post-`exec`
  transfer is `replaceDirectoryContents` — delete every top-level entry, copy the
  downloaded tree over — so a hook write that landed during the `exec` window is
  lost unconditionally. There is no mtime comparison to save it. `memory/` sits
  inside the mirrored tree and is not on the exclusion list (`hooks`,
  `git-hooks`, `.git`), so it is fully exposed. This is "memory silently
  truncates to the last sync", not "rare lost note", and it is the single
  strongest argument for moving durable notes into git (K1/K2).

  Mitigations, in rough order of effort: move memory outside the mirrored tree if
  `memory.qmd.paths` permits it (unclear — the docs say collections come from
  `memory.qmd.paths` **plus** default workspace memory files, which reads as
  additive rather than replacing); or go git-native and drop `memory_*`, which
  removes the second writer entirely.

  **Current state (post-#3556) is otherwise coherent, and memory should keep working.**
  Mirror mode is still on, and the memory loop is gateway-internal on both ends:
  the `session-memory` hook writes gateway-side, `memory_*` and QMD read
  gateway-side. Mirror's post-`exec` sync is what carries sandbox-written
  workspace content back into the indexed tree, so notes the agent produces via
  `exec` still become searchable. Two caveats rather than breakage: QMD refreshes
  on boot and on an interval, so search is eventually-consistent; and a memory
  write inside a command that yields is subject to the retention bug like any
  other file.

  **#3556 also disabled automated memory compaction, which was not noticed at the
  time.** OpenClaw's memory-flush run (`trigger: "memory"`) does not use
  `memory_*` at all: it rebuilds the tool set from exactly
  `MEMORY_FLUSH_ALLOWED_TOOL_NAMES = new Set(["read", "write"])` and wraps
  `write` into an append-only writer pinned to the memory file
  (`src/agents/agent-tools.ts:113,1053`, `wrapToolMemoryFlushAppendOnlyWrite`).
  That wrapper _is_ sandbox-aware — it takes the fs bridge when one exists — but
  the allow/deny policy pipeline runs **after** the flush subsetting
  (`src/agents/agent-tools.ts:1101`), so with `read` and `write` denied the flush
  run ends up holding no tools at all. The agent is told as much through
  `unavailableCoreToolReason` ("memory-triggered compaction runs expose only read
  and append-only write"). Worth confirming against the live config whether flush
  was firing before #3556; if it was, memory compaction is now silently a no-op.

  The remaining decision is not urgent but is real: **`MEMORY.md` curation is now
  hook-only**, since the agent lost the tools it used to edit that file with. If
  agent-authored long-term memory matters, either restore a narrow write path or
  **drop `memory_*` as well** and let durable notes live in git — the haku-state
  pattern (K1/K2), which would also make `remote` mode viable and retire the
  retention bug and the `/tmp` carve-out together. Keeping `memory_*` and mirror
  mode is the coherent status quo; going git-native is the coherent end state.
  What doesn't work is remote mode with `memory_*` still enabled.

  **This is also the answer to the "shared vs. private" workspace question.** The
  policy makes `/sandbox` and `/tmp` the only read-write paths
  (`policy.yaml` `filesystem_policy`), which maps cleanly onto: `/sandbox` =
  shared, persistent, mirrored state that propagates across sessions; `/tmp` =
  private-ish, disposable, per-task scratch. `scope` is one-dimensional
  (`agent`/`session`/`shared`), so it cannot express "partly shared" on its own —
  the `/sandbox` vs `/tmp` split is what supplies the second axis, with git as the
  durable propagation mechanism for anything that must outlive the pod.

- **The name-length bug (B1) is real, tracked, and currently blocked for a reason
  directly relevant to C8**: the actual fix is
  [openclaw/openclaw PR #114177](https://github.com/openclaw/openclaw) ("OpenShell
  Sandbox Name Bounding" — a **draft PR**, not a shipped fix), blocked specifically
  because the new naming scheme has "no legacy lookup, adoption, migration, or
  compatibility behavior," which would silently orphan **existing sandboxes in
  remote-mode deployments where the workspace is canonical** — i.e., the exact
  persistent-workspace failure mode this project needs to avoid. Ducktape's own shim
  (`openshell-cli-compat.yaml`) is a stopgap tracking this same PR, already
  correctly not relying on it landing soon. A separate, thinner issue,
  [#115057](https://github.com/openclaw/openclaw/issues/115057) ("closed", tagged
  `needs-info`/`no-new-fix-pr`/`needs-product-decision`, empty body), is not where the
  real fix lives — don't confuse the two if searching the tracker later.

### C8 — everything else that reads or writes the workspace

`exec`, the file tools, and `memory_*` are not the whole surface. Sorted by the
`fsBridge` seam above, verified at `v2026.7.1`:

**Bridge-aware — follows execution into the sandbox:**

- the file tools (`read`/`write`/`edit`/`apply_patch`), disabled here since #3556
- all media tools — `image`, `image_generate`, `pdf`, `music_generate`,
  `video_generate`, and prompt-image attachments via `sandbox-media-paths.ts`
- the memory-flush append-only write (see above)
- skills materialization: uploaded one-way to
  `/sandbox/.openclaw/sandbox-skills`, then stripped back out of every download
  so the sandbox copy cannot overwrite the gateway's

**Gateway-pinned — never coordinates with off-gateway execution:**

| Surface                                            | What it touches                                                                                                                                                                                 |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessions.files.{list,get,set,reveal}` gateway RPC | Full read **and write** API over the agent workspace, plus `runGit`. Zero sandbox awareness (`src/gateway/server-methods/sessions-files.ts`). This is the app's file browser/editor.            |
| Workspace bootstrap (`src/agents/workspace.ts`)    | Writes `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `BOOTSTRAP.md`, `MEMORY.md` when absent, plus a hash attestation; re-read each turn for the system prompt. |
| `session-memory` hook                              | `path.join(workspaceDir, "memory")` via `node:fs/promises` (`src/hooks/bundled/session-memory/handler.ts:160`).                                                                                 |
| `memory_get` / `memory_search` + QMD index         | No bridge use anywhere in the memory code; the index is state-dir SQLite.                                                                                                                       |
| Git worktrees (`src/agents/worktrees/*`)           | Gateway-local git, no sandbox concept.                                                                                                                                                          |
| Skill workshop (`src/skills/workshop/*`)           | Authors skills gateway-side.                                                                                                                                                                    |

Two things this changes:

- **The gateway RPC is a second writer outside the tool policy entirely.**
  Disabling `write`/`edit` in `tools.allow` does not disable
  `sessions.files.set`; editing a file from the web UI during an `exec` window
  is lost to the post-`exec` replace exactly like a hook write. (The MCP surface
  is not affected — `src/gateway/mcp-http.runtime.ts:21` excludes
  `read`/`write`/`edit`/`apply_patch`/`exec`/`process` from what it exposes.)
- **The workspace bootstrap files are gateway-authored and sandbox-visible.**
  They are pushed into `/sandbox` by every pre-`exec` upload, so instructions
  land in the sandbox, but an edit made _in_ the sandbox comes back only through
  the same lossy path as everything else.

### C8 — OpenClaw already has a git-based split-execution model: cloud workers

The `git init` in the agent workspace (`src/agents/workspace.ts:697`) is a red
herring on its own: it runs **only for a brand-new workspace**, ignores its own
failures, and nothing else in the harness reads it. There is no auto-commit, no
remote, no push/pull anywhere near it.

**But OpenClaw does implement git-backed workspace synchronization — for its
_cloud workers_ feature, and it is markedly better engineered than the OpenShell
mirror this cluster uses.** (`src/gateway/worker-environments/`,
`docs/gateway/cloud-workers.md`.)

Outbound, the gateway ships a git **pack** and the worker reconstructs a pinned
shallow repo (`workspace-sync-scripts.ts`): `git init`, `index-pack`, write the
base OID to `.git/shallow`, verify `rev-parse` matches the synced pack, then
`update-ref refs/heads/openclaw-worker` and check it out. Inbound, results come
back as a **git ref staged under `refs/openclaw/worker-results/` before being
applied**, so the cloud version stays recoverable if the gateway dies
mid-apply, and the apply is a **three-way merge against the dispatch-time
manifest** — cloud-only changes applied, local-only left alone, both-sides
conflicts resolved keep-local with the staged ref named in the notice for manual
inspection. Git file semantics (modes, symlinks, add/change/delete) are
preserved.

Compare with the OpenShell mirror: tar upload, whole-tree destructive replace, no
merge base, no conflict handling, no staging ref, and a sync that fires on yield.
**The two mechanisms are solving the same problem at very different maturity
levels**, which is the strongest available evidence for B3's "the
execute-elsewhere path is under-tested" — it is under-tested _in the OpenShell
plugin_, while the cloud-worker path got the careful design.

Two properties matter directly for the requirements:

- **Credential placement is inverted, in the good direction.** "Workspace git
  history is authored on the box credential-free; the Gateway adopts commits and
  owns push/PR", and there are "no standing model, forge, or cloud credentials on
  the box" — inference travels by `{provider, model}` reference. That is
  strictly better than the current sandbox, which holds a real `GITHUB_TOKEN`.
  It also means S2's PR flow is a first-class supported path rather than
  something the agent has to improvise with `gh`.
- **The worker provider is pluggable.** `WorkerProvider` is a public plugin-SDK
  type with `registerWorkerProvider` (`src/plugin-sdk/plugin-entry.ts:267`); the
  bundled `crabbox` provider leases cloud VMs (AWS/Hetzner), which is wrong for
  personal-data agents, but the interface is not cloud-specific.

**So there is a third topology worth costing before settling**: keep the harness
in-cluster and write a Kubernetes-pod `WorkerProvider`, getting W2's
harness/execution split on OpenClaw's mature git path instead of the OpenShell
tar mirror. Unknowns: how much of the provider contract is VM-shaped, whether a
pod can satisfy the setup/lease lifecycle, and whether the sandboxing (H3) would
then have to come from the pod spec rather than from OpenShell.

### C9 — robust handling of overlong tool output

**Confirmed hard requirement, already surveyed once and re-verified per-harness
here rather than duplicated.** `docs/self_hosted_coding_agent_platforms.md`
§"Tool-output robustness" already ranks this dimension across every candidate
platform — that survey is the primary reference, not repeated here. Per-harness
status relevant to this project specifically:

- **kagent**: ✗, confirmed the hard way — no client-side output budget, killed a
  real session on this cluster, and is the direct reason kagent was retired
  (`cluster/archive/2026_07_kagent/`). Disqualifying on its own for any harness
  choice here.
- **OpenClaw**: ✓ for the single-oversized-result case, ✗ (open, `P1`) for
  aggregate overflow across several medium outputs in one turn — see the detailed,
  primary-source-verified writeup under "public coder" below. Good enough to build
  on, not bulletproof.
- **Claude Code / Codex CLI wrappers** (`siteboon/claudecodeui`, `agent-sandbox` +
  `agentapi`): ✓, inherited for free from the wrapped CLI's own Bash-tool
  truncation — the survey's preferred axis for exactly this reason.
- **`x/agent_server`, `agent_core`, `x/editor_agent`** (in-repo experimental agent
  loops): **explicitly ruled out as reuse candidates by the user** — if a
  bespoke harness ever gets written for this project, it should not be based on
  any of them. Consequently their C9 behavior is moot and was not audited; do
  not treat "unaudited" here as "pending evaluation."
