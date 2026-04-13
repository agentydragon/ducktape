---
name: web_selfcheck
description: >
  Diagnose the health of a Claude Code web session — checks whether
  web_setup.sh ran, whether the session start hook succeeded, whether
  the installed claude-hooks package is stale relative to the repo,
  whether each SOPS-encrypted credential is decryptable and live-tests
  each one against its upstream API, and whether ducktape git hooks
  (pre-commit, commit-msg) actually pass when committing. Reports what's
  broken and how to fix it. Use when the user asks "did setup go ok",
  "why isn't bbr working", "check credentials", "selfcheck", "why do my
  commits fail", or any question about web session health.
---

# Web Session Selfcheck

Comprehensive health check for a Claude Code web session. Run all checks,
then produce a single structured report with clear pass/fail status and
actionable remediation steps for anything that's broken.

## CRITICAL: Observe Only — Do NOT Fix Without Explicit User Approval

This is a **diagnostic skill**. Treat a broken session like a crime scene:
observe, document, and report — do not touch.

**Do NOT run any remediation commands** (e.g. `web_setup.sh`, re-triggering
SessionStart, sourcing env files, installing packages) unless the user
explicitly says to proceed. The "Fix" blocks throughout this skill are
documentation of what _could_ be done — they are **not instructions for you
to execute**. Report your findings and wait for a go-ahead.

**Exception — debugging workarounds**: when the session hooks are broken and
you are actively debugging or documenting (e.g. committing this very skill),
the following lightweight workarounds are acceptable without explicit approval:

- Committing with hooks bypassed: `git commit --no-verify` (or point hooks at
  `/dev/null` temporarily) to record diagnostic work while hooks are broken
- Unsetting `BUILDBUDDY_API_KEY` to force local bazel when bbr is broken
- Creating a `bazel` wrapper in the session bin that injects `--bazelrc` when
  the session bazelrc exists but the shim is missing

Run all `Bash` commands with `dangerouslyDisableSandbox: true` (needs network
and filesystem access outside the sandbox).

## What to Check

Run all checks in parallel where possible.

---

### 1. web_setup.sh

**Goal**: confirm Nix and the `devtools` profile were installed successfully,
and that setup ran from the current repo commit.

**VM reuse warning**: Anthropic reuses Firecracker microVMs between sessions.
`/tmp` persists across reuses, so `/tmp/web-setup.log` may be from a prior
session running an older version of `web_setup.sh`. Always verify the setup
commit matches the current repo HEAD — a stale setup means Nix devtools and
skills may not match what the current code expects.

```bash
# Was it run at all?
ls -la /tmp/web-setup.log 2>/dev/null || echo "MISSING"
# Did it succeed? (last line should be "Setup complete.")
tail -5 /tmp/web-setup.log 2>/dev/null
# Was it recent? (mtime)
stat -c '%y' /tmp/web-setup.log 2>/dev/null
# Did Nix install?
nix --version 2>/dev/null || echo "nix not found"
# Is the devtools profile active?
nix profile list 2>/dev/null | grep -E 'devtools|claude-hooks' | head -5 || echo "no devtools profile"

# What commit did web_setup.sh run from?
grep 'web_setup.sh commit:' /tmp/web-setup.log 2>/dev/null | tail -1 || echo "commit not logged (old web_setup.sh)"
# Current repo HEAD
git -C /home/user/ducktape rev-parse HEAD

# Do they match?
SETUP_COMMIT=$(grep 'web_setup.sh commit:' /tmp/web-setup.log 2>/dev/null | tail -1 | grep -oE '[0-9a-f]{40}' || echo '')
HEAD_COMMIT=$(git -C /home/user/ducktape rev-parse HEAD)
if [ -z "$SETUP_COMMIT" ]; then
  echo "UNKNOWN: web_setup.sh predates commit logging — assume STALE"
elif [ "$SETUP_COMMIT" = "$HEAD_COMMIT" ]; then
  echo "OK: setup commit matches HEAD ($HEAD_COMMIT)"
else
  echo "STALE: setup ran from ${SETUP_COMMIT:0:12}, HEAD is ${HEAD_COMMIT:0:12}"
fi

# What env var keys were present when web_setup.sh ran?
grep -A200 'environment keys' /tmp/web-setup.log 2>/dev/null | grep -B200 '^---$' | grep -v '^---'
```

