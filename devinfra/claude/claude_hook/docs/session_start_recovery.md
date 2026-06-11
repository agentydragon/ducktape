# Session Start Hook Recovery

Use this when a Claude Code session shows setup failures such as certificate
errors, `bazel: command not found`, missing BuildBuddy credentials, or
`Unable to resolve host remote.buildbuddy.io`.

Do not bypass proxy or certificate failures with `--noverify`,
`SSL_VERIFY=false`, or similar. The durable fix is to recover the session-start
hook path.

## Check The Live Session

Find the live Rust daemon session ID:

```bash
LIVE=$(ps aux | grep 'claude-hook daemon' | grep -v grep | grep -oP '(?<=--sock /tmp/claude-hd/)[^/]+')
echo "$LIVE"
```

If that fails, derive the session ID from the env file path:

```bash
LIVE=$(basename "$(dirname "${CLAUDE_ENV_FILE:-}")")
echo "$LIVE"
```

Then inspect the daemon logs:

```bash
tail -100 "/tmp/claude-hd/$LIVE/daemon.err.log" 2>/dev/null
tail -100 "/tmp/claude-hd/$LIVE/daemon.log" 2>/dev/null
```

Check whether the agent env file was written:

```bash
head -20 "$HOME/.claude/session-env/$LIVE/sessionstart-hook-0.sh" 2>/dev/null
```

If the env file exists, source it and verify the basics:

```bash
source "$HOME/.claude/session-env/$LIVE/sessionstart-hook-0.sh"
command -v claude-hook
command -v bazelisk
bazelisk info
```

## Re-Trigger SessionStart

If the daemon is alive but the env file is missing or incomplete, re-trigger
`SessionStart` on the existing daemon:

```bash
LIVE=<live_session_id>
SOCK="/tmp/claude-hd/$LIVE/d.sock"
python3 -c "
import json, os
env = dict(os.environ)
env['CLAUDE_ENV_FILE'] = os.path.expanduser(f'~/.claude/session-env/$LIVE/sessionstart-hook-0.sh')
env['CLAUDE_PROJECT_DIR'] = os.getcwd()
print(json.dumps({'hook': {'hook_event_name': 'SessionStart', 'session_id': '$LIVE',
  'cwd': os.getcwd(), 'transcript_path': '/tmp/transcript.json',
  'source': 'startup'}, 'env': env}))
" | curl -s --max-time 300 --unix-socket "$SOCK" http://localhost/hook -X POST \
  -H 'Content-Type: application/json' -d @-
```

Then source the env file and retry the failing command.

## Manual Recovery

If no daemon can be started, rebuild the minimum useful environment:

```bash
# Web profile: requires SOPS_AGE_KEY in the Claude process environment.
source devinfra/secrets/web_env.sh

SD="$HOME/.claude/session-env/<LIVE>"
mkdir -p "$SD/bin"
cat > "$SD/sessionstart-hook-0.sh" <<EOF
export BUILDBUDDY_API_KEY=${BUILDBUDDY_API_KEY}
export GITHUB_TOKEN=${GITHUB_TOKEN:-}
export DUCKTAPE_OTEL_BEARER_TOKEN=${DUCKTAPE_OTEL_BEARER_TOKEN:-}
export PATH="$SD/bin:\$PATH"
EOF
chmod 600 "$SD/sessionstart-hook-0.sh"
```

This is only a temporary debugging environment; it does not recreate the Rust
daemon, mailbox, background tasks, or shims. If manual recovery is needed in a
real session, report that the session-start hook is broken.

## Stale Install

If the installed hook binary or Python statusline is behind the repo pin, rerun
the setup path:

```bash
bash devinfra/claude/web_setup.sh
```

Then start a fresh session or re-trigger `SessionStart` using the recipe above.
