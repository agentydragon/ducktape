# 2026-07-04 — Stalwart `/jmap/session` 403 from an empty `email` claim, and how the rotator now self-heals it

## Symptom

Stalwart (Haku's mailbox, `cluster/k8s/haku/mailbox`) returned 403 at
`/jmap/session` for a JWT that authenticated fine — Stalwart's own logs showed
the token's signature and issuer checked out, but the server never resolved a
mailbox account for it.

## Root cause (fixed, #2771): the shared `haku` Authentik service account had no `email` attribute

Stalwart's OIDC directory for the `stalwart-haku` provider
(`cluster/k8s/haku/mailbox/app/mailbox-plan.ndjson`) sets
`requireScopes: {openid: true, email: true}` and derives the mailbox login
from the `email`/`preferred_username` claims. The JWT was minted from the
`haku` Authentik service account (`tf/gitops/agent-machine-access/main.tf`,
`authentik_user.haku_grocy` — the same account used for Grocy read access),
which had never set `email`. Authentik's `email` scope mapping emitted `""`,
so the token authenticated but Stalwart never resolved a mailbox account for
it.

Grocy's own auth path never noticed because it authorizes on
`X-authentik-username` (forwarded by the outpost), not `email` — so this
account had been "working" everywhere else for weeks.

Fixed by adding `email = "haku@allegedly.works"` to the `authentik_user`
resource (six-line diff). No changes needed to Stalwart's config, the
provider, or RBAC — the account was otherwise correctly wired end to end.

## Generalization (#2772): `authentik-jwt-rotation` now asserts and self-heals on claims, not just audiences

The rotator (`cluster/rotators/authentik_jwt_rotation/rotate.py`) already had
an `expected_audiences` mechanism: it asserts required audiences on every
mint, and forces an early re-mint (bypassing the ~44-day freshness gate) when
the stored token's `audiences_unencrypted` stamp doesn't already cover them —
added originally so a new audience requirement rolls out on the next hourly
run instead of waiting for expiry.

This incident was the same shape, just for a different claim: the email fix
landed in Authentik, but the already-minted `haku-mail` JWT
(`secrets/haku-mail-jwt.yaml`, valid until 2026-08-02) would have sat valid —
carrying the stale empty `email` claim — for another month before its normal
rotation, still 403ing Stalwart the whole time.

Generalized the mechanism to arbitrary claims: a new `expected_claims: dict[str, str]`
field, stamped as `claims_unencrypted` (same plaintext-suffix trick as
`audiences_unencrypted` — no SOPS decrypt needed to check freshness), asserted
post-mint (raising loudly on a real mismatch instead of silently shipping a
broken token), and forcing an early re-mint when the stored stamp doesn't
match. `rotations.yaml`'s `haku-mail` entry now declares:

```yaml
expected_claims:
  email: haku@allegedly.works
```

## Confirmed: it actually self-healed, no manual re-mint needed

The `haku-mail` entry's stored token had no `claims_unencrypted` stamp at all
(it predated this change), so on the very next hourly run after the new
rotator image deployed, the freshness check forced a re-mint automatically —
commit `e115460841744e2e26c9ea8dbc03742494bce54c`, "chore: rotate authentik
JWTs (2026-07-04): haku-mail":

```diff
-expires_unencrypted: "2026-08-02T01:15:14Z"
+expires_unencrypted: "2026-08-03T01:15:11Z"
 audiences_unencrypted:
     - stalwart-haku
+claims_unencrypted:
+    email: haku@allegedly.works
 jwt: ENC[...fresh ciphertext...]
```

The post-mint assertion also passed silently (the minted token's actual
`email` claim matched `expected_claims`), independently confirming the
Authentik fix was genuinely live before this rotation even ran.

## Takeaway

An OIDC principal that authenticates fine but gets an unexpected 403 further
downstream is a strong signal to check _which claims the consumer actually
requires_ against what the identity provider's scope mapping is really
emitting for that principal — `X-authentik-username`, `email`, and `groups`
are populated independently, and a principal can be perfectly correctly wired
for one consumer (Grocy, on username) while silently missing what a different
consumer needs (Stalwart, on email).

More generally: any rotator/minter that asserts required claims should also
be able to _detect drift_ in the already-stored artifact and force a refresh,
not just gate freshness on expiry. Otherwise every fix to an upstream
attribute needs a paired manual "now go force a re-mint" step — easy to
forget, and in this case would have left the mailbox broken for another
month. `expected_audiences`/`expected_claims`'s stamp-and-compare pattern in
`cluster/rotators/authentik_jwt_rotation/rotate.py` is a reusable shape for
this: cheap to check (no decryption), cheap to extend (one more dict key),
and it converges automatically within the hour instead of requiring an
operator to notice, diagnose, and manually intervene.