**Failure indicators**: log missing, last line not "Setup complete", nix not
found, devtools not in profile list, setup commit doesn't match HEAD.

**Fix**: re-run setup from the Claude Code web UI setup command:

```
bash ducktape/devinfra/claude/web_setup.sh
```

If re-running, note that `SOPS_AGE_KEY` is typically not available when
`web_setup.sh` runs — all SOPS decryptions will fail. Secrets are instead
decrypted by the session start hook daemon (which inherits `SOPS_AGE_KEY`
from the container after k8s injects it). This is expected and not a bug.

---

### 2. claude-hooks Daemon Version

**Goal**: check whether the installed `claude-hooks` daemon matches the current
repo code, how far behind it is, and whether any breaking changes have landed
since the pinned commit.

Do this early — a stale daemon is often the root cause of session hook failures.

```bash
# Pinned commit (what's actually installed)
python3 -c "
import json, re
pins = json.load(open('/home/user/ducktape/npins/sources.json'))['pins']
url = pins.get('claude-hooks', {}).get('url', '')
m = re.search(r'claude-hooks-([0-9a-f]+)', url)
print('pinned commit:', m.group(1) if m else 'unknown')
print('pin url:', url[:100])
"

# Current HEAD of the repo
git -C /home/user/ducktape rev-parse --short HEAD
git -C /home/user/ducktape log --oneline -1

# How many devinfra/claude/ commits have landed since the pin was last updated?
git -C /home/user/ducktape log --oneline -10 -- devinfra/claude/ npins/sources.json

# When was the pin last updated?
git -C /home/user/ducktape log --oneline -3 -- npins/sources.json
```

**If the pin is behind HEAD**, diff the installed package against the repo
source to spot breaking changes: look at the git log for `devinfra/claude/`
since the pinned commit, read the relevant changed files in both the installed
Nix store package and the repo, and use your judgement to assess whether any
of those changes are likely to cause incompatibility with the running session.
Report any suspicious mismatches (e.g. renamed classes, changed config file
paths, new required fields, removed hooks).

Also check GitHub CI on `agentydragon/ducktape` to
understand whether an update is expected soon or something is wedged. Look at:

- Recent `release.yml` runs on `devel` — did it pass after the relevant commit?
- Recent `sync-pins.yml` runs — did it succeed and push?
- Recent `ci.yml` runs on `devel` — any blocking test failures?

For each, report: last run status, when it ran, and if failed, what failed.

**Interpretation**:

- `release.yml` failing → new daemon won't be released; find the failing test/step
- `sync-pins.yml` not running or failing → pin won't auto-update
- CI tests failing on `devel` → release is blocked until tests are fixed

**Suggested fix** (do not run — report to user):

1. If `release.yml` recently passed after the relevant commit: `sync-pins.yml`
   will update the pin within 30 min; wait or trigger manually
2. If `release.yml` hasn't run or failed: identify and fix the blocking issue on `devel`
3. Once pin is updated and merged, re-run `web_setup.sh`

---

### 3. Session Start Hook

**Goal**: confirm the session start hook ran successfully and wrote the env file.

