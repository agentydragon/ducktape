# haku/gmail_labeling

A FastMCP server that lets Haku autonomously manage Gmail labels confined to a
configurable namespace (`allowed_prefix`, default `haku/`). It talks to the Gmail
REST API directly (via `gmail_api`) and exposes **only** label management.

- **Contract:** `SPEC.md` — the closure invariant the server guarantees.
- **Deployment + one-time OAuth bootstrap:** `../../cluster/k8s/agents/gmail-labeling/README.md`.
- **Remaining work:** `../TODO.md`.

## Tools

All mutating tools reject any label name outside `allowed_prefix`.

| Tool           | Effect                                                         |
| -------------- | -------------------------------------------------------------- |
| `list_labels`  | List managed labels (those under the prefix).                  |
| `apply_label`  | Add a managed label to a thread (creates the label if needed). |
| `remove_label` | Remove a managed label from a thread.                          |
| `create_label` | Create a managed label without applying it.                    |
| `rename_label` | Rename a managed label (both names must be under the prefix).  |
| `delete_label` | Delete a managed label (drops it from every thread).           |

Enforcement lives in `client.py` (`LabelClient`), not in the agent's prompt — the
namespace check runs before any Gmail call.

## Config

Environment variables, prefix `GMAIL_LABELING_`:

| Var               | Default            | Meaning                                                                                   |
| ----------------- | ------------------ | ----------------------------------------------------------------------------------------- |
| `GMAIL_TOKEN_DIR` | required           | Dir with the Airlock-managed `gmail.modify` access token (`access_token` + `expires_at`). |
| `ALLOWED_PREFIX`  | `haku/`            | Managed namespace; only labels under this prefix are touched.                             |
| `STATIC_BEARER`   | unset              | If set, require `Authorization: Bearer <token>` on `/mcp`.                                |
| `HOST` / `PORT`   | `0.0.0.0` / `8080` | Bind address.                                                                             |

## Credentials

Two layers (see `SPEC.md`):

- **Haku → this server:** a static bearer (the `tana-mcp-ro` pattern), so Haku's
  own Google token can stay `.readonly`.
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
