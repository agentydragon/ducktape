# Standalone subscriptions and notifications

Status: **Actions provider shipped and verified end-to-end in staging (2026-10-03).**
Next slice: **GitHub PR, branch, and commit subscriptions**, with the agreed design below. The implemented
[service contract](../notification_service/README.md), HTTP OpenAPI, and provider discovery are the
source of truth for the existing API, storage limits, and recovery behavior; the old illustrative
pre-implementation API is no longer a backlog item.

## Shipped / burned down

- [x] Independent Sandbox Service for provisioning, explicit destination/ServiceAccount bindings,
      session access, commands, and event following. No integration-app dependency or runner callback
      to notifications. The app remains a client and owns its archive.
- [x] Standalone notification HTTP API, authenticated subscription CRUD/provider discovery, owned
      PostgreSQL database/migrations, multi-replica fenced workers, quotas, retention, and error state.
- [x] Actions-first provider: canonical event history, source ownership checks despite the service's
      broad read access, replay after subscribe races, retained payloads, and overlapping-match deduplication.
- [x] Session inboxes: committed cursor prefix, non-destructive reads, explicit monotonic acknowledgement,
      and notification coverage independent of acknowledgement. No repeated reminders for unread entries.
- [x] Native delivery through Sandbox Service: persisted command identity, admission versus causal
      harness confirmation, lost-response recovery, no automatic startup/resume.
- [x] Agent egress and initial subscribe/read/ack prompt guidance with explicit destination IDs.
- [x] Long-session delivery fix: checkpoint the journal tail before the first attempt, rather than
      replaying unrelated history. After an attempt, recover receipts without skipping entries.
- [x] App-independent native Claude/Codex acceptance, including busy/idle harnesses and lost responses.
- [x] Live staging smoke: replayed five Action events, received the automated message, verified native
      confirmation, explicitly acknowledged through inbox cursor 5, and cancelled only the test
      subscription. Payloads remained stored and the session inbox remained usable.