```bash
# Find live session ID (from hook_daemon process)
LIVE=$(ps aux | grep hook_daemon | grep -v grep | grep -oP '(?<=--sock /tmp/claude-hd/)[^/]+' | head -1)
echo "live session: $LIVE"

# Check env file (presence + CANARY marker = success)
head -3 ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh 2>/dev/null || echo "ENV FILE MISSING"

# Check daemon log for errors
grep -E 'ERROR|Exception|FileNotFoundError|sessionstart|SessionStart' \
  ~/.claude/session-env/$LIVE/hook-daemon/daemon.log 2>/dev/null | tail -20

# Is BUILDBUDDY_API_KEY set?
echo "BUILDBUDDY_API_KEY in env: $([ -n "${BUILDBUDDY_API_KEY:-}" ] && echo YES || echo NO)"

# Is the auth proxy running?
ls ~/.claude/session-env/$LIVE/auth-proxy/combined_ca.pem 2>/dev/null && echo "CA present" || echo "CA MISSING"
ls ~/.claude/session-env/$LIVE/bazelrc 2>/dev/null && echo "session bazelrc present" || echo "BAZELRC MISSING"

# Is the git proxy shim running? (bbr connects via 127.0.0.1:35233)
ss -tlnp 2>/dev/null | grep 35233 || echo "git proxy NOT listening on 35233"
```

**Suggested fix if env file is missing** (do not run — report to user):

Re-trigger SessionStart on the live daemon:

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
source ~/.claude/session-env/$LIVE/sessionstart-hook-0.sh
```

**Manual fallback** (if daemon is down or still broken after fix):

```bash
source /home/user/ducktape/devinfra/secrets/web_env.sh
mkdir -p ~/.config/bazel
cat > ~/.config/bazel/buildbuddy.bazelrc <<EOF
common --remote_header=x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}
build --config=rbe
EOF
```

---

### 4. Credentials — SOPS Decryption

**Goal**: confirm `SOPS_AGE_KEY` is present and can decrypt all claude-web secrets.

```bash
echo "SOPS_AGE_KEY present: $([ -n "${SOPS_AGE_KEY:-}" ] && echo YES || echo NO)"
echo "Age public key: $(echo "${SOPS_AGE_KEY:-}" | age-keygen -y 2>/dev/null || echo 'age-keygen not found')"

# Expected public key from .sops.yaml (claude-web entry):
grep 'claude-web' /home/user/ducktape/.sops.yaml

for f in \
  secrets/buildbuddy.yaml \
  secrets/github-pat-agentydragon-agent.yaml \
  secrets/github-ci-read-pat.yaml \
  secrets/alloy-otlp-bearer-token.yaml \
  secrets/claude-web-k8s-token.yaml \
  secrets/docker-ci/client-key.sops.pem; do
    result=$(sops -d /home/user/ducktape/$f 2>&1 | head -1)
    if echo "$result" | grep -qE 'FAILED|failed|error|Error'; then
        echo "FAIL: $f — $result"
    else
        echo "OK:   $f"
    fi
done
```

**Failure indicator**: any `FAIL` line, or `SOPS_AGE_KEY` not present.

**Fix**: if `SOPS_AGE_KEY` is missing, the session didn't receive the age
private key at startup. This is injected from the `claude-sandbox` k8s Secret
by the container runtime. Check whether the k8s Secret exists:

```bash
kubectl -n claude-sandbox get secret claude-web-age-key 2>/dev/null
```

---

### 5. Credentials — Live API Tests

Run each live test and capture HTTP status / response content.

#### BuildBuddy API Key

```bash
BB_KEY=$(sops -d /home/user/ducktape/secrets/buildbuddy.yaml 2>/dev/null \
  | awk '/buildbuddy_api_key:/ {print $2}')
# Test via bbapi (needs BUILDBUDDY_API_KEY in env)
export BUILDBUDDY_API_KEY="$BB_KEY"
curl -s -o /dev/null -w "%{http_code}" \
  -H "x-buildbuddy-api-key: $BB_KEY" \
  -H "Content-Type: application/proto" \
  "https://remote.buildbuddy.io/rpc/BuildBuddyService/GetUser" \
  --data-binary ''
