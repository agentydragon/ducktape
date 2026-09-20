---
name: cihealth
description: >-
  Check CI/CD pipeline health: devel status, per-commit checks, release
  artifacts, pin staleness, image publishing, scheduled jobs. Fixes trivial
  issues as PRs, proposes diagnosis plans for deeper ones. Use when CI seems
  broken, red, lagging, or stuck.
---

# CI Health Check

Audit the current CI/CD configuration for agentydragon/ducktape. Discover
workflow names, schedules, release outputs, and image ownership from the repo
and GitHub at run time. Do not copy old workflow inventories, timestamps, pin
values, or image counts into the report.

Autonomously fix trivial issues (for example, formatting failures); propose a
diagnosis plan for anything that needs deeper investigation.

## Setup — GitHub Authentication

gh reads GITHUB_TOKEN. In a Claude Code web session, the read-only token is
available as DUCKTAPE_CI_READ_GITHUB_TOKEN (exported by
devinfra/secrets/web_env.sh via the session start hook):

```bash
export GITHUB_TOKEN="$DUCKTAPE_CI_READ_GITHUB_TOKEN"
```

Outside that context, decrypt secrets/github-ci-read-pat.yaml with the SOPS
age key and export it as GITHUB_TOKEN; see devinfra/secrets/web_env.sh for
the exact pattern. If auth is unavailable, continue with the public API and
note the limitation in the report.

## Phase 1 — Discover CI Organisation

Read what the repository currently defines:

```bash
ls .github/workflows/
sed -n '1,100p' devinfra/ci/artifacts.py
cat nix/artifact-pins.json
cat devinfra/image_pins.json 2>/dev/null
git log --oneline origin/devel | head -1
```

From the workflow YAML and repository configuration, identify:

- workflows triggered by pushes or pull requests
- scheduled and manually triggered workflows, including their configured cadence
- build, release, and pin-sync dependencies
- released artifacts, pinned images, and the workflows that own them

## Phase 2 — Gather Data

### 2a. Per-commit workflow status on devel

Read the trigger definitions, then query recent runs for every workflow with a
push or pull-request trigger. This loop discovers file names from the checkout:

```bash
REPO=agentydragon/ducktape

for workflow in .github/workflows/*.yml .github/workflows/*.yaml; do
  [ -f "$workflow" ] || continue
  if ! rg -q '^\s+(push|pull_request):|^on:.*(push|pull_request)' "$workflow"; then
    continue
  fi
  name=$(basename "$workflow")
  gh run list --repo "$REPO" --workflow "$name" \
    --branch devel --limit 5 \
    --json headBranch,status,conclusion,displayTitle,createdAt,databaseId
done
```

For any workflow whose most recent run is not successful, read its failure log
and identify the exact failed job, step, and error:

```bash
gh run view <run-id> --repo "$REPO" --log-failed 2>&1 | tail -150
```

### 2b. Release artifact staleness

For each entry in nix/artifact-pins.json, inspect its URL and hash. Repo-built
artifacts commonly encode a source commit in the release tag; compare that
commit with origin/devel and report the distance and age. Third-party pins may
use semantic versions instead.

For image pins, read devinfra/image_pins.json and each publishing workflow's
path filters. Image digests should move when their owned source paths change.
The RBE worker digest is part of every Bazel action cache key, so it should
remain stable unless the RBE worker image itself changes.

### 2c. Scheduled workflow health

Discover scheduled workflows from .github/workflows/, read each schedule, and
compare it with the latest run history:

```bash
REPO=agentydragon/ducktape

for workflow in .github/workflows/*.yml .github/workflows/*.yaml; do
  [ -f "$workflow" ] || continue
  if ! rg -q '^\s+schedule:' "$workflow"; then
    continue
  fi
  name=$(basename "$workflow")
  gh run list --repo "$REPO" --workflow "$name" --limit 5 \
    --json status,conclusion,createdAt,databaseId
done
```

