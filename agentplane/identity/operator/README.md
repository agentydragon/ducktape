# Operator OIDC verification

This package verifies operator access tokens against reviewed issuer, audience, JWKS endpoint and token-profile
settings. It returns only the verified issuer and subject; each caller applies its own operator authorization.
The shared Python schema for those settings is in `settings.py`.

Operator OIDC is separate from workload ServiceAccount or SVID authentication and from delegated Identity
authority. `MatchesUnverifiedIssuer` can select which verifier tries a token, but only `Verify` authenticates it.