```

Expected: `200` (or `400` for malformed proto — means auth passed).
`401`/`403` means key is invalid or expired.

**Fix if invalid**: regenerate key in BuildBuddy org settings, re-encrypt into
`secrets/buildbuddy.yaml`, push to `devel`, wait for `sync-pins.yml`.

#### GitHub Agent PAT (`agentydragon-agent`)

```bash
GH_TOKEN=$(sops -d /home/user/ducktape/secrets/github-pat-agentydragon-agent.yaml 2>/dev/null \
  | awk '/github_token:/ {print $2}')
curl -s -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/user \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('login:', d.get('login'), 'message:', d.get('message',''))"
```

Expected: `login: agentydragon-agent`.
`Bad credentials` or `Requires authentication` means token expired/revoked.

**Fix**: generate new PAT for `agentydragon-agent` machine user (Settings →
Developer Settings → Personal Access Tokens), re-encrypt into
`secrets/github-pat-agentydragon-agent.yaml`, push to `devel`.

#### GitHub CI Read PAT (`agentydragon` fine-grained)

```bash
GH_CI=$(sops -d /home/user/ducktape/secrets/github-ci-read-pat.yaml 2>/dev/null \
  | awk '/github_token:/ {print $2}')
curl -s -H "Authorization: Bearer $GH_CI" https://api.github.com/user \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('login:', d.get('login'), 'message:', d.get('message',''))"
```

Expected: `login: agentydragon`.

#### K8s Service Account Token

```bash
K8S_TOKEN=$(sops -d /home/user/ducktape/secrets/claude-web-k8s-token.yaml 2>/dev/null \
  | awk '/k8s_token:/ {print $2}')
curl -sk -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $K8S_TOKEN" \
  "https://api.allegedly.works:16443/api/v1/namespaces/claude-sandbox"
```

Expected: `200`. `401` means the token was rotated and the SOPS file wasn't
updated yet.

**Note**: this token is **auto-rotated by an in-cluster CronJob**. The SOPS
file should be updated automatically. If it returns 401, check:

```bash
# Check CronJob last run and next run
kubectl -n default get cronjob claude-web-token-rotator -o yaml 2>/dev/null | grep -E 'lastScheduleTime|schedule'
kubectl -n default get jobs -l app=claude-web-token-rotator 2>/dev/null | tail -5
```

#### OTLP Bearer Token (Grafana Alloy)

```bash
OTLP_TOKEN=$(sops -d /home/user/ducktape/secrets/alloy-otlp-bearer-token.yaml 2>/dev/null \
  | awk '/token:/ {print $2}')
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $OTLP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  "https://alloy-otlp.allegedly.works/v1/traces"
```

Expected: `200` or `400` (bad proto = auth passed). `401` means token
was rotated. **Fix**: bump `rotation_version` in
`cluster/terraform/gitops/alloy-otlp-bearer-token/`, apply with `tofu`.

---

### 6. bbr / BuildBuddy RBE

```bash
# Set key from SOPS if not already in env
[ -z "${BUILDBUDDY_API_KEY:-}" ] && \
  export BUILDBUDDY_API_KEY=$(sops -d /home/user/ducktape/secrets/buildbuddy.yaml 2>/dev/null \
    | awk '/buildbuddy_api_key:/ {print $2}')

# Fix origin/HEAD if missing (needed by bbr)
git -C /home/user/ducktape remote set-head origin --auto 2>/dev/null || true

# Test bbr connectivity (dry run)
bbr build //devinfra:gazelle --nobuild 2>&1 | tail -5
```

**Failure: `cannot connect to 127.0.0.1:35233`** → session start hook didn't
run; the git proxy shim is not running. Follow session start hook fix above.

**Failure: `Unable to resolve host remote.buildbuddy.io`** → TLS proxy/CA
issue; session start hook didn't set up auth proxy. Follow session start hook
fix above.

---

### 6b. BuildBuddy Runner Recycling & Analysis Cache

**Goal**: Confirm that `bbr` reuses the same BuildBuddy runner VM between
invocations so the Bazel analysis cache is warm for subsequent builds. A
recycled runner means the second build completes analysis significantly faster
than the first cold build.

**Calibration note**: This is a low-precision, high-recall sensor with a high
false-positive rate. A single "not recycling" result may be transient (runner
rotation, BB server restart, cache eviction). Report the finding but don't act
on a single failure. Stop early rather than spending many minutes trying.

**Method — cache poisoning**: Append a comment to `MODULE.bazel` so the first
build is guaranteed cold even if the runner was already warm, then measure
whether the immediately-following identical build is significantly faster.

```bash
cd /home/user/ducktape

