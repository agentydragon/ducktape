# Agentplane staging deployment artifact

The environment-level Flux Kustomization consumes the `agentplane-staging`
ExternalArtifact at the environment root. Its artifact contains the complete
Kustomize input closure for staging; the Flux declaration itself remains owned
by the root Kustomization and is excluded from the artifact.

`agentplane.k8s.yaml` is one generated chart for the whole environment (Namespace,
quota, RBAC, the model-catalog ConfigMap, and the workload surface -- db, llm-ingress,
egress, app, actions) -- see `cluster/cdk8s/agentplane/`. SOPS-encrypted Actions data
is decrypted by the environment-level Flux consumer. The consumer explicitly checks the
trust-manager-generated CA ConfigMap and Bundle because the ConfigMap is created
asynchronously outside the artifact.

## Action policies

This environment's Git-managed `ActionPolicySet`s and the bindings for labeled caller
ServiceAccounts such as `claude-ai` are defined in
`cluster/cdk8s/agentplane/actions_staging_policies.py` and generated into
`agentplane.k8s.yaml`; a new set, or a binding for a ServiceAccount, is a PR to
that Python module (regenerate with `bb run //cluster/cdk8s:generate_manifests`). Bindings
for Sandbox subjects are written by the integration app when it creates the Sandbox and
are never checked in. `//cluster/validation:test_agentplane_action_policies` parses every
set and binding with the Action Service's own models, so a spec the service would refuse
fails CI instead of reporting `Ready=False` on the cluster.

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

MCP OAuth redirects in the `agentplane-actions-settings` ConfigMap's `settings.yaml`
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
