# Agentplane staging deployment artifact

The environment-level Flux Kustomization consumes the `agentplane-staging`
ExternalArtifact at the environment root. Its artifact contains the complete
Kustomize input closure for staging; the Flux declaration itself remains owned
by the root Kustomization and is excluded from the artifact.

`agentplane-staging.k8s.yaml` is one generated chart for the whole environment (Namespace,
RBAC, the model-catalog ConfigMap, and the workload surface -- db, llm-ingress,
egress, app, actions) -- see `cluster/cdk8s/agentplane/`. SOPS-encrypted Actions data
is decrypted by the environment-level Flux consumer. The consumer explicitly checks the
trust-manager-generated CA ConfigMap and Bundle because the ConfigMap is created
asynchronously outside the artifact.

## Image automation

The shared `all-images` automation owns the tags in `image-pins/` through Flux Setters
markers. See the
[lifecycle contract](../../../agentplane/docs/runner_delivery_and_harness_design.md#rollout-and-rollback).

The `agentplane-staging` and `agentplane-staging-delivery` Flux Kustomizations consume
this Component from the repo source. A template update affects future Sandbox
incarnations; it does not upgrade a running Sandbox. Keep existing Sandbox UIDs and
storage when suspending/resuming to adopt a new runner image.

## Native executor rollout

The Pod and VM templates explicitly select the native execution backend. This records the
staging choice; it is not a rollout gate: the Runtime already defaults to native when the
implementation field is absent. The shared image automation can advance the runner disk before
this documentation change merges. A template or image update applies to new Sandbox incarnations,
not an already-running one.

Before replacing a Sandbox, inventory active sessions, execution handles, and authored data.
A backend restart loses execution handles and does not replay uncertain work. Sandbox deletion
can also remove its owned PVCs; follow [staging data preservation](../../../agentplane/docs/staging_data_preservation.md)
and preserve the workspace and state before any deletion. The operator can delete Sandboxes for
this switchover, but that does not make authored contents disposable.

Verify the runner-disk image tag and the staging root and delivery Flux revisions contain the
intended implementation, then start a fresh disposable Pod Sandbox. Confirm that the Runtime and
executor companion are ready and that an execution session can start a process, read output,
write stdin, and read a file. Exercise the Codex WebSocket surface as well as the typed execution
API. Repeat with a VM Sandbox if the VM template is in use. Check for backend restarts and failed
or uncertain executions before replacing additional Sandboxes. Record the applied image, source
revision, Sandbox identity and observed results.

To select the Codex-backed implementation instead, change the template's `implementation` to
`codex`, regenerate the staging manifest, and roll out a new Sandbox incarnation. Check the same
execution flows after rollback. In-flight handles from the old backend cannot be resumed.

## Runtime configuration v3 and systemd placement

Roll out the v3 Runtime ConfigMaps, runner disk, and NixOS VM root together. Match
the published source revisions to the live image references. The new loader
rejects v2, and the old loader rejects v3. New or restarting Sandboxes can fail
while the image pins and ConfigMaps reconcile. The VM root supplies
`agentplane-commands.slice` and reads the explicit supervision setting.

Use a fresh disposable Pod to check direct execution. In a fresh disposable VM,
check ordinary command output and verify that the executor and commands occupy
separate slices. An OOM within the command budget should produce a failed command
and allow a subsequent command in the same execution session to succeed. Check
suspend and resume as well. Preserve existing Sandboxes, PVCs, and authored data
during the cutover.

To revert only placement, select `direct`, remove the guest's `executor_workload`
budget, and restore the previous combined executor budget. To revert the wire
version, restore the v2 ConfigMaps and matching runner disk and VM root pins
together. Verify fresh Sandboxes after Flux applies either rollback.

## Sessions and Environments rollout checks

Check that the staging root and delivery Kustomizations have applied the intended source
revision, both Services have ready EndpointSlices, and `agentplane-sandbox-controller`
renews its Lease from a live `environments` Pod. Mimir should have one healthy
`up{namespace="agentplane-staging",service="environments"}` series per ready Environments Pod.

From `environment-runtime` in a fresh disposable Pod Sandbox, run both read-only RPCs through
the Sandbox relay.

```bash
agentplane session-dispatcher list --limit 1
agentplane environments list --limit 1
```

## Action policies

This environment's Git-managed Action policy sets and standing `PolicyBinding`s are defined in
`cluster/cdk8s/agentplane/actions_staging_policies.py` and generated into
`agentplane-staging.k8s.yaml`. OpenClaw's separate egress `PolicyBinding` is defined in
`cluster/cdk8s/public_coder/egress.py`. A new set or caller binding is a PR to its owning Python
module (regenerate with `bb run //cluster/cdk8s:generate_manifests`). Each set is
rendered as a `PolicySet` whose rules the egress proxy and the Action Service each read by
type (`egress`, `action_auto_approve`), bound by `PolicyBinding`s. The generator parses every set with the Action Service's own models, so a
spec the service would refuse fails generation (and CI's `//cluster/cdk8s:test_generate_manifests`)
instead of granting nothing on the cluster.

## Browser notifications

`web-push-vapid.sops.yaml` owns a staging-only P-256 VAPID identity. The
Action Service reads the private key through a Secret-backed environment variable
and derives the public subscription key at startup. Redeployment preserves the key;
rotation requires browsers to register again. Do not reuse it for testing or Haku.

The application allowlist and the HTTPS FQDN/SNI egress rule are both built from one
tuple in `cluster/cdk8s/agentplane/staging.py`. Currently Chrome/Chromium (FCM) and
Firefox (Mozilla Autopush) are allowed. Other browser push services require explicit
review before joining it. DNS inspection lets Cilium learn the endpoint IPs; it
does not authorize arbitrary outbound HTTPS.

Acceptance: sign into staging, open Notifications, register the browser, and confirm
that `/push/config` supplies a public key and `/push/subscriptions` records the
browser. Submit a harmless approval request and verify its notification arrives;
resolve it through the authenticated UI and verify the notification is retracted.
Repeat after a service restart to check identity persistence. A push-service HTTP
acknowledgement alone does not prove browser display. Notification payloads contain
Action identity/version, not Action arguments or results.

To disable delivery, remove the `web_push` settings and the private-key environment
variable together; retain the encrypted key so re-enabling preserves subscriptions.

## MCP OAuth callbacks

MCP OAuth redirects in the `actions-settings` ConfigMap's `settings.yaml`
must return to the staging integration app's `/mcp-linkage/callback`. The app forwards
completion to the Action Service using the operator's authenticated session, then
returns the browser to the MCP servers page.

Kubernetes uses the public `kubectl-passthrough-mcp` client. Its exact callback allowlist
is managed in `tf/gitops/agent-machine-access/kubectl-common.tf`; that Terraform change
must reconcile before linking Kubernetes through this origin.

GitHub uses the existing GitHub App credentials, which ESO copies from
`haku-console/haku-console-github-mcp-client-credentials`. The App registration is managed
outside this repository. Its owner must include
`https://agentplane-staging.allegedly.works/mcp-linkage/callback` among the user-authorization
callback URLs. GitHub Apps support
[multiple callback URLs](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url).
Changing this deployment's `redirect_uri` does not update the registration.

## Staging GitHub App

The `agentplane-staging` GitHub App serves the staging environment, initially for
PR, branch, and commit notifications. It is separate from the existing MCP OAuth App
above. All authenticated agents may subscribe to repositories accessible through the
new App, including private repositories; install it with that sharing policy in mind.

Registration, event permissions, and access-loss semantics are recorded in the
[GitHub App setup](../../../agentplane/notification_service/README.md#github-app-setup).
The public App ID is configured in the notification service YAML settings, not a Secret.
The operator-supplied `github-app.sops.yaml` contains the `agentplane-github-app` Secret
with only `private-key` and `webhook-secret`. The staging Kustomization includes it for
Flux decryption. The notification server reads these through Secret-backed
`AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY` and
`AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET` environment variables. Existing MCP
credentials/callbacks are unchanged.
Testing has no GitHub configuration or Secret references.

App **5188971** is enabled and the operator confirmed installation on all their repositories.
Real CI events and a PR comment from `agentydragon/ducktape` reached an agent inbox and harness on
2026-10-04; see the [acceptance record](../../../agentplane/notification_service/docs/staging_github_acceptance.md)
for evidence and remaining checks. The webhook is active at the URL below; keep signing and TLS
verification enabled. This proof does not audit all installed repositories or event permissions.

To disable the source, set staging's `notifications_github` to `None`; this removes both
its YAML settings and secret environment variables. Retain the encrypted Secret so
re-enabling does not require rotating App credentials.

### Webhook ingress

The staging chart routes this exact path directly to the notification service:

```text
https://agentplane-notifications-staging.allegedly.works/v1/webhooks/github
```

The shared Gateway uses the existing `*.allegedly.works` DNS record and wildcard TLS
certificate; no per-host DNS record or certificate is needed. The staging-only Cilium
policy admits the Gateway identity to the notification service port. The HTTPRoute
exposes no subscription, inbox, discovery, docs, or health endpoints. Webhooks use
GitHub signature verification, not operator SSO or workload authentication.

After Flux applies the configuration, check notification replica readiness, GitHub
source discovery, the HTTPRoute's `Accepted` and `ResolvedRefs` conditions, and TLS
verification. A POST with a valid `X-GitHub-Delivery` UUID and `X-GitHub-Event: ping`
but no signature must return HTTP 401. Confirm private paths such as `/v1/inboxes` and
`/v1/subscriptions` return Gateway 404s. Then test a real signed GitHub delivery through
durable receipt, inbox entry, runner notice, read, and explicit acknowledgement.
Track App enablement and end-to-end verification in
[ducktape#8956](https://github.com/agentydragon/ducktape/issues/8956).
