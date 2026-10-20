# Python-to-Go cutover

How the Notification Service moves from its Python image to the Go image, on `agentplane-testing` and then
staging. The Python service serves until the step that flips it; this file goes with the Python service.

## What fixes the order

- **One Flux policy per image name.** CI publishes the roster entries `agentplane-notification-service` and
  `agentplane-notification-service-migrate` ([`image_targets.json`](../../../devinfra/ci/image_targets.json)), and one
  `ImagePolicy` per name feeds both environments' `image-pins` through the `$imagepolicy` marker. Both Go targets
  build (`//agentplane/notification_service:service_image`, `//agentplane/notification_service/migrations:image`)
  but the roster still names the Python ones. Repointing an entry publishes the Go image to both environments, so an
  environment is held by dropping its pin's marker with a `CLEANUP` note.
- **Migrator before service.** The Goose baseline is idempotent over a database at Alembic head
  `0010_drop_write_only_columns` and refuses one behind it. It takes the Alembic runner's advisory lock key
  `0x4E4F5446`, so the two never run at once, and records itself in `notification_service_goose_version`. It reads
  `AGENTPLANE_NOTIFICATIONS_DATABASE_URL`, which the init container already sets. From the flip on, a schema change
  is a Goose file in `migrations/notifications/`, never an Alembic revision.
- **Settings and environment need no change.** The Go service reads the keys the manifests' `notifications-settings`
  set and the `AGENTPLANE_NOTIFICATIONS_*` variables the Deployment sets, and refuses unknown keys. Before each
  environment's flip, run the built binary on that environment's rendered ConfigMap. Exit status 2 with
  `invalid Notification Service configuration` is a settings error; a failure to load the in-cluster configuration
  (outside a cluster) means the settings loaded.

  ```bash
  AGENTPLANE_NOTIFICATIONS_CONFIG_FILE=<rendered notifications-settings> \
    AGENTPLANE_NOTIFICATIONS_DATABASE_URL=postgres://fake@127.0.0.1:1/fake \
    <bazel-bin>/agentplane/notification_service/notification-service_/notification-service
  ```

  Staging also needs `AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY` (a throwaway PEM RSA key) and
  `AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET` (16 or more characters).

- **The probes change with the pin, per environment.** The Go service answers `/healthz` and `/readyz` with 404 and
  serves `grpc.health.v1` on port 8080: service `""` is readiness (the LISTEN connection is up and a query succeeds)
  and `livez` is liveness. A Go Pod with the HTTP probes never becomes Ready and a Python Pod with `grpc` probes
  fails them. `cluster/cdk8s/agentplane/notifications.py` renders one probe shape for both environments, so the
  probe change needs a temporary per-environment parameter that the staging flip removes.
- **No Python and Go workers at once.** Testing's Deployment is `Recreate`. Staging's is `RollingUpdate` with two
  replicas, which would run both: its flip needs a temporary `Recreate` that lands ahead of the pin change (a
  strategy edit alone rolls nothing) and is removed after.
- **Callers move with the service.** The Go HTTP form writes `camelCase`, 64-bit integers as strings and a `source`
  oneof, has no `/v1/sources`, and serves the operator streams over gRPC. The callers are the integration app, the
  `agentplane notifications` CLI in the runner images, the egress allowlist for the Notification Service host and the
  platform prompt, which still points agents at `agentplane notifications sources`, a command the CLI no longer has.

## Sequence

### Testing

1. **Migrator.** One change: `agentplane-notification-service-migrate` targets
   `//agentplane/notification_service/migrations:image` (test `//agentplane/notification_service/migrations/...`),
   and staging's migrator pin drops its marker. Testing follows the policy to the Go tag.

   Once it has rolled:

   ```bash
   kubectl -n agentplane-testing get pod -l app.kubernetes.io/name=notifications -o jsonpath='{range .items[*]}{.metadata.name} {.status.initContainerStatuses[0].image} {.status.initContainerStatuses[0].state.terminated.exitCode}{"\n"}{end}'
   kubectl -n agentplane-testing exec postgres-1 -c postgres -- psql -d notifications \
     -c 'SELECT version_id, is_applied FROM notification_service_goose_version ORDER BY id' \
     -c 'SELECT version_num FROM alembic_version'
   ```

   The init container ran the Go tag and exited 0; the Goose table holds versions 0 and 1; `alembic_version` is
   still `0010_drop_write_only_columns`; the Python service is Ready and its tables kept their rows. A Pod restart
   runs the migrator again and changes nothing. A failed baseline leaves testing without a Pod (`Recreate`).