Compare each successful run with the configured cadence and a reasonable grace
period for that workflow's purpose. Read failure logs for unsuccessful runs.

### 2d. Release pipeline status

Read the workflow YAML to find which jobs build and publish each artifact. Query
the latest relevant runs and compare the resulting GitHub releases with
devinfra/ci/artifacts.py. Flag a missing or old release only when its publish
step should have completed successfully.

```bash
REPO=agentydragon/ducktape
gh run list --repo "$REPO" --branch devel --limit 50 \
  --json workflowName,status,conclusion,createdAt,displayTitle,databaseId
gh release list --repo "$REPO" --limit 50
```

### 2e. Container image currency

Use devinfra/image_pins.json to enumerate pinned images. Read each owning
workflow's path filters and recent runs; decide whether relevant source changes
should have triggered a new publish.

### 2f. Other repository-defined checks

Inspect .github/workflows/ for workflows not covered above and spot-check
their recent runs. Look for open PRs that touch artifact pins or release/sync
configuration, since they may explain a pin that has not advanced.

## Phase 3 — Diagnose failures

Classify each failure:

- **Trivial / auto-fixable**: formatting, whitespace, import ordering, or minor
  linting. Implement a fix commit and open a PR.
- **Test failure**: read the test output and determine whether the cause is a
  focused fix or needs deeper investigation.
- **Infrastructure failure**: runner cannot reach a dependency, a required
  secret is missing, a cache is full, or an external API is down. Report the
  evidence and a specific diagnosis plan.
- **Pin/release stuck**: trace the configured build, publish, and repin workflow
  dependencies to locate the break.

For non-trivial issues, give the user specific commands to confirm the root
cause and describe the proposed fix.

## Phase 4 — Report

Produce one concise markdown report. Healthy items get one line; issues get
details. The schema below is illustrative: every angle-bracket value is a
placeholder to replace with current data, and no row is a claim about current
CI state.

```markdown
# CI Health Report — <report time in UTC>

## Summary

<overall status and issue count>

## Per-commit checks

| Workflow name from config | Status   | Latest run time from GitHub | Note   |
| ------------------------- | -------- | --------------------------- | ------ |
| <workflow>                | <status> | <timestamp or not run>      | <note> |

## Scheduled jobs

| Workflow name | Schedule from config | Latest successful run | Status   |
| ------------- | -------------------- | --------------------- | -------- |
| <workflow>    | <schedule>           | <timestamp or none>   | <status> |

## Artifact pins

| Pin name from JSON | Pinned version or commit | Distance from source | Assessment |
| ------------------ | ------------------------ | -------------------- | ---------- |
| <pin>              | <version or commit>      | <distance or n/a>    | <status>   |

## Image pins

| Image name from config | Pinned digest | Relevant source changes | Assessment |
| ---------------------- | ------------- | ----------------------- | ---------- |
| <image>                | <digest>      | <changes or none>       | <status>   |

## Issues found

### <severity>: <issue>

**Workflow:** <workflow name from config>
**Run:** <GitHub run link and timestamp>
**Error:** <brief description>
**Root cause:** <if determinable>
**Fix:** <PR link or proposed steps>
```

## Important patterns to detect

- **Scheduled workflow missing**: compare its latest success with the current
  schedule and expected grace period.
- **Release blocked**: a successful build with a failed dependent publish job
  leaves artifacts and pins unchanged.
- **Artifact drift with healthy pipeline**: pins can lag while relevant runs are
  active. Trace the build, publish, and repin steps before calling it stuck.
- **Formatting churn**: repeated failures on the same file may indicate an
  unresolved formatter conflict.
- **Image pin staleness**: an old digest is expected when its owned source paths
  have not changed; flag it when a relevant change should have published.
- **Build timeout**: compare the run with its configured timeout and the work
  performed. Retry only when logs indicate a transient failure.
