# Draft email to BSC interoperability support — 2026-05-29

**To:** interoperabilitysupp@blueshieldca.com
**Subject:** Sandbox synthetic-member credentials for app `ducktape-test`

---

Hi,

I've completed sandbox onboarding for a personal third-party app and have
hit the only remaining blocker: I don't have credentials for a synthetic
sandbox member to authenticate as during the OAuth member-login step and
I can't find any documentation on how to obtain sandbox member credentials.
Could you please let me know where to find or provision them?

**App details:**

- Developer portal account: (the one used to register the app below)
- App name: `ducktape-test`
- Subscribed product: `fhir-cloud-patient-access-v1:1.0.0` (Cloud Patient Access)
- Client ID: (redacted in repo — kept in the sent email)
- Redirect URI: `https://airlock.allegedly.works/oauth/callback/bsc`

**What works:**

- Authorization endpoint `https://dev-ext.blueshieldca.com/as/authorization.oauth2`
  accepts my request with `response_type=code`, `client_id`, `redirect_uri`,
  `scope=openid interop PatientEOB PatientRead`, `code_challenge` (S256), and
  `aud=https://api-dev.blueshieldca.com/bsc/fhir-sandbox/fhir-server/api/v4/cloud/`,
  and redirects the browser to the PingFederate login screen.

**What fails:**

- The login screen rejects credentials and PingFederate redirects back to
  the registered redirect URI with
  `?error=access_denied&error_description=Authentication+failed.`
- Confirmed via `/.well-known/openid-configuration` that the OAuth client
  configuration is correct (scopes supported, PKCE S256 supported, endpoints
  match). The failure is at the member-authentication step, not the client.
- The dev portal documentation at `/bsc/fhir-sandbox/subscribeapi` describes
  the OAuth flow but stops at "Member will login via BSC login screen and
  Provide the member username and password" — without specifying which
  credentials to use in the sandbox. Real `blueshieldca.com` member
  credentials are (correctly) rejected by `dev-ext.blueshieldca.com` since
  the sandbox PingFederate is a separate user directory.

**Ask:**

Could you send me the username and password for a sandbox synthetic test
member I can authenticate as for `ducktape-test`? Even one test member
with a populated `ExplanationOfBenefit` / `Coverage` / `Patient` record set
is enough to validate the integration.

Thanks,
Rai (agentydragon@gmail.com)
