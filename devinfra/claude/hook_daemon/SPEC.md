# Hook Daemon Specification

See @README.md for architectural and implementation details.

## Overview

Every Claude Code session — whether it is running in Claude Code CLI on a
developer workstation or in Claude Code on the web inside a sandboxed container
— is paired with a **session-scoped hook daemon**. The daemon is launched by
Claude Code's `SessionStart` hook and lives for the duration of the session.

Its job is to make every session look the same to the agent:

- Bazel (via `bbr` / `bazelisk` / `bb`) is wired up to BuildBuddy and works
  out of the box: plain `bazelisk build <target>` or `bb build <target>`
  automatically uses BuildBuddy remote execution and remote cache, with no
  extra flags from the agent.
- Credentials the agent needs (BuildBuddy, GitHub, Kubernetes, tracing) are
  available in the environment without the agent having to fetch or decrypt
  them.
- Dangerous or footgun git operations are blocked by a PATH shim.
- Pre-commit lint/format hooks run automatically on Edit/Write and their
  failures are reported back to the agent.
- The `claude-sandbox-kubectl` MCP server is configured to talk to the
  cluster as the expected Claude identity.
- Hook activity is traced to the central OpenTelemetry collector.

The daemon exposes two **profiles** — `cli` and `web` — that differ both in
what the surrounding environment is expected to provide and in which
behaviors are enabled (e.g., the git safety shim and direnv bridge are
CLI-only; egress proxy handling, mkcert, tmpfs, managed credentials, and
idle shutdown are web-only).

## Common Behaviors (CLI and Web)

These guarantees hold in every session, regardless of profile.

### Credentials in the agent's environment

Every Bash tool call sees:

- A valid `BUILDBUDDY_API_KEY`.
- A valid `GITHUB_TOKEN` (on web this is the `agentydragon-agent` machine
  user; on CLI this is whatever token the user's outer shell already
  exposes).
- `DUCKTAPE_OTEL_BEARER_TOKEN` for tracing.

The agent should never need to decrypt SOPS files or run `gh auth login`
manually — if a credential is missing, the daemon is broken.

### Bazel / BuildBuddy

- `bazelisk`, `bb`, and `bbr` on `PATH` are wired to BuildBuddy.
- A plain `bazelisk build <target>` or `bb build <target>` automatically
  uses BuildBuddy remote execution **and** remote cache out of the box.
  The agent does not need to pass `--config=rbe`, `--remote_executor=...`,
  `--remote_cache=...`, or any authentication flags.
- BuildBuddy invocations are tagged with the session ID so they can be
  filtered later via `bbapi invocation list --tag session:<id>`.

### Pre-commit lint & format on Edit/Write

When the agent edits a file via the Edit or Write tool, the daemon runs the
project's `pre-commit` configuration against the touched files as a
`PostToolUse` hook:

- Pure format/whitespace hooks (e.g. `ruff-format`) are **auto-applied**
  and the fixed file is kept. See the profile YAMLs under <profiles/> for
  the full auto-apply list.
- Any other hook that fails blocks the edit: changes made by that hook are
  reverted and the failure is reported back to Claude as a `PostToolUse`
  block, so Claude can fix and retry.

### OpenTelemetry tracing

- Every hook invocation (SessionStart, PreToolUse, PostToolUse, background
  tasks) is traced to the central OTLP collector with a bearer token.
- Traces are keyed by session ID so they can be retrieved per session for
  debugging.

### MCP servers

- The `claude-sandbox-kubectl` MCP server is configured and authenticated so
  that `kubectl`-equivalent calls act as the cluster's designated Claude
  identity (see <../../../cluster/k8s/agents/claude-rbac/>). The agent
  should always prefer it over raw `Bash(kubectl ...)` for `claude-sandbox`
  operations.

### Observability

- Hook daemon logs are available on disk under the session directory for the
  duration of the session (exact path documented in <README.md>).
- A session context banner surfaces warnings from setup and background tasks
  to the agent at SessionStart.

## CLI Profile

The CLI profile targets a developer workstation where the user is already
logged in and has a `nix`/`direnv`-managed devshell. The daemon therefore
relies on the outer environment for most credentials and focuses on safety
rails.

### What the surrounding environment provides

- **Credentials come from `.envrc`** (via `direnv`), which sources the
  repo's encrypted CLI env script. `BUILDBUDDY_API_KEY`, `GITHUB_TOKEN`, and
  `DUCKTAPE_OTEL_BEARER_TOKEN` are expected to already be in the process
  environment when Claude Code launches. They reflect the **user's own**
  identity (the developer's GitHub PAT, the user's own BuildBuddy key).
- **Kubeconfig comes from `~/.kube/config`** — the user's personal cluster
  access. The daemon does not write its own kubeconfig; MCP and `kubectl`
  use whatever the user has.
- **The devshell provides `bazelisk`, `bb`, `sops`, `gh`, etc.** on PATH via
  Nix home-manager.

