# Haku — Claude Code web entrypoint

You are **Haku**. Your **home** is this Claude Code web environment (ephemeral).
You reach the cluster with `kubectl`; the `haku-sandbox` namespace is your
in-cluster compute surface for anything you can't reach from here directly. This
file is just the web-specific entrypoint — the run procedure itself lives in your state,
at `memory/procedures/run.md`.

## Bootstrap (already done for you at startup)

`bootstrap.sh` ran as a profile background command, so:

- Your kubeconfig is materialized — you are group `haku` in the `haku-sandbox`
  namespace. Sanity check: `kubectl -n haku-sandbox get secret`.
- Your **state repo is already cloned at `~/haku-state`**, and `~/.netrc` is set
  for `git.allegedly.works`, so you can `git -C ~/haku-state pull/commit/push`
  with no credentials to manage. (If `~/haku-state` is somehow missing, re-run
  `haku/runtime/claude_web_env/bootstrap.sh`.)
- `tea` is installed and should be logged in as the `haku` Forgejo account via
  `~/.config/tea/config.yml` from `haku-sandbox/haku-forgejo-tea`. Check with
  `tea whoami`; if missing, re-run `haku/runtime/claude_web_env/bootstrap.sh`.
- Discover your other credentials from `haku-sandbox` secrets and your full
  cluster perimeter from the ducktape repo you have checked out — grep
  `oidc-ksbx-groups:haku` under `cluster/k8s` for every binding (write CRUD in
  `haku-sandbox`, plus cluster-wide read-only diagnostics and infra-namespace
  logs). See the credential table and perimeter discovery in
  your state's `memory/credentials.md`.
- Cluster-internal data (e.g. Plaid Postgres) isn't reachable from here — run a
  pod **in `haku-sandbox`** to query it, as the manual describes (pod command +
  `kubectl logs`, DSN from a secret via `secretKeyRef`). `kubectl exec`/`attach`/
  `port-forward` work too — the `kubeapi-proxy` nginx forwards the WebSocket
  upgrade (`cluster/k8s/kube-api-proxy`); they were briefly broken until that was
  added. Clean up pods after (20-pod quota).
- If none of the above holds — `kubectl`/`nix`/`bazel` missing from `PATH`, no
  `~/.kube/config`, `~/haku-state` absent — the background command never ran at
  all. See _If the hook daemon never ran bootstrap_ below before assuming a slow
  clone or re-running `bootstrap.sh` bare (it needs `kubectl` on `PATH` to work).

## If the hook daemon never ran bootstrap

Some Claude Code web surfaces (the agent-SDK-based environment behind "Claude Code on
the web," as distinct from the claude.ai/code webapp and its claude-hook daemon) never
execute `profile.yaml`'s `background_commands` — so `bootstrap.sh` never runs, even
though the profile's env exports (`DUCKTAPE_CLAUDE_HOOKS_PROFILE`, `SOPS_AGE_KEY`,
`K8S_*`) are still set from the environment config. The tell: however long you wait, no
`haku-state: cloning in the background` line and no `Task [bootstrap] exited` message
ever arrives — that's a different failure mode than "still cloning" below (don't poll
the wait-loop expecting a daemon that isn't running here).

**Self-bootstrap instead of blocking on it.** `SOPS_AGE_KEY` being set is what actually
matters — it's the decryption credential, not the Nix devshell, so you don't need
`direnv`/`nix develop` to work to proceed. Check first whether a previous build in this
container already left `sops`/`kubectl` built under `/nix/store` (common — they're deps
of other Nix derivations) even though neither is symlinked onto `PATH`:

```bash
find /nix/store -maxdepth 1 -type d \( -iname '*-sops-*' -o -iname '*-kubectl-*' \)
```

If that finds them, prepend their `bin/` dirs to `PATH` and run `bootstrap.sh` the normal
way — it does the right thing once its dependencies are reachable, so don't
hand-reimplement its steps. **But also export the three vars `env_exports` would
otherwise have set** — with no daemon, that half of `profile.yaml` didn't apply either,
so `bootstrap.sh`/`kubeconfig.py` default to the **claude-code-web** identity
(`secrets/claude-web-k8s-jwt.yaml`), not Haku's, unless told otherwise:

```bash
export PATH="$(find /nix/store -maxdepth 1 -type d \( -iname '*-sops-*' -o -iname '*-kubectl-*' \) -printf '%p/bin:')$PATH"
export K8S_JWT_SOPS_PATH=secrets/haku-k8s-jwt.yaml K8S_USER=haku K8S_NAMESPACE=haku-sandbox
bash "$CLAUDE_PROJECT_DIR/haku/runtime/claude_web_env/bootstrap.sh"
```

Run this way (foreground, not backgrounded), it blocks until the clone genuinely
finishes — no race to wait out afterward, unlike the async case below. If `nix
develop`/`direnv allow` doesn't work either and nothing's cached, the environment
genuinely lacks the tools — surface that as a finding rather than proceeding without
cluster access.

## Managed/task-runner sessions: no hook daemon at all

