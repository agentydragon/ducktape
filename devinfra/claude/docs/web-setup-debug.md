# Web Setup Script Debugging

Current runbook for `devinfra/claude/web_setup.sh` failures in Claude Code web
sessions.

Older pre-Firecracker setup investigations are preserved in git history. Keep
this file focused on the current Firecracker environment and failure modes that
still affect live sessions.

## First Checks

Check the setup script that actually ran:

```bash
ls -la /tmp/web-setup.log 2>/dev/null || echo "MISSING"
head -5 /tmp/web-setup.log 2>/dev/null
tail -20 /tmp/web-setup.log 2>/dev/null
```

The first line should include `web_setup.sh commit: <sha>`, and the last line
should be `Setup complete.`. On persistent Firecracker rootfs, the log can come
from an older session, so compare it with the repo checkout:

```bash
SETUP_COMMIT=$(grep 'web_setup.sh commit:' /tmp/web-setup.log 2>/dev/null | tail -1 | grep -oE '[0-9a-f]{40}')
HEAD_COMMIT=$(git -C /home/user/ducktape rev-parse HEAD)
[ "$SETUP_COMMIT" = "$HEAD_COMMIT" ] && echo "OK" || echo "STALE: setup=$SETUP_COMMIT head=$HEAD_COMMIT"
```

Check the hook daemon logs next:

```bash
LIVE=${CLAUDE_ENV_FILE:+$(basename "$(dirname "$CLAUDE_ENV_FILE")")}
tail -100 "/tmp/claude-hd/$LIVE/daemon.err.log" 2>/dev/null
tail -100 "/tmp/claude-hd/$LIVE/daemon.log" 2>/dev/null
```

## Known Failure Modes

### Stale Setup Script URL

Anthropic may cache the setup script URL at configuration time. If a web UI
configuration points at a branch-ref raw GitHub URL, new sessions can continue
receiving the older fetched script after the branch moves.

When debugging freshness, use a commit-pinned URL or ensure the configured URL
changes after the fix lands. The script also prints its own commit to
`/tmp/web-setup.log`, which is the runtime truth.

### Local Nix Builds Disabled

If Nix reports local builds are disabled, or exits with a confusing assertion
failure after build failures, dump the active Nix config:

```bash
nix config show | rg 'max-jobs|sandbox|substituters|trusted-public-keys'
```

`web_setup.sh` passes `--max-jobs auto` on profile installs so trivial
`symlinkJoin` / profile wrapper derivations can build locally when they miss the
cache. Do not set `max-jobs=0` for this environment.

### Non-Deterministic Hook Release Pins

If `claude-hooks` releases or `nix/artifact-pins.json` churn without code
changes, inspect the wheel payload for stamped files. A previous issue was
caused by `devinfra/_build_status.txt` entering the wheel dependency tree and
changing every build. The fix was to keep build stamping out of the runtime
wheel closure.

### Pin Drift On Persistent Rootfs

**Symptom**: SessionStart crashes during template render, for example
`'Undefined' object has no attribute '<field>'`. Alternatively, profile YAML
fields silently no-op and expected env vars are missing.

**Root cause**: `nix profile install "${FLAKE}#devtools"` is add-if-missing,
not install-or-upgrade. Firecracker microVMs can persist the rootfs across
sessions, and `web_setup.sh` re-runs each session. Without a forced remove or
upgrade, the installed `claude-hooks` wheel can stay at the first-boot store
path while the repo checkout and `nix/artifact-pins.json` move forward.

**Current fix**: `web_setup.sh` runs `nix profile remove devtools || true`
before `nix profile install`, forcing re-evaluation of `.#devtools` against the
current flake on every session. The remove/install pair is idempotent and cheap
in steady state because Nix substitutes unchanged closure paths from cache.

Diagnose future occurrences with:

```bash
# Check daemon error log for profile/schema/template crashes.
tail -100 /tmp/claude-hd/*/daemon.err.log 2>/dev/null

# Inspect the pin URL.
jq -r '.pins["claude-hooks"].url' nix/artifact-pins.json

# Confirm the installed binary resolves from the Nix profile.
readlink -f /nix/var/nix/profiles/default/bin/claude-hook
claude-hook --version
```

## Lessons

- `nix profile install` is "add if missing"; pair it with remove or upgrade on
  persistent rootfs when the selected flake output must track the current repo.
- Schema-level changes in `claude-hooks` profiles or templates can break live
  sessions until the installed wheel catches up.
- Pydantic's default `extra="ignore"` can silently drop new config fields.
  Consider `extra="forbid"` where schema drift should crash loudly.
- Put actionable setup output near the end of the script log; the Claude Code
  UI may show only the tail.
