# Running the Haku run inside the in-cluster sandbox

Status: **probe complete** (2026-07-24), every number below measured live through
haku-console's `sandbox_mcp` tools against claim `haku-run-test` → pod `haku-qrpjx`.
Proposal, not yet adopted. Companion to [runtime_options.md](runtime_options.md): this
does not change _who runs the agent loop_ (still Runtime A, Claude Code web) — it moves
**where the run's commands execute** from the Anthropic-provisioned container into the
`haku-sandbox` pod the sandbox-provisioning MCP hands out
(<../../cluster/k8s/haku/workspaces/>, <../sandbox_mcp/>).

## Why bother

The Anthropic container has no Bazel-runnable haku-state toolchain: the run's validators,
the bookmark ledger, and the state tests are reachable there only through the borrowed
Nix-closure python (`tools/agent_python.sh`), which exists by accident of the devshell and
breaks whenever the harness variant changes (four separate runs lost time to it — see
haku-state `memory/environment.md`). The sandbox image bakes the real thing: bazelisk, a
JDK, build-essential, the egress CA in the JVM truststore. `bazel run //cli:validate` there
is the same command CI runs.

## What the probe established

**Works, unmodified:**

| Step of `haku/run.md`             | In-sandbox result                                                                                                                                |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| bootstrap / state checkout        | `provision_sandbox` → `ready` + `bootstrap_state=succeeded` in one call, warm-pool hit; `.netrc` written, `/workspace/haku-state` cloned at HEAD |
| run-start gate 1 (SF date)        | `TZ=America/Los_Angeles date` — fine                                                                                                             |
| run-start gate 3 (bookmark check) | `bazel run //cli:bookmark -- check` → `ledger clean`                                                                                             |
| orient (memory/, log/, items/)    | fine, with the output-cap caveat below                                                                                                           |
| intake / responses reduction      | plain file ops                                                                                                                                   |
| state validation                  | `bazel run //cli:validate` → 1161/1161 valid; `//cli:freshness` → clean                                                                          |
| full test suite                   | `bazel test //...` → **25/26**; only `//ui/e2e:test_e2e` fails (no Docker socket, deliberate)                                                    |
| cold/warm build cost              | cold `//cli:validate //cli:bookmark` 83s; warm 1.3s off `--disk_cache`                                                                           |
| commit + push to `main`           | `tools/push_state.sh` → `direct OK`; CI triggered on the pushed SHA                                                                              |
| k8s API from inside               | pod `haku` SA token → 200                                                                                                                        |
| haku-console reachability         | 200 both in-cluster and at `haku.allegedly.works`                                                                                                |

**Does not work / needs a workaround:**

| Gap                                                                     | Effect                                                                               | Fix                                                                                                                                         |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| No git identity in the image                                            | first `git commit` dies "Author identity unknown"                                    | one `git config` pair in the MCP `bootstrap.script`                                                                                         |
| No `kubectl`                                                            | `tools/ci_wait.sh` and `tools/plaid_q.sh` both fail at line 1 of work                | bake `kubectl` into the image; `ci_wait.sh` can also just prefer `$HAKU_GIT_PASSWORD` (already in the pod env) over the kubectl secret read |
| No `python3` / Nix closure, and `cli/main.py` has **no Bazel target**   | `haku read --all` and the gmail/tana/console/plaid/location scanners are unavailable | give `cli/main.py` a `py_binary` (below)                                                                                                    |
| `@pypi//fastmcp_slim` lacks the `[client]` extra                        | `from fastmcp import Client` → ImportError, so no console-MCP client                 | add the client extra to `ui/backend/requirements.txt`, or a second pip hub for the CLI                                                      |
| `cli/k8s_secrets.py` shells to `kubectl` for the console bearer         | console client can't authenticate even with fastmcp                                  | inject `HAKU_CONSOLE_TOKEN` via `secretKeyRef` in the SandboxTemplate (`console.py` already prefers the env var)                            |
| `NO_PROXY` covers `.svc.cluster.local` but not `kubernetes.default.svc` | the short API hostname is thrown at the egress proxy and hangs                       | use the FQDN, or extend `NO_PROXY`                                                                                                          |
| `tools/validate_local.sh` is Nix-closure-only                           | exits 2 in-sandbox; `push_state.sh` degrades to "rely on CI"                         | prefer `bazel run //cli:{validate,freshness}` when `bazel` is on PATH                                                                       |
| haku-state is cloned `--depth 1`                                        | fine today; a rebase against a moved `origin/main` is untested shallow               | deepen on demand in `push_state.sh`, or drop to `--depth 50`                                                                                |
| The in-cluster ducktape mirror lagged real `devel`                      | base-sync from inside the sandbox would compare against a stale HEAD                 | keep base-sync on the harness-side ducktape checkout, or check the mirror's freshness first                                                 |

