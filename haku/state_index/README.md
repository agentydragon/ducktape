# haku-state index

Semantic search over the files at a branch tip of a git repository, for Haku runtimes that
have no OpenClaw-style workspace index — and no checkout at all, in the case of the console.

The index is derived state: it can be thrown away and rebuilt from git at any time.

## Design

Two tables, and the split between them is the point:

| table    | keyed by             | holds                                                                      |
| -------- | -------------------- | -------------------------------------------------------------------------- |
| `chunks` | content (`blob_sha`) | every embedding ever computed, including for content that has left the tip |
| `tip`    | `path`               | the tree at the indexed commit, replaced wholesale per sync                |

Search joins `tip` to `chunks`, so **the join is the tip filter**: content that is no longer
at the branch tip is unreachable by construction, not by a delete pass someone has to
remember to run. History is never indexed — only `git ls-tree -r <tip>` is.

Because `chunks` is content-addressed it is also the embedding cache. A revert, a rebase, a
force-push, a file moved between paths, a squash-merge of a branch already indexed — all reuse
their vectors. The cache key is `(blob_sha, chunk_no, chunker_version, model_key)`: changing
the chunker or the embedding model misses the cache rather than silently serving vectors
computed over different text or by a different model.

A sync is one transaction. A run that dies halfway — embedder gone, connection lost — leaves
the previous tip searchable rather than a half-swapped one; `test_sync.py` asserts this.

### Two choices worth knowing

**No ANN index.** Exact KNN over the joined set, which at this corpus size is a scan of a few
megabytes. That removes the ANN-plus-filter correctness problem entirely, and it removes
pgvector's 2000-dimension index limit as a constraint (the limit applies to index builds, not
to storage or queries). Revisit past ~100k chunks.

**CPU embeddings, pinned.** `bge-small-en-v1.5` (384-dim) under onnxruntime, weights pinned as
Bazel `http_file`s. Search has to embed the _query_, so an embedder that is sometimes
unreachable takes search down and not just indexing — which is the argument against reaching
for in-cluster Ollama first. When Ollama does become the backend, implement `Embedder` against
it; the differing `model_key` invalidates the cache on its own.

## Evaluating it locally

Everything here is runnable against a clone and a throwaway Postgres, which is the point:
whether semantic retrieval over haku-state beats ripgrep is an empirical question, and it
gates whether any of this is worth deploying.

```bash
docker run -d --rm -e POSTGRES_PASSWORD=x -p 5432:5432 pgvector/pgvector:pg18
export HAKU_STATE_INDEX_DATABASE_URL=postgresql+asyncpg://postgres:x@localhost:5432/postgres

bb run //haku/state_index:main -- index https://git.allegedly.works/haku/haku-state \
    --mirror /tmp/haku-state.git --username haku --password "$FORGEJO_TOKEN"
bb run //haku/state_index:main -- query "how do I file an intake item"
bb run //haku/state_index:main -- status
```

`index` is idempotent and incremental: re-running it after new commits embeds only blobs it
has never seen.

## Not here yet

Deployment, deliberately — it depends on the evaluation above:

- **Schema ownership.** `store.ensure_schema` creates the extension, schema, and tables for
  the CLI and tests. A deployed index gets them from a migration in the console's Alembic
  chain instead (the console's CNPG cluster is the intended home — no new stateful service,
  and `haku/console/README.md` already names Postgres as an accepted private boundary).
- **The MCP tool surface.** The consumer is meant to be an in-process FastMCP server in
  haku-console (`haku/console/in_process_servers.py`), returning path + byte range + score so
  a caller reads current content rather than a cached copy. Which agents see it, and under
  which auto-approval policy, is a config decision in `cluster/k8s/haku/console/config.yaml`.
- **The sync CronJob.** Image, Flux wiring, and the Forgejo credential — which must come from
  `tf/gitops/haku-state`, not a hand-minted token.
- **Eviction.** `last_seen_at` is maintained but nothing sweeps it. At 384 dims a chunk's
  vector is ~1.5 KB; add `DELETE FROM chunks WHERE last_seen_at < now() - interval '90 days'`
  when it shows up on a disk graph, not before.