# Step 1: Poison the analysis cache.
# Any uncommmitted change to MODULE.bazel forces Bazel to re-analyse from
# scratch on the runner, even if it was already warm.
POISON_MARKER="# selfcheck-poison-$(date +%s)"
echo "$POISON_MARKER" >> MODULE.bazel

# Step 2: Cold build. MODULE.bazel changed → Bazel must re-analyse everything.
# --nobuild skips compilation; we only care about analysis time.
# Cold analysis of //... can take 5–20 minutes on this repo — be patient.
# If it hangs with no output for >10 minutes, abort (Ctrl-C), restore
# MODULE.bazel with `git checkout -- MODULE.bazel`, and skip this check.
echo "--- Cold build (poisoned MODULE.bazel) ---"
T1_START=$(date +%s%N)
bbr build //... --nobuild 2>&1 | tail -5
BBR_EXIT=$?
T1_END=$(date +%s%N)
T1_SEC=$(( (T1_END - T1_START) / 1000000000 ))
echo "Cold build: ${T1_SEC}s (exit $BBR_EXIT)"

if [ $BBR_EXIT -ne 0 ]; then
  git checkout -- MODULE.bazel
  echo "SKIP: Cold build failed — skipping cache warmth check."
else
  # Step 3: Warm build. Same poisoned MODULE.bazel, same runner expected.
  echo "--- Warm build (same inputs, runner should be recycled) ---"
  T2_START=$(date +%s%N)
  bbr build //... --nobuild 2>&1 | tail -5
  T2_SEC=$(( ($(date +%s%N) - T2_START) / 1000000000 ))
  echo "Warm build: ${T2_SEC}s"

  # Step 4: Restore MODULE.bazel.
  git checkout -- MODULE.bazel

  # Step 5: Assess.
  echo "--- Result: cold=${T1_SEC}s  warm=${T2_SEC}s ---"
  if [ "$T1_SEC" -lt 5 ]; then
    echo "AMBIGUOUS: Cold build was <5s — build graph may be too small to"
    echo "           measure, or poisoning had no effect on this runner."
  elif [ "$T2_SEC" -lt $(( T1_SEC / 3 )) ]; then
    echo "OK: Warm build (${T2_SEC}s) < 1/3 of cold (${T1_SEC}s)."
    echo "    Runner recycling + analysis cache reuse is working."
  else
    echo "WARN: Warm build (${T2_SEC}s) is not much faster than cold (${T1_SEC}s)."
    echo "      Runner may not be recycled, or analysis cache is evicted."
    echo "      High false-positive rate — verify with a second run before diagnosing."
  fi
fi
```

**Interpreting results:**

| Warm / Cold ratio | Interpretation                                                |
| ----------------- | ------------------------------------------------------------- |
| < 33%             | ✅ Runner recycling and analysis cache are working            |
| 33–70%            | ⚠️ Partial benefit; runner may be rotating                    |
| > 70%             | ⚠️ Likely no recycling — but check again, high FP rate        |
| Cold < 5s         | ❓ Ambiguous — build graph too small or poisoning ineffective |

**If consistently warm ≈ cold across two runs**: check the BuildBuddy run UI
(`bbapi invocation <id>`) to see if runner IDs differ between invocations. If
they do, BB is not reusing runners — this may be a BB configuration or quota
issue. If runner IDs are the same but analysis is still slow, the Bazel server
on the runner may be restarting between invocations.

---

### 7. Ducktape Git Hooks

**Goal**: confirm pre-commit hooks actually pass when committing. Hooks break in
web sessions due to `bbr` using the session's local git proxy URL
(`127.0.0.1:*`) that the BuildBuddy cloud runner can't reach, or due to
`DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG` being active while the commit-msg hook
receives no argv.

#### 7a. Framework & installation

```bash
# Are the git hook shims installed?
ls -la /home/user/ducktape/.git/hooks/pre-commit \
       /home/user/ducktape/.git/hooks/commit-msg 2>/dev/null || echo "HOOKS NOT INSTALLED"

