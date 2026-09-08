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
It does not expose GitHub-compatible REST endpoints for log download or rerun/retry; the
one Actions write route is `workflow_dispatch` (§ Re-running).

Gotchas that look like a wrong route:

- An unauthenticated call for a private repo returns the generic 404
  `{"message":"The target couldn't be found."}`, identical to a bad path. Authenticate
  first: `curl -n` reads `~/.netrc` and Basic auth is enough for `/api/v1/...`.
- `/actions/runs` returns the whole run history unless paginated. On `haku/haku-state`
  the unbounded response was over 9 MB and outran a 90 s timeout; `?limit=15&page=1` is
  140 KB and answers in about a second.

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
- A job re-run keeps the run's `index_in_repo`. It shows up as a **new task row** with
  the same `run_number` and a later `run_started_at`, and the run's `status` flips back
  to `running`; watching the run list for a higher index misses it.

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

A green run on a non-main branch does not prove a main-gated publish step ran. A green
`workflow_dispatch` run also does not prove the push-triggered path is healthy; compare
the `event` field before using one run to explain another.

## Actions CI Timing

The `logs` subcommand (below) answers "why did this run fail?"; `timing` answers "why is CI
slow?" — a per-job duration distribution over recent tasks. The script is self-contained
(PEP 723 inline deps), so `uv run` fetches `httpx` + `pydantic` on the fly; in-repo,
`bb run //skills/forgejo/scripts:cli -- timing …` works too:

```bash
uv run skills/forgejo/scripts/forgejo.py timing --owner "$OWNER" --repo "$REPO"
# --list       also prints individual recent tasks
# --limit N    analyze the N most-recent finished tasks (default 200)
# --max-seconds S  drop longer rows as outliers (default 1800, the runner job timeout)
```

It reads `FORGEJO_URL` (defaults to this deployment) and authenticates via `~/.netrc` (or
`FORGEJO_USER` / `FORGEJO_PASSWORD`). Sample (`haku/haku-state`):

```text
job               n    min    p50    p90    max
bazel            37   494s   538s   593s   659s
validate         51   161s   223s   247s   276s
linkcheck        92    20s    61s    72s   131s
```

A flat `min ≈ p50` on every run (no run ever incremental) is the signature of a build with no
persistent cache.

Manual equivalent — duration is `updated_at - run_started_at`, per finished task:

```bash
curl -fsS -u "$USER:$PASS" "$FORGEJO_URL/api/v1/repos/$OWNER/$REPO/actions/tasks" \
  | jq -r '.workflow_runs[]
      | select(.status=="success" or .status=="failure")
      | [.name, ((.updated_at|fromdateiso8601) - (.run_started_at|fromdateiso8601) | floor)]
      | @tsv'
```

Timing-field gotchas on this deployment (they bite a naive reading):

- **No `conclusion`, no `stopped_at`.** `status` carries
  `success`/`failure`/`cancelled`/`running`; `updated_at` is the completion time of a finished
  task.
- **The start field is `run_started_at`, not `started_at`** — there is no `started_at`, so
  reading it silently yields `null` and drops every row.
- **Duration is run + queue wall time.** The runner is capacity-limited, so a row can sit
  queued before it runs; treat a long tail as queue wait and filter outliers (the helper's
  `--max-seconds`).
- **`limit` is ignored** — the endpoint returns the whole task list under `workflow_runs`;
  slice client-side.

## Logs

On this deployment, logs are web UI endpoints, not documented REST routes. Treat these
routes as Forgejo UI implementation details: discover the current URL shape from the run
page each time, prefer page-provided attributes over hardcoded IDs, and expect this recipe
to need adjustment after Forgejo upgrades. REST Basic auth is enough for `/api/v1/...`, but
not for the web log endpoints. Start a temporary web session with the same credential:

Prefer the bundled helper when you need logs:

```bash
# List step indexes from the run page.
uv run skills/forgejo/scripts/forgejo.py logs \
  --owner "$OWNER" --repo "$REPO" --run "$RUN_NUMBER" --list-steps

# Fetch one expanded step's log.
uv run skills/forgejo/scripts/forgejo.py logs \
  --owner "$OWNER" --repo "$REPO" --run "$RUN_NUMBER" --step "$STEP_INDEX"
```

The helper logs in, fetches the run page, parses the page-provided `data-*` attributes, and
posts the UI's JSON cursor payload. If `uv run` picks a stripped system interpreter (the
symptom is `ModuleNotFoundError: No module named 'math'` from inside the stdlib, seen in
the Claude Code web container), point it at a full one with `uv run --python <path>`.

Keep credentials in environment variables or temporary shell variables (`FORGEJO_URL`, `FORGEJO_USER`, `FORGEJO_PASSWORD`); do not print them.

Manual equivalent:

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
- There is no REST rerun or retry; § Re-running covers what exists.
- The download link in the gear menu is
  `$ACTIONS_URL/runs/$RUN_INDEX/jobs/$JOB_INDEX/attempt/$ATTEMPT/logs`, but the JSON POST is
  better for targeted diagnostics and works with expanded-step cursors.
