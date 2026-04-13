---
name: web_selfcheck
description: >
  Diagnose the health of a Claude Code session (CLI or web) by running the
  observable acceptance criteria in the hook daemon SPEC against the live
  session. Also runs out-of-SPEC diagnostics (web_setup.sh freshness,
  claude-hooks pin staleness, bbr runner recycling, git hook origin-URL
  issues). Use when the user asks "did setup go ok", "why isn't bbr
  working", "check credentials", "selfcheck", "why do my commits fail", or
  any question about session health.
---

# Session Selfcheck

This skill is the **runnable acceptance test** for the hook daemon
specification at <../../devinfra/claude/hook_daemon/SPEC.md>.

## How to use this skill

1. **Read SPEC.md first.** It enumerates every behavior a healthy session
   must satisfy, split into `### Common`, `### CLI only`, and
   `### Web only` under the `## Observable Acceptance Criteria` heading.
   The SPEC is the source of truth. If the SPEC and this skill disagree,
   the SPEC wins — update the skill.
2. **Detect the profile.** `$DUCKTAPE_CLAUDE_HOOKS_PROFILE` (or the file
   path that the daemon was launched with) tells you whether to run the
   CLI or Web criteria. Always run the Common criteria.
3. **For each SPEC criterion, run the matching check** from the
   "SPEC acceptance checks" section below.
4. **Then run the out-of-SPEC diagnostics** section, which catches
   real-world failure modes the SPEC does not (yet) codify.
5. **Produce the report** using the format at the end.

Run all `Bash` commands with `dangerouslyDisableSandbox: true` (needs
network and filesystem access outside the sandbox). Run independent checks
in parallel where possible.

## CRITICAL: observe only — do NOT fix without explicit user approval

This is a **diagnostic skill**. Treat a broken session like a crime scene:
observe, document, and report — do not touch.

**Do NOT run any remediation commands** (e.g. `web_setup.sh`, re-triggering
SessionStart, sourcing env files, installing packages, re-running
`git remote add`) unless the user explicitly says to proceed. If a check
fails, the fix is "the daemon is broken, tell the user" — not "let me
work around it."

**Exception — debugging workarounds**: when the session hooks are
demonstrably broken and you are actively debugging or documenting, the
following lightweight workarounds are acceptable without explicit
approval:

- Committing with hooks bypassed: `git commit --no-verify` to record
  diagnostic work while hooks are broken
- Unsetting `BUILDBUDDY_API_KEY` to force local bazel when bbr is broken
- Creating a `bazel` wrapper in the session bin that injects `--bazelrc`
  when the session bazelrc exists but the shim is missing

## SPEC acceptance checks

Each check below corresponds one-to-one with a numbered criterion in
SPEC.md. Cross-reference the SPEC for the authoritative statement of what
the check is verifying.

### Common

**C1 — BUILDBUDDY_API_KEY is present and valid.**

```bash
[ -n "${BUILDBUDDY_API_KEY:-}" ] || echo "FAIL: BUILDBUDDY_API_KEY unset"
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "x-buildbuddy-api-key: ${BUILDBUDDY_API_KEY}" \
  -H "Content-Type: application/proto" \
  --data-binary '' \
  https://remote.buildbuddy.io/rpc/BuildBuddyService/GetUser
```

Pass: `200` (or `400` = malformed proto but auth passed). `401`/`403` =
invalid key.

**C2 — GITHUB_TOKEN is present and valid.**

```bash
curl -s -H "Authorization: Bearer ${GITHUB_TOKEN}" https://api.github.com/user \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('login:', d.get('login'), 'message:', d.get('message',''))"
```

Pass on web: `login: agentydragon-agent`. Pass on CLI: the user's own
GitHub login. `Bad credentials` = expired/revoked.

**C3 — `bbr build <trivial>` succeeds without TLS or proxy errors.**

```bash
cd /home/user/ducktape
bbr build //devinfra:gazelle --nobuild 2>&1 | tail -5
```

Pass: exit 0 with no `Unable to resolve host`, `certificate`,
`127.0.0.1:*`, or proxy errors.

**C4 — bazelisk shim is active and invocations are session-tagged.**

```bash
ls -l "$(command -v bazelisk)"  # must point into $DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/bin
grep -E 'build_metadata|TAGS' "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/bbr.bazelrc" 2>/dev/null
```

Pass: bazelisk resolves inside the session dir, and bbr.bazelrc contains
`session:<id>` metadata.

**C5 — PostToolUse pre-commit auto-apply works.**

Hard to automate without actually exercising Edit/Write. Report this as
"manually verified" if you have just edited a file in this session and
observed `ruff-format` apply, or as "NOT TESTED" otherwise. Do not fake
this check.

**C6 — throwaway-commit pre-commit end-to-end.**

