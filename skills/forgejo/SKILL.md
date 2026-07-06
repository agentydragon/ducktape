---
name: forgejo
description: Inspect Forgejo repositories through the REST API and web endpoints, especially Actions CI, task/run metadata, logs, package registry tags, haku-state CI, and Flux/image rollout debugging. Use when diagnosing Forgejo Actions failures, missing logs, branch badge state, package/image publish gaps, or when endpoint shape is unclear.
---

# Forgejo

Use live Forgejo evidence before inferring from manifests. For private repos, get a real
repo credential first and keep it in shell variables; never print tokens or passwords.

## Endpoint Discovery

Check the deployment's OpenAPI before guessing:

```bash
curl -fsS "$FORGEJO_URL/swagger.v1.json" \
  | jq -r '.paths | keys[] | select(test("actions|packages|repos"))'
```

This Forgejo deployment exposes Actions metadata under `/api/v1/repos/.../actions/...`.
It does not expose a GitHub-compatible REST log-download endpoint.

## Actions Metadata

List runs:

```bash
curl -fsS -u "$USER:$PASS" \
  "$FORGEJO_URL/api/v1/repos/$OWNER/$REPO/actions/runs" \
  | jq -r '.workflow_runs[] |
      [.index_in_repo, .id, .workflow_id, .status, .prettyref, .commit_sha[0:9], .title] | @tsv'
```

Important ID namespaces:

- `index_in_repo` is the UI/display run number, e.g. `/actions/runs/396`.
- `id` is the internal REST run id for `GET /api/v1/repos/$OWNER/$REPO/actions/runs/$id`.
- `actions/tasks` returns job/task rows; its `id` is a task id, not the run id.

List task rows:

```bash
curl -fsS -u "$USER:$PASS" \
  "$FORGEJO_URL/api/v1/repos/$OWNER/$REPO/actions/tasks" \
  | jq -r '.workflow_runs[] |
      [.run_number, .id, .name, .workflow_id, .status, .head_branch, .head_sha[0:9], .display_title] | @tsv'
```

Always inspect `event_payload` for the actual ref and changed paths:

```bash
curl -fsS -u "$USER:$PASS" \
  "$FORGEJO_URL/api/v1/repos/$OWNER/$REPO/actions/runs/$INTERNAL_RUN_ID" \
  | jq -r '.prettyref, .event_payload | fromjson? | {ref, before, after, commits}'
```

A green run on a non-main branch does not prove a main-gated publish step ran.

## Logs

On this deployment, logs are web UI endpoints, not documented REST routes. Treat these
routes as Forgejo UI implementation details: discover the current URL shape from the run
page each time, prefer page-provided attributes over hardcoded IDs, and expect this recipe
to need adjustment after Forgejo upgrades. REST Basic auth is enough for `/api/v1/...`, but
not for the web log endpoints. Start a temporary web session with the same credential:

```bash
cookie=$(mktemp)
curl -fsS -c "$cookie" "$FORGEJO_URL/user/login" -o /tmp/forgejo-login.html
csrf=$(rg -o 'name="_csrf" value="([^"]+)"' -r '$1' /tmp/forgejo-login.html | head -1)
curl -fsS -L -b "$cookie" -c "$cookie" -o /tmp/forgejo-after-login.html \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "_csrf=$csrf" \
  --data-urlencode "user_name=$USER" \
  --data-urlencode "password=$PASS" \
  "$FORGEJO_URL/user/login"
```

Fetch the run page using the UI/display run number, then read the attributes the Vue app
uses:

```bash
curl -fsS -L -b "$cookie" \
  "$FORGEJO_URL/$OWNER/$REPO/actions/runs/$RUN_NUMBER" \
  -o /tmp/forgejo-run.html
```

On this deployment the relevant HTML attributes are:

- `data-actions-url`, e.g. `/haku/haku-state/actions`
- `data-run-index`, the UI run number
- `data-job-index`, zero-based index in the run's job list
- `data-attempt-number`
- `data-initial-post-response`, JSON-escaped initial job state containing step indexes,
  statuses, and the UI job ids

To pull one step's log, POST JSON to the same endpoint the UI uses:

```bash
endpoint="$FORGEJO_URL${ACTIONS_URL}/runs/${RUN_INDEX}/jobs/${JOB_INDEX}/attempt/${ATTEMPT}"
curl -fsS -L -b "$cookie" -H 'Content-Type: application/json' \
  --data '{"logCursors":[{"step":STEP_INDEX,"cursor":null,"expanded":true}]}' \
  "$endpoint" \
  | jq -r '.logs.stepsLog[] | .lines[] | [.timestamp, .message] | @tsv'
```

Notes:

- `jobs/$JOB_INDEX` is the zero-based UI job index, not the REST task id and not the UI job
  id embedded in `data-initial-post-response`.
- The download link in the gear menu is
  `$ACTIONS_URL/runs/$RUN_INDEX/jobs/$JOB_INDEX/attempt/$ATTEMPT/logs`, but the JSON POST is
  better for targeted diagnostics and works with expanded-step cursors.
- If the POST returns an empty `stepsLog`, expand the failing step by index. The initial
  page state lists each step summary/status under `state.currentJob.steps`.
- Some repos also publish fallback logs, e.g. a `ci-logs` branch or workflow artifact; check
  workflow comments before assuming web logs are the only channel.

## haku-state CI And UI Rollout

For `haku/haku-state`, the useful read credential is `haku-state-git-write` from
`haku-sandbox`. Use it for API reads; do not use scratch tokens.

Distinguish the two CI surfaces:

- `validate-state.yaml` green means the current `main` data contract is valid. It does not
  build or publish `haku-ui`.
- `bazel-ci.yaml` on `main` is the image publish path. Path filters mean data-only commits
  do not run it.

To answer "is a UI change live?", verify the whole chain:

1. `bazel-ci.yaml` ran on `main` for the UI commit, not only on a PR or `wip/*`.
2. The registry has a matching `git.allegedly.works/haku/ui:main-<utc>-<sha7>` tag.
3. `ImageRepository/haku-ui` scanned that tag.
4. `ImagePolicy/haku-ui` selected it.
5. `ImageUpdateAutomation/haku-ui` committed the tag into `haku-state`.
6. `Kustomization/haku-state-workloads` applied that revision.
7. `Deployment/haku-ui` is running the selected image and the served bundle contains the
   expected UI strings/routes.

If there is no registry tag for the UI commit, this is a CI/publish problem, not Flux lag.
If the latest green run is only `validate-state.yaml`, the branch badge can be green while
the UI remains stale.