2. **Go service published, held.** `agentplane-notification-service` targets `:service_image`, and both
   environments' service pins drop their markers and keep the Python tag. This is the only way to publish the image
   before its probes ship: the pin and the `grpc` probes must reach a Pod in one commit, and the tag has to exist
   first. Nothing rolls; the tag appears in the registry.
3. **Service flip.** One change: testing's service pin takes the published Go tag with its marker restored; testing's
   probes become `grpc` (`""` for readiness, `livez` for liveness); the app, CLI and egress allowlist changes of the
   callers; the README system overview and [`docs/system_overview.md`](../../docs/system_overview.md), which draw the
   edges that go live; the platform prompt and the allowlist's `/v1/sources` entry lose their sources pointers. The
   generated manifests under `cluster/generated/agentplane-testing/` are regenerated, not edited. Every staging pin
   follows its policy, so the images that carry the callers (the app, and the runner and sandbox images that bundle
   the `agentplane` release pinned in `nix/artifact-pins.json`) reach staging too, so their staging pins are held
   before the first of those images publishes with the new callers.
4. **Acceptance on testing.** From a testing Sandbox, subscribe to an Action's request, submit the Action, wait out the
   debounce (`notice_debounce.max_wait_seconds`), read the entries, acknowledge, cancel; the Session receives the
   notice as a user message. `/llms.txt`, `/docs/agents/` and `/openapi.json` answer; a `github` source answers
   `PERMISSION_DENIED` (GitHub is off on testing). The app's drawer and Thread-list badges follow the operator
   streams, and the logs show no worker stopping (a worker that returns ends the process).

### Staging

5. **Before anything changes.**
   - **Backup and restore proof**, per [staging data preservation](../../docs/staging_data_preservation.md). Name the
     recovery point (the latest base backup of the staging `postgres` Cluster, daily between 01:00 and 05:00 UTC,
     with a seven-day WAL window) and recover it into an isolated Cluster that cannot reach Actions, the Runner
     Gateway, the History Service, the Dispatcher or GitHub: a restored copy with live workers would send notices to
     live Sessions. Over that copy, the Go service's list and read answers for every inbox and subscription equal the
     Python service's, field for field, after the representation differences above. No comparison tool exists yet;
     building it is part of this step.
   - **Largest stored integer.** The Go service holds payloads as `Struct`, whose numbers are doubles. Sample
     `github_delivery.payload` and `entry.event` read-only (at most 10,000 rows, `TABLESAMPLE`) for integers of 16 or
     more digits; anything above 2^53 stops the cutover until the payload is a JSON string field.
   - **Live Sandboxes** count, and which of them carry the old CLI: they cannot talk to the new shapes until they
     restart.
6. **Migrator.** Staging's migrator pin takes its marker back and follows the policy to the Go tag. The rollout is
   `RollingUpdate`: if the baseline fails, the new Pods stay in `Init` and the old ones keep serving. Check as in
   step 1, in `agentplane-staging`.
7. **Recreate.** A change that sets the Deployment's strategy to `Recreate` and nothing else.
8. **Service flip.** One change: staging's service pin takes its marker back at the Go tag testing runs; staging's
   probes become `grpc`; the held caller pins take their markers back with it; the temporary `Recreate` and the
   per-environment probe parameter come out in the next change.
   Afterwards, redeliver the webhooks of the gap and run step 4 against staging, with a `github` subscription as well.

### The webhook gap (staging)

Staging is the only environment with `POST /v1/webhooks/github`, and its `Recreate` stops it. GitHub does not
retry a failed delivery. Note the last delivery the Python service answered before the roll and the time the Go Pod
became Ready. Then, in the GitHub App's settings under Recent Deliveries (or with the App's delivery API:
`GET /app/hook/deliveries`, `POST /app/hook/deliveries/{id}/attempts`; not run against this App), redeliver each
delivery in that window that did not get a `2xx`. The Go handler answers `202` with `duplicate: true` for a receipt it
already committed and `duplicate: false` for a new one; a redelivery keeps its delivery ID, and a replay under a new
one does not deduplicate. Recent Deliveries then shows `2xx` for the window, and a subscription's observations
([API doc](api.md#github-webhook-ingestion-and-matching)) show no new error.

## Rollback

Reverting a flip's change restores the previous pin, probes and callers together, which is why each flip is one
change. The migrator's table is harmless to the Python migrator. A reverted service flip hands the Python service rows
the Go service wrote; their stored forms are pinned by the golden vectors from the Python code, and no end-to-end
run on real data has been made. Never restore an old backup over writes made after the cutover.

## When it is done

Delete this file with the Python service, in the same change that deletes the Alembic directory, the Python migration
image and their roster entries.
