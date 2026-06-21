# haku/agent — deployment plan

Status: the **runtime is feature-complete and green** — agent (`:scan`), supervisor
(`:serve`, warm session + `/wake` + scheduler), unit tests, Valkey durable history
(`RedisHistoryProvider`), and `SummarizationStrategy` compaction. What remains is
**deployment**, which is operator-owned perimeter and partly blocked in the Claude-web
sandbox (apt + Go-SDK fetches return 403). This plan captures the design and the
decisions that are yours.

## The one architectural fork: how Haku reaches its sources

Haku's sources — Tana, Grocy, Plaid-Postgres, Gmail, Calendar, Drive, PostScanMail —
are **all already MCP servers**. Two ways to wire them, and the choice drives the image:

|                | Shell (`run_command` + binaries)              | MCP toolsets (recommended)                                   |
| -------------- | --------------------------------------------- | ------------------------------------------------------------ |
| How            | `bash`: kubectl / psql / curl / fastmcp       | one `MCPStreamableHTTPTool` per source (Tana done)           |
| Image          | apt layer: git, curl, ca-certs, psql, kubectl | ≈ `haku/console`: debian-slim + Python + pygit2              |
| apt block here | yes — blocks the image build in sandbox       | none                                                         |
| Idiom          | shell-ish                                     | MAF-native (typed tools)                                     |
| Cost           | manual already assumes it                     | rework manual's source access to tools; wire each MCP's auth |

**Decision (chosen): the shell route.** Haku gets a real CLI toolbox (git, curl, psql,
… via apt; python from the image base), so `run_command` reaches sources and commits
`haku-state` with shell git — no pygit2 write-model needed. MCP toolsets stay a future
per-source simplification (Tana is already wired as one). This is why the image takes a
debian base + apt layer (next section).

## Image (`oci_image`, modeled on `finance/beancount_export` + `haku/console`)

Apt manifest is written: <trixie_haku_agent.yaml> (ca-certificates, git, curl,
postgresql-client; kubectl + the fastmcp CLI are not in trixie main — kubectl from an
upstream static binary if needed, fastmcp from the `agent-haku` Python devshell). The
lock + image **build on CI / a full-network machine** — debian repos are 403 in this
sandbox, and an `apt.install` whose `.lock.json` is missing breaks MODULE.bazel eval, so
none of the wiring below is committed yet. Turnkey steps where apt resolves:

1. Add to MODULE.bazel's `apt` extension (then add the name to `use_repo(apt, …)`):

   ```
   apt.install(
       name = "trixie_haku_agent",
       lock = "//haku/agent:trixie_haku_agent.lock.json",
       manifest = "//haku/agent:trixie_haku_agent.yaml",
   )
   ```

2. Generate the lock: `bazel run @trixie_haku_agent//:lock`.
3. Add `oci_image` (+ `oci_load`) to `haku/agent/BUILD.bazel`: base
   `@debian_trixie_slim_linux_amd64`; `tars` = the `:serve` `py_image_layer`, a
   `pkg_tar` baking `haku/base/` + `haku/run.md` at `/opt/haku` (the console's `web_tar`
   pattern), and `"@trixie_haku_agent//:flat"` (the apt layer, the
   `finance/beancount_export` pattern); entrypoint runs `:serve`.
4. `bbr build //haku/agent:image`, then GHCR push + Flux as for the console.

`haku-state` checkout: the supervisor clones/pulls it at startup via subprocess git (git
is in the image) — the next buildable-here increment.

## Bootstrap / entrypoint contract

At startup the supervisor needs: a writable `haku-state` checkout at `HAKU_STATE_DIR`
(clone from the in-cluster Forgejo, git-write creds from a secret); `HAKU_LITELLM_*`;
`HAKU_REDIS_URL`; per-source MCP creds (e.g. `HAKU_TANA_RO_TOKEN`). A kubeconfig only if
the shell route needs kubectl. All via env / mounted secrets — the code already reads
them, so the entrypoint is a thin bootstrap, not new behavior.

## k8s wiring (`cluster/k8s/haku/agent/`) — operator-owned

Mirror the console's perimeter, plus what this runtime additionally needs:

- **Deployment** `haku-agent` in `haku-sandbox`, non-root, behind `haku-mitmproxy`.
- **Valkey** with **AOF (`appendonly yes`, `everysec`) on a PVC** for durable history;
  `HAKU_REDIS_URL` → its Service. (Or reuse an existing durable Valkey.)
- **Secrets**: `HAKU_REDIS_URL`, the LiteLLM virtual key, `haku-state-git-write`, and
  each source's MCP token.
- **Trigger**: Forgejo webhook on `haku-state` → `POST /wake` (+ optional
  `HAKU_WAKE_INTERVAL_SECONDS` scheduler tick).

### Decisions that are yours (perimeter is broader than the console's)

1. **Cluster/kubectl access at all?** Only the shell route needs it; the MCP-toolset
   route runs with **no** service-account token, same as the console.
2. **Egress**: the existing `haku-sandbox` mitmproxy policy, or additions for the
   endpoints the MCP toolsets call?
3. **Model**: `HAKU_MODEL` + the virtual key's budget. (Summarization can use a cheaper
   model via `HAKU_SUMMARIZE_MODEL`; it reuses `HAKU_MODEL` when unset.)
4. **Valkey**: dedicated vs. reuse; AOF settings; PVC size.

## Blocked in this sandbox (need CI / a full-network machine)

- `oci_image` apt layer — debian repos (snapshot.debian.org) 403.
- `bazel run //devinfra:gazelle_python_manifest.update` — Go SDK (go.dev) 403; the Redis
  lockfile add needs this run elsewhere, otherwise the manifest-sync CI test may go red.
