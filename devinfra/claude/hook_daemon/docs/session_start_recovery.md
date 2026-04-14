# Session Start Hook Recovery (Claude Code Web)

When running in Claude Code Web (`CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE` is set), the
session start hook sets up Bazel, the auth proxy, TLS CA, secrets, BuildBuddy RBE,
and other tooling. Symptoms of failure: certificate errors, `bazelisk: command not found`,
`Unable to resolve host remote.buildbuddy.io`, missing env files.

**Check the daemon log first:**

```bash
LIVE=$(ps aux | grep hook_daemon | grep -v grep | grep -oP '(?<=--sock /tmp/claude-hd/)[^/]+')
tail -100 ~/.claude/session-env/$LIVE/hook-daemon/daemon.log
```

**Recovery: read the implementation to understand what failed.**

1. Read <devinfra/claude/hook_daemon/session_start/handler.py> for the full setup sequence
2. Read <devinfra/claude/README.md> for architecture context
3. Read <devinfra/claude/hook_daemon/profiles/web/profile.yaml> for the web profile config (env scripts, proxy, k8s settings)

**Key facts about secrets:**

Secrets are populated by profile-configured env scripts (`devinfra/secrets/*.sh`), sourced
at daemon startup. The env script is specified in the profile YAML (`env_script` field).
Web mode uses `devinfra/claude/hook_daemon/profiles/web/profile.yaml` → `devinfra/secrets/web_env.sh`.

- `BUILDBUDDY_API_KEY` — set by env script (decrypted from SOPS by the script)
- `GITHUB_TOKEN` — set by env script
- `DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN` — injected by cluster at container start (fallback)

Without `BUILDBUDDY_API_KEY`, RBE is unavailable. All other secrets are non-critical.

## Step 1: Check if the env file was already written

A 500 response from the hook daemon often means the response _rendering_ failed, not the
setup itself — the daemon frequently writes the env file before the template error occurs.
Always check:

```bash
LIVE=$(ps aux | grep hook_daemon | grep -v grep | grep -oP '(?<=--sock /tmp/claude-hd/)[^/]+')
head -3 ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh 2>/dev/null
# If it has the CANARY marker, source it:
source ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh
bazelisk info  # verify it works
```

## Step 2: Re-trigger SessionStart on the live daemon

If the env file is missing or incomplete, re-trigger the hook:

```bash
LIVE=<live_session_id>
SOCK=/tmp/claude-hd/$LIVE/d.sock
python3.13 -c "
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
# Then source regardless of HTTP status (500 may still mean env file was written):
source ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh
```

## Step 3: Manual assembly (daemon unavailable or env file missing)

If the daemon is down, source the env script directly:

```bash
# Source the web env script — populates BUILDBUDDY_API_KEY, GITHUB_TOKEN, etc.
# Requires SOPS_AGE_KEY in env (always present in web containers).
source /home/user/ducktape/devinfra/secrets/web_env.sh
```

**Configure BuildBuddy** (writes `~/.config/bazel/buildbuddy.bazelrc`):

```bash
mkdir -p ~/.config/bazel
cat > ~/.config/bazel/buildbuddy.bazelrc <<EOF
common --remote_header=x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}
build --config=rbe
EOF
```

**Assemble the minimal env file** — copy from a previous session and patch the session ID:

```bash
PREV=$(ls ~/.claude/session-env/ | grep -v "$LIVE" | head -1)
SD=~/.claude/session-env/$LIVE
mkdir -p "$SD"
sed "s|$PREV|$LIVE|g" ~/.claude/session-env/$PREV/sessionstart-hook-0.sh > "$SD/sessionstart-hook-0.sh"
source "$SD/sessionstart-hook-0.sh"
```

If no previous session exists, write from scratch (fill in `<LIVE>` with the session ID):

```bash
SD=~/.claude/session-env/<LIVE>
mkdir -p "$SD/auth-proxy" "$SD/bin"
cat > "$SD/sessionstart-hook-0.sh" <<'ENVEOF'
export PATH="<SD>/bin:$PATH"
export SESSION_BAZELRC="<SD>/bazelrc"
export DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR="<SD>"
export DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT="19001"
export SSL_CERT_FILE="<SD>/auth-proxy/combined_ca.pem"
export REQUESTS_CA_BUNDLE="<SD>/auth-proxy/combined_ca.pem"
export CURL_CA_BUNDLE="<SD>/auth-proxy/combined_ca.pem"
export NODE_EXTRA_CA_CERTS="<SD>/auth-proxy/combined_ca.pem"
export DOCKER_HOST="unix:///var/run/docker.sock"
export GITHUB_TOKEN="<from env script above>"
export BUILDBUDDY_API_KEY="<from env script above>"
export DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS="0"
export NO_PROXY="localhost,127.0.0.1,169.254.169.254,metadata.google.internal,*.svc.cluster.local,*.local"
export no_proxy="$NO_PROXY"
ENVEOF
```

Note: without the auth proxy CA (`combined_ca.pem` and `cacerts.jks`), Bazel repository
rules and the JVM truststore won't work. The CA files are created by the daemon. If the
daemon is completely unavailable, re-run `devinfra/claude/web_setup.sh` to reinstall it.

**Verify**:

```bash
bazelisk info  # should show output_base
```

**Do NOT** bypass certificate/proxy errors with `--noverify`, `SSL_VERIFY=false`, etc. The
root cause is always a missing/broken session start hook. Notify the user.

## Failure mode: installed wheel drifted behind the pin

**Symptom**: SessionStart returns 500, and `daemon.err.log` contains one of:

- `AttributeError: 'Undefined' object has no attribute '<field>'` from a Mako template render
- `TypeError: <handler> got an unexpected keyword argument '<field>'`
- Silently missing env vars (`BUILDBUDDY_API_KEY`, `GITHUB_TOKEN`, `KUBECONFIG`)
  because a new `profile.yaml` field was silently dropped by Pydantic

**Root cause**: the installed `claude-hooks` wheel was pinned on container
first-boot and has never been refreshed. Meanwhile the working tree has moved
forward and introduced schema-level changes in `profile.yaml` / `context.mako`
/ `config.py` that the old wheel doesn't understand.

This is structural — see <../../docs/web-setup-debug.md> "Pin drift on
persistent rootfs". The short version: Firecracker rootfs is persistent, and
`environment-manager`'s `Initialize` re-runs `init_script` (web_setup.sh) on
every session, but `nix profile install` is a no-op if the attrpath is already
in the profile, so without an explicit `nix profile remove` the version
freezes at first-boot.

**Diagnose**:

```bash
# 1. Confirm wheel drift
readlink /nix/var/nix/profiles/default/bin/claude-hook
python3 -c "import json; p=json.load(open('npins/sources.json'))['pins']['claude-hooks']; print(p['url'])"
# The hash on the readlink line and the commit SHA in the URL should match.

# 2. Confirm the specific crash
tail -100 ~/.claude/session-env/*/hook-daemon/daemon.err.log
```

**Recover**:

```bash
# Re-run web_setup.sh — this now does `nix profile remove devtools` first,
# so it actually pulls forward to the current pin.
bash devinfra/claude/web_setup.sh

# Then re-trigger SessionStart using the Step 2 recipe above (or just start
# a fresh session — the new wheel will be picked up automatically).
```

If the container is from before the `web_setup.sh` fix landed (pre-commit on
devel adding `nix profile remove` before `install`), the re-run from a stale
container will itself be stale. In that case you need a fresh container from
the Claude Code web UI.
