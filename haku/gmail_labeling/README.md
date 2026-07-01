# haku/gmail_labeling

A FastMCP server that lets Haku autonomously manage Gmail labels confined to a
configurable namespace (`allowed_prefix`, default `haku/`). It talks to the Gmail
REST API directly (via `gmail_api`) and exposes **only** label management.

- **Contract:** `SPEC.md` — the closure invariant the server guarantees.
- **Deployment + one-time OAuth bootstrap:** `../../cluster/k8s/agents/gmail-labeling/README.md`.
- **Remaining work:** `../TODO.md`.

## Tools

All mutating tools reject any label name outside `allowed_prefix`.

| Tool            | Effect                                                                  |
| --------------- | ----------------------------------------------------------------------- |
| `list_labels`   | List managed labels (those under the prefix).                           |
| `modify_labels` | Add and/or remove managed labels across a batch of threads in one call. |
| `create_label`  | Create a managed label without applying it.                             |
| `rename_label`  | Rename a managed label (both names must be under the prefix).           |
| `delete_label`  | Delete a managed label (drops it from every thread).                    |

`modify_labels` takes `thread_ids` plus `add`/`remove` label-name lists, mirroring
Gmail's own `batchModify` request shape (a set of IDs, labels to add, labels to
remove). Gmail has no `threads.batchModify` endpoint, so the backend folds the
per-thread `threads.modify` calls into as few HTTP requests as possible via Gmail's
generic batch-request mechanism (`GmailLabelBackend.modify_threads`, `backend.py`)
instead of one round trip per thread.

Enforcement lives in `client.py` (`LabelClient`), not in the agent's prompt — the
namespace check runs before any Gmail call.

## Config

Environment variables, prefix `GMAIL_LABELING_`:

| Var                                                                                        | Default            | Meaning                                                                                                  |
| ------------------------------------------------------------------------------------------ | ------------------ | -------------------------------------------------------------------------------------------------------- |
| `GMAIL_TOKEN_DIR`                                                                          | required           | Dir with the Airlock-managed `gmail.modify` access token (`access_token` + `expires_at`).                |
| `ALLOWED_PREFIX`                                                                           | `haku/`            | Managed namespace; only labels under this prefix are touched.                                            |
| `STATIC_BEARER`                                                                            | unset              | Machine bearer for `/mcp` (Haku). Accepted alongside `AUTHENTIK__*` when both are set; sole gate if not. |
| `AUTHENTIK__OIDC_ISSUER` (+ `__OIDC_CLIENT_ID`/`__OIDC_CLIENT_SECRET`/`__PUBLIC_BASE_URL`) | unset              | If set, also gate `/mcp` with an Authentik OAuth flow for an interactive operator (e.g. claude.ai).      |
| `HOST` / `PORT`                                                                            | `0.0.0.0` / `8080` | Bind address.                                                                                            |

## Authentication

`/mcp` accepts **two credentials on one endpoint**, composed into a single FastMCP
`MultiAuth`:

- **Static bearer** (`STATIC_BEARER`) — Haku's machine path (`fastmcp call … --auth <token>`).
- **Authentik OAuth** (`AUTHENTIK__*`) — an interactive operator (agentydragon) attaching the
  server to claude.ai / Claude Code as an OAuth connector; the JWT is verified against
  Authentik's JWKS, and the Authentik application restricts consent to agentydragon. Provisioned
  in `tf/gitops/agent-machine-access`, mirroring `grocy-sf`/`manifold-mcp`.

OAuth authenticates the _caller_ to use the server; the server still talks to Gmail with its
own `gmail.modify` token (below) — not the operator's Google creds. With neither set, `/mcp` is
unauthenticated (local/dev only).

## Credentials

Layers (see `SPEC.md`):

- **Haku → this server:** a static bearer (the `tana-mcp-ro` pattern), so Haku's
  own Google token can stay `.readonly`. The operator may instead reach it via the
  Authentik OAuth flow above (same endpoint).
- **This server → Gmail:** a `gmail.modify` access token, **provisioned and rotated
  by Airlock** (the `gmail_modify` provider). Airlock holds the refresh token and writes
  an access-only secret; ESO mirrors it into the server's namespace and the server
  re-reads the rotating token via google-auth's `refresh_handler` (no restart). It owns
  no OAuth client or refresh token of its own. Deployment + bootstrap:
  <../../cluster/k8s/agents/gmail-labeling/README.md>.

## Run

```bash
bb run //haku/gmail_labeling:server_cli
# with GMAIL_LABELING_GMAIL_TOKEN_FILE (+ GMAIL_LABELING_STATIC_BEARER in-cluster) set
```
