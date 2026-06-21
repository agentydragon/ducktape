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

**Recommendation: MCP toolsets.** It sidesteps the apt block, is MAF-idiomatic, and
in-cluster the loop reaches in-cluster MCP Services directly. Migrate sources
incrementally (Tana wired; add Grocy / PostScanMail / Google / Plaid as toolsets), and
keep a minimal `run_command` (haku-state git via pygit2, like the console) for commits.
The shell route stays the fallback for any source without a usable MCP surface.

## Image (`oci_image`, modeled on `haku/console` + `finance/beancount_export`)

- Base `@debian_*_slim`; bake `haku/base/` + `haku/run.md` at `/opt/haku` (pkg_files /
  pkg_tar, like the console's `web_tar`); entrypoint runs `:serve`.
- MCP-toolset route → **no apt layer**: clone `haku-state` with pygit2 in the supervisor
  lifespan (exactly what the console does).
- Shell route → apt layer + a kubectl binary, which **cannot build in this sandbox**
  (debian repos 403) — CI / full-network only.

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
3. **Model**: `HAKU_MODEL` + the virtual key's budget — and a **separate, cheaper model
   for summarization** (`SummarizationStrategy` currently reuses `HAKU_MODEL`)?
4. **Valkey**: dedicated vs. reuse; AOF settings; PVC size.

## Blocked in this sandbox (need CI / a full-network machine)

- `oci_image` apt layer — debian repos (snapshot.debian.org) 403.
- `bazel run //devinfra:gazelle_python_manifest.update` — Go SDK (go.dev) 403; the Redis
  lockfile add needs this run elsewhere, otherwise the manifest-sync CI test may go red.
