# x/bsc

Experimental: pull personal claims / EOBs / reimbursements from **Blue Shield of California**
via the CMS-mandated Patient Access FHIR API. No code yet — only the registration and
research notes.

## Goal

Programmatically read:

- `ExplanationOfBenefit` — claims, allowed amounts, member responsibility, payment to provider, reimbursements (CARIN BB profile)
- `Coverage` — plan + group + member ID
- `Claim` — submitted claims pre-adjudication
- US Core resources (`Condition`, `MedicationRequest`, `Procedure`) — for completeness

Use case: feed `augur` and finance tooling, reconcile out-of-network reimbursements
against bank-side transactions (Plaid).

## The API

CMS Interoperability and Patient Access Final Rule (45 CFR 156.221) requires every
qualified payer to expose patient health data via a FHIR R4 endpoint with SMART-on-FHIR
auth. BSC's implementation:

- Developer portal: <https://devportal-dev.blueshieldca.com/bsc/fhir-sandbox/>
  ("dev" in the hostname is misleading — this IS the public portal)
- Supported profiles: <https://devportal-dev.blueshieldca.com/bsc/fhir-sandbox/supportedProfiles>
- Sandbox token URL: `https://dev-ext.blueshieldca.com/as/token.oauth2/`
- Sandbox FHIR base: `https://api-dev.blueshieldca.com/bsc/fhir-sandbox/fhir-server/api/v4/cloud/`
- Production FHIR base: not published — issued after attestation review
- Auth: OAuth2 authorization-code + PKCE (SMART App Launch); member logs in with their
  blueshieldca.com credentials and grants consent

Standards stack: HL7 SMART App Launch · FHIR R4 · CARIN BB (commercial-payer EOB) ·
Da Vinci PDex (Coverage) · US Core.

## Registration log

### 2026-05-29 — initial attempt

The landing page <https://devportal-dev.blueshieldca.com/bsc/fhir-sandbox/> has
two distinct "create account" CTAs:

1.  **Broken: `/bsc/qa2/createapps`** — one of the inline "create" links points
    here. 404s into the site's "no such page" search fallback:

    > The page you requested does not exist. For your convenience, a search was
    > performed using the query createapps.

    The `qa2` path is presumably a staging-namespace URL that was leaked into the
    public CTA and never fixed.

2.  **Apparently working: top-right "Create account" button** — leads to the
    normal signup flow on `/bsc/fhir-sandbox/`. The signup form offers two
    user registries to register against — and **picking the wrong one is the
    actual bug.**

**BSC LDAP APIC USER REGISTRY** — BSC's internal corporate LDAP (employee
directory). External developers can't self-register into this; the request
queues for a human approver who won't approve. This was the first attempt; it
returned simultaneous banners:

- Red: `Unauthorized`
- Green: `Your registration request has been received. You may now sign in if
your request has been successful.`

Screenshot: <screenshots/2026-05-29_signup_ldap_failure.png>

**fhir-sandbox Catalog User Registry** — APIC's built-in catalog-local user DB.
This is the standard self-service path for third-party devs; no LDAP, just
username/password stored in the catalog. Should activate immediately or only
require email verification. **This is the registry to use.**

Subsequent login attempt at the signin form (same portal):

> Unauthorized
> Unable to sign in. This may be because the credentials provided for authentication
> are invalid or the user has not been activated. Please check that the user is
> active, then repeat the request with valid credentials. Please note that repeated
> attempts with incorrect credentials can lock the user account.

Consistent with "account exists but pending admin activation."

**Resolution: use the Catalog User Registry, not LDAP.** Signup via Catalog
User Registry at
`/user/register?registry_url=/consumer-api/user-registries/1ec6dc66-d3db-4eca-9b3d-e8ed5e43147e`
worked: "Your registration request has been received. You will receive an email
with activation instructions if your request has been successful." The
activation email arrived; clicking the link redirected to the portal homepage
with no toast (a second click showed "There was an error while processing your
activation. Has this activation link already been used?" — which confirms the
first click succeeded silently). Account is in.

### 2026-05-29 — app registered

Registered an app named **`ducktape-test`** in the BSC dev portal. Subscribed to
the **`fhir-cloud-patient-access-v1:1.0.0` (Cloud Patient Access)** product.

