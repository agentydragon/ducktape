# Running the Haku run inside the in-cluster sandbox

Status: **Phases 0-2 done; Phase 3 still not recommended.** Originally probed 2026-07-24
against claim `haku-run-test` → pod `haku-qrpjx`. **Re-tested 2026-07-25 by executing a real
(small) run end to end inside the sandbox** — claim `haku-run-sandboxtest` → pod `haku-dqrwp`
— which confirmed Phase 0 and Phase 2 landed and worked, and produced the Phase 1 doc changes
plus a second round of image fixes. See _2026-07-25 re-test_ below for what changed; the
2026-07-24 tables are kept as the original measurement.

Companion to [runtime_options.md](runtime_options.md): this
does not change _who runs the agent loop_ (still Runtime A, Claude Code web) — it moves
**where the run's commands execute** from the Anthropic-provisioned container into the
`haku-sandbox` pod the sandbox-provisioning MCP hands out
(<../../cluster/k8s/haku/workspaces/>, <../sandbox/>).

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

| Step of the run procedure         | In-sandbox result                                                                                                                                |
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

**Does not work / needs a workaround** (rows marked _fixed here_ are addressed by this PR or
the companion haku-state PR):

| Gap                                                                                    | Effect                                                                                                                                                                                       | Fix                                                                                                                                         |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| No git identity in the image _(fixed here)_                                            | first `git commit` dies "Author identity unknown"                                                                                                                                            | one `git config` pair in the baked `haku-sandbox-setup.sh`                                                                                  |
| No `kubectl` _(fixed here)_                                                            | `tools/ci_wait.sh` and `tools/plaid_q.sh` both fail at line 1 of work                                                                                                                        | bake `kubectl` into the image; `ci_wait.sh` can also just prefer `$HAKU_GIT_PASSWORD` (already in the pod env) over the kubectl secret read |
| No `python3` / Nix closure, and `cli/main.py` has **no Bazel target** _(fixed here)_   | `haku read --all` and the gmail/tana/console/plaid/location scanners are unavailable                                                                                                         | give `cli/main.py` a `py_binary` (below)                                                                                                    |
| `@pypi//fastmcp_slim` lacks the `[client]` extra _(fixed here)_                        | `from fastmcp import Client` → ImportError, so no console-MCP client                                                                                                                         | add the client extra to `ui/backend/requirements.txt`, or a second pip hub for the CLI                                                      |
| `cli/k8s_secrets.py` shells to `kubectl` for the console bearer _(fixed here)_         | console client can't authenticate even with fastmcp                                                                                                                                          | inject `HAKU_CONSOLE_TOKEN` via `secretKeyRef` in the SandboxTemplate (`console.py` already prefers the env var)                            |
| `.netrc` has only `forgejo-http.forgejo` _(fixed here)_                                | the CLI's REST readers hit the public host unauthenticated — `haku read --source cpap` 404s                                                                                                  | write both machines in `haku-sandbox-setup.sh`                                                                                              |
| `NO_PROXY` covers `.svc.cluster.local` but not `kubernetes.default.svc` _(fixed here)_ | the short API hostname is thrown at the egress proxy and hangs                                                                                                                               | extend `NO_PROXY` (suffix matching is literal — the short form never matched the FQDN suffix)                                               |
| `tools/validate_local.sh` is Nix-closure-only _(fixed, haku-state #41)_                | exits 2 in-sandbox; `push_state.sh` degrades to "rely on CI"                                                                                                                                 | prefer `bazel run //cli:{validate,freshness}` when `bazel` is on PATH                                                                       |
| `tools/ci_wait.sh` reported a FALSE GREEN _(fixed, haku-state #41 + python3 here)_     | its `python3` run-status parse was absent, so every `[ ]` integer test errored and control fell through to "all runs green", exit 0 — the end-of-run CI gate passing while verifying nothing | guard the parse and exit 2; put `python3-minimal` back in the image so it is functional, not just loud                                      |
| haku-state is cloned `--depth 1`                                                       | fine today; a rebase against a moved `origin/main` is untested shallow                                                                                                                       | deepen on demand in `push_state.sh`, or drop to `--depth 50`                                                                                |
| The in-cluster ducktape mirror lagged real `devel`                                     | base-sync from inside the sandbox would compare against a stale HEAD                                                                                                                         | keep base-sync on the harness-side ducktape checkout, or check the mirror's freshness first                                                 |

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
attempts failed" after the backend was healthy again. A third outage came from the other end
of the chain entirely (`Anthropic Proxy: Invalid content from server` on every haku-console
call, cluster fully healthy), which no cluster-side fix addresses. The Anthropic container has
no such dependency; a run that lives entirely in the sandbox inherits **both** the cluster's
availability and the console hop's. That is the strongest argument for keeping the harness
path working as a fallback rather than inverting the two (Phase 3).

**Process hygiene:** each `exec_sandbox` whose client call is abandoned leaves its process
tree reparented to PID 1 as zombies (the probe accumulated five). Harmless at this scale, but
a long-lived claim driven by many backgrounded execs should reap or re-provision.

## 2026-07-25 re-test — a real run, executed entirely in the sandbox

The 07-24 probe exercised the steps individually. This one ran an actual (narrow) Haku run
inside the pod: reduced five operator intake notes, validated, logged, wrote a manifest,
pushed, and waited for CI. It landed as haku-state `f38f7ea`+`41ab63b` → `c1c41ae`, **CI
4/4 green**. Everything Phase 0/2 promised held:

| Claim from Phase 0/2                 | Verified 2026-07-25                                                                       |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| git identity baked                   | yes — `haku` / `haku@allegedly.works`, no manual step                                     |
| both `.netrc` machines               | yes                                                                                       |
| `kubectl` in the image               | yes, authenticates as the pod SA                                                          |
| `NO_PROXY` covers the short API name | yes — `kubernetes.default.svc` and the FQDN both return 200                               |
| `HAKU_CONSOLE_TOKEN` injected        | yes                                                                                       |
| `//cli:haku` exists                  | yes — `haku console list` returned the live console tool surface from inside the pod      |
| validators + tests                   | `bookmark check` clean, `validate` 1165/1165, `freshness` clean, `bazel test //...` 25/26 |
| push + CI                            | `push_state.sh` → `direct OK`; `ci_wait.sh` → all 4 runs green                            |

**Two open concerns from 07-24 resolved:**

- **The shallow clone survives a moved `origin/main`.** A concurrent writer landed
  `ef1747c` mid-run; `push_state.sh` → rebase → a real content conflict in `log/2026-07-25.md`
  → resolve → `--continue` → `direct OK` all worked. The "untested shallow rebase" row can go.
- **ducktape _can_ be cloned from inside the cluster**, so base-sync is not structurally
  impossible after all — see below.

**New findings, fixed in this PR:**

- **`python3-minimal` is much worse than "no `json`".** Measured missing: `json`,
  `urllib.request`, `http.client`, `shutil`, `difflib`, `dataclasses`, `sqlite3`. This is not
  a marginal gap, because **heredoc'd `python3` is the file-authoring motion** when driving
  the box through `exec_sandbox` — the run's first edit script died on `import shutil`. Now
  full `python3`.
- **`jq` and `tea` were absent.** `tea` matters specifically because haku-state's
  `procedures/code_changes.md` routes code changes through Forgejo PRs. Both added.
- **`git` did not trust the egress CA.** `curl https://github.com/` returned 200 while
  `git ls-remote` beside it died `server certificate verification failed. CAfile: none` —
  git's OpenSSL reads neither `SSL_CERT_FILE` nor `CURL_CA_BUNDLE`. One
  `git config --global http.sslCAInfo` fixes it, and it is the difference between "the
  cluster has no GitHub egress" (wrong) and "git wasn't told where the trust store is".
- **Base-sync now works in-sandbox.** With that CA fix, a `--filter=blob:none` partial clone
  of `agentydragon/ducktape` from GitHub takes **11s / ~102 MB** and keeps **all 13,904
  commits**, so `git log <pin>..HEAD -- haku/base haku/run.md` resolves — which `--depth 1`
  cannot. Added to the bootstrap. Deliberately **GitHub, not the in-cluster Forgejo
  `haku/ducktape` mirror**: the mirror exists but is not auto-synced and measured 3 commits
  behind `devel` (`97a23895` vs `a4c497f7`), and base-sync against a stale HEAD silently
  under-reports contract changes. This also gives an ad-hoc claude.ai chat — which has no
  ducktape checkout of its own — a way to read Haku's manual at all.
- **The 60s ceiling is exact and is a client-side cutoff.** `sleep 110` with
  `timeout_seconds: 180` fails at 60s while the server-side process keeps running. So it is
  the MCP client, not `exec_sandbox`, that gives up — which is what makes
  `nohup`-and-poll work, and what a client-side timeout override would fix outright.
- **Cold Bazel is ~3.5 min on a fresh claim** (bazelisk fetches 8.6.0, then cold analysis),
  vs. the 83s measured on 07-24's already-warm box. Warm calls stay sub-second.
- **Zombie count confirmed**: 5 accumulated in one run, as predicted. `tini` as PID 1 in the
  Nix image is the fix.

**Phase 1 (docs) landed with this PR**: `haku/runtime/claude_web_env/run.md` gains a
"Bazel-backed steps run in the in-cluster sandbox" section carrying the standing environment
facts, the harness/sandbox split, the unavailability fallback, and the base-sync caveat.

**Phase 3 (invert the default) stays not-recommended**, unchanged: a sandbox-only run
inherits both the cluster's availability and the console hop's, `haku-sandbox-mcp` is a
single replica, and one 07-24 outage came from the Anthropic side entirely. Keep the harness
path working as a real fallback.

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

### Phase 0 — close the cheap gaps — **LANDED in this PR**

1. The per-claim bootstrap gains the commit identity (`git config --global user.{name,email}`)
   and a **second `.netrc` machine** for the public `git.allegedly.works` — the live scan
   below 404'd on `haku read --source cpap` because only the in-cluster
   `forgejo-http.forgejo` was authenticated. In the same pass the bootstrap moves out of the
   MCP's config YAML and into the image's `haku-sandbox-setup.sh`, which already held the
   egress-CA half: a 25-line bash blob in YAML is what STYLE.md forbids, and in the script
   `shfmt`/`shellcheck` lint it. Deviation: changing bootstrap behavior now costs an image
   rebuild + rollout rather than a ConfigMap edit — accepted because the MCP's environment
   contract hash never covered the image tag anyway (`haku/sandbox/config.py`), so no
   drift detection is lost.
2. `cluster/k8s/kyverno/policies/inject-haku-egress-proxy.yaml`: add `.svc` and
   `kubernetes.default.svc` to `NO_PROXY`. Suffix matching is literal, so the short
   in-cluster apiserver name — the one every client library builds by default — never
   matched the `.svc.cluster.local` entry and hung against the proxy.
3. `cluster/k8s/haku/workspaces/image/Dockerfile`: `kubectl`, pinned to the control-plane
   minor. Unblocks `ci_wait.sh`/`plaid_q.sh` and `cli/k8s_secrets.py`.
4. `cluster/k8s/haku/workspaces/app/sandboxtemplate-haku.yaml`: `HAKU_CONSOLE_TOKEN` from the
   `haku-console-agent-api` secret via `secretKeyRef`, mirroring the `HAKU_GIT_*` pair — so
   the CLI's console client authenticates with no kubectl and no per-run token fetch.
5. Still open, haku-state (**Forgejo PR**, not a direct push — `procedures/code_changes.md`):
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

### Phase 2 — close the scanner gap — **built and verified live**, landing via a haku-state PR

The lockfile turned out not to need regenerating: `ui/backend/pyproject.toml` already asks for
`fastmcp-slim[client]`, so every one of the extra's distributions is in `requirements.txt`
already. The gap was purely Bazel deps.

**Gotcha worth keeping:** the pip hub models an extra's requirements as ordinary _separate_
distributions, and `@pypi//fastmcp_slim` carries only the base package's edges. So a target
depending on `@pypi//fastmcp_slim` alone gets `from fastmcp import Client` →
`ImportError: FastMCP client support is not installed`, and the real cause is hidden behind
that hint (`raise … from exc`). Each extra's packages have to be named explicitly — including
the _nested_ ones: `py-key-value-aio[filetree,keyring,memory]` needs `aiofile`, `anyio`,
`cachetools`, and `keyring` on top. Import the submodule directly (`import fastmcp.client`) to
see the real `ModuleNotFoundError` behind the hint.

haku-state `cli/BUILD.bazel` therefore gains a `FASTMCP_CLIENT` dep list, `py_library` targets
for every fetcher, and a `//cli:haku` `py_binary` (with `tools/plaid_q.sh` as `data`, since
`haku plaid` shells out through a path derived from `__file__` that resolves inside runfiles).
The file's header comment — "they run via the `haku` closure recipe, not Bazel, so they carry
no targets" — is now false and gets replaced.

Verified in the sandbox: `bazel run //cli:haku -- console list` returns the live console tool
surface, `-- bookmark check` reports a clean ledger, and `-- read --all` runs the real
multi-source sweep. With this, `tools/agent_python.sh` loses its reason to exist and the
"borrowed Nix closure" class of breakage — the most-repeated environment failure in
`memory/environment.md` — is gone from the run for good.

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

## Build the image with Nix instead of a Dockerfile

**Status: written and CI-built as of this PR, NOT yet cut over.**
`cluster/k8s/haku/workspaces/image/default.nix` +
`.github/workflows/haku-sandbox-image-nix.yml` publish it to a separate
`haku-sandbox-image-nix` repo that nothing consumes, so the runtime bet below can be tested
against a throwaway Pod in `haku-sandbox` before `sandboxtemplate-haku.yaml` moves. The
cutover checklist lives with the image
(<../../cluster/k8s/haku/workspaces/image/README.md>). Both builds bake the same
`haku-sandbox-setup.sh`, so the per-claim bootstrap cannot drift between them while both
exist. Rationale and the prior art:
<../../x/codex_pod_image/> builds a **Kubernetes pod** image purely from Nix — a
`dockerTools` archive, tool set as one `buildEnv` on `/bin`, home-manager files baked in,
non-root UID 1000, no runtime bootstrap script, pushed by CI with `skopeo copy
docker-archive:` and rolled by Flux image automation. That is the same shape the Haku
sandbox image needs, and it would dissolve the recurring "the image is missing X" class of
bug this plan keeps hitting (`kubectl`, `python3`, `jq`, …): the tool list becomes one Nix
attribute set instead of an `apt-get install` line nobody revisits. It also brings `tini` as
PID 1, which would reap the exec zombies noted above.

**The one real risk, and it is specific to this image.** <../../x/nix_rbe_image/README.md>
records why the Nix approach was abandoned for the RBE worker: NixOS glibc has nix-store
paths compiled into its library search path, so **dynamically-linked binaries downloaded at
runtime cannot find `libstdc++.so.6`** — and it names exactly the two this image lives on,
"Bazel from bazelisk" and "python-build-standalone". This sandbox's whole job is bazelisk
fetching Bazel (which runs a bundled JDK) and `rules_python` fetching a hermetic CPython, so
it is the worst case for that failure mode, not an incidental user of it.

What makes it plausible here anyway: the RBE blocker was that **BuildBuddy's Firecracker
goinit never runs the container's `/init`**, so nix-ld's env (`NIX_LD`,
`NIX_LD_LIBRARY_PATH`) never gets set and systemd-based activation never happens. A
`SandboxTemplate` pod has no such constraint — we own the pod spec and can set those env
vars declaratively, the same way `HAKU_GIT_*` and `HAKU_CONSOLE_TOKEN` are set today. So the
thing that killed it for RBE does not obviously apply.

Sequence, before committing to it: build the Nix image, run `bazel build //cli:validate` and
`bazel test //...` inside it, and confirm the bazelisk-downloaded Bazel and the hermetic
CPython both start. If they do, the rest is mechanical (flake attribute + a CI job mirroring
`codex-pod-image`'s push). If they do not, `nix-ld` via pod env is the next thing to try, and
the current Dockerfile stays — it is not costing much beyond the occasional missing tool.