The daemon's job is to propagate those env vars into every Bash tool call
(since Claude Code's Bash tool does not automatically run through direnv) and
to layer the shims on top.

### CLI-specific guarantees

- **Git safety shim.** A `git` wrapper on PATH blocks footgun commands:
  - `git commit --amend` (prevents rewriting shared history)
  - `git add -A` / `git add .` (forces explicit file listing)
  - `git stash` (prevents accidental stash-and-forget)

  Blocked commands exit non-zero with a clear error and are never run.
  Read-only operations (`git stash list`, `git stash show`) are allowed.

- **direnv bridge.** Every Bash tool call sees the env exported by the
  nearest `.envrc`, so `cd`-ing between subprojects picks up the right
  devshell environment.

### What CLI does NOT do

- Does not configure an egress proxy.
- Does not set up tmpfs, mkcert, docker, or supervisor.
- Does not write a kubeconfig — the user provides one.
- Does not idle-shutdown.

## Web Profile

The Web profile targets Claude Code on the web, running inside a sandboxed
container with TLS-inspecting network egress. The surrounding environment
provides almost nothing beyond a SOPS age key and the agent's container
identity; the daemon is responsible for standing up everything else.

### What the surrounding environment provides

- The **`web_setup.sh`** bootstrap script has already run and installed Nix,
  devtools, skills, and a `settings.local.json` containing secrets needed by
  MCP servers.
- A **SOPS age key** (`SOPS_AGE_KEY`) that can decrypt the repo's
  `claude-web` secrets.
- A **TLS-inspecting egress proxy** via `HTTPS_PROXY` / `HTTP_PROXY` with a
  periodically-refreshed JWT. The pre-installed TLS inspection CA is present
  on the container filesystem.

### Web-specific guarantees

- **Managed credentials.** The daemon decrypts SOPS secrets at startup and
  injects them into the agent's environment:
  - `BUILDBUDDY_API_KEY` — shared BuildBuddy key for the claude-web identity.
  - `GITHUB_TOKEN` — the **`agentydragon-agent` machine user PAT**, not a
    personal token. The agent commits and pushes as that identity.
  - `DUCKTAPE_OTEL_BEARER_TOKEN` — for tracing.
  - Kubernetes service account token (see below).

  Credentials that the cluster rotates (e.g., the k8s service account token)
  are refreshed regularly so that long-running sessions keep working. The
  agent should never see a stale token as a session drags on.

- **Kubernetes access as `claude-code-web` ServiceAccount.** The daemon
  writes a kubeconfig pointing at the cluster API, authenticated as the
  `claude-code-web` ServiceAccount. `KUBECONFIG` is exported into the
  agent's environment, and the `claude-sandbox-kubectl` MCP server uses the
  same identity. Both `kubectl` and MCP calls land with the RBAC documented
  in <../../../AGENTS.md>.

- **GitHub fork remote.** If the machine user has a fork of the repo, the
  daemon configures it as a `fork` remote with push credentials, so that
  `git push -u fork <branch>` works without further setup.

- **Network to BuildBuddy works out of the box.** `bazelisk`, `bb`, and
  `bbr` reach BuildBuddy and the Bazel Central Registry successfully on
  the first invocation. The agent never has to configure CA bundles,
  truststores, proxy env vars, or `--remote_proxy` flags to get builds
  working over the container's constrained egress.

- **Container runtime.** Docker, supervisor, and mkcert are set up so that
  integration tests that need a local container runtime work.

- **Tmpfs caching.** Performance-sensitive caches (Bazel output base, Docker
  storage when the container root is slow) are backed by tmpfs. From the
  agent's perspective this is invisible except that Bazel is not absurdly
  slow.

- **Idle shutdown.** The daemon auto-exits after a period of inactivity so
  stale containers don't accumulate.

### What Web does NOT do

- Does **not** install the git safety shim. (Web sessions push to a fork,
  not to `devel`, so `git amend`/`add -A` are less dangerous. If this ever
  changes, update this file.)

## Observable Acceptance Criteria

These are the checks that the `/web_selfcheck` skill effectively runs as an
acceptance test against a live session. A healthy session satisfies all
applicable criteria for its profile.

### Common

1. `echo $BUILDBUDDY_API_KEY` is non-empty, and a GetUser RPC against
   `remote.buildbuddy.io` authenticates successfully.
2. `echo $GITHUB_TOKEN` is non-empty, and `GET https://api.github.com/user`
   returns the expected login (`agentydragon-agent` on web, the developer's
   own login on CLI).
3. `bbr build <trivial target> --nobuild` succeeds without TLS or proxy
   errors.
4. `bazelisk` on PATH points at the daemon's shim, and invocations are
   tagged with `session:<id>` in BuildBuddy.
5. Editing a Python file via Write/Edit triggers `ruff-format`
   (auto-applied) and, on a lint violation, the edit is blocked with a
   clear reason.
6. Pre-commit runs end to end: a throwaway commit on a scratch branch
   passes all hooks.
7. Hook daemon logs are present and contain no unhandled exceptions from
   SessionStart.
8. Tracing reaches the OTLP collector (the bearer token test returns a
   non-auth-error status).

### CLI only

1. `git commit --amend` fails with a `[git-shim] BLOCKED` error.
2. `git add -A` / `git add .` fails with a `[git-shim] BLOCKED` error.
3. `git stash` (without `list`/`show`) fails with a `[git-shim] BLOCKED`
   error.
4. A `cd` into a subproject with its own `.envrc` propagates the expected
   env vars into the next Bash tool call.

### Web only

1. `kubectl get pods -n claude-sandbox` works, authenticated as
   `claude-code-web`. The `claude-sandbox-kubectl` MCP server returns the
   same pod list.
2. `$GITHUB_TOKEN` resolves to the `agentydragon-agent` machine user (not a
   personal account).
3. `git remote -v` shows a `fork` remote with push access to the machine
   user's fork.
4. `bbr build <any target>` works out of the box. No extra flags, no
   manual `git remote` setup, no prompt asking the user to pick a remote —
   the default remote is selected automatically, and the build reaches a
   BuildBuddy runner on the first try.
5. Docker is available (`docker info` succeeds) for tests that need a local
   container runtime.

Anything that fails these criteria is a daemon bug, not a user problem. The
`/web_selfcheck` skill is the canonical runnable acceptance test for this
spec.
