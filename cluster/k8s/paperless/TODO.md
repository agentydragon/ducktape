# Paperless TODO

## Validate the Authentik-backed single-user admin

[PR #6571](https://github.com/agentydragon/ducktape/pull/6571) implements the
single-user design:

- Authentik owns a Paperless-specific `paperless_admins` group containing only
  `agentydragon`.
- The Paperless OIDC provider emits an app-specific `groups` claim. Membership in
  `paperless_admins` produces both `paperless_users` and `paperless_admins` claim values.
- Paperless synchronizes those values to local Django groups and maps
  `paperless_admins` to Django superuser status.
- The bootstrap Job creates the local groups and their document/application permissions;
  it does not create or promote a Paperless user.
- Paperless access is gated by the dedicated group, not the shared global Authentik Admins
  group. Regular password login remains disabled.

Paperless's `DEFAULT_GROUPS` and `SYNC_GROUPS` settings require the named local Django
groups to already exist. They do not create groups or permissions. `PAPERLESS_ADMIN_USER`
only creates a local password-based superuser and does not link an OIDC account. The
current bootstrap is therefore still required for local group/permission schema, even
though Authentik owns membership.

### Post-merge acceptance

After Flux applies the Terraform and Paperless changes:

1. Confirm the Authentik `paperless_admins` group contains only `agentydragon`, and the
   Paperless application policy is bound to that group.
2. Confirm the provider's ID token or userinfo response contains the expected
   `groups` values: `paperless_users` and `paperless_admins`.
3. Confirm `paperless-bootstrap-group` succeeds and both local groups have the expected
   permissions.
4. Log in through Authentik and confirm the account is `agentydragon`, has both local
   groups, and is a Django superuser. Verify the Paperless UI and API, including the admin
   area, while confirming regular password login remains unavailable.
5. Re-check the fresh-install behavior separately. The v3 adapter still has a special
   first-user path; if `/accounts/login/` routes to local signup on an empty database,
   retain the documented direct OIDC claim path rather than weakening the SSO-only policy.

### Follow-up decisions

- Keep `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS=paperless_users` as the safe fallback until
  the claim path is verified. It can be removed in a later small change if the only login
  path is the Authentik-admin claim.
- Do not remove the bootstrap Job merely because group synchronization works. If desired,
  reduce it to the smallest idempotent local schema/permission bootstrap; remove it only
  after another declarative mechanism takes over that responsibility.
- Do not add staff or another superuser group unless the account model changes from the
  current single-admin setup.

### Rollback

If the claim is missing or incorrect, disable
`PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS` and
`PAPERLESS_SOCIAL_ACCOUNT_SYNC_SUPERUSER_GROUP`, restore the prior Paperless access-policy
binding, and retain the bootstrap Job. Do not delete local groups or permissions during
the first rollout.