# What version of pre-commit?
pre-commit --version 2>/dev/null || echo "pre-commit not found"

# Which backend will detect_bazel_backend() pick?
python3 -c "
import os, shutil
bb = shutil.which('bbr')
key = os.environ.get('BUILDBUDDY_API_KEY')
print('bbr on PATH:', bool(bb))
print('BUILDBUDDY_API_KEY set:', bool(key))
print('=> backend:', 'BUILDBUDDY (bbr)' if bb and key else 'LOCAL (bazel)')
"

# Active test-tag enforcement?
echo "DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG=${DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG:-<unset>}"
```

#### 7b. bbr git remote URL

When the backend is `BUILDBUDDY`, `bb remote` reads `git remote -v` locally and
sends the remote URL to the cloud runner. If `origin` is `127.0.0.1:*` (Claude
Code web session proxy), the runner can't reach it.

```bash
git -C /home/user/ducktape remote -v | head -4

# Is origin a local proxy?
ORIGIN_URL=$(git -C /home/user/ducktape remote get-url origin 2>/dev/null)
if echo "$ORIGIN_URL" | grep -qE '127\.0\.0\.1|localhost'; then
  echo "WARN: origin is a local proxy ($ORIGIN_URL)"
  echo "      BuildBuddy cloud runner cannot reach this — bbr queries will fail"
  echo "FIX:  git remote add github https://github.com/agentydragon/ducktape"
  echo "      git config buildbuddy.remote-bazel-default-remote github"
else
  echo "OK: origin is externally reachable ($ORIGIN_URL)"
fi

# Is the bb default remote override already set?
git -C /home/user/ducktape config buildbuddy.remote-bazel-default-remote 2>/dev/null \
  && echo "(buildbuddy.remote-bazel-default-remote override is set)" \
  || echo "(no remote override — bb will use origin)"
```

#### 7c. Direct hook invocation

Test each hook binary directly, bypassing the full `git commit` flow.

```bash
# --- pytest-main-check ---
# Unset BUILDBUDDY_API_KEY to force local bazel (avoids bbr/proxy issues)
BUILDBUDDY_API_KEY= \
  ducktape-pytest-main-check \
  /home/user/ducktape/devinfra/claude/hook_daemon/test_hook_daemon.py \
  2>&1 | tail -3
echo "pytest-main-check exit: $?"

# --- commit-msg hook ---
# Write a sample commit message and run the hook against it.
TMP_MSG=$(mktemp)
cat > "$TMP_MSG" <<'MSG'
test: dummy message for hook selfcheck

https://claude.ai/code/test
MSG

DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG= \
  ducktape-commit-msg "$TMP_MSG" 2>&1
echo "commit-msg exit: $?"
rm -f "$TMP_MSG"
```

**Failure: `pytest-main-check` exits 1 with `Command 'bazel' returned non-zero exit status 255`**

Two sub-causes:

- _bbr backend, local proxy_: `BUILDBUDDY_API_KEY` is set and origin is `127.0.0.1:*`. The cloud runner fetches from the local proxy URL it received in `RunRequest.repo.url` and fails. Fix: add the `github` remote and set `buildbuddy.remote-bazel-default-remote` (see 7b above). Or temporarily unset `BUILDBUDDY_API_KEY` before committing.
- _Concurrent bbr calls_: `build_bazel_index` runs two concurrent `bbr query` calls; if both race to re-initialise the runner repo, one fails. The local-proxy issue is the root cause in web sessions.

**Failure: `commit-msg` exits 1 with `commit message file path required as argument`**

`DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG=1` is active but `pass_filenames: false` was set (now fixed in `.pre-commit-config.yaml`). Confirm the fix is present:

```bash
grep -A6 'ducktape-commit-msg' /home/user/ducktape/.pre-commit-config.yaml | grep pass_filenames \
  && echo "WARN: pass_filenames still set" \
  || echo "OK: pass_filenames not set"
