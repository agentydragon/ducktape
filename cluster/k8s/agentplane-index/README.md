# Repository indexes

Two independent single-index workers serve Ducktape `devel` and Haku state `main`:

| Service in `agentplane-index` | Database     | Flux source in `flux-system`  |
| ----------------------------- | ------------ | ----------------------------- |
| `ducktape:8080`               | `ducktape`   | `agentplane-index-ducktape`   |
| `haku-state:8080`             | `haku_state` | `agentplane-index-haku-state` |

Each source includes the full repository tree except `.git/`, overriding Flux's default
deployment-oriented exclusions. The indexer accepts only strict UTF-8 content without NULs.
Haku state's source reuses `flux-system/haku-forgejo-git`, maintained by the existing
`haku-state` Terraform reconciliation; indexer pods receive no Git credential.

Both use Ollama's internal `/v1/embeddings` endpoint and `qwen3-embedding:4b` (2560 dimensions).
The query instruction matches Qwen's query/document asymmetry. Workers and PostgreSQL run
in OVH; Ollama runs on the GPU node, so embedding and query requests depend on that node.
Each database has its own content/embedding cache. CNPG owns database creation and pgvector
installation; the app Flux dependency waits for both Database resources to report the
extension applied.

The services expose `/status` and `/search` internally and require the bearer in
`agentplane-index-read-token:token`. They have no public route. For an operator session, use
`kubectl -n agentplane-index port-forward service/ducktape 18080:8080` (or `service/haku-state`)
and send the bearer in the Authorization header. `/healthz` is unauthenticated and checks
database access; use authenticated status and a real search to assess ingestion.

Read-token and image-pull credentials are declaratively generated/distributed by ESO. The
shared read token grants access to both repositories; separate authorization can be added
when callers need different access. Source reads are scoped independently by ServiceAccount
to `get` one GitRepository each. ImagePolicy `flux-system/agentplane-index` advances both
workers to published devel images.

See the [service contract](../../../../x/agentplane/indexing/SPEC.md) and
[database configuration](db/README.md). Index state is rebuildable; full initial ingestion is
asynchronous, and mixed-revision search during updates is reported in the response.
