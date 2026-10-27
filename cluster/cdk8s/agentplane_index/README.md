# Agentplane repository index

**Parked (2026-10-20):** not deployed; its namespace and database were deleted. Revive by
calling `workers.agentplane_index` from `generate_manifests.py` again (Kustomization, artifact,
`namespace_dependencies`) and restoring the `agentplane-index` entries in `namespace_access.py`,
`loki_read_proxy.py`, the `kubernetes-forgejo-images-secret-store` namespaces
(`external_secrets/config.py`) and the `haku-forgejo-git` mirror (`haku/workspaces.py`).

`workers.py` deploys one single-repository index worker
([service contract](../../../agentplane/indexing/SPEC.md)) per indexed branch into namespace
`agentplane-index`:

| Service           | Database     | Remote                                                 | Branch  |
| ----------------- | ------------ | ------------------------------------------------------ | ------- |
| `ducktape:8080`   | `ducktape`   | `https://github.com/agentydragon/ducktape.git`         | `devel` |
| `haku-state:8080` | `haku_state` | `http://forgejo-http.forgejo:3000/haku/haku-state.git` | `main`  |

Each worker keeps a bare clone in an emptyDir and fetches it on every poll, so a commit
costs one incremental fetch and a pod restart costs one clone. Ducktape is public and its
worker holds no Git credential. Haku state is private, so its worker reads
`haku-forgejo-git`, the `haku-state` Terraform credential that Reflector mirrors here from
`haku-sandbox`.

Both embed with Ollama's internal `/v1/embeddings` endpoint and `qwen3-embedding:4b` (2560
dimensions); the query instruction matches Qwen's query/document asymmetry. Workers and
PostgreSQL run in OVH and Ollama on the GPU node, so embedding and search depend on that
node.

ESO generates the read token and distributes the image-pull credential. ImagePolicy
`flux-system/agentplane-index` advances both workers. They need no Kubernetes API access and
mount no ServiceAccount token.

## Access

`/status` and `/search` require the bearer in `agentplane-index-read-token:token`, one token
for both repositories. There is no public route; from an operator session, port-forward and
send the bearer in the `Authorization` header:

```bash
kubectl -n agentplane-index port-forward service/ducktape 18080:8080  # or service/haku-state
```

`/healthz` is unauthenticated and checks only database access; judge ingestion by
authenticated `/status` and a real search. Initial ingestion is asynchronous, and a search
during an update reports mixed revisions in its response.

## Database

`agentplane-index-db` follows the OVH-HA CNPG profile: two instances on separate OVH nodes on
`piraeus-ssd-local`. Both workers connect as the `indexer` owner with the CNPG-generated
`agentplane-index-db-app` credential, but to separate databases, so they share no cached
embeddings or tables. The shared owner is no access boundary between the indexes. Each worker
gets a standard `postgresql://` URI assembled from the shared credential and its database
name: the generated CNPG URI targets the bootstrap `ducktape` database, while the Haku worker
must connect to `haku_state`. The service selects `asyncpg` for SQLAlchemy itself.

CNPG installs `vector`, which the Postgres image ships, before the workers start: pgvector
needs a superuser, while the workers only own their databases and create their own schema.
The Flux Kustomization's `Database` health expression requires the extension applied.
Removing a `Database` resource retains its database; that does not protect the Cluster or
its PVCs.

No backup is configured: the index rebuilds from the Git remotes and Ollama.