```

#### 7d. Live end-to-end commit test

Actually exercises the full hook pipeline (pre-commit + commit-msg) on a
throwaway commit in a temp branch. Cleans up afterwards.

```bash
set -e
cd /home/user/ducktape

# Create a throwaway branch from HEAD
TEST_BRANCH="selfcheck/hook-test-$(date +%s)"
git checkout -q -b "$TEST_BRANCH"

# Make a trivial tracked change
TEST_FILE=$(mktemp /home/user/ducktape/selfcheck-hook-test-XXXXX.txt)
echo "hook selfcheck $(date -Iseconds)" > "$TEST_FILE"
git add "$TEST_FILE"

# Commit with BUILDBUDDY_API_KEY unset (forces local bazel in pre-commit)
# and DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG unset (skips test-tag check)
BUILDBUDDY_API_KEY= DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG= \
  git commit -m "test: selfcheck hook test — delete me" 2>&1
COMMIT_EXIT=$?

# Clean up: remove branch and file regardless of outcome
git checkout -q -
git branch -D "$TEST_BRANCH"
rm -f "$TEST_FILE"

echo "Live commit test exit: $COMMIT_EXIT"
[ "$COMMIT_EXIT" -eq 0 ] && echo "PASS: git hooks work" || echo "FAIL: git hooks broken"
```

Add `| grep -E '^(PASS|FAIL|Passed|Failed|error|Error)'` to the git commit
line to reduce noise, or run it unfiltered to see all hook output.

**If the live test fails**: capture the full pre-commit output, then run the
failing hook directly (7c) to isolate which hook is broken.

---

## Report Format

After running all checks, produce:

```
# Web Session Selfcheck — <timestamp>

## Summary
<one-line: healthy / degraded / broken>

## Checks

| Check                        | Status   | Detail                                          |
| ---------------------------- | -------- | ----------------------------------------------- |
| web_setup.sh ran             | OK/FAIL  | ...                                             |
| web_setup.sh commit          | OK/STALE | setup=<sha12> head=<sha12> (VM reuse risk)      |
| claude-hooks daemon version  | OK/STALE | pinned=<sha> head=<sha> N commits behind; CI status |
| Session start hook           | OK/FAIL  | CANARY present / FileNotFoundError              |
| SOPS_AGE_KEY                 | OK/FAIL  | age public key matches .sops.yaml               |
| Secret: buildbuddy.yaml      | OK/FAIL| decrypts / API <http_code>          |
| Secret: github-agent-pat     | OK/FAIL| decrypts / login=agentydragon-agent |
| Secret: github-ci-read-pat   | OK/FAIL| decrypts / login=agentydragon       |
| Secret: k8s-token            | OK/FAIL| decrypts / API <http_code>          |
| Secret: otlp-token           | OK/FAIL| decrypts / API <http_code>          |
| bbr / BuildBuddy RBE         | OK/FAIL| ...                                 |
| bbr runner recycling         | OK/WARN/AMBIG | cold=Xs warm=Ys ratio=Z%     |
| Git hooks (pre-commit)       | OK/FAIL| backend=LOCAL/bbr; live commit pass/fail |
| Git hooks (commit-msg)       | OK/FAIL| pass_filenames ok; ENFORCE_TEST_TAG state |

## Issues & Remediation

### <issue title>
**Impact**: <what's broken>
**Root cause**: <why>
**Fix**: <exact commands or steps>

...
```

Prioritize issues by impact: hook failure > stale claude-hooks > credential
failures > CI pipeline issues.