Bug #3: the subscription confirmation page
`/subscription_noplan/confirm?js=nojs` returned "The website encountered an
unexpected error. Try again later." — but the subscription actually went
through. Confirmed by retrying, which surfaced: "The 'ducktape-test'
application is already subscribed to a plan in the 'fhir-cloud-patient-access-v1:1.0.0
(Cloud Patient Access)' product."

OAuth client credentials stored at
<../../cluster/k8s/agents/airlock/bsc-client-credentials.sops.yaml> — SOPS
encrypted, mounted into the airlock deployment as env vars (pattern matches
plaid/google/oura). Redirect URI registered with BSC:
`https://airlock.allegedly.works/oauth/callback/bsc`.

Subscription page bug aside, BSC sandbox onboarding is now complete.

### 2026-05-29 — airlock server-side wired up

SMART on FHIR turns out to be vanilla OAuth2 auth-code + PKCE + a few extras, so
no new provider type — just extended the existing `GenericOAuth2Provider`:

- `airlock/oauth/provider.py`: added `use_pkce: bool` and `aud: str | None` to
  `OAuth2ProviderConfig`; added `generate_pkce_pair()` (S256 verifier+challenge);
  `build_authorize_url(state, code_challenge=...)` emits `code_challenge`,
  `code_challenge_method=S256`, and `aud` when configured; `exchange_code(code,
code_verifier=...)` sends the verifier in the token POST.
- `airlock/oauth/routes.py`: replaced the module-level `_pending_states` global
  with a per-router `pending_states` dict closed over by `create_oauth_router`,
  carrying a frozen `_PendingState(provider_name, code_verifier | None)`.
- `cluster/k8s/agents/airlock/config.yaml`: added `bsc` provider entry —
  `use_pkce: true`, `aud` = sandbox FHIR base, scopes `interop offline_access`,
  redirect `https://airlock.allegedly.works/oauth/callback/bsc`. Tokens stored
  in `airlock` namespace only (no reflector annotations — keep BSC tokens
  unreflected for now).
- `cluster/k8s/agents/airlock/deployment.yaml`: `BSC_CLIENT_ID` /
  `BSC_CLIENT_SECRET` env vars from `bsc-client-credentials` k8s Secret.
- Tests in `airlock/oauth/test_provider.py` cover PKCE pair generation, S256
  challenge correctness, authorize URL with PKCE+aud, exchange with verifier,
  and the no-PKCE-no-aud default path.

**Remaining steps:**

1. Commit + push + wait for Flux reconcile.
2. Hit `https://airlock.allegedly.works/oauth/authorize/bsc` in a browser, log
   in **as a sandbox synthetic member** (NOT a real blueshieldca.com member —
   see "Sandbox member credentials" below), approve consent.
3. Confirm `bsc-tokens` and `bsc-access-token` k8s Secrets appear in the
   `airlock` namespace.
4. Call the FHIR endpoint with the access token —
   `GET /Patient`, `/Coverage`, `/ExplanationOfBenefit` against the sandbox FHIR
   base URL.
5. Iterate on scopes if `interop` doesn't unlock everything we need (may need
   granular `patient/ExplanationOfBenefit.read` etc).

### 2026-05-29 — first end-to-end attempt: `access_denied` at member login

First browser run of `/oauth/authorize/bsc` redirected to BSC PingFederate as
expected, but came back to
`https://airlock.allegedly.works/oauth/callback/bsc?error=access_denied&error_description=Authentication+failed.`

This is **not** an airlock bug — the OAuth wiring is fine (verified the
authorize redirect, state echo, and PingFederate's `/.well-known/openid-configuration`:
issuer, endpoints, supported scopes `openid interop PatientEOB PatientRead`, and
`code_challenge_methods_supported: ["plain","S256"]` all match what we send).
`error=access_denied` + `error_description=Authentication+failed.` is the
PingFederate canned response when the **member-login step** rejects the
username/password — distinct from `invalid_client` (bad client secret),
`invalid_scope` (scope not granted to the client), or `unauthorized_client`
(grant type not enabled).

Root cause: **the sandbox PingFederate has its own member directory, separate
from production blueshieldca.com.** Real BSC member credentials do not exist
on `dev-ext.blueshieldca.com`. The OAuth flow needs a **synthetic sandbox
test member**, which the dev portal does not self-service.

### Sandbox member credentials

The dev portal does **not** publish sandbox test member credentials anywhere
browsable. Surveyed 2026-05-29:

- `/bsc/fhir-sandbox/` — no
- `/bsc/fhir-sandbox/supportedProfiles` — no
- `/bsc/fhir-sandbox/subscribeapi` — describes the OAuth flow including
  "Member will login via BSC login screen and Provide the member username
  and password" but provides no actual credentials