```bash
set -e
cd /home/user/ducktape
TEST_BRANCH="selfcheck/$(date +%s)"
git checkout -q -b "$TEST_BRANCH"
TEST_FILE=$(mktemp /home/user/ducktape/selfcheck-XXXXX.txt)
echo "selfcheck $(date -Iseconds)" > "$TEST_FILE"
git add "$TEST_FILE"
git commit -m "test: selfcheck — delete me" 2>&1
EXIT=$?
git checkout -q -
git branch -D "$TEST_BRANCH"
rm -f "$TEST_FILE"
echo "exit: $EXIT"
```

Pass: exit 0.

**C7 — hook daemon logs present, no unhandled exceptions.**

```bash
LOG="$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.log"
[ -f "$LOG" ] && grep -cE 'ERROR|Traceback|Exception' "$LOG" || echo MISSING
```

Pass: log exists, zero matches (or only expected warnings — use judgement).

**C8 — OTLP tracing reaches the collector.**

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer ${DUCKTAPE_OTEL_BEARER_TOKEN}" \
  -H "Content-Type: application/json" -d '{}' \
  https://alloy-otlp.allegedly.works/v1/traces
```

Pass: `200` or `400` (bad proto = auth passed). `401` = token rotated or
missing.

**C9 — `bbr` preserves the analysis cache on a second identical run.**

Low-precision, high-recall sensor with a high false-positive rate (runner
rotation, BB server restart, cache eviction can all cause transient cold
hits). Report the finding but don't act on a single failure. Stop early
rather than spending many minutes retrying.

**Method — cache poisoning**: append a comment to `MODULE.bazel` so the
first build is guaranteed cold, then time an immediately-following
identical build. The SPEC permits occasional cold-hits; only flag if
warm ≈ cold across **two** repeated runs.

```bash
cd /home/user/ducktape
echo "# selfcheck-poison-$(date +%s)" >> MODULE.bazel
T1_START=$(date +%s%N)
bbr build //... --nobuild 2>&1 | tail -3
T1_SEC=$(( ($(date +%s%N) - T1_START) / 1000000000 ))
T2_START=$(date +%s%N)
bbr build //... --nobuild 2>&1 | tail -3
T2_SEC=$(( ($(date +%s%N) - T2_START) / 1000000000 ))
git checkout -- MODULE.bazel
echo "cold=${T1_SEC}s warm=${T2_SEC}s"
```

Interpret: `warm < cold/3` = recycling works. `warm ≈ cold` = likely not
recycling (but re-run before diagnosing — high FP rate). `cold < 5s` =
build graph too small to measure. If consistently warm≈cold across two
runs, inspect `bbapi invocation <id>` for runner IDs.

### CLI only

**CLI1 — `git commit --amend` is blocked by the git shim.**

```bash
(cd /tmp && git init -q selfcheck && cd selfcheck && \
  git commit --allow-empty -m init -q 2>/dev/null && \
  git commit --amend --no-edit 2>&1 | grep -c '\[git-shim\] BLOCKED')
rm -rf /tmp/selfcheck
```

Pass: `1`.

**CLI2 — `git add -A` / `git add .` is blocked.**

```bash
(cd /tmp && mkdir -p selfcheck2 && cd selfcheck2 && \
  git init -q && git add -A 2>&1 | grep -c '\[git-shim\] BLOCKED')
