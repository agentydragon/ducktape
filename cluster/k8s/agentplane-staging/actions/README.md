# Staging Action Service

## Browser notifications

`web-push-vapid.sops.yaml` owns a staging-only P-256 VAPID identity. The
Action Service reads the private key through a Secret-backed environment variable
and derives the public subscription key at startup. Redeployment preserves the key;
rotation requires browsers to register again. Do not reuse it for testing or Haku.

The application allowlist in `settings.yaml` and HTTPS FQDN/SNI egress in
`networkpolicy.yaml` must agree. Currently Chrome/Chromium (FCM) and Firefox
(Mozilla Autopush) are allowed. Other browser push services require explicit review
and changes to both lists. DNS inspection lets Cilium learn the endpoint IPs; it
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

MCP OAuth redirects in `settings.yaml` must return to the staging integration app's
`/mcp-linkage/callback`. The app forwards completion to the Action Service using the
operator's authenticated session, then returns the browser to the MCP servers page.

Kubernetes uses the public `kubectl-passthrough-mcp` client. Its exact callback allowlist
is managed in `tf/gitops/agent-machine-access/kubectl-common.tf`; that Terraform change
must reconcile before linking Kubernetes through this origin.

GitHub uses the existing GitHub App credentials reflected from
`haku-console/haku-console-github-mcp-client-credentials`. The App registration is managed
outside this repository. Its owner must include
`https://agentplane-staging.allegedly.works/mcp-linkage/callback` among the user-authorization
callback URLs, retaining Haku Console's existing callback. GitHub Apps support
[multiple callback URLs](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-user-authorization-callback-url).
Changing this deployment's `redirect_uri` does not update the registration.
