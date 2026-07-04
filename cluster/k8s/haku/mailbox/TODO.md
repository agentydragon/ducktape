# haku mailbox — TODO

- **Restore DKIM/DMARC-based gating** (operator, 2026-07-04). The SMTP-time whitelist
  gate runs on the SPF verdict only, because Stalwart (0.16.11) executes the DATA-stage
  script before its DKIM/DMARC analysis — so mail relayed through a non-Google server
  bounces even when its DKIM signature would validate (direct Gmail sends are
  unaffected). Paths, whichever lands first:
  - Upstream: a rejection-capable hook that runs after message analysis (or the
    verdicts exposed to the DATA-stage script). Re-check the changelog on Stalwart
    upgrades — this TODO's gate is "once `env.dmarc.result` is populated at a hook
    that can still 550".
  - Delivery-time Sieve instead of SMTP-time: the per-account script sees
    `Authentication-Results` (DKIM included) and could file non-passing mail to a
    quarantine folder (or DSN-reject). Trades the clean SMTP 550 for
    forwarding-tolerance; only worth it if forwarded mail becomes a real workflow.
