# Testing operator federation: Dex claim contract

## Observed boundary

The deployed MCP acceptance cases could log in through Dex but failed operator BFF
requests with `operator_federation_token_invalid`. On 2026-09-15, testing used Dex
2.45.1, direct federation, and app image
`devel-20260915090623-1364769`.

A read-only aggregate over the app's `operator_browser_session` rows decoded token
claims inside PostgreSQL. No token, cookie or subject was returned:

```json
{
  "sessions": 8,
  "missing_azp": 8,
  "matching_issuer": 8,
  "matching_subject": 8,
  "expected_single_audience": 8,
  "has_required_times": 8
}
```

The comparisons were against the stored login issuer/subject and the configured
testing audience. This checks claim presence/equality, not token signatures or
whether old stored tokens remain unexpired. Every sampled token lacks `azp`; the
configured Authentik verifier requires it, so these tokens cannot pass that verifier.
Dex's pinned source confirms this is its single-audience token shape, not a
transient signing-key fetch failure. The maintained verification and deployment
contract is in [operator federation](../docs/operator_federation.md).

The deployed direct-federation code also reconstructed the token response without
the required `token_type`. [#7057](https://github.com/agentydragon/ducktape/pull/7057)
has landed the independent envelope correction. Its signed-token BFF regression failed
before the change in [3a06cbc8](https://app.buildbuddy.io/invocation/3a06cbc8-fa13-48ed-8421-136a813eb779).

## Candidate and remaining proof

Select a concrete Dex verifier explicitly for testing; preserve the stricter
Authentik contract for staging, Haku and external MCP enrollment. Both reuse the
same pinned-key, signature, audience, subject and lifetime checks. Code lands before
the testing ConfigMap activates the new profile.

The signed-token security suite, both profiles through the actual Action Service
operator API, and the changed libraries' type/lint checks passed in
[45479ab9](https://app.buildbuddy.io/invocation/45479ab9-ec23-43d7-ab14-cf3350a93466).
The app's direct Dex path accepts the signed token only when both login and target
profiles explicitly select Dex; leaving either Authentik refuses it. This and the
unchanged Haku Authentik adapter passed in
[ece19014](https://app.buildbuddy.io/invocation/ece19014-6779-464d-b9a2-4cfd17173107).
This is not deployed acceptance: after the Dex candidate and both service images land,
activate the testing profile, run the real MCP/operator cases from devbox, and
verify teardown. Do not resend the user's failed staging input.

## Testing activation and acceptance, 2026-09-15

[#7066](https://github.com/agentydragon/ducktape/pull/7066) activated all three Dex
profile fields. Flux applied merge `76fa24ce` and reported Healthy at 23:27:29Z.
The reloaded app (`devel-20260915230405-dd0439f`) and Action Service
(`devel-20260915225846-3299654`) were Ready; both images contain #7059.

The controlled-devbox [live MCP run](https://app.buildbuddy.io/invocation/3277d91f-c663-4b46-9c52-39fe2fa55d18)
passed real MCP Action execution on Claude and Codex, but all eight operator cases
stopped at the fixture's post-login probe. A [focused diagnostic](https://app.buildbuddy.io/invocation/4583177a-fa32-4a11-9d1a-f38e89eff687)
proved that the response was the correct `404` with structured `UpstreamFailure`
metadata: `GET`, `HTTPStatusError`, upstream status `404`, and the exact fresh
`/v1/operator/action-requests/<probe-id>` path. The fixture still expected the
legacy detail string replaced by #6129. No credential material was printed.

Both runs used the devbox's existing Haku-mediated kubeconfig. The complete suite
returned to its zero-Sandbox/zero-policy-object baseline without manual cleanup.
Operator scenarios still need rerunning with the corrected preflight; these results
do not establish OAuth linkage, BFF decisions, or argument-schema policy acceptance.
