# coinbase-read

Namespace-local ESO copy of the read-only Coinbase CDP API key for the Haku
balances source. The canonical SOPS Secret lives in
`cluster/k8s/external-creds`; separate ExternalSecrets reconcile copies into
`coinbase-read` and `haku-sandbox`. Keeping a destination here decouples the
credential from any consuming service namespace.

## Credential

`coinbase-read-api-credentials` (ESO copy, `api_key` + `api_secret`): a **CDP API key**
(`organizations/…/apiKeys/…` + a PEM EC P-256 private key) used with JWT/ES256
Bearer auth against the Advanced Trade API. **Read-only** — `GET
/api/v3/brokerage/key_permissions` returns `can_view=true, can_trade=false,
can_transfer=false`. Auth recipe and endpoints:
<../../../../haku/base/sources/coinbase.md>.

## Distribution

ESO reads the canonical Secret from `ducktape-flux` and reconciles a separate Secret
into `haku-sandbox` as `haku-sandbox-coinbase-api-credentials`. Each destination
is an independent ExternalSecret; adding a
consumer requires an explicit source-side grant in
`cluster/cdk8s/external_creds.py`. Haku reads it from a `haku-sandbox` pod;
egress to `api.coinbase.com` is allowlisted in
<../../../cdk8s/egress_fences.py>.

## Verification

```bash
kubectl -n coinbase-read get secret coinbase-read-api-credentials
kubectl -n haku-sandbox get secret haku-sandbox-coinbase-api-credentials
```
