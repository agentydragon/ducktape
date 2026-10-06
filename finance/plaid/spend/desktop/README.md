# Plaid Spend clients

This directory contains the per-user clients for `https://plaid-spend.allegedly.works`:

- `plaid-spend-daemon` owns the Authentik login, bearer token, SSE connection and session-bus API.
- `plaid-spend` prints the current card view and flexible allowance (when configured), reports the daemon status, or starts sign-in.
- The GNOME Shell extension renders the current allowance or statement-cycle spend and opens the login flow.

The CLI, extension and daemon are installed together by the `ducktape.plaidSpend` Home Manager module. The daemon starts for the graphical session on opted-in hosts. The CLI works from a terminal in that user session without opening the panel extension.

```sh
plaid-spend             # show flexible allowance and statement-cycle spend
plaid-spend --json      # print the API view as JSON
plaid-spend status      # show daemon connection and sign-in state
plaid-spend login       # start Authentik sign-in in the browser
```

From the repository, run the same CLI with:

```sh
bazelisk run //finance/plaid/spend/desktop:plaid_spend_cli
```

The CLI reads the same view and status over the session D-Bus interface as the panel extension. OAuth tokens remain owned by the daemon and stored in Secret Service.

## Wire contract

The client calls `GET /api/v1/view` and `GET /api/v1/events` on the configured HTTPS API base. Both use `Authorization: Bearer <access-token>`. The event endpoint must use `text/event-stream`; its initial snapshot and subsequent notifications are full JSON views under the `view` event name. Each view is expected to be an object with `generated_at` and `cards` fields. A card uses the server-computed fields `label`, `account_name`, `institution_name`, `mask`, `currency`, `cycle_start`, `spend_minor_units`, `posted_minor_units`, `pending_minor_units`, `limit_minor_units`, `alert_threshold_percent`, `spend_percent`, `alert_state`, `last_synced_at`, and `statement_available`.

When present, the `allowance` object supplies the available balance, monthly credit, current-cycle spend, pending spend, pace alert, projected cycle-end balance, next credit and sync time; unavailable allowance states are shown without inventing a balance. Money fields use the currency's minor unit. `spend_percent` and `alert_state` are rendered as supplied; the desktop client does not calculate alerts or spend. Null spend and cycle fields render as unavailable.

## Login and credentials

OIDC endpoints are discovered from the configured Authentik issuer. Login uses authorization code + PKCE (S256), `state`, a fixed callback bound only to `127.0.0.1:43821`, and the public client ID `plaid-spend-desktop`. The client requests `openid offline_access` so it can renew the API token after startup. Only the refresh token is persisted, as an item in the user's Secret Service keyring; access tokens remain in daemon memory. The client never reads or writes Plaid credentials.

The D-Bus interface is `works.allegedly.PlaidSpend1` at `/works/allegedly/PlaidSpend` on `works.allegedly.PlaidSpend`. `GetView()` returns the latest server JSON view. `ViewChanged(JSON)` announces a new full view. Read-only `Status` and `LastError` properties communicate local connection/login state, and `Login()` starts the browser flow.

## Deployment assumptions

The backend must accept an Authentik access token issued for the configured OIDC client/audience and provide the endpoints and event shape above. The default issuer slug and client ID are both `plaid-spend-desktop`; both can be overridden in Nix if the Authentik provider is registered with different values. The OAuth provider needs the exact callback URL `http://127.0.0.1:43821/callback` and permission to issue refresh tokens for `offline_access`.
