# haku index

Semantic search over the things a Haku runtime should be able to recall, for runtimes that have
no OpenClaw-style workspace index — and no checkout at all, in the case of the console.

A **logical index** is the durable occurrence and future authorization boundary. Each configured
index has an `index_id` and an `index_type`; it never obtains identity from a Python default or a
conventional name. `git` is currently the only index type — a storage and provenance shape rather
than a permission or query scope:

| index type | source shape                                     | a hit points at         |
| ---------- | ------------------------------------------------ | ----------------------- |
| `git`      | files at a branch tip of a configured Git remote | a path and a byte range |

When enabled, the deployment registry in `cluster/cdk8s/haku/console_config.py` declares Git
indexes such as `haku-state` over Haku's Forgejo remote and `ducktape-public` over the public
Ducktape `devel` branch. Adding another index is a reviewed configuration change; it is not an
unscoped runtime default.

The index is derived state: it can be thrown away and rebuilt from git at any time.

## Source materialization and embedding are separate stages

A Git sweep owns its source-specific work: it chunks changed source material, writes the
occurrence rows that preserve provenance, and inserts the normalized strings into global
`contents`. It never waits for an embedding endpoint. `contents` is consequently the durable,
content-addressed queue shared by every source and logical index.

One independent embedding maintenance loop then selects content that lacks a vector for the active
`model_key`, sends bounded batches to the configured provider, and writes
`content_embeddings`. Search joins source occurrences through that model-specific table, so a
materialized chunk becomes searchable only after its vector arrives. A model change does not
re-fetch or re-chunk Git sources: the worker drains the same global content queue for the new
model. Status distinguishes current source chunks, embedded chunks, and pending chunks so a
partial result cannot be mistaken for an up-to-date empty corpus.

## Design

The index has globally-addressed semantic content plus index-type-specific occurrences:

| table                | keyed by                                        | holds                                                       |
| -------------------- | ----------------------------------------------- | ----------------------------------------------------------- |
| `indexes`            | `index_id`                                      | one boundary plus its `index_type` (`git`)                  |
| `contents`           | `content_sha`                                   | the exact normalized string sent to an embedder             |
| `content_embeddings` | `(content_sha, model_key)`                      | that content's vector in one model's vector space           |
| `git_chunks`         | `(index_id, blob_sha, chunker_key, byte_start)` | a Git blob span and its referenced global content           |
| `git_tip`            | `(index_id, path)`                              | the tree at the indexed commit, replaced wholesale per sync |
| `git_sync_state`     | `index_id`                                      | what the branch points at, and which commit `git_tip` holds |

### Logical indexes bound occurrences

`contents` and `content_embeddings` are global deduplication layers, but they are not a recall
authority. Every Git occurrence belongs to one durable `index_id`; `indexes` names that
boundary and carries its `index_type`. An enabled deployment can register `haku-state` and
`ducktape-public`. A second Git index may reuse an identical content vector, but its tip, revision
state, and matches remain a separate set of occurrences.

An index is its upstream collection: a Git index's configured remote and branch are part of that
index's type-specific deployment configuration, not a second durable database identity. Future
Flux artifact ingestion adds an index type/configuration shape; it does not add a generic source
layer. Reader grants and RLS bind callers to `index_id`, never to the global content cache.

`contents.content_sha` is the SHA-256 of the exact UTF-8 encoding of `contents.content`.
It has one namespace across index types: the same rendered content appearing in either configured
index, or again in another revision, is the same content row. `content_embeddings` then adds
the model identity, because the same content can legitimately have vectors in more than one
vector space. These are durable semantic-index tables, not an evictable cache.

The occurrence rows remain intentionally source-specific. A Git occurrence says which bytes of a
blob yielded one content value, keeping citations and source lifecycle local while semantic
materialization is shared. `byte_start` distinguishes adjacent Git chunks.

**Chunk size and overlap are configurable, and they live inside `chunker_key`** — canonical JSON,
`{"max_bytes":3000,"overlap_codepoints":128,"target_bytes":1500,"version":2}` — rather than beside it. The same blob chunked to a different size or overlap is a different retrieval layout, so a re-tune has to select a distinct regime automatically rather than relying on someone to remember it. Size is in bytes rather than tokens because chunking must not depend on a tokenizer now that the model is behind an HTTP endpoint; English prose runs about four bytes to the token, so a budget approximates the model's window on purpose. Overlap is Unicode code points, so no boundary ever splits UTF-8. The default was chosen for a 512-token model and is conservative for the one in use; raising it is a retrieval question — bigger chunks match more broadly and cite less precisely — which is why it is a knob and not a constant. Index and query must use the same budget: a query under a different one searches a regime nothing was written under.

**Reads take the budget, never the key.** `store.chunker_key_for` derives it from the index type.
A reader that passed a hand-formatted key would work until the budget moved, and then match
nothing, which is indistinguishable from a subject that never came up. Writers still pass theirs
explicitly: they record it in the index's sync state as well as on the chunk.

The key is serialized from the budget rather than formatted by hand, and is one column rather
than several, for the same reason in both cases: **a regime filter cannot be under-specified.**
A field added to `ChunkBudget` lands in the key automatically, and a query either matches the
whole regime or does not — where separate `target_bytes`/`max_bytes` columns would let a query
forget one and quietly mix two chunkings, which is the failure this schema exists to prevent.
The cost is a ~50-byte string in a primary key; if that ever shows up on a disk graph, the move
is a `chunk_regimes` table with an integer id, which keeps the single-column filter in the hot
table and makes the parameters queryable.