**Two hard ceilings, neither fixable in haku-state:**

- **100 KB of output per `exec_sandbox` call.** `items/` alone is 346 KB, so orienting has to
  go frontmatter-digest first, then targeted reads. Workable, but it is a different motion
  from the harness's file tools.
- **The console→MCP hop gives up at ~60s** even though `exec_sandbox` accepts
  `timeout_seconds` up to 300. Server-side commands **survive** the client timeout (an 86s
  clone and a multi-minute `bazel test` both completed after their calls had errored out), so
  the pattern is `nohup … > /tmp/x.log 2>&1 &` then poll — but every long step needs that
  wrapper.

**Availability:** during the probe, nodes flapped `NotReady` twice (21:38Z, 22:19Z), and with
`haku-sandbox-mcp` scheduled on one of them the entire tool path was dead ~5 min each time.
Recovery was automatic, plus one `get_mcp_server_status(server_id='sandbox-mcp')` to force the
console's MCP client to reconnect — without that call it kept returning "All connection
attempts failed" after the backend was healthy again. The Anthropic container has no such
dependency; a run that lives entirely in the sandbox inherits cluster availability.

## The shape that fits: harness reasons, sandbox executes

A wholesale move is the wrong target. The run's **source scans and consent surfaces already
live in the harness** — Gmail/Calendar/Tana/Grocy/GitHub are wired claude.ai connectors, and
haku-console's approval-gated tools are in-session MCP calls. Re-plumbing those through the
sandbox would replace working in-session tools with a fastmcp client that has to be built,
credentialed, and kept in sync, for no gain.

What the sandbox is unambiguously better at is everything that wants a **toolchain**: the
ledger, the validators, the tests, `bazel run`, and the git push. So:

- **Harness (Anthropic container):** orient, source scans via connectors, reasoning and
  synthesis, tool-call approval, writing state files.
- **Sandbox:** `bazel run //cli:*`, `bazel test //...`, and — once state files are there —
  validate + commit + push.

The friction in that split is that state files then exist in two places. Two ways to resolve
it, and the probe favors the second:

1. **Two checkouts, harness authoritative** — edit in the harness, `git push`, then
   `git pull` in the sandbox before running validators. Costs a round trip per iteration and
   invites divergence.
2. **One checkout, in the sandbox** — the harness writes through `exec_sandbox` heredocs
   (`cat > file <<'EOF'`) or `git apply` of a patch. This is what the probe did end to end,
   including this run's log entry, base-sync bump, and run manifest. It works; the cost is
   that authoring goes through bash instead of the Edit tool, which is more error-prone for
   large surgical edits and gives up per-file diffs.

