@README.md

## Target Platform

Linux by default. macOS-only components (Seatbelt, Sandboxer) are explicitly documented.

@STYLE.md

## Recovering from a Broken Session Start Hook (Claude Code Web)

When running in Claude Code Web (`CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE` is set), the
session start hook sets up Bazel, the auth proxy, TLS CA, k8s secrets, BuildBuddy RBE,
and other tooling. Symptoms of failure: certificate errors, `bazel: command not found`,
`Unable to resolve host remote.buildbuddy.io`, missing env files.

**Recovery: read the implementation and replicate it manually.**

1. Read <devinfra/claude/hook_daemon/session_start/handler.py> (`_setup_web()`) for the full setup sequence
2. Read <devinfra/claude/README.md> for architecture context
3. Read `.claude_hooks/config.yaml` for k8s server, namespace, secret mappings
4. Read <devinfra/claude/config/bazelrc.mako> for the session bazelrc template
5. Read <devinfra/setup_buildbuddy.sh> for BuildBuddy configuration

**Do not skip steps.** In particular:

- **k8s secrets setup** fetches the BuildBuddy API key (`DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN` env var). Without it, RBE is unavailable.
- **BuildBuddy setup** (`devinfra/setup_buildbuddy.sh`) provides remote cache/execution.

Check the hook daemon log first: `tail -100 ~/.claude/session-env/<session_id>/hook-daemon/daemon.log`

### Quick Recovery: Reuse a Previous Session

If a previous session completed successfully, you can bootstrap from its files:

1. **Find the active daemon**: `ps aux | grep hook_daemon` — note the session ID from the `--sock` argument
2. **Find a previous working session**: look for `sessionstart-hook-0.sh` under `~/.claude/session-env/*/`
3. **Trigger SessionStart on the live daemon** via `curl --unix-socket /tmp/claude-hd/<live_session_id>/d.sock`:
   ```bash
   LIVE=<live_session_id>
   SOCK=/tmp/claude-hd/$LIVE/d.sock
   # Build JSON with hook={hook_event_name: SessionStart, session_id: $LIVE, ...} and env=os.environ
   python3 -c "
   import json, os
   env = dict(os.environ)
   env['CLAUDE_ENV_FILE'] = f'/root/.claude/session-env/$LIVE/sessionstart-hook-0.sh'
   env['CLAUDE_PROJECT_DIR'] = '/home/user/ducktape'
   env['CLAUDE_CODE_REMOTE'] = 'true'
   print(json.dumps({'hook': {'hook_event_name': 'SessionStart', 'session_id': '$LIVE',
     'cwd': '/home/user/ducktape', 'transcript_path': '/tmp/transcript.json',
     'source': 'startup'}, 'env': env}))
   " | curl -s --max-time 300 --unix-socket $SOCK http://localhost/hook -X POST \
     -H 'Content-Type: application/json' -d @-
   ```
4. If the hook completes, `source ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh`
5. If the hook 500s (e.g. k8s secrets timeout), manually assemble from partial results:
   - The daemon typically creates `bin/bazel` (wrapper), `auth-proxy/` (CA certs, truststore), and starts supervisor+docker before failing
   - Write the `bazelrc` manually from <devinfra/claude/config/bazelrc.mako> — key vars: `remote_proxy_sock` = `/tmp/claude-hd/$LIVE/remote-proxy.sock`, `truststore_path` = `$SD/auth-proxy/cacerts.jks`, `combined_ca_path` = `$SD/auth-proxy/combined_ca.pem`
   - Copy secrets (`GITHUB_TOKEN`, `BUILDBUDDY_API_KEY`) from a previous session's env file
   - Write and source a minimal env file (see below)
   - Patch the bazel wrapper's `DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR` if it was copied from another session

### Minimal env file template

```bash
SD=~/.claude/session-env/<session_id>
cat > "$SD/sessionstart-hook-0.sh" << 'EOF'
export PATH="<session_dir>/bin:$PATH"
export SESSION_BAZELRC="<session_dir>/bazelrc"
export BAZELISK_PATH="~/.cache/claude-hooks/bin/bazelisk"
export DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR="<session_dir>"
export DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT="19001"
export SSL_CERT_FILE="<session_dir>/auth-proxy/combined_ca.pem"
export REQUESTS_CA_BUNDLE="<session_dir>/auth-proxy/combined_ca.pem"
export CURL_CA_BUNDLE="<session_dir>/auth-proxy/combined_ca.pem"
export NODE_EXTRA_CA_CERTS="<session_dir>/auth-proxy/combined_ca.pem"
export DOCKER_HOST="unix:///var/run/docker.sock"
export GITHUB_TOKEN="<from old session or k8s>"
export BUILDBUDDY_API_KEY="<from old session or k8s>"
export DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS="0"
export NO_PROXY="localhost,127.0.0.1,169.254.169.254,metadata.google.internal,*.svc.cluster.local,*.local"
export no_proxy="$NO_PROXY"
EOF
```

