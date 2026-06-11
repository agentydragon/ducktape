# Agent DAG with IPLD/IPFS (Overview)

Use a content‑addressed DAG (dag‑cbor) as the canonical agent state, publish mutable heads via IPNS, and keep the agent container read‑only. Optionally interlink Git commits and IPLD CIDs.

## Goals

- Canonical, content‑addressed event/state graph (no redaction or size caps).
- Mutable heads via IPNS; append‑only Heads history for audit.
- Proposals as CAR bundles, approved under host‑side policy into head advances.
- Agent containers read‑only; host/state‑manager performs writes (ingest/pin/publish).

## Agent Metadata in IPLD (Heads and Config)

Make heads and agent config first‑class IPLD nodes so metadata “lives in IPFS/IPLD” and participates in history.

Schemas (illustrative, dag‑cbor)

- Heads
  - `{ version: 1, agent_id: String, default_branch: String, branches: { String: Link }, updated_at: String, prev?: Link, signatures?: [Bytes] }`
  - `branches` maps branch names → root CIDs. `prev` links to the prior Heads node, forming an append‑only chain.
- AgentConfig
  - `{ version: 1, agent_id: String, name: String, description?: String, config: Any, valid_from: String, valid_to?: String, prev?: Link }`
  - Store mutable agent config as a chain; the active config is referenced from Heads or Checkpoints.

Resolution

- Publish an IPNS record whose value is the current Heads CID (one key per agent). Readers resolve IPNS → Heads → branch head CIDs.

Updates (CAS)

- Construct a new `Heads{ branches: {...}, prev: <old_heads> }`, pin it, publish via the agent IPNS key.
- Treat as compare‑and‑set by verifying `prev == expected_old_heads_cid` to catch concurrent writers; resolve via a merge Heads when needed.

Container Integration (RO)

- Agent containers read via IPNS or a mounted gateway/FUSE; no write path from containers.
- Host/state‑manager ingests CAR proposals, validates ancestry/invariants, pins blocks, and updates IPNS under an approval policy.

## Proposals in IPLD (Graph PRs)

- Proposal payload: a CAR bundle containing new/updated DAG nodes and a new root CID.
- Manifest: `{ proposal_id, base: <root_cid>, new: <root_cid>, author, ts, summary }`.
- Validation: ancestry (new descends from base), schema checks, typed invariants (timestamps monotonic, no illegal rewrites).
- Approval: pin CAR; create a new Heads node with `branches[branch] = new` and `prev = old_heads`; publish IPNS. Deny: discard.

## Optional: Hybrid Git ↔ IPLD Interlinking

Keep Git for policy/templates PRs and interlink with IPLD for reproducibility.

Interlink patterns

- Commit → CID: on meaningful Git commits (run boundary, summary, policy activation), create a top‑level `CommitMirror` IPLD node: `{repo, commit_sha, tree_sha, author, ts, links: {events_root, summaries_root, resources_root, policy_ref}}`; export CAR, pin, and record CID via commit trailer, note, or annotated tag.
- CID → Commit: include `{git_commit, git_repo}` in `CommitMirror`; optionally maintain a Git mirror `meta/ipld-heads.json` for branch → CID maps (reviewable), with IPNS remaining the source of truth.
