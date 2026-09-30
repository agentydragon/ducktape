# The pairing page

`export_sessions_bin serve` runs the [sync](sync.md) loop and a page in one process. The page shows how the sync is
doing, pairs it with a Claude account, and starts a cycle on demand. One process does both because the credential
has one owner: every refresh rotates the refresh token.

## Pairing

Claude Code's public OAuth client registers only `http://localhost:54545/callback` as its redirect. A browser sent
there from anywhere but the machine running `pair` gets a page that fails to load, which is what the page relies on:

1. **Start pairing** returns Claude's authorize URL; the server keeps the PKCE verifier and `state` in memory.
2. Approve in a browser signed in to the account. The browser is sent to the loopback address and shows an error.
3. Copy the whole address from the address bar into the page and finish. The server checks `state`, exchanges the
   code, saves the credential and switches the sync loop to it, with no restart.

A new attempt replaces one still waiting, and any attempt is spent once a URL has been pasted, right or wrong: a code
is single-use. A server restart between the steps also spends it. The pasted URL carries the code, so it is never
echoed back in an error.

## Who may use it

Login is the app's own Authentik OIDC flow (authorization code with PKCE), the same as Plaid Link's, admitting the
one Authentik `sub` in `SESSION_SYNC_OIDC_ALLOWED_SUBJECT`. Authentik's application policy should bind the provider
to that user as well; the check here holds if the binding is loosened. The browser keeps only a signed, `__Host-`
cookie with identity and expiry, and a write to the API from another origin is refused.

## Settings

Environment variables, all prefixed `SESSION_SYNC_` (`settings.py`):

| Variable                                              | Meaning                                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                        | The PostgreSQL the sync writes                                                        |
| `CREDENTIALS_FILE`                                    | Where the OAuth credential is kept (0600)                                             |
| `PUBLIC_BASE_URL`                                     | The page's origin, as Authentik redirects to it                                       |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The Authentik provider                                                                |
| `OIDC_SESSION_SECRET`, `OIDC_SESSION_SECONDS`         | Signing key and lifetime of the browser session                                       |
| `OIDC_ALLOWED_SUBJECT`                                | The one `sub` admitted                                                                |
| `HOST`, `PORT`                                        | Where it listens (default `0.0.0.0:8080`)                                             |
| `INTERVAL_SECONDS`, `WORKERS`                         | Between cycles (300); sessions read at once (3)                                       |
| `LIVE_STREAMS`, `LIVE_WINDOW_SECONDS`                 | Sessions streamed live at once (20; 0 is off); how recent a last event follows (1800) |

A cycle that fails is shown on the page and retried at the next interval; the process keeps running so the page
can still pair again. The page also shows whether live following is on, how many streams are open, and when a frame
last arrived ([sync.md](sync.md) § Live following).