- `/bsc/fhir-sandbox/product?title=Patient%20Access` and
  `/bsc/fhir-sandbox/product/989` (Cloud Patient Access) — no
- `/bsc/fhir-sandbox/releasenotes` — no
- `/bsc/fhir-sandbox/createapps` — no (just account/app setup)
- `/bsc/fhir-sandbox/interoperability` — no (CMS rule text only)
- `/bsc/fhir-sandbox/productionaccess` — no test creds, **but** this is
  where the support email surfaces: `interoperabilitysupp@blueshieldca.com`
- `/bsc/fhir-sandbox/sitemap` — no `/test-users` or equivalent route exists
- `/bsc/fhir-sandbox/contact` — no published credentials, but the contact
  form is the documented support path

**What `subscribeapi` does leak (incidentally):**

The page embeds sample, never-rotated request/response payloads from
someone else's working sandbox session. Decoding them yields the synthetic
test-member identifier pattern, even though the password is absent:

- Sample sandbox access_token JWT payload:
  ```json
  {
    "sub": "910019283user1",
    "title": "91001928300",
    "sn": "USER11LASTNAME",
    "givenName": "USER11FIRSTNAME",
    "client_id": "8b6bfeba916146c8a1a076ccfae28d11",
    "iss": "https://dev-ext.blueshieldca.com"
  }
  ```
- Sample sandbox id_token payload includes `patient: f3e4189b-0669-43ba-8372-0d405e318452`
  (the FHIR Patient resource ID for that synthetic member).
- Sample Basic-auth header decodes to client_id/secret
  `8b6bfeba916146c8a1a076ccfae28d11:e704d918f98d5876bad381c402ee43cc` —
  **not ours**, do not use; this belongs to whoever built BSC's sample doc.

So the sandbox member identifier scheme is clearly `<groupId>user<N>` (e.g.
`910019283user1`), with placeholder display names `USER<NN>FIRSTNAME` /
`USER<NN>LASTNAME` and corresponding FHIR Patient UUIDs. The login screen
still needs a password we don't have.

**Paths to obtain creds (try both):**

1. **Email** `interoperabilitysupp@blueshieldca.com` (BSC's interoperability
   support address, documented on `/productionaccess`). Subject something
   like "Sandbox synthetic-member credentials for app `ducktape-test`".
   Mention: developer-portal account, registered app `ducktape-test`,
   subscribed to `fhir-cloud-patient-access-v1:1.0.0`, redirect URI
   `https://airlock.allegedly.works/oauth/callback/bsc`, ask for
   test-member username/password so the OAuth member-login step works.
2. **Contact form** at `https://devportal-dev.blueshieldca.com/bsc/fhir-sandbox/contact`,
   request type "Technical issues" or "Make changes to existing Sandbox account".

Until those creds arrive, `/oauth/authorize/bsc` will keep returning
`access_denied` regardless of how many times it's retried.

### Authenticated portal survey

Verified once with a real dev-portal session (cookies copied from a
logged-in browser) that **logging in does not unlock any new docs page or
credentials section**. The integration guide is `/bsc/fhir-sandbox/subscribeapi`
and it renders byte-identical content authenticated vs anonymous.

What logging in _does_ unlock — none of it useful for the `access_denied`:

- Per-API pages `/bsc/fhir-sandbox/product/989/api/<id>` (one per FHIR resource:
  Patient, EOB, MetaData, DiagnosticReport, MedicationKnowledge, …) embed a
  Swagger 2.0 spec as base64 JSON inside `drupalSettings`. Just endpoint
  shapes — no auth examples, no test-member references.
- Internal API hostnames behind the public `api-dev.blueshieldca.com`:
  `esbndp-api2.bsc.bscal.com:443` and `esbnxg-api2.bsc.bscal.com` (ESB endpoints,
  not directly callable; the public `api-dev` host fronts both).
- Our registered app metadata is rendered inline in every product/api page
  as JSON: `title`, `redirectUri`, `credentials[].client_id`, and
  `subscribed: true`. Confirms the app is correctly subscribed to Cloud
  Patient Access and that the redirect URI matches what airlock sends. The
  authoritative `client_id` lives in
  <../../cluster/k8s/agents/airlock/bsc-client-credentials.sops.yaml>; the
  portal HTML value should match the SOPS file (if it doesn't, the deployed
  airlock is using a stale credential).

