# Agent DAG: Python Integration (IPLD/IPFS)

Minimal client patterns for working with IPLD/IPFS heads and DAG nodes from Python.

## Dependencies

- `httpx` (or `requests`) for Kubo HTTP RPCs
- Optionally `multiformats` and `dag-cbor` to encode/decode IPLD locally
- `ipfshttpclient` is also fine if you prefer its API

## Agent role (read‑only)

- Resolve heads: IPNS `name/resolve` → Heads CID → `dag/get` (dag‑json) → pick branch → root CID
- Traverse history: repeatedly `dag/get` the node and follow `parent` links; stop at genesis
- Read nodes: Event/Generation/ToolResult/Resource are dag‑cbor nodes returned as dag‑json

## Host/state‑manager role (writes under approval)

- Ingest proposals: accept CAR bytes; `dag/import`; collect root CIDs from NDJSON; validate ancestry (`new_root` descends from `base`)
- Advance heads (CAS): read current Heads via IPNS; build a new Heads `{prev: old_heads_cid, branches[branch]=new_root}`; `dag/put` (dag‑cbor), `pin/add`, `name/publish` with the agent’s IPNS key
- Export checkpoints: `dag/export?arg=<cid>` to produce CAR bundles for backups/mirroring

## Typed models (suggested)

- Heads: `{version, agent_id, default_branch, branches: {name: Link}, updated_at, prev?: Link}`
- Event: `{kind, ts, parent?: Link, payload: Any, links?: {message?: Link, tool?: Link, resource?: Link}}`
- Checkpoint (optional): `{parents: [Link], author, ts, message, links: {run_root?: Link, policy_ref?: Link}}`
- Implement with Pydantic and `.model_dump()` to dag‑json before `dag/put`

## IPNS key management

- One key per agent for heads: `ipfs key gen agent-<id>-heads`
- Keep the private key on the host/state‑manager; publish via `name/publish`

## Streams and large artifacts

- Add via `/api/v0/add` (UnixFS); link returned root CID from Event/Generation nodes; no filtering/redaction

## Concurrency

- Treat head updates as CAS: resolve current Heads; require `prev == expected_old_heads_cid`; on conflict, rebase/merge and retry

## Sidecar option

- For a higher‑level SDK, use a tiny Node (ipfs-http-client) or Go (go‑ipfs‑api) sidecar exposing: `dag_put/get`, `car_import/export`, `pin_add`, `name_publish/resolve`, `advance_heads_cas` — then call from Python
