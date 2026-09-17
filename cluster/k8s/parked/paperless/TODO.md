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

## Make bootstrap ordering explicit

**Problem.** `paperless-app` is consumed from a generated `ExternalArtifact`: the
artifact generator copies the whole `cluster/k8s/paperless/app/**` tree, and one Flux
Kustomization applies the Deployment and `paperless-bootstrap-group` Job together.
Flux does not provide resource-level ordering within that Kustomization. The Job must
therefore tolerate starting before Paperless has completed its image-managed database
migrations; an init container would run even earlier and could block the Deployment on
the very migrations that Paperless performs during startup.

**Future options.** Keep the Job idempotent and retrying in the current artifact, or,
if strict ordering becomes necessary, create a separate generated bootstrap artifact
and Flux Kustomization that depends on `paperless`. The latter also requires updating
the artifact generator and the root Flux consumer declarations; it is not just a
directory split. Do not convert this to an init container without also taking ownership
of the migration ordering.
