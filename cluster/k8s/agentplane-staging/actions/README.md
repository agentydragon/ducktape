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