Recommendation: **(2) for runs that only append/rewrite whole files** (log, manifest,
bookmarks, new items — most of a scan run's writes), falling back to (1) when a run needs
heavy in-place editing across many items.

## Phased plan

### Phase 0 — close the cheap gaps (no doc changes, no behavior change)

1. `cluster/k8s/agents/haku-sandbox-mcp/app/config.yaml` → `bootstrap.script`: add
   `git config --global user.name haku` / `user.email haku@allegedly.works`. One line each,
   no image rebuild (the bootstrap is deliberately image-independent).
2. `cluster/k8s/haku/workspaces/image/Dockerfile`: add `kubectl` (and `jq`). This unblocks
   `ci_wait.sh`/`plaid_q.sh` and makes the pod's existing `haku` SA usable the normal way.
3. `cluster/k8s/haku/workspaces/app/sandboxtemplate-haku.yaml`: add `HAKU_CONSOLE_TOKEN` from
   the `haku-console-agent-api` secret via `secretKeyRef`, mirroring the existing
   `HAKU_GIT_*` pair.
4. haku-state (**Forgejo PR**, not a direct push — `procedures/code_changes.md`):
   `tools/validate_local.sh` prefers `bazel run //cli:validate` + `//cli:freshness` when
   `bazel` is on PATH, keeping the Nix path as fallback; `tools/ci_wait.sh` uses
   `$HAKU_GIT_PASSWORD` when set instead of the kubectl secret read.

### Phase 1 — make the sandbox the documented execution surface

Edit `haku/runtime/claude_web_env/run.md` (and haku-state
`procedures/run_start.md`, which currently says "fastmcp-free fallback:
`bazelisk run //cli:bookmark`" as if Bazel were the exotic path):

- New section, **"Bazel-backed steps run in the in-cluster sandbox"**: `provision_sandbox`
  with a stable claim name at run start; `exec_sandbox` for gates 2–3, validators, and tests;
  what to do when the tools go unavailable (`get_mcp_server_status` to reconnect; the run is
  not blocked — fall back to the harness path and file a finding).
- Record the two ceilings (100 KB output, ~60s client timeout → background-and-poll) as
  standing environment facts rather than per-run rediscovery.
- State that base-sync stays on the harness-side ducktape checkout (the in-cluster mirror
  lags).
- Keep the existing harness bootstrap intact — this is additive; a run whose sandbox is
  unavailable still completes exactly as today.

### Phase 2 — close the scanner gap (unlocks the full run in-sandbox)

Only worth doing if Phase 1 proves the sandbox is where a run wants to live:

1. haku-state `ui/backend/requirements.txt`: `fastmcp-slim[client]` (or a dedicated
   `cli/requirements.txt` + second `pip.parse` hub, which avoids widening the backend image).
2. `cli/BUILD.bazel`: `py_binary(name = "haku", main = "main.py", …)` plus `py_library`
   targets for the fetch modules, deps on the fastmcp hub. The comment at the top of that
   file ("they run via the `haku` closure recipe, not Bazel, so they carry no targets")
   becomes obsolete and should be replaced, not just edited around.
3. Then `bazel run //cli:haku -- read --all` works in-sandbox, `tools/agent_python.sh` loses
   its reason to exist, and the "borrowed Nix closure" class of breakage is gone from the run
   for good — the single biggest recurring environment failure in `memory/environment.md`.

### Phase 3 — optional, later

Only if Phases 1–2 land cleanly: make the sandbox the default and the harness the fallback,
i.e. invert the current entrypoint. Not recommended before there is a story for the
availability dependency (a sandbox-unavailable run currently has no automatic degrade path,
and `haku-sandbox-mcp` is a single replica).

## What this does not change

- The trust model. Everything in `haku-sandbox` stays force-proxied by the cluster-scoped
  `haku-sandbox-force-proxy` CCNP and baseline-PodSecurity-confined, both outside Haku's
  RBAC — see the header comment on `sandboxtemplate-haku.yaml`.
- Approval routing: privileged external actions still go through haku-console's tool-call
  queue, not through `exec_sandbox`.
- Runtime choice (A/B/C). This is orthogonal — every runtime in
  [runtime_options.md](runtime_options.md) gains the same execution surface.
