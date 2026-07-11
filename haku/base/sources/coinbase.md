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
  (`balance.amount` + `currency` + `name`), **including staked assets** — the operator's
  `Staked ETH` wallet appears here. Paginate with `?limit=100`.
- `GET /api/v3/brokerage/orders/historical/fills` — trade-execution history.
- `GET /api/v3/brokerage/key_permissions` — cheap sanity check; returns `can_view=true, can_trade=false, can_transfer=false` (the key is read-only).
- `GET /v2/prices/<from>-<to>/spot` — **public, no auth** — spot price for USD marking.

**Sharp edge — for balances use `/v2/accounts`, NOT `/api/v3/brokerage/accounts`.** The v3
brokerage endpoint (`get_accounts`) returns only *tradeable* balances and **silently omits
staked assets**: it reported 5.37 liquid ETH but hid a 38-ETH `Staked ETH` wallet (~$68k, a
~40% undercount of the whole account) until it was caught against the operator's app (Jul 11).
`/v2/accounts` lists every wallet, staked included. Keep v3 only for `fills` and
`key_permissions`.

Gotcha: there is **no** generic transactions list endpoint — top-level
`/api/v3/brokerage/transactions` → `401` and per-account
`/accounts/{uuid}/transactions` → `404`; trade history is `fills`. A `401` means
the JWT is malformed (`iss`/`nonce`/`uri`), not that the key rotated — re-check
those first.

What to _do_ with the balances (mark to USD, surface large moves / new holdings) →
the finance pass in your procedures (`procedures/finance.md`, in your state).
