# Coinbase

Crypto balances and trade fills on the operator's Coinbase account. Coinbase is
not a Plaid institution, so this is the only read path for crypto holdings.
Reached directly from a `haku-sandbox` pod over the egress proxy (`api.coinbase.com`
is allowlisted — `cluster/k8s/agents/haku-egress-proxy/cnp-haku-cloud-api-egress.yaml`).

The credential is the reflected read-only `coinbase-api-credentials` secret (primary
in the `coinbase-read` namespace, mirrored into `haku-sandbox` by emberstack
Reflector):

```bash
KEY=$(kubectl -n haku-sandbox get secret coinbase-api-credentials -o jsonpath='{.data.api_key}'   | base64 -d)
SEC=$(kubectl -n haku-sandbox get secret coinbase-api-credentials -o jsonpath='{.data.api_secret}' | base64 -d)
```

It is a **CDP API key** — `api_key` is the resource name `organizations/…/apiKeys/…`
and `api_secret` is a PEM EC P-256 private key (no passphrase; not a legacy HMAC
key). Authenticate per request with a short-lived **JWT** signed ES256 with that
private key (matches `coinbase-advanced-py`'s `build_rest_jwt`):

```text
header : { "alg": "ES256", "kid": <api_key>, "nonce": <random hex> }   # nonce is required
payload: { "sub": <api_key>, "iss": "cdp", "nbf": <now>, "exp": <now+120>,
           "uri": "<METHOD> api.coinbase.com<path>" }                    # uri is path only, no query
header sent: Authorization: Bearer <jwt>
```

`iss` is the **literal string `"cdp"`**, not the api_key — getting this wrong (or
omitting `nonce`, or putting the query string in `uri`) returns `401 Unauthorized`
on every call. JWT signing needs a crypto env (`cryptography` + `pyjwt`), so run it
from a `haku-sandbox` pod (like the Plaid `psql` pod) or a `uv run --script`
one-liner — not a bare `curl`. Easiest is `coinbase-advanced-py`'s
`RESTClient(api_key, api_secret).get(<path>, params=…)`, which mints the JWT for you.
Endpoints (all on `api.coinbase.com`):

- **`GET /v2/accounts` — balances; use this one.** One entry per **wallet**
  (`balance.amount` + `currency` + `name`), **including staked assets** — a separate
  `Staked ETH`-style wallet shows here. Paginate with `?limit=100`.
- `GET /api/v3/brokerage/orders/historical/fills` — trade-execution history.
- `GET /api/v3/brokerage/key_permissions` — cheap sanity check; returns `can_view=true, can_trade=false, can_transfer=false` (the key is read-only).
- `GET /v2/prices/<from>-<to>/spot` — **public, no auth** — spot price for USD marking.

**Sharp edge — for balances use `/v2/accounts`, NOT `/api/v3/brokerage/accounts`.** The v3
brokerage endpoint (`get_accounts`) returns only _tradeable_ balances and **silently omits
staked assets** — staked ETH sits in a separate `Staked ETH` wallet that v3 never returns, so a
v3-only read can undercount the account by a wide margin (caught against the operator's app,
Jul 11). `/v2/accounts` lists every wallet, staked included. Keep v3 only for `fills` and
`key_permissions`.

**Transaction history is a v2 read too** (verified 2026-07-12, 379 rows, zero permission
errors): `GET /v2/accounts/{id}/transactions`, paginated via `pagination.next_uri` — full
per-wallet history (buys, sells, sends, receives, `staking_reward`, internal
`staking_transfer` pairs that net to zero). The dead ends are all **v3**: top-level
`/api/v3/brokerage/transactions` → `401` and the v3 per-account
`/accounts/{uuid}/transactions` → `404`; v3 trade history is `fills` (readable but empty on
a portfolio with no Advanced Trade orders). `native_amount` is Coinbase's USD mark at
transaction time — good for approximate cost basis, not tax-grade lots; for assets
transferred in, receipt-FMV is a proxy and the true basis lives wherever they were
originally bought. A `401` means the JWT is malformed (`iss`/`nonce`/`uri`), not that the
key rotated — re-check those first.

What to _do_ with the balances (mark to USD, surface large moves / new holdings) →
the finance pass in your procedures (`procedures/finance.md`, in your state).