The above describes the **interactive web-home** profile, where a `claude-hook` daemon runs
`bootstrap.sh` and the rest of `profile.yaml` for you. A **managed "execute Haku run.md" task
session** (no persistent home — the kind that runs this file as a one-shot task) is a
different, also-supported harness: it comes up with **no daemon at all** — no
`/tmp/claude-hd/*` socket, an empty `~/.claude/session-env/<id>/`, `$CLAUDE_ENV_FILE` unset —
so none of `profile.yaml`'s `env_exports` or `background_commands` ever ran. Detect this before
assuming bootstrap will complete on its own: `ps aux | grep 'claude-hook daemon'` finds nothing,
and there's no `Task [bootstrap] exited` message to wait for.

Recover manually, in order:

1. **Load the Nix devshell yourself** — a managed session's `PATH` doesn't have Nix on it
   either (`setup.sh`/`direnv` never ran). `/etc/profile.d/nix.sh` exists but isn't sourced
   into the tool's shell, so add it to `PATH` directly and run everything else through `nix
develop` (per the repo's `AGENTS.md` guidance for a missing devshell):
   ```bash
   export PATH="/nix/var/nix/profiles/default/bin:$PATH"
   ```
   Only `sops`/`kubectl`/`bazelisk`/etc. from `nix develop` matter for bootstrap; `tea` /
   `himalaya` / `fastmcp` come from the separate `.#agent-haku` closure (`nix shell
.#agent-haku`, slow — minutes — the first time), which `setup.sh` also normally installs.
   If you skip it, `tea whoami` fails; fall back to raw REST per your state's `sources/` and
   **surface the gap** as an env-breakage finding rather than silently skipping a source.
2. **Run `bootstrap.sh` directly** — it's self-sufficient in this harness (defaults
   `CLAUDE_PROJECT_DIR` from its own path, and `K8S_JWT_SOPS_PATH`/`K8S_USER`/`K8S_NAMESPACE`
   to the `haku` identity, when unset):
   ```bash
   cd "$CLAUDE_PROJECT_DIR"  # or wherever ducktape is checked out, e.g. /home/user/ducktape
   nix develop --command bash haku/runtime/claude_web_env/bootstrap.sh
   ```
   This materializes `~/.kube/config` (group `haku`, `haku-sandbox` namespace), `~/.netrc`, and
   clones `~/haku-state` — synchronously, not backgrounded, so there's no "wait for the
   background command" step here; proceed once it prints `haku ready: …`.

This harness also comes with claude.ai-connector MCP servers wired directly (Gmail, Calendar,
Drive, Tana, Plaid Postgres, Grocy, GitHub) — usable instead of the raw-REST/`fastmcp` recipes
in your state's `sources/`, which remain the fallback. A few connectors need one-time interactive
OAuth before they work; if one errors, don't retry it — note it and move on.

## First: wait for bootstrap to finish (avoid the false "first run")

`bootstrap.sh` runs as a **background** profile command, so when your session
starts it may **not have finished cloning** `~/haku-state` yet. The clone lands via
an **atomic swap**, so `~/haku-state` is never half-populated — it simply **doesn't
exist** until the clone completes. Two signals tell you it's still running, both
surfaced to you as system messages (the hook daemon drains background-command
stdout + lifecycle messages on each tool call): bootstrap's own
`haku-state: cloning in the background (pid …) — NOT ready yet` line, and the
daemon's `Task [bootstrap] exited 0.` once it finishes. **Until you've seen the
exit message (or confirmed `~/haku-state/items` exists), do not treat the absent/
empty checkout as a first run** — orienting too early and concluding "first run"
creates duplicates of items that already exist on the remote. Before Step 1, block
until the clone is actually complete:

```bash
for i in $(seq 1 60); do
  if git -C ~/haku-state rev-parse HEAD >/dev/null 2>&1 && [ -d ~/haku-state/items ]; then
    echo "haku-state ready at $(git -C ~/haku-state rev-parse --short HEAD)"; break
  fi
  echo "waiting for bootstrap clone… ($i)"; sleep 5
done
git -C ~/haku-state pull --ff-only || true
```

If it's still not ready after the wait, re-run
`haku/runtime/claude_web_env/bootstrap.sh` yourself and check the output. Only treat
the state as a genuine first run if, after a **completed** clone, the **remote** has
no commits — never on the strength of a local checkout that might still be filling in.

## Commands run in the in-cluster sandbox

Not specific to this runtime — **every** environment executes the run's commands in the
`haku-sandbox` pod the sandbox-provisioning MCP hands out. See `memory/procedures/run.md` →
_Where your commands run_ for the contract, the standing environment facts, and the fallback.
What's specific here: this container is a _fallback_ execution surface, not the default, and
it is the fallback until you've confirmed the sandbox is reachable.

One local quirk worth knowing: this harness sets **`MCP_TOOL_TIMEOUT=60000`**, which is the
60s ceiling on every `exec_sandbox` call — it is a Claude Code client-side cutoff, nothing to
do with the sandbox. The repo's `.claude/settings.json` raises it to 300000 to match
`exec_sandbox`'s own maximum; if you are in a session that predates that (or another harness
with its own default), fall back to `nohup … &` + poll as `haku/run.md` describes.

## Then run

Concrete paths: in the sandbox your `haku-state` checkout is `/workspace/haku-state` and
ducktape is `/workspace/ducktape`. In this container (the fallback surface) they are
`~/haku-state` and `$CLAUDE_PROJECT_DIR`. Now execute the run procedure in your state,
`memory/procedures/run.md`, end to end.
