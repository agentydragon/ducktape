---
name: cihealth
description: >
  Check the health of the CI/CD pipeline — devel branch status, per-commit
  checks, release artifacts, artifact pin staleness, image publishing, scheduled
  jobs, repinning cadence. Autonomously creates fix PRs for trivial issues
  (formatting, etc.) or proposes a diagnosis plan for deeper problems. Use when
  you suspect CI is broken, main is red, releases are lagging, repinning is
  stuck, or images are falling behind.
---

# CI Health Check

Comprehensive CI/CD pipeline health audit for the `agentydragon/ducktape`
repository. Gather data in parallel, then produce a single structured report.
Autonomously fix trivial issues (e.g. prettier formatting failures); propose
diagnosis plans for anything that needs deeper investigation.

## Setup — GitHub Authentication

Set `GITHUB_TOKEN` for `gh` commands. In a Claude Code web session the token
is already in the environment as `DUCKTAPE_CI_READ_GITHUB_TOKEN` (exported by
`devinfra/secrets/web_env.sh` via the session start hook). Outside that
context, decrypt `secrets/github-ci-read-pat.yaml` with the SOPS age key —
see `devinfra/secrets/web_env.sh` for the exact pattern.

If auth is unavailable, continue with the public API (rate-limited) and note
it in the report.

## Phase 1 — Discover CI Organisation

**Don't hardcode checks.** Read what the repo actually defines:

```bash
# List all workflows
ls /home/user/ducktape/.github/workflows/

# Read devinfra/ci/artifacts.py to understand what gets released
cat /home/user/ducktape/devinfra/ci/artifacts.py 2>/dev/null | head -80

# Read npins/sources.json to see what's pinned
cat /home/user/ducktape/npins/sources.json

# Read devinfra/image_pins.json for Dockerfile-based image pinning
cat /home/user/ducktape/devinfra/image_pins.json 2>/dev/null

# Count commits on devel since last pin update for each artifact
git -C /home/user/ducktape log --oneline origin/devel | head -1  # current HEAD
```

From the discovery, build a mental model of:
- Which workflows run on push to devel (per-commit checks)
- Which workflows run on a schedule (repinning, attic push)
- Which workflows are triggered manually only
- What artifacts are released and pinned back into the repo

## Phase 2 — Gather Data (Run in Parallel)

Run all data-gathering commands in parallel.

### 2a. Per-commit Workflow Status on devel

For each push-triggered workflow, fetch the last 5 runs on devel:

```bash
REPO=agentydragon/ducktape

# Per-commit workflows to check:
for wf in pre-commit ci bazel-ci ansible-lint nix-attic-push \
           push-images props-images container-images \
           openclaw-image tana-mcp-image; do
  echo "=== $wf ==="
  gh run list --repo $REPO --workflow "${wf}.yml" \
    --branch devel --limit 5 \
    --json headBranch,status,conclusion,displayTitle,createdAt,databaseId \
    --jq '.[] | [.conclusion, .createdAt[:16], .displayTitle[:60]] | @tsv' \
    2>/dev/null || echo "(no runs or workflow not found)"
done
```

For any workflow whose **most recent run** is not `success`:
- Read the failure log: `gh run view <id> --repo $REPO --log-failed 2>&1 | tail -100`
- Identify exactly what failed (hook name, step name, error message, diff)

### 2b. Release Artifact Staleness

For each entry in `npins/sources.json`, the URL encodes the pinned commit:
ducktape-produced pins follow the pattern
`.../releases/download/<artifact>-<sha7>/<filename>`. Extract that sha7,
count how many commits `origin/devel` is ahead of it, and report the age of
the pinned commit.

External pins (e.g. `bb` from buildbuddy-io) have semantic version tags
instead — just report the version.

Also read `devinfra/image_pins.json` for Dockerfile-based image digests
(rbe-worker, freecad-test). These are updated by `container-images.yml` only
when the relevant Dockerfiles change, so staleness is expected unless those
paths were recently touched.

### 2c. Scheduled Workflow Health

Check that scheduled jobs are running and succeeding:

