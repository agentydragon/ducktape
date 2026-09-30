# The pairing page

`export_sessions_bin serve` runs the [sync](sync.md) loop and the page in one process for local use. In the cluster,
`web` serves the owner-authenticated page and `control` owns the sync loop and OAuth credential. The web Deployment
can have several replicas; all control actions are forwarded to the one control Pod, which keeps the rotating refresh
token and pairing attempt single-owned.

## What the page shows

Two mechanisms keep the database current, and the page reports each on its own ([sync.md](sync.md) has the design):

- **Live following** streams the events of the sessions in use as they happen; a session watch finds new sessions at
  once. It shows the sessions streaming, whether the watch is connected, and when an event last arrived.
- **Polling** lists every session each interval and reads the events of any that moved on. It works without live
  following and catches what that missed. It shows the state, when it last ran and what it read, when the next poll
  is due, and how many sessions are behind.

"Behind" leaves out the sessions with a live stream: a stream keeps its session current but never marks it level, so
counting it would show a healthy sync as lagging. Both "Poll now" and the interval start the same poll.

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

Login is the app's own Authentik OIDC flow (authorization code with PKCE), the code Plaid Link shares
(`util/oidc_login.py`), admitting the one Authentik `sub` in `SESSION_SYNC_OIDC_ALLOWED_SUBJECT`. Authentik's application policy should bind the provider
to that user as well; the check here holds if the binding is loosened. The browser keeps only a signed, `__Host-`
cookie with identity and expiry, and a write to the API from another origin is refused.

## Settings

Environment variables are prefixed `SESSION_SYNC_` (`settings.py`). `web` uses the database URL, OIDC fields,
`PUBLIC_BASE_URL`, and `CONTROL_BASE_URL`; `control` uses the database URL, `CREDENTIALS_FILE`, and sync settings.
`serve` combines both settings for local use.

| Variable                                              | Meaning                                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                        | The PostgreSQL the sync writes                                                        |
| `CREDENTIALS_FILE`                                    | Where the OAuth credential is kept (0600)                                             |
| `PUBLIC_BASE_URL`                                     | The page's origin, as Authentik redirects to it                                       |
| `CONTROL_BASE_URL`                                    | Private in-cluster control Service URL, used by web replicas                          |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The Authentik provider                                                                |
| `OIDC_SESSION_SECRET`, `OIDC_SESSION_SECONDS`         | Signing key and lifetime of the browser session                                       |
| `OIDC_ALLOWED_SUBJECT`                                | The one `sub` admitted                                                                |
| `HOST`, `PORT`                                        | Where it listens (default `0.0.0.0:8080`)                                             |
| `INTERVAL_SECONDS`, `WORKERS`                         | Between cycles (300); sessions read at once (3)                                       |
| `LIVE_STREAMS`, `LIVE_WINDOW_SECONDS`                 | Sessions streamed live at once (20; 0 is off); how recent a last event follows (1800) |

A cycle that fails is shown on the page and retried at the next interval; the process keeps running so the page
can still pair again. The page also shows whether live following is on, whether the session watch is connected, how many streams
are open, when an event last arrived over one, and any source that is failing with its reason ([sync.md](sync.md) § Live following).
