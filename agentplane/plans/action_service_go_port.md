# Action Service Go rollout

The Go service, generated callers and disposable K3s acceptance are merged in #11454, including
client retirement and typed fixture cleanup from #11459 and #11460. The full migration CI passed
151 test targets, including Go-backed MCP, DCR and both VM sandbox harnesses.

**Stage M accepted (2026-10-22).** Testing runs the Go Actions service and Goose migrator with the
generated callers. The Action Deployment was Ready 1/1 and its migration init container exited 0.
A disposable Sandbox (`actions-go-catalog-probe-20261022-160600`, UID prefix `20a68144`) ran one
`agentplane actions groups --json` command through the normal Runner and egress path, succeeding on
the first attempt. Cleanup selected the Sandbox by UID; its Pod and both PVCs were confirmed absent.

**Stage N preflight (read only, 2026-10-22).** The staging database had
`alembic_version=0028_drop_external_grant`, with 4,597 requests, one Connection and three grants.
The check did not read request or credential values; it confirms the Goose baseline and records the
pre-cutover row counts.

**Stage N service cutover accepted (2026-10-22; manual client reauthorization remains).** Actions is live
on Go in both staging and testing; Goose applied migrations 1–3. Staging retained its 4,597 requests,
one Connection and three grants, and the original
Sandbox identities. A disposable staging catalog probe (UID prefix `31fe1a2c`) succeeded through the
Runner and egress path and was removed. Public discovery, protected-resource and CIMD metadata returned
200; `/mcp` returned 401 with the expected challenge. External-user reauthorization remains a manual
follow-up; no end-to-end external login is claimed.

The approved destination keeps the Actions lifecycle, policies, executors and Web Push in
`actions`; Connections, OAuth and `/mcp` become `mcp-edge` after the runtime cutover. Both still run
in the one Go binary until that separate extraction.

## Remaining stages

| Stage | Status   | Change                                                                                                                                                                                                                                                                               |
| ----- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| N     | Accepted | Go Actions and Goose are live in staging and testing; existing Action/Connection/grant counts and Sandbox identities are preserved. The disposable Runner/egress catalog probe and public-route metadata/authentication checks passed. External-user reauthorization remains manual. |
| O     | Active   | Reader retirement is deployed in both tiers and migration 00004 is applied in staging. Deploy migration 00005 to drop the final unused grant column, then verify preserved state. Keep published rollback images.                                                                    |

## Cutover requirements

- **Preserve existing data.** The operator waived restoration testing on 2026-10-22. A restore
  rehearsal and Python/Go comparisons on a restored copy are not rollout prerequisites. Do not
  drop/recreate the staging database, Sandboxes or their volumes. Retain Connection and grant
  identities and existing request/history records.
- **Published images before config.** Use verified service and Goose migrator images plus the
  generated-client app, Notification Service, runner disk and Sandbox images. Restore each
  environment's held image-policy markers in its own cutover. Keep staging held until testing
  passes. Image tags for unchanged content may remain older only when their digest matches the
  current build output.
- **No overlapping Python and Go workers.** Keep `Recreate`: their dispatch leases and Web Push
  locking protocols must not run concurrently against the same database. Use gRPC readiness and
  the `livez` liveness service for Go.
- **Sandbox CLIs update on resume.** The CLI lives in `agentplane-runner-disk`, not the toolbox or
  VM root image. The existing controller renders current templates for new Pods and halted-VM
  resumes while preserving VM/PVC identities. Reconcile the new disk reference into templates
  before resuming an old Sandbox. The 2026-10-22 staging inventory has three suspended Sandboxes:
  `public-coder-1`, `public-coder-ducktape-1`, and idle `public-coder-ducktape-2`; preserve their
  suspended state during preparation. An idle Sandbox needs the app's resume operation, which
  advances its wake generation; setting `running` to true alone is insufficient.
- **External MCP clients re-authorize once**, at the staging flip, not again at the later edge
  split. FastMCP-issued tokens cannot be honored by the Go authorization server. The reconnect
  path binds a new authorization to the existing Connection after operator confirmation.
- **OAuth storage.** The operator accepts database-stored secrets without application-level
  encryption (2026-10-22). Recoverable upstream refresh tokens may be stored directly; issued
  opaque token values remain stored only as hashes. Encryption is not a rollout prerequisite.
- **Acceptance follows the real path.** The Go-backed K3s suite already covers both harnesses,
  operator decisions, policy approval, OAuth linkage and DCR. After rollout, verify image
  digests, gRPC health, the CLI through egress's credential substitution, operator review and a
  fresh/resumed Sandbox. Before staging, verify the public MCP/OAuth path and existing Connection
  reconnect behavior. No production outage or restoration drill is required.

## Python retirement

The accepted Go cutover permits source retirement now. The Python wait, operator stream, Web Push, MCP
executor, coordinator, store, policy evaluator and informer runtimes and their runtime tests are retired
in the source tree. The app still imports policy view types, the PolicySet parser, operator identity,
policy kind/source and shared configuration types. The Alembic ORM, update broker, migration runner,
revision source and Alembic compatibility cases are retired; Goose is the sole migrator. This source
cleanup makes no schema or data changes. Published rollback images, Goose SQL/history and Sandbox
storage remain. Stage O remains active.

Goose migration 00004 drops `agentplane_oauth_kv`, enrollment `decision_digest`/`browser_hash`, and
grant `request_digest`. The grant `activation_deadline` reader and API field are retired. Both tiers run the reader-free
`6e7b459` image; migration 00005 drops its unused SQL column. Verify that migration after deployment.
Other deployment contracts remain.

## After the flip: the edge split

1. The edge's database and a copy of its tables (database before code).
2. Actions accepts ServiceAccount tokens without a Pod binding in source through the explicit account-only
   resolver. Verify that image has replaced deployed Actions before the edge starts minting such tokens.
   Caller namespace, audience, ServiceAccount UID and live label checks still apply. This is token acceptance
   before token issuance; the edge acts as an owner-scoped caller, not an all-owner `reader_accounts` identity.
3. `mcp-edge`: `main`, Deployment, restricted TokenRequest rights and token minting for the currently bound
   accounts; the in-process `ActionService` call becomes gRPC. Its minted audience must match the Actions
   verifier (currently `agentplane-egress`; a dedicated audience is a separate reader-before-writer change).
   Preserve grant/binding rechecks on every MCP call and while forwarding watches. The Connection API keeps
   operator-OIDC verification and the app's request-bound operator bearer independently of MCP client OAuth.
4. The public route and the app's Connection calls move to the edge; `actions` stops being public.
5. The tables leave the `actions` database, in a separate data change.