```bash
REPO=agentydragon/ducktape

# sync-pins runs every 30 minutes — should have a recent success
echo "=== sync-pins (every 30 min) ==="
gh run list --repo $REPO --workflow sync-pins.yml \
  --limit 5 \
  --json status,conclusion,createdAt,databaseId \
  --jq '.[] | [.conclusion, .createdAt[:16]] | @tsv' 2>/dev/null

# nix-attic-push runs on push
echo "=== nix-attic-push (on push) ==="
gh run list --repo $REPO --workflow nix-attic-push.yml \
  --branch devel --limit 3 \
  --json status,conclusion,createdAt,displayTitle \
  --jq '.[] | [.conclusion, .createdAt[:16], .displayTitle[:50]] | @tsv' 2>/dev/null

# nix-flake-update is manual — just report last run
echo "=== nix-flake-update (manual) ==="
gh run list --repo $REPO --workflow nix-flake-update.yml \
  --limit 3 \
  --json status,conclusion,createdAt \
  --jq '.[] | [.conclusion, .createdAt[:16]] | @tsv' 2>/dev/null
```

**sync-pins health criteria:**
- Should have a `success` run within the last 45 minutes
- If last success is >60 min ago: warn; if >2 hours: flag as stuck
- If recent runs are failing: read failure log and diagnose

### 2d. Release Pipeline Status

Check that the release job ran and released all expected artifacts after the
most recent devel push:

```bash
REPO=agentydragon/ducktape

echo "=== ci.yml (main pipeline) ==="
gh run list --repo $REPO --workflow ci.yml \
  --branch devel --limit 5 \
  --json status,conclusion,createdAt,displayTitle,databaseId \
  --jq '.[] | [.conclusion, .createdAt[:16], .displayTitle[:60]] | @tsv' 2>/dev/null

# Latest release artifacts on GitHub
echo "=== Latest GitHub releases ==="
gh release list --repo $REPO --limit 20 2>/dev/null \
  | head -30
```

Cross-reference: after the most recent successful `ci.yml` run, there should be
GitHub releases for all 6 release artifacts (`claude-hooks`, `ducktape-util`,
`ducktape`, `gterm-theme`, `skills`, `bbapi`). If any are absent or older than
the CI run, the release job may have failed silently.

### 2e. Container Image Currency

```bash
REPO=agentydragon/ducktape

echo "=== container-images (rbe-worker, freecad-test) ==="
gh run list --repo $REPO --workflow container-images.yml \
  --branch devel --limit 5 \
  --json conclusion,createdAt,displayTitle \
  --jq '.[] | [.conclusion, .createdAt[:16], .displayTitle[:50]] | @tsv' 2>/dev/null

echo "=== push-images (14 OCI images) ==="
gh run list --repo $REPO --workflow push-images.yml \
  --branch devel --limit 3 \
  --json conclusion,createdAt,displayTitle \
  --jq '.[] | [.conclusion, .createdAt[:16]] | @tsv' 2>/dev/null

echo "=== openclaw-image ==="
gh run list --repo $REPO --workflow openclaw-image.yml \
  --branch devel --limit 3 \
  --json conclusion,createdAt \
  --jq '.[] | [.conclusion, .createdAt[:16]] | @tsv' 2>/dev/null

echo "=== tana-mcp-image ==="
gh run list --repo $REPO --workflow tana-mcp-image.yml \
  --branch devel --limit 3 \
  --json conclusion,createdAt \
  --jq '.[] | [.conclusion, .createdAt[:16]] | @tsv' 2>/dev/null
```

### 2f. Anything Else the Repo Defines

After running the above, glance at `.github/workflows/` for any workflows not
covered above. Spot-check their last run status.

Also check for signs of CI health issues beyond GitHub Actions:

```bash
# Are there uncommitted pin updates stuck in a PR?
gh pr list --repo agentydragon/ducktape --limit 10 \
  --json title,state,createdAt,headRefName \
  --jq '.[] | [.state, .createdAt[:10], .headRefName[:40], .title[:50]] | @tsv' \
  2>/dev/null | grep -iE 'pin|sync|chore.*sync' | head -10
```

## Phase 3 — Diagnose Failures

For every failure found in Phase 2:

1. **Read the failure log** (already done for per-commit checks above). If not
   yet read, do it now: `gh run view <id> --repo ... --log-failed 2>&1 | tail -150`