Evidence: [implementation #8845](https://github.com/agentydragon/ducktape/pull/8845),
[HTTP cleanup/schema #8851](https://github.com/agentydragon/ducktape/pull/8851), and
[delivery fix and live proof #8853](https://github.com/agentydragon/ducktape/pull/8853).
The verified staging image was `devel-20261003083540-82f0720`, with two updated/ready replicas.
This is not a claim that every production environment, backup/restore scenario, or native crash
window has been audited.

[Agent guidance #8860](https://github.com/agentydragon/ducktape/pull/8860) and
[subscription idempotency #8867](https://github.com/agentydragon/ducktape/pull/8867) are merged.
Staging runs image `devel-20261004005216-69f9589` with two updated/ready notification replicas;
its live schema exposes `idempotency_key`, and an existing subscription remains readable with its
identity, key, inbox, and cancelled state preserved. Prompt changes apply to new sessions, not
immutable existing session specs.

## GitHub: agreed next slice

### Agent-facing subscription

Use a common subscription envelope (`destination_ref`, `session_id`, `idempotency_key`, lifetime)
and a nested `source` discriminated union tagged by `source.provider`. `ActionsSource` owns
`request_id` and `after_sequence`; `GitHubSource` owns repository, subject, and event filters.
Filtering and payload schemas belong to the provider, not a universal filter language.
The following is a planned source, not a deployed request schema; common envelope fields are omitted:

```json
{
  "source": {
    "provider": "github",
    "repository": "agentydragon/ducktape",
    "subject": { "kind": "pull_request", "number": 8860 },
    "events": [
      { "event": "pull_request" },
      { "event": "issue_comment", "actions": ["created", "edited"] },
      { "event": "pull_request_review", "actions": ["submitted"] },
      { "event": "pull_request_review_comment", "actions": ["created", "edited"] },
      { "event": "check_run", "actions": ["completed"] },
      { "event": "status" }
    ]
  }
}
```

Initial subjects are a GitHub-owned tagged union:

- `pull_request`: repository + PR number; follows head changes. Defaults to lifecycle, comments,
  reviews, completed checks, and statuses.
- `branch`: repository + exact branch name; push/create/delete activity, completed checks, and
  statuses correlated to branch revisions.
- `commit`: repository + full commit SHA; completed checks and statuses on that fixed revision,
  never retargeted.

Omitting `events` selects the documented subject default. Explicit selection supports comments-only
or CI-only subscriptions; validate event/subject combinations. Use GitHub event and action names:
merge is `pull_request` / `closed` with GitHub's merged fields, not an invented event. General reviews
and inline review comments remain distinct. Repository identity must match, not just an object number.
Branch names are exact names, not glob filters. Branch deletion is a deliverable event; the subscription
keeps following that exact name if recreated, never another ref. A commit subscription stays fixed.

Implement subjects incrementally and advertise only working ones through provider discovery. PR CI
and branch CI are activity feeds, not required-checks or aggregate health evaluators. Keep event SHA
and branch/workflow metadata so an agent can read authoritative current state before proceeding.
`workflow_run` and `check_suite` may be explicit selections once supported, not duplicate default CI
notifications. Workflow-specific filters, issues, repository-wide events, tags/releases, and deployments
are on the radar but not required for this slice. This is not GitHub's personal Notifications API.

### Matching PR CI events correctly

Checks/statuses are associated with commits, not reliably with a PR number. Resolve the PR's current
head through the provider's GitHub read access; refresh on `pull_request` / `synchronize`. Correlate
by repository identity and SHA, not a global SHA lookup or branch-name substring. Handle empty
`pull_requests` arrays, fork PRs, multiple PRs sharing a head, delayed deliveries, and out-of-order
head/check updates. Ambiguous or temporarily unresolved matches need bounded reconciliation rather
than silently dropping a relevant completion.

Default intent is **follow the PR as its head changes**, not a fixed commit. Keep the event SHA in the
payload and do not present an old-head completion as current PR health. Define and test this behavior
before enabling checks/status subscriptions. GitHub remains authoritative for current PR/CI state;
we do not build another required-checks/mergeability evaluator.

### Direct webhook ingress (decided), provider-owned verification

**Decision:** GitHub targets the notification service directly over HTTPS, for example a provider route
`POST /v1/webhooks/github`. There is no integration-app relay or separate webhook/demultiplexing service.
The notification service verifies, durably ingests, and demultiplexes deliveries to subscriptions.
Use the App's shared webhook across installations, never one webhook per agent subscription. The existing Flux webhook handles push/registry reconciliation; it is not an Agentplane
notification ingress and should not be repurposed to couple these services.

The provider verifies `X-Hub-Signature-256` against the exact raw body with a configured signing secret,
validates the event and repository/installation against the source configuration, and applies body/rate
limits before durable acceptance. This route uses GitHub authentication, not agent workload bearer auth;
agent subscription/inbox routes remain workload-authenticated. Provision secrets, HTTPS ingress, and
narrow network access declaratively, without exposing credentials to agents.

Persist verified delivery metadata and the actual GitHub payload before acknowledging acceptance.
Use the source plus `X-GitHub-Delivery` as a retry identity, detect conflicting reuses, and deduplicate
fanout into each inbox even when subscriptions overlap. Perform fanout asynchronously with existing
PostgreSQL fencing/recovery; no new message broker. An acknowledged webhook must survive worker restart.
Return a failure if durable acceptance fails rather than acknowledging and losing it.

GitHub webhooks do not provide the Actions provider's complete historical replay contract. Establish
an explicit local subscription boundary against durable ingress so arrivals during creation are not
lost. V1 can be live-follow from that boundary: advise agents to **subscribe, then read current PR
state** so an already-completed check does not leave them waiting. Do not invent historical webhook
notifications from a current snapshot. Redelivery/local retained-delivery replay is distinct from
reconstructing everything that happened before the webhook was configured; broader backfill is deferred.

### App, authorization, and access loss (decided)

Create a new **`agentplane-staging` GitHub App**, not a notification-specific App and not the existing
MCP OAuth App. It is the staging environment's App and can support other Agentplane integrations later.
Install it on the operator's selected repositories, with multiple installations/repositories supported
by the design. Do not hardcode a single owner, repository, installation ID, or public-only restriction.
The operator is comfortable granting broad repository permissions; notification support does not impose
a read-only ceiling on the App. Additional write permissions do not imply agent write authorization.

**Initial authorization policy:** any authenticated Agentplane workload may subscribe to any repository
currently accessible through this App. This intentionally includes private repositories: installing the
App makes their captured webhook payloads available to agents. No per-ServiceAccount repository grants
in this slice. Existing inbox ownership still governs reads; shared source access does not grant access
to another workload's inbox. TODO: consider narrower repository/event grants if needed.

Resolve repositories to stable IDs and authorized installations. Verify signatures and validate payload
installation/repository against the App's current access, at creation and before fanout. Installation
suspension/uninstallation, repository removal, or confirmed permission loss stops future matching and
surfaces an actionable subscription error; retain delivered entries under normal inbox retention.
Transient API failures/rate limits instead retry with backoff, not permanent access revocation. Process
installation lifecycle events and reconcile access so a missed webhook does not leave stale authority.
Recheck access on recovery; do not promise replay of events missed while access was unavailable.

### Registration and credential preparation

The operator will register the App and supply SOPS-encrypted credentials later. The checked-in
[credential template](github-app-credentials.example.yaml) contains only empty placeholders and is not
part of any Kustomization. It is a proposed Secret contract for the implementation, not live wiring.
Populate the final `cluster/k8s/agentplane-staging/github-app.sops.yaml` through the existing cluster SOPS
workflow; never commit plaintext credentials. The App private key and webhook signing secret stay in
trusted services, not agent prompts, agent sandboxes, or the integration app's user sessions.

Registration checklist:

- Name: `agentplane-staging` (subject to GitHub name availability); this is a new environment-wide App.
- Direct HTTPS webhook route: `/v1/webhooks/github` on the notification service's ingress. The final
  public hostname/DNS/certificate and route must be wired before enabling real deliveries. No app relay.
- Notification-required repository permissions: Metadata read, Contents read (refs/commits/pushes),
  Pull requests read, Issues read (issue comments), Checks read, Commit statuses read; Actions read for
  workflow-run selection when implemented. The operator may grant additional permissions for other
  Agentplane uses. Notification installation tokens should request only the read permissions they need.
- Subscribe to `pull_request`, `issue_comment`, `pull_request_review`, `pull_request_review_comment`,
  `check_run`, `status`, `push`, `create`, and `delete`; handle App installation/repository lifecycle
  events too. Add `workflow_run`/`check_suite` when their matching is implemented. Verify actual GitHub
  permission/event requirements during registration and fixture work, especially fork PR checks.
- Use App JWT authentication to mint short-lived installation tokens, route reads by installation,
  and handle expiry/rotation. No user OAuth client secret or browser callback is needed for this flow.
- Do not assume a base-repository installation receives every event produced in an uninstalled fork.
  Check actual delivery coverage; bounded API reconciliation can recover observable current state,
  but cannot claim complete webhook history from repositories outside the installation.
- Existing staging GitHub MCP OAuth uses credentials copied from Haku and a user callback; leave it
  untouched. The staging Kustomization already consumes SOPS Secrets, but this template is deliberately
  not included and no new Secret reference or ingress is deployed by this planning change.

### Small, data-preserving provider generalization

Current storage and wire models contain Action-specific `request_id` and sequence fields. Add only
the provider-tagged subscription/event identity needed by the second real provider. Keep provider
filter/content schemas concrete and discoverable through `/v1/providers`; no speculative plugin framework
or universal event DSL. Move Actions fields into its `source` variant and update callers/prompts in the
same change. Give entries provider-tagged source identities rather than fabricating Action UUIDs for
GitHub events; retain raw GitHub content and delivery metadata.

Use an additive migration/backfill that preserves existing subscriptions, entries/payloads, cursors,
acknowledgements, notice IDs, and receipt checkpoints. Rollout must tolerate old/new worker overlap;
activate GitHub sources only when all serving workers understand the new provider. Reuse existing
inbox/HWM, retention, notice delivery, and Sandbox Service authorization instead of duplicating them.

### Build order / remaining work

Implementation and fixture-based tests can proceed with placeholders; operator registration and
credentials block live integration, not the provider model/storage work.

- [x] Agree on the environment-wide App, shared repository access, access-loss handling, and PR/branch/commit scope.
- [ ] Register the App and supply encrypted credentials (operator); validate event coverage and wire ingress.
- [ ] Implement the minimal provider-tagged models and data-preserving storage migration; retain Actions behavior.
- [ ] Add verified, durable webhook intake, source deduplication, and restart-safe fanout.
- [ ] Add PR/comment/review matching, then branch/commit matching, head-aware CI, and bounded reconciliation.
- [ ] Wire declarative source credentials/webhooks/ingress/egress, provider discovery, and agent examples.
- [ ] Prove signed GitHub delivery through the real inbox/harness/read/ack path in staging, with no app dependency.

Acceptance includes invalid signature and oversized-body rejection; unauthenticated callers and
repositories outside App access; retry/conflicting delivery IDs; overlapping subscriptions; creation/cancellation races; fork/head-change
and empty-PR-list CI cases; branch deletion/recreation; fixed commit identity; multiple installations;
access loss and transient failure handling; preserved Actions data; concurrent replicas; and restart
after HTTP acceptance but before fanout. Reuse the native delivery tests rather than building another
runner test stack. Live proof should cover a real PR comment and check/status update, not only a synthetic
signed payload or a successful ingress HTTP response.

## Still deferred

- TODO: consider removing subscription pause and `lifetime_days`, rather than carrying them forward
  as requirements of the provider abstraction. Delete/recreate can replace pause/resume (with a fresh
  idempotency key under the current cancellation semantics). Mandatory finite lifetimes can silently
  stop notifications an agent is relying on. Prefer no automatic expiry; if expiry remains, make it
  optional and explicitly requested, and define how the agent is warned/notified when it expires.
  Review the combined pause/renew PATCH contract as part of this simplification. This TODO does not
  change current API behavior or stored subscriptions.
- Command-scoped admission/confirmation/failure tracking through Sandbox Service, resumable by command
  ID and backed by the runner's existing journal. Notifications does not need conversation content;
  today's tail checkpoint is the bounded fix, not the eventual interface.
- Automatic Action following or a submission convenience flag; durable authorized intent and reconciliation
  are required, not a best-effort second HTTP request.
- GitHub personal Notifications API, generic arbitrary filters, automatic webhook creation per agent,
  complete historical reconstruction, issue/repository subjects, workflow-specific filters, tags/releases,
  and deployment/environment subscriptions. PR, branch, and commit subjects are in the current slice.
- Notification-triggered provisioning/resume, offline-delivery guarantees, and wake budgets.
- Runner-hosted MCP context conveniences, per-session identities, cross-account delivery, or implicit
  retargeting across successor sessions. ServiceAccount authority and explicit destinations remain.
- Proper runner RPC authentication/TLS. Service APIs are already authenticated; this is the runner-leg follow-up.