**Source identity and content identity are deliberately different.** A Git `blob_sha` names
source bytes; `content_sha` names the exact string embedded from one chunk. Neither source
namespace leaks into the content address, which is why an identical embedding input can be shared
across corpora.

### git: the join is the tip filter

Search joins `git_tip` to `git_chunks`, then to `contents` and `content_embeddings`, so
content that is no longer at the branch tip is unreachable **by construction**, not by a delete
pass someone has to remember to run. History is never indexed — only `git ls-tree -r <tip>` is. A
sync publishes that tip in one transaction: a source-stage failure leaves the previous tip in
place rather than a half-swapped one. Embedding failures happen after this source publication and
are surfaced as pending chunks instead; `test_sync.py` asserts both boundaries.

**The tip swap is the source atomic step.** A Git sweep writes source chunks and swaps the tip in
one transaction, so a source failure leaves the previous tree visible. It does not call the
embedding provider. After that commit, the shared embedding worker fills vectors in bounded,
independently committed batches; a provider failure leaves the new tip and its pending content
visible to status, then a later worker retry resumes. Search sees only the subset whose vectors
exist for its active model.

A sync whose commit and regime already match what `git_sync_state` records returns
`AlreadyCurrent` without touching git or the tables, so it costs one `SELECT`. That is what lets
a push-triggered sync and a slow reconciling cron both fire as often as they like — webhooks get
dropped, so you want the belt as well as the braces.

### Two choices worth knowing

**Vectors are `halfvec`, not `vector`.** The model returns 2560 dimensions, where a `vector`
costs 4 bytes per dimension — ~10 KiB a chunk — and pgvector's HNSW and IVFFlat refuse anything
over **2000 dimensions**, so a `vector` column here could never be indexed at all. `halfvec` is
2 bytes per dimension (~5 KiB) and indexable to 4000. The cost is IEEE half precision, about
three decimal digits per component, which is noise beside what the embedding itself rounds off —
and these values are only ever compared, never read back and used for anything.

**No ANN index**, which at this size is still a choice rather than a limit. Exact KNN scans the
joined set and so has no ANN-plus-filter correctness problem. Sizing it honestly: ~5 KiB a chunk
means a query reads ~50 MB at 10k chunks and ~500 MB at 100k, against a database whose volume is
**2Gi in total** and shared with the approval ledger — so the volume runs out around the same
place the scan does.

Two things that revisit would need, in order: a **typmod** — the column is declared without one so
that changing models is not a migration, and an index cannot be built on a column whose dimension
is undeclared — and then an HNSW index, which `halfvec(2560)` is eligible for. Neither requires
re-embedding anything. Dropping to fewer dimensions is the other lever, since Qwen3-Embedding is
Matryoshka-trained; **that one does require re-embedding, and the dimension would have to enter
`model_key`**, because the model name alone would no longer identify the vector space and
`content_embeddings` would otherwise silently mix two of them.

**Embeddings come from Ollama**, over its OpenAI-compatible `/v1/embeddings`, so the backend is a
base URL and a model name rather than an implementation — LiteLLM or anything else speaking that
format is a config change. `model_key` identifies the model's vector space, so a model change
creates a distinct set of content embeddings while an endpoint-address change reuses them.

Two consequences to hold onto, because search embeds its _query_ and therefore inherits whatever
the embedder is:

- **A failed embed is a failed search, and must read as one.** An empty result means "nothing was
  said about this"; an unreachable embedder means "could not look". The tools keep those apart,
  and the client carries an explicit timeout rather than the library's ten-minute default.
- **Ollama is a zone away from the console.** It runs on `wyrm2` (zone `atlas`) for its GPUs,
  while the console is pinned to `hil-ovh` because cross-zone round trips are what turned a 4.6ms
  query into a two-second request there. Every search pays that hop. If it bites, the fix is an
  embedding-only Ollama in `hil-ovh` — qwen3-embedding at this size runs on CPU — rather than
  unpinning either side.

No network policy stands in the way today: neither the `ollama` namespace nor `haku-console`
is selected by any Cilium policy, and the CCNPs that exist are scoped to other namespaces by
`endpointSelector`. Adding an ingress policy to `ollama` later would need this flow allowed
explicitly.

## Console integration retired

The `haku/console` Recall reader, authorization policy, MCP tool wrapper, and database schema
verification were removed. The shared package remains used by Agentplane's indexing code.

The Console Recall schema, indexed data, and `vector` extension stay in its database during this
rollout because the outgoing Console version maps and validates those tables at startup. A later
release can drop that retained schema after the retiring image has fully converged. No Console
worker or request path reads or writes the retained index now.

## Not here yet

- **A push-triggered sweep.** Nothing fires one, so a haku-state commit waits for the next
  thirty-second git tick.

- **Retention is not a cache policy.** `contents` and `content_embeddings` are durable semantic
  index data, including content that has left the current tip. No retention/garbage-collection
  policy exists yet. If one is added, it must remove a content row and all of its model embeddings
  only when `git_chunks` no longer references it; the source-occurrence tables, not a last-seen
  timestamp, are the liveness signal.

## Test

```bash
bbr test //haku/recall_index/...
```
