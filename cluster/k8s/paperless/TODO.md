# Paperless TODO

## Make first-user bootstrap declarative (remove the manual SSO claim)

**Problem.** On a fresh install (zero real users + zero docs), Paperless's adapter
force-opens the local signup page and the Angular frontend redirects `/accounts/login/`
→ `/accounts/signup/`, hiding the "Log in via Authentik" button. The only way to claim
the first account via SSO is to hit allauth's endpoint directly
(`/accounts/oidc/authentik/login/?process=login`). This is a manual, out-of-band step —
it violates the cluster's "declarative turnkey bootstrap" directive — and it leaves a
window where local signup is open to anyone who reaches the URL. Background:
<../../docs/lessons_learned/2026_06_22_paperless_servicelinks_and_fresh_install_sso.md>.

**Goal.** Bring up Paperless with no manual claim step and no open-signup window: after
Flux reconciles, agentydragon's SSO-linked account already exists.

**Approach to evaluate.** Extend the `paperless-bootstrap-group` Job (or add a sibling)
to idempotently pre-create the `agentydragon` user with an unusable password, add it to
`paperless_users` (and set `is_superuser`/`is_staff` if we decide we want admin), and
**pre-link the allauth `SocialAccount`** so the first OIDC login resolves to it instead
of triggering signup. The Authentik `sub` for agentydragon is
`5f1415ca9b1e49945b31dcbf4bfa27daafbd9b0aea4a30baa740918439aa5e3f` (provider `authentik`).
Hardcoding the `sub` is brittle if the Authentik user is recreated — prefer fetching it
from Authentik (or accept the documented DR re-claim). Once a real user exists, the
fresh-install branch is off, so signup closes and the SSO login page behaves normally.

**Open questions.**

- Why didn't `PAPERLESS_REDIRECT_LOGIN_TO_SSO=true` redirect `/accounts/login/` straight
  to Authentik on fresh install? If it can be made to, that alone removes the manual step
  without pre-seeding the user.
- Decide superuser vs. regular for the pre-seeded account (currently regular by design).

## Move group membership to Authentik (future)

Paperless v3.1.3 can synchronize Django group membership from an OIDC claim. This is a
future multi-user/admin improvement, not part of the v3.1.3 upgrade. The current setup
uses `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS=paperless_users`; group synchronization is
not enabled, and `paperless-bootstrap-group` creates that local group and grants its
permissions. See the [Paperless configuration reference](https://github.com/paperless-ngx/paperless-ngx/blob/v3.1.3/docs/configuration.md).

### Intended ownership

- Authentik owns membership: which people belong to `paperless_users`, `paperless_staff`,
  or an explicitly approved admin group.
- Paperless owns application authorization: the corresponding Django groups must exist,
  and their document/application permissions must be defined locally.
- The OIDC `groups` claim values must match the local Django group names exactly. Group
  synchronization changes memberships on login; it does not create missing groups or
  grant permissions.
- Do not map an Authentik group to Paperless superuser status casually. A superuser can
  manage users and bypass normal authorization checks; keep the current regular-user
  default unless there is a specific admin requirement.

### Migration sequence

1. Choose stable names and semantics for the Authentik groups. At minimum, preserve
   `paperless_users` as the baseline non-admin group. Decide separately whether staff or
   superuser access is needed.
2. Add an Authentik property mapping/provider configuration that emits the selected group
   names in the OIDC `groups` claim. Verify the actual claim in a test token or userinfo
   response; do not assume Authentik's UI name is the emitted value.
3. Ensure the matching Django groups already exist before enabling synchronization. Keep
   `paperless-bootstrap-group` during this phase so it remains the declarative owner of
   `paperless_users` and its `documents`/`paperless` permissions. Add any staff/admin
   groups with deliberately scoped permissions.
4. Deploy `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS=true`, setting
   `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS_CLAIM` if the claim is not `groups`. Before the
   first login with sync enabled, ensure the current user receives `paperless_users` in
   the claim: synchronization replaces the user's Paperless groups and an omitted
   baseline group would remove ordinary access.
5. Log in as each affected user and verify group membership, document access, sharing,
   and any staff/admin boundary. Confirm that removing an Authentik group removes the
   corresponding Paperless access on the next login. Keep regular login disabled and the
   SSO redirect settings unchanged during this rollout.
6. Only after the claim, local groups, permissions, and rollback path are proven should
   we remove `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS` or change the Authentik provider's
   access policy.

### Bootstrap retirement gate

Group synchronization alone is not a reason to delete the bootstrap Job. Retire it only
if another declarative mechanism creates the required local Django groups and assigns
their permissions. Otherwise, reduce the Job to that local schema/bootstrap role while
letting Authentik own membership. `PAPERLESS_ADMIN_USER` is not a replacement: it creates
a local password-based superuser and does not link or synchronize an OIDC account.

### Rollback

If group claims are missing or incorrect, disable `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS`,
restore the known-good default-group configuration, and retain the bootstrap Job. Do not
delete local groups or permissions as part of a first rollout.
