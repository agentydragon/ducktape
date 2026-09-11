# Index databases

`agentplane-index-db` follows the OVH-HA CNPG profile: two instances on separate
OVH nodes with `local-path-ovh-ssd`. Both indexes share the `indexer` owner and
CNPG-generated `agentplane-index-db-app` credential, but use separate databases,
`ducktape` and `haku_state`. They do not share cached embeddings or application
tables. The shared owner is not an access boundary between the indexes.

CNPG installs `vector` before the application starts: pgvector requires a
superuser, whereas the application only owns its databases and creates its own
schema. The image includes pgvector. Database resources retain their databases
if removed; this does not protect against deleting the underlying Cluster/PVCs.

Index data can be rebuilt from Flux artifacts and Ollama; no backup is configured.
The `20Gi` PVC request is not a hard capacity limit with local-path storage.
