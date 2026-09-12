# Repository indexes

Two independent single-index workers serve Ducktape `devel` and Haku state `main`:

| Service in `agentplane-index` | Database     | Remote                                                 |
| ----------------------------- | ------------ | ------------------------------------------------------ |
| `ducktape:8080`               | `ducktape`   | `https://github.com/agentydragon/ducktape.git`         |
| `haku-state:8080`             | `haku_state` | `http://forgejo-http.forgejo:3000/haku/haku-state.git` |

Each worker keeps a bare clone in an emptyDir and fetches it on every poll, so a commit
costs one incremental fetch and a pod restart costs one clone. The Ducktape worker ignores
`props/specimens/` (a copy of code indexed at its real path) and `*.gz`; the indexer
accepts only strict UTF-8 content without NULs. Haku state is private, so its worker reads
`haku-forgejo-git`, the read credential the `haku-state` Terraform reconciliation reflects
into this namespace. Ducktape is public and its worker holds no credential.

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
when callers need different access. The workers need no Kubernetes API access and mount no
ServiceAccount token. ImagePolicy `flux-system/agentplane-index` advances both workers to
published devel images.

See the [service contract](../../../../x/agentplane/indexing/SPEC.md) and
[database configuration](db/README.md). Index state is rebuildable; full initial ingestion is
asynchronous, and mixed-revision search during updates is reported in the response.