rm -rf /tmp/selfcheck2
```

Pass: `1`.

**CLI3 — `git stash` is blocked (but `list` / `show` allowed).**

```bash
git stash 2>&1 | grep -c '\[git-shim\] BLOCKED'
git stash list 2>&1 | grep -c '\[git-shim\] BLOCKED'  # must be 0
```

**CLI4 — direnv bridge propagates `.envrc` exports into Bash tool calls.**

```bash
# Expect a representative env var (e.g. one set only by .envrc) to appear
# after cd into a subproject that has one.
cd /home/user/ducktape && env | grep -c '^DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG='
```

Pass: `1` (or whatever var your `.envrc` exports).

### Web only

**W1 — `kubectl` works as `claude-code-web`; MCP returns the same pods.**

```bash
kubectl -n claude-sandbox get pods 2>&1 | tail -5
```

Then invoke the `mcp__claude-sandbox-kubectl__pods_list_in_namespace` tool
with `namespace=claude-sandbox` and compare. Pass: both succeed and agree.

**W2 — `$GITHUB_TOKEN` identifies as `agentydragon-agent`.**

Covered by C2 on web — no separate check.

**W3 — `fork` remote is configured with push access.**

```bash
git -C /home/user/ducktape remote -v | grep '^fork'
```

Pass: `fork` appears with a URL the machine user can push to. If absent
with no warning in the context banner, the fork-remote background task
failed.

**W4 — `bbr build <any target>` works out of the box, no manual remote
setup, no remote picker.**

```bash
cd /home/user/ducktape
# Run interactively — the test fails if bb prints a remote picker prompt
# or errors on missing git config.
timeout 60 bbr build //devinfra:gazelle --nobuild 2>&1 | tail -10
```

Pass: exit 0, no "which remote" prompt, no `Unable to resolve host`, no
`127.0.0.1:*` in the runner's origin URL.

**W5 — Docker is available.**

```bash
docker info >/dev/null 2>&1 && echo OK || echo FAIL
```

## Out-of-SPEC diagnostics

These are not in SPEC.md but catch real-world failure modes. Include them
in the report under a separate "Diagnostics" heading.

### D1 — `web_setup.sh` freshness (web only)

Anthropic reuses Firecracker microVMs; `/tmp/web-setup.log` may be from a
prior session running an older `web_setup.sh`. A stale setup means Nix
devtools and skills may not match the current code.

```bash
ls -la /tmp/web-setup.log 2>/dev/null || echo "MISSING"
tail -3 /tmp/web-setup.log 2>/dev/null                    # last line should be "Setup complete."
SETUP_COMMIT=$(grep 'web_setup.sh commit:' /tmp/web-setup.log 2>/dev/null | tail -1 | grep -oE '[0-9a-f]{40}')
HEAD_COMMIT=$(git -C /home/user/ducktape rev-parse HEAD)
[ "$SETUP_COMMIT" = "$HEAD_COMMIT" ] && echo "OK" || echo "STALE: setup=$SETUP_COMMIT head=$HEAD_COMMIT"
```

### D2 — `claude-hooks` daemon pin staleness

A stale installed daemon is often the root cause of session hook failures.
Check the pinned commit against HEAD, and check whether release CI is
passing so that the pin would move if you waited.

```bash
python3 -c "
import json, re
pins = json.load(open('/home/user/ducktape/npins/sources.json'))['pins']
url = pins.get('claude-hooks', {}).get('url', '')
m = re.search(r'claude-hooks-([0-9a-f]+)', url)
print('pinned:', m.group(1) if m else 'unknown')
"
git -C /home/user/ducktape log --oneline -5 -- devinfra/claude/ npins/sources.json
```

If the pin is behind HEAD, diff the installed Nix store package against
the repo source for breaking changes (renamed classes, changed config
paths, removed hooks). Check GitHub CI on `agentydragon/ducktape`:
recent `release.yml` and `sync-pins.yml` runs on `devel`.

### D3 — `git remote origin` URL reachability (web only)

Known failure mode: `bb remote` reads `git remote -v` locally and sends
the URL to the cloud runner. If `origin` is `127.0.0.1:*` (Claude Code
web session proxy), the runner can't reach it and `bbr` fails on hook
invocations.

```bash
ORIGIN=$(git -C /home/user/ducktape remote get-url origin)
echo "$ORIGIN" | grep -qE '127\.0\.0\.1|localhost' && echo "WARN: local proxy origin" || echo "OK"
git -C /home/user/ducktape config buildbuddy.remote-bazel-default-remote 2>/dev/null \
  && echo "(buildbuddy remote override is set)" \
  || echo "(no remote override)"
```

## Report Format

```
# Session Selfcheck — <timestamp>

Profile: <CLI/Web>    Summary: <healthy / degraded / broken>

## SPEC acceptance criteria

| ID  | Check                          | Status   | Detail                |
| --- | ------------------------------ | -------- | --------------------- |
| C1  | BUILDBUDDY_API_KEY valid       | OK/FAIL       | HTTP <code>           |
| C2  | GITHUB_TOKEN valid             | OK/FAIL       | login=...             |
| C3  | bbr build trivial              | OK/FAIL       | ...                   |
| C4  | bazelisk shim + session tag    | OK/FAIL       | ...                   |
| C5  | PostToolUse auto-apply         | OK/SKIP       | manually verified?    |
| C6  | throwaway commit end-to-end    | OK/FAIL       | ...                   |
| C7  | daemon log clean               | OK/FAIL       | N errors              |
| C8  | OTLP tracing                   | OK/FAIL       | HTTP <code>           |
| C9  | bbr analysis cache warm        | OK/WARN/AMBIG | cold=Xs warm=Ys       |
| CLI1–4 / W1–5                   | ...           | ...                   |

## Out-of-SPEC diagnostics

| ID | Check                          | Status        | Detail                 |
| -- | ------------------------------ | ------------- | ---------------------- |
| D1 | web_setup.sh freshness         | OK/STALE/MISS | setup=<sha> head=<sha> |
| D2 | claude-hooks pin staleness     | OK/BEHIND     | pin=<sha>, CI status   |
| D3 | origin URL reachable for bbr   | OK/WARN       | origin=...             |

## Issues & remediation

### <issue title>
**Spec criterion violated**: <ID>
**Impact**: <what's broken for the agent>
**Root cause**: <why>
**Fix** (for the user to run, not the skill): <exact commands>
```

Prioritize: SPEC violations first (the daemon is broken), then out-of-SPEC
diagnostics.
