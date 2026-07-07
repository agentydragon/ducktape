# haku/runtime/agent — experimental deployment plan

Status: **experimental Runtime C, not the primary live Haku runtime.** Haku
currently runs through the manually configured Claude Code web home in
<../claude_web_env/>. The Runtime C implementation pieces here are
feature-complete and green — agent (`:scan`), supervisor (`:serve`, warm
session, `/wake`, scheduler), unit tests, Valkey durable history
(`RedisHistoryProvider`), `SummarizationStrategy` compaction, and startup
cloning of ducktape and haku-state (`bootstrap.py`). What remains is
**deployment**: the loop/tools split below, the two images, and the
operator-owned k8s perimeter. Some steps are blocked in the Claude-web sandbox
(apt and Go-SDK 403) and must run on CI / a full-network machine.

## Execution model: loop and tools containers (`pods/exec`)

One Pod in `haku-sandbox`, two containers sharing an `emptyDir` at `/workspace`:

- **loop** — the `:serve` image: MAF agent and supervisor (`/wake`, scheduler). Lean,
  like `haku/console` (debian-slim, Python, pygit2, **no apt**). Clones ducktape and
  haku-state (pygit2, `bootstrap.py`) onto `/workspace`. `run_command` execs into the
  tools container. **Buildable in this sandbox** (no apt — a `rules_py` `oci_image` like
  the console).
- **tools** — sidecar: the `trixie_haku_agent` apt image (git, curl, ca-certificates,
  postgresql-client), a kubectl static binary, and the `fastmcp` CLI, kept alive with
  `sleep infinity`. Mounts the same `/workspace`; its shell `git` / `kubectl` / `psql` /
  `fastmcp` operate on the checkout. **CI-only build** (apt 403 in this sandbox).

`run_command(cmd)` execs `sh -lc <cmd>` (cwd = the haku-state checkout) in the tools
container of its own Pod via the k8s `pods/exec` API: the sync `kubernetes` client's
`stream(connect_get_namespaced_pod_exec, …, STDOUT_CHANNEL / STDERR_CHANNEL)` wrapped in
`asyncio.to_thread` (the `cluster/network_readiness.py` pattern), capturing
stdout+stderr, tail-capped. Own Pod name via the downward API (`HAKU_POD_NAME`),
container `tools`.

### Auth / RBAC — mostly already exists

Haku already has a k8s identity: the `haku-k8s` machine principal maps to group `haku`,
which holds a **full-CRUD `haku-sandbox` Role** plus cluster-diagnostics read (see
`cluster/k8s/agents/agent-rbac-base`). The sandbox Role pattern includes `pods/exec` and
`pods/attach` (verify the `haku-sandbox` Role lists it), so exec into the sidecar (same
namespace) is **already within Haku's perimeter — no new RBAC**. The loop authenticates
with the `haku-k8s` JWT, building its kubeconfig via `devinfra/k8s/kubeconfig.py` (the
mechanism the console/web session already uses), targeting `kubeapi.allegedly.works`.

### Shared state and creds

`/workspace` (`emptyDir`) holds the ducktape and haku-state checkouts, mounted in both
containers. `bootstrap.py` (loop) clones onto it via pygit2. The `~/.netrc` it writes
must land where the tools container's shell `git` reads it, so write `/workspace/.netrc`
and set `HOME=/workspace` (or `GIT_CONFIG`) on the tools container — a small
`bootstrap.py` tweak from the current `~/.netrc`.

## Images

- **loop** (`//haku/runtime/agent:image`, **buildable here**): a `rules_py` `oci_image` on
  `@debian_*_slim`, like `haku/console` — the `:serve` `py_image_layer`, no apt, no baked
  manual (cloned at startup); entrypoint runs `:serve`.
- **tools** (CI-only): the apt manifest is written (<trixie_haku_agent.yaml>); its lock
  and image build where apt resolves. An `apt.install` whose `.lock.json` is missing
  breaks MODULE.bazel eval, so this is not committed yet.

Turnkey tools-image wiring where apt resolves — add to MODULE.bazel's `apt` extension
(and the name to `use_repo(apt, …)`):

```starlark
apt.install(
    name = "trixie_haku_agent",
    lock = "//haku/runtime/agent:trixie_haku_agent.lock.json",
    manifest = "//haku/runtime/agent:trixie_haku_agent.yaml",
)
```

Then `bazel run @trixie_haku_agent//:lock`, add an `oci_image` modeled on
`finance/beancount_export` (base `@debian_trixie_slim_linux_amd64`, the
`"@trixie_haku_agent//:flat"` apt layer, a kubectl static binary, fastmcp; command `sleep
infinity`), and `bbr build` then GHCR push plus Flux.

## k8s wiring (`cluster/k8s/haku/runtime/agent/`) — operator-owned

- **Deployment** `haku-agent` in `haku-sandbox`: two containers (loop, tools) sharing an
  `emptyDir` at `/workspace`, non-root, behind `haku-egress-proxy`.
- **haku-k8s kubeconfig** mounted in the loop (the existing JWT secret) so `run_command`
  can exec — reuses the existing `haku` RBAC.
- **Valkey** with **AOF (`appendonly yes`, `everysec`) on a PVC** for durable history;
  `HAKU_REDIS_URL` points at its Service. (Or reuse an existing durable Valkey.)
- **Secrets**: `HAKU_REDIS_URL`, the LiteLLM virtual key, `haku-state-git-write`, the
  `haku-k8s` JWT, each source's MCP token.
- **Trigger**: Forgejo webhook on `haku-state` to `POST /wake` (plus optional
  `HAKU_WAKE_INTERVAL_SECONDS` tick).

### Open decisions (yours)

1. **Exec auth path**: the `haku-k8s` JWT via the `kubeapi` proxy (reuses existing wiring)
   vs. an in-cluster ServiceAccount bound to the `haku` perimeter (no public hop, new RBAC
   subjecting).
2. **Egress**: the existing `haku-sandbox` mitmproxy policy, or additions for what the
   tools container reaches.
3. **Model**: `HAKU_MODEL` and the virtual-key budget (summarization can use a cheaper
   `HAKU_SUMMARIZE_MODEL`).
4. **Valkey**: dedicated vs. reuse; AOF; PVC size.

## Code changes for the split (from today's in-process `run_command`)

- `agent.py`: `run_command` becomes a k8s `pods/exec` into the tools container (sync exec
  in `asyncio.to_thread`, output cap as today).
- `config.py`: `HAKU_POD_NAME`, tools container name, namespace, kubeconfig path, netrc
  path.
- `bootstrap.py`: write `~/.netrc` onto the shared `/workspace`.
- `BUILD.bazel`: the loop `oci_image` (buildable here) now; the tools image on CI.

## Blocked in this sandbox (need CI / a full-network machine)

- The tools image apt layer — debian repos (snapshot.debian.org) 403.
- `bazel run //devinfra:gazelle_python_manifest.update` — Go SDK (go.dev) 403; the Redis
  lockfile add needs this run elsewhere, else the manifest-sync CI test may go red.