2. **Classify the failure**:
   - **Trivial / auto-fixable**: prettier formatting, trailing whitespace,
     import ordering, minor linting — create a fix commit and open a PR
   - **Test failure**: a specific test broke — read the test output, identify
     the cause, decide if it's a one-line fix or needs deeper investigation
   - **Infrastructure failure**: runner can't reach a dependency, SOPS key
     missing, cache full, external API down — propose diagnosis steps
   - **Pin/release stuck**: artifact not being released or pinned — trace the
     release pipeline (ci.yml → release.yml → sync-pins.yml) to find the break

3. **For trivial fixes**: implement them directly, commit, push a branch, and
   open a PR targeting `devel`. Report the PR URL in the health report.

4. **For non-trivial issues**: write a diagnosis plan with specific commands
   the user can run to confirm the root cause, followed by proposed fixes.

## Phase 4 — Report

Produce a single markdown report. Healthy items get one line. Issues get details.

```markdown
# CI Health Report — <date> <time UTC>

## Summary

<one-liner: all green / N issues found>

## Per-Commit Checks (devel)

| Workflow           | Status | Last run       | Note                    |
| ------------------ | ------ | -------------- | ----------------------- |
| pre-commit         | ✅     | 2026-04-13 06:19 |                        |
| bazel-ci           | ✅     | 2026-04-13 06:19 |                        |
| release            | ✅     | 2026-04-13 06:19 | all 6 artifacts released |
| push-images        | ✅     | 2026-04-13 06:15 |                        |
| ansible-lint       | ✅     | 2026-04-12 14:03 |                        |
| nix-attic-push     | ⚠️     | 2026-04-12 10:00 | 28h ago, timed out     |
| container-images   | ✅     | 2026-04-11 08:00 | no Dockerfile changes  |
| <other>            | ...    | ...            | ...                     |

## Scheduled Jobs

| Job           | Schedule     | Last success       | Status |
| ------------- | ------------ | ------------------ | ------ |
| sync-pins     | every 30 min | 2026-04-13 06:32   | ✅     |
| nix-flake-update | manual    | 2026-04-10 (manual) | ✅    |

## Artifact Pin Staleness

| Artifact      | Behind devel | Pinned commit | Age        |
| ------------- | ------------ | ------------- | ---------- |
| claude-hooks  | 0 commits    | 35b400e       | up-to-date |
| skills        | 2 commits    | 2de0bf8       | 1 hour ago |
| ducktape      | 0 commits    | 071dad5       | up-to-date |
| bbapi         | 0 commits    | 1710bc6       | up-to-date |
| bb (external) | n/a          | 5.0.339       | external   |

## Dockerfile Image Pins

| Image      | Pin age  | Status |
| ---------- | -------- | ------ |
| rbe-worker | current  | ✅     |
| freecad-test | current | ✅    |

## Issues Found

### <severity>: <title>

**Workflow**: <name>
**Run**: <id> at <time>
**Error**: <brief description>
**Root cause**: <if determinable>
**Fix**: <action taken (link to PR) or proposed steps>
```

## Important Patterns to Detect

- **sync-pins not running**: if last `sync-pins.yml` success is >1h ago,
  something is wrong with the scheduler or the workflow itself is failing
- **Release blocked**: if `ci.yml` succeeded but `release.yml` sub-job failed,
  artifacts won't update and pins will grow stale
- **Artifact drift with healthy pipeline**: pins can legitimately lag by
  several commits while a CI run is still in progress (commits often land
  while the release job is building). To assess whether drift is a problem,
  trace the pipeline: did `ci.yml` complete successfully for the commits since
  the pin? Did the `release` sub-job succeed for that specific artifact's test
  matrix entry? Did `sync-pins.yml` run after that release completed? If all
  three happened and the pin still hasn't moved, something is stuck upstream
- **Prettier / formatting churn**: if pre-commit fails on the same file
  repeatedly across different commits, the file likely has an ongoing formatting
  conflict (contributor not running `pre-commit` locally)
- **Container image stuck**: if `container-images.yml` last ran weeks ago and
  the Dockerfiles haven't changed, this is expected — only flag if the image
  should have updated based on path changes in recent commits
- **nix-attic-push timeout**: the job has a 120-minute timeout; timeouts are
  not uncommon for large Nix builds but should be retried
