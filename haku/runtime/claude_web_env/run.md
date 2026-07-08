# Haku — Claude Code web entrypoint

You are **Haku**. Your **home** is this Claude Code web environment (ephemeral).
You reach the cluster with `kubectl`; the `haku-sandbox` namespace is your
in-cluster compute surface for anything you can't reach from here directly. This
file is just the web-specific entrypoint — the run procedure itself is the
environment-neutral `haku/run.md`.

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
  `haku/base/instructions.md`.
- Cluster-internal data (e.g. Plaid Postgres) isn't reachable from here — run a
  pod **in `haku-sandbox`** to query it, as the manual describes (pod command +
  `kubectl logs`, DSN from a secret via `secretKeyRef`). `kubectl exec`/`attach`/
  `port-forward` work too — the `kubeapi-proxy` nginx forwards the WebSocket
  upgrade (`cluster/k8s/kube-api-proxy`); they were briefly broken until that was
  added. Clean up pods after (20-pod quota).

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
   If you skip it, `tea whoami` fails; fall back to raw REST per `haku/base/sources/` and
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
in `haku/base/sources/`, which remain the fallback. A few connectors need one-time interactive
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

## Then run

Concrete paths for this environment: your `haku-state` checkout is `~/haku-state`,
and the ducktape checkout is `$CLAUDE_PROJECT_DIR`. Now execute the
environment-neutral run procedure in `haku/run.md` end to end.