Search of the authenticated pages for `test patient`, `test member`,
`sample user/member/patient`, `user1`, `user2`, `910019283`, `login as`,
`use the following`, `sandbox user`, `PingFederate`, `test credentials`,
`demo user` returned zero hits. There is genuinely no public docs page,
authenticated or otherwise, that explains the member-login step.

The natural location for those creds would be `/bsc/fhir-sandbox/subscribeapi`,
right next to the existing line "Member will login via BSC login screen and
Provide the member username and password" — but that section just stops there.

## Production access path

Sandbox client_id is self-service (modulo the broken portal above). Production access
requires:

1. Public privacy policy URL on a stable domain
2. Third-party app attestation form (HIPAA-equivalent practices, data retention, breach
   notification, secondary use, opt-out mechanism)
3. Production redirect URI on a real TLS domain (e.g. `*.allegedly.works`)
4. Short security-architecture writeup

The CMS rule restricts payers to denying only for documented security reasons.
Wall-clock turnaround across payers is typically 2–12 weeks; no BSC-specific
datapoint yet.

## Flexpa: works technically, ruled out by price

Flexpa's May 2025 _State of the Payer Patient Access API Report_ explicitly
recognizes Blue Shield of California "for making all lines of business available
through their Patient Access API ahead of (and despite) the extended regulatory
requirements" — i.e. real patients have completed the OAuth flow through Flexpa
to BSC and pulled their data. So BSC's production FHIR endpoint **does** work in
practice; the bottleneck is purely the attestation gatekeeping.

But Flexpa's pricing rules them out for personal use (as of 2026-05):

- **Essential Network** $65,000/year (3 payers)
- **Complete Network** $130,000/year (400+ payers)
- **Omni Network** $350,000/year
- **No-Code Field Test** "from $10,000" (5 records, no engineering)
- All sales-gated, no free tier, no self-service signup, no per-member pricing.

Source: <https://www.flexpa.com/pricing>

Other aggregators (1upHealth, Particle Health, Zus Health) appear to be in the
same enterprise-only tier.

## Fasten Health: doesn't help

[fastenhealth/fasten-onprem](https://github.com/fastenhealth/fasten-onprem)
(GPL-3.0, self-hostable) is a personal health record but **only supports
provider/EHR FHIR endpoints, not payer Patient Access**. Their source list
(SOURCE_LIST.md in `cfu288/fasten-sources`) includes 9 BCBS plans — Vermont,
Kansas, Louisiana, Massachusetts, etc. — but **not Blue Shield of California**.
BSC is an independent California nonprofit, not a BCBSA member plan, so it
wouldn't be covered by future BCBSA support either.

## Apple Health Records: doesn't help

BSC is not in Apple's payer list at <https://institutions.healthrecords.apple.com/>
as of 2026-05. Apple's payer roster is small in general.

## Direct BSC path (fallback)

If Flexpa doesn't work or pricing is bad: skip the broken self-service sandbox
portal and submit BSC's **production attestation form directly** to their interop
team. The `devportal-dev.blueshieldca.com` host with the "Unauthorized" and `qa2`
404 bugs is the sandbox portal; production access goes through a different intake.
Concrete auth endpoints once registered:

- Authorization: `https://dev-ext.blueshieldca.com/as/authorization.oauth2`
- Token: `https://dev-ext.blueshieldca.com/as/token.oauth2/`
- Scope: `interop`
- Profiles: US Core + CARIN BB (PDEX)
- Auth: OAuth2 / SMART App Launch 1.0.0

## Gmail/PDF fallback

If both API paths fail: BSC emails EOB notifications, member portal exports PDFs.
Parse those. Lossy (no per-line CPT, allowed amount, NPI) but no gatekeepers and
covers ~90% of reconciliation needs. Already partially built elsewhere.

## Background research

- Aggregators surveyed: Flexpa ✓ (BSC validated), 1upHealth (enterprise pricing,
  payer coverage TBD), Particle Health, Zus Health. Plaid/MX don't cover health.
- Apple Health Records: BSC not in payer list (<https://institutions.healthrecords.apple.com/>)
  as of 2026-05.
- CARIN Alliance directory + CARIN BB profile (commercial-payer EOB) vs Blue
  Button 2.0 (Medicare-only).
- No public GitHub code, Reddit, or chat.fhir.org posts about BSC specifically —
  so you'd be alone if self-implementing, but Flexpa has already done it.
