# haku/gmail_labeling

A FastMCP server that lets Haku autonomously manage Gmail labels confined to a
configurable namespace (`allowed_prefix`, default `haku/`). It talks to the Gmail
REST API directly (via `gmail_api`) and exposes **only** label management.

- **Contract:** `SPEC.md` — the closure invariant the server guarantees.
- **Design & rationale, deployment open questions:** `PLAN.md`.

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

| Var                | Default            | Meaning                                                       |
| ------------------ | ------------------ | ------------------------------------------------------------- |
| `GMAIL_TOKEN_FILE` | required           | Path to the `gmail.modify` authorized-user token JSON.        |
| `ALLOWED_PREFIX`   | `haku/`            | Managed namespace; only labels under this prefix are touched. |
| `STATIC_BEARER`    | unset              | If set, require `Authorization: Bearer <token>` on `/mcp`.    |
| `HOST` / `PORT`    | `0.0.0.0` / `8080` | Bind address.                                                 |

## Credentials

Two layers (see `SPEC.md`):

- **Haku → this server:** a static bearer (the `tana-mcp-ro` pattern), so Haku's
  own Google token can stay `.readonly`.
- **This server → Gmail:** a `gmail.modify` OAuth token, **provisioned and rotated
  by Airlock** (which already manages Google OAuth). The server just reads the
  resulting token file/secret; it owns no OAuth client of its own.

## Run

```bash
bb run //haku/gmail_labeling:server_cli
# with GMAIL_LABELING_GMAIL_TOKEN_FILE (+ GMAIL_LABELING_STATIC_BEARER in-cluster) set
```
