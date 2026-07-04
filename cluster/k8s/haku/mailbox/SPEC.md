# haku mailbox — SPEC

A Stalwart mailserver serving `allegedly.works` mail, whose single mailbox
(`haku@allegedly.works`) belongs to Haku. Promises:

- **Only verified operator mail is delivered.** At the SMTP DATA stage the
  server rejects (550) any message whose envelope sender fails SPF
  verification or is not on the operator whitelist. Mail from a spoofed
  whitelisted address fails SPF (the spoofer's IP isn't in the sender
  domain's SPF record) and never lands. SPF rather than DMARC: Stalwart
  runs the DATA-stage script before its DKIM/DMARC analysis, so the DMARC
  verdict structurally isn't available at rejection time (verified on
  0.16.11); the MAIL-stage SPF verdict is.
- **Policy is operator-owned.** The whitelist, listeners, directory, and
  every other server setting live in the provisioning plan in this directory
  (applied idempotently at pod start); changing any of it is a ducktape PR.
  Haku is a mail _user_ — it holds no admin credential and cannot reach the
  `haku-mailbox` namespace through its RBAC.
- **Haku authenticates only with its Authentik identity.** The server's OIDC
  directory validates bearer tokens against the dedicated `stalwart-haku`
  provider and rejects tokens minted for any other audience — on every
  reading channel (JMAP over the public route, IMAP cluster-internal via
  SASL OAUTHBEARER). No mailbox password exists, and the directory
  structurally rejects password authentication.
- **The mailbox is Haku's to manage.** Read, organize, delete — the contents
  are agent-owned state (not operator-audited, unlike the old spool design).
- **Receive-only.** No submission or relay service is configured; the server
  cannot send outbound mail, and the domain publishes SPF `-all` /
  DMARC `reject`.
- **Authenticated envelope, not content.** Delivery verifies the sending
  address; quoted/forwarded text inside a verified message is still
  third-party data.