**Do NOT** bypass certificate/proxy errors with `--noverify`, `SSL_VERIFY=false`, etc. The root cause is always a missing/broken session start hook. Notify the user.

## Sandbox

Run `bazel`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl`, and other network/system commands **outside the sandbox** (`dangerouslyDisableSandbox: true`). The sandbox blocks their network calls (including localhost, e.g., `kubectl` to haproxy on `localhost:7445`).

## Refactoring

When renaming/moving/deleting files or symbols, search **all references** across the entire codebase (imports, BUILD files, CI configs, docs, Dockerfiles, k8s manifests). Missing a reference is worse than being thorough.

**Atomic API changes**: update all callers in the same commit. No transitional shims within this monorepo.

## Before Hand-off

```bash
bazel build //...
bazel test //...
```

Lint (ruff + mypy) runs by default. Use `--config=nolint` to skip.
If you touched `ansible/`, also follow <ansible/AGENTS.md>.

## Git

**NEVER amend a commit that has already been pushed.**

**NEVER use `git reset --soft` to squash onto a base branch that has moved on the remote.** `git reset --soft origin/devel` collapses _all_ differences between HEAD and `origin/devel` into the staging area — including commits other people landed on devel since your branch diverged. The resulting "squashed" commit silently re-applies every upstream change as if it were yours. Use `git rebase origin/devel` first to rebase, then squash with `git reset --soft $(git merge-base HEAD origin/devel)` so only your branch's changes are staged.

## Debug Notes

Convention: `<subproject>/debug/<topic>.md` for persistent investigation notes (RCAs, debug logs). Examples: `debug/spice_lag/README.md`, `debug/wyrm-oom/INVESTIGATION.md`. The `cluster/` subproject uses `cluster/docs/lessons_learned/` instead.

## Plans

`plans/` directories are for future work or work in progress. Once a plan is fully completed, remove it from `plans/` (delete, or squash into a short tombstone/summary elsewhere).

## TODO Tracking

Subprojects use `TODO.md` for persistent TODO tracking. TODOs local to a specific code location are fine as inline comments; cross-cutting or project-level TODOs belong in `TODO.md`.

## Testing

**Always use Bazel**, not direct pytest/python:

```bash
bazel test //path/to:test_target
bazel run //path/to:binary_target
```

**CRITICAL gotcha**: All `py_test` targets MUST have a `pytest_bazel.main()` entry point. Without it, Bazel runs the file as a script which exits 0 without running tests. Add `@pypi//pytest_bazel` to deps.

```python
import pytest_bazel
# ... tests ...
if __name__ == "__main__":
    pytest_bazel.main()
```

**pytest-asyncio auto mode**: configured via `conftest.py` hooks. Do NOT add `@pytest.mark.asyncio` decorators.

**No test skips for missing tools**: let the test fail. Tools come from Bazel runfiles or the RBE worker image.

**Test timeouts mean hangs, not slowness**: When a test times out, assume it is wedged — an internal operation is waiting on something that will never arrive (deadlock, stuck future, container that never becomes ready, connection to a port nothing is listening on). Do NOT bump `size`/`timeout` as a fix. Instead, trace the execution to find what is blocked: run with `--test_output=streamed --test_arg=-s`, add logging around fixture setup, check for stuck containers (`docker ps`), etc. A test that ran in 35s last week and now times out at 60s is not "slow" — something broke internally.

### Updating syrupy snapshots

Snapshot tests use syrupy (`.ambr` files in `__snapshots__/`). To update after intentional changes, run locally (not RBE) so syrupy can write through the execroot symlinks to the source tree:

```bash
bazel test //path/to:snapshot_test \
  --test_arg=--snapshot-update \
  --remote_executor="" \
  --nocache_test_results
```

Then commit the updated `.ambr` files.

### Live OpenAI API Tests

Use `live_openai_py_test` from `//openai_utils/testing:testing.bzl`. Generates `.mock` and `.live` targets. CI excludes `.live` via `--test_tag_filters=-live_openai_api`.

```python
# test_foo.py
async def test_mock(mock_client): ...

@pytest.mark.live_openai_api
async def test_live(live_openai): ...
```

## JavaScript / TypeScript

Uses `@aspect_rules_js`. **Do NOT run raw `pnpm install`** -- Bazel manages pnpm (pinned in `MODULE.bazel`).

Adding deps: add to `package.json`, run Bazel (first build updates lockfile and fails), run again, commit `pnpm-lock.yaml`.

See <props/frontend/AGENTS.md> for frontend conventions.