- If the POST returns an empty `stepsLog`, expand the failing step by index. The initial
  page state lists each step summary/status under `state.currentJob.steps`.
- Some repos also publish fallback logs, e.g. a `ci-logs` branch or workflow artifact; check
  workflow comments before assuming web logs are the only channel.

## Re-running

There is no REST rerun or retry on this deployment (15.0.3+gitea-1.22.0; `swagger.v1.json`
has no path matching `rerun|retry|cancel`). Two routes exist:

**`workflow_dispatch`, the REST route.** Works only for a workflow that declares
`on: workflow_dispatch`; `haku/haku-state`'s `bazel-ci.yaml` does, as its documented manual
re-run, and its gate runs the `image` job for every non-push event:

```bash
curl -fsS -n -X POST -H 'Content-Type: application/json' \
  --data '{"ref":"my-branch"}' \
  "$FORGEJO_URL/api/v1/repos/$OWNER/$REPO/actions/workflows/bazel-ci.yaml/dispatches"
# 204 No Content; the run appears in /actions/runs a few seconds later.
```

How the dispatched run reads back, and what it does not do:

- In the run list its `prettyref` is the bare branch name (a PR run shows `#N`) and its
  `event` is null, so a filter on `.event == "workflow_dispatch"` finds nothing.
- It posts **no commit status** on the commit, not even its own context: after a green
  dispatch of `bazel-ci.yaml`, `/commits/{sha}/statuses` still listed only the
  `(pull_request)` contexts, with the failed `image` one untouched. So a dispatch proves
  the commit builds; the PR's checks go green only through the UI re-run or the next push.

**The UI re-run, the web route.** The run page's re-run buttons POST to
`$ACTIONS_URL/runs/$RUN_INDEX/rerun` and `.../jobs/$JOB_INDEX/rerun` with the CSRF token
from the page, over the web session from § Logs. Not exercised from an agent session yet:
the form login needs the password in a shell command, and an auto-mode session may refuse
that step.

## Actions Artifacts

Verified on this deployment (Forgejo 15.0.3+gitea-1.22.0, probe 2026-07-12; byte-identical
sha256 round-trip).

Upload, in workflows: `actions/upload-artifact@v4` fails with `GHESNotSupportedError` —
the GitHub action refuses any non-github.com host. Use
`https://code.forgejo.org/forgejo/upload-artifact@v4` (preferred, v4 semantics) or
`actions/upload-artifact@v3`.

Download: no REST endpoints (`/api/v1/.../actions/artifacts` and
`.../actions/runs/$ID/artifacts` both 404, and Basic auth on the web routes also 404s) —
web session only (login recipe under Logs above). Gotcha: the two web routes key on
different run identifiers, both available from `GET
/api/v1/repos/$OWNER/$REPO/actions/runs` (`id` = DB id, `index_in_repo` = UI/display run
number; the tasks endpoint's `url` field also embeds the display number):

```bash
# List (display run number): -> {"artifacts":[{name,size,status}]}
curl -fsS -b "$cookie" "$FORGEJO_URL/$OWNER/$REPO/actions/runs/$RUN_INDEX/artifacts"

# Download (DB id! the display number here returns 404 "no such run").
# The response is always a ZIP wrapping the uploaded file(s).
curl -fsS -b "$cookie" -o artifact.zip \
  "$FORGEJO_URL/$OWNER/$REPO/actions/runs/$RUN_DB_ID/artifacts/$ARTIFACT_NAME"
unzip artifact.zip
```

## Actions Secrets And Registry Auth

Forgejo Actions secret metadata is deliberately opaque. `GET
/api/v1/repos/$OWNER/$REPO/actions/secrets` proves a secret exists, but it does not reveal
the value and may not include a useful `updated_at`. Do not conclude a registry credential
was refreshed from that listing alone. Verify with behavior: an authenticated registry probe
such as `/v2/`, a workflow preflight step, or a real image push.

When diagnosing image publishes, compare registry responses:

```bash
curl -sS -o /tmp/registry-probe.json -w '%{http_code}\n' \
  -u "$REGISTRY_USER:$REGISTRY_PASSWORD" \
  "$FORGEJO_URL/v2/"
```

On this deployment, valid haku credentials return `200` for `/v2/`; empty or wrong
passwords return `401`. A workflow-only `403` points at the exact Actions context or
generated Docker auth config, not at the registry being globally down.

## haku-state CI And UI Rollout

For `haku/haku-state`, the useful read credential is `haku-forgejo-git` from
`haku-sandbox`. Use it for API reads; do not use scratch tokens.

`bazel-ci.yaml` publishes the failing job's Bazel output to the `ci-logs` branch
(`tools/ci/publish_bazel_log.sh`), and only on failure, so a green run leaves no log there.
The branch holds one commit that each publish replaces, and a job re-run republishes under
the **same** subject, `ci log: run N (sha)`. Poll for a new hash or commit date, never for
a new subject:

```bash
git fetch origin ci-logs && git log -1 --format='%h %ci %s' FETCH_HEAD
git show FETCH_HEAD:bazel-ci.log
```

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
