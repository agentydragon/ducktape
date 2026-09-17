# Paperless-ngx: service-link env collision + fresh-install SSO claim

Two non-obvious gotchas hit while bringing up `cluster/k8s/parked/paperless/` (single-user
Authentik OIDC, passwordless). Both are reusable beyond Paperless.

## 1. Service named `paperless` crashed the app via legacy service-link env vars

**Symptom**: app pod `CrashLoopBackoff`, startup probe `connection refused`, logs:

```text
Error: Invalid value for '--port' (env var: 'None'): 'tcp://10.99.121.47:8000' is not a valid integer.
```

**Root cause**: Kubernetes injects legacy Docker-link env vars for every Service in the
namespace: `<SVCNAME>_PORT=tcp://<clusterIP>:<port>`, `<SVCNAME>_SERVICE_HOST`, etc.
The Service was named `paperless`, so the pod got `PAPERLESS_PORT=tcp://<clusterIP>:8000`
— which **collides with Paperless-ngx's own `PAPERLESS_PORT` config var**. granian then
received a URL instead of an integer and died on every start.

**Fix**: `enableServiceLinks: false` on the pod spec (commit in
`cluster/k8s/parked/paperless/app/deployment.yaml`). granian falls back to its default 8000.

**General lesson**: any app whose config env var matches `<UPPERCASE_SERVICE_NAME>_PORT`
(or `_SERVICE_HOST`/`_SERVICE_PORT`) will be silently overwritten by the service-link
injection. Set `enableServiceLinks: false` (good default for any single-purpose app) or
avoid naming the Service the same token the app uses as a config prefix.

## 2. Fresh-install Paperless hides SSO and opens local signup

**Symptom**: `/accounts/login/` auto-redirects (Angular frontend) to `/accounts/signup/`,
a local username+password registration form. `PAPERLESS_DISABLE_REGULAR_LOGIN=true` and
`PAPERLESS_ACCOUNT_ALLOW_SIGNUPS=false` are **ignored**, and the "Log in via Authentik"
button is unreachable through the UI.

**Root cause** (`paperless/adapter.py`): `CustomAccountAdapter.is_open_for_signup`
force-returns `True` when there are zero real users (`exclude(username__in=["consumer",
"AnonymousUser"])`) **and** zero documents — the "fresh install" claim flow. The frontend
routes login → signup to match.

**Fix / claim procedure**: hit allauth's provider-login endpoint directly — it's
server-rendered by Django, bypassing the Angular redirect:

```text
https://paperless.allegedly.works/accounts/oidc/authentik/login/?process=login
```

→ "Continue" → Authentik → first SSO login creates the account. Once a real user exists,
the fresh-install branch flips off: signup closes and the SSO login page behaves normally.

**Outcome gotcha — first SSO user is NOT a superuser**: the adapter's fresh-install
superuser promotion (`save_user`, `is_superuser=True`) only runs on the _local-signup_
path, not the social path. The first SSO user lands as a **regular** user with an unusable
password, in whatever `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS` names (here `paperless_users`,
granted all `documents`+`paperless` perms by the `paperless-bootstrap-group` Job). This is
fine for single-user/non-admin use. To grant real Django admin later (manage other users /
Django admin backend), one-off promote:

```bash
kubectl exec -n paperless deploy/paperless -- python3 manage.py shell -c \
  "from django.contrib.auth.models import User; u=User.objects.get(username='agentydragon'); u.is_superuser=u.is_staff=True; u.save()"
```

**DR note**: if the Postgres DB is wiped/restored-empty, the cluster is "fresh" again —
repeat the claim procedure to recreate the SSO-linked account.
