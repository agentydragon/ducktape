# coinbase-read

Primary home of the read-only Coinbase CDP API key, reflected into `haku-sandbox`
for the Haku balances source. Decoupled from any consuming service — it previously
lived under `openclaw/sandbox-secrets`, but openclaw is turned down, so a credential
Haku still needs must not ride on a suspended service's namespace.

## Credential

`coinbase-api-credentials` (SOPS, `api_key` + `api_secret`): a **CDP API key**
(`organizations/…/apiKeys/…` + a PEM EC P-256 private key) used with JWT/ES256
Bearer auth against the Advanced Trade API. **Read-only** — `GET
/api/v3/brokerage/key_permissions` returns `can_view=true, can_trade=false,
can_transfer=false`. Auth recipe and endpoints:
<../../../../haku/base/sources/coinbase.md>.

## Reflection

emberstack Reflector mirrors the Secret into `haku-sandbox` (one revocable
read-only credential shared with the sandbox — same pattern as `plaid-mcp-db-readonly`
and other reflected agent credentials). Adding `augur` later is a one-line annotation
change. Haku reads it from a `haku-sandbox` pod; egress to `api.coinbase.com` is
allowlisted in <../haku-egress-proxy/cnp-haku-cloud-api-egress.yaml>.

## Verification

```bash
kubectl -n coinbase-read get secret coinbase-api-credentials
kubectl -n haku-sandbox get secret coinbase-api-credentials
```
