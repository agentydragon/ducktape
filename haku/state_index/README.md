# haku index

Semantic search over the things a Haku runtime should be able to recall, for runtimes that have
no OpenClaw-style workspace index — and no checkout at all, in the case of the console.

Two corpora, named explicitly everywhere. They answer different questions and are built from
different sources, and a query that silently searched the wrong one would look like a retrieval
quality problem:

| corpus | source                                                              | a hit points at               |
| ------ | ------------------------------------------------------------------- | ----------------------------- |
| `git`  | the files at a branch tip of a git repository (haku-state)          | a path and a byte range       |
| `chat` | the console's `claude_chat_messages` — Matrix and SPA conversations | a session and its message ids |

The index is derived state: it can be thrown away and rebuilt from git and Postgres at any time.

## Design

One shared table and a per-corpus set around it:

| table                 | keyed by                 | holds                                                                    |
| --------------------- | ------------------------ | ------------------------------------------------------------------------ |
| `chunks`              | `(corpus, content_sha)`  | every embedding ever computed, including for content no longer reachable |
| `git_tip`             | `path`                   | the tree at the indexed commit, replaced wholesale per sync              |
| `git_sync_state`      | singleton                | which commit `git_tip` holds                                             |
| `chat_chunks`         | `(session_id, chunk_no)` | the searchable windows of each chat session                              |
| `chat_chunk_messages` | window + ordinal         | **which messages each window holds**                                     |
| `chat_sessions`       | `session_id`             | each session's shape as last indexed, which is what decides re-indexing  |

`chunks` is content-addressed, so it has no notion of where content currently lives and keeps
embeddings for content that has left the indexed set. That is the cache: a revert, a rebase, a
force-push, a file moved between paths, a chat session re-windowed as it grows — all re-use
their vectors. The cache key is `(corpus, content_sha, byte_start, chunker_key, model_key)`: changing the chunker
or the embedding model misses the cache rather than silently serving vectors computed over
different text or by a different model. `byte_start` is what tells one chunk of a blob from the
next — an ordinal beside it would be a second name for the same fact, and the offsets are the ones
a caller can actually slice with. (The `chunk_no` in `chat_chunks` is a different thing: a
window's position in its session, which is what `chat_chunk_messages` hangs off.)

**Chunk size is configurable, and it lives inside `chunker_key`** — canonical JSON,
`{"max_bytes":3000,"target_bytes":1500,"version":1}` — rather than beside it. The same blob chunked to a different size is different text, so a re-tune has to
invalidate exactly like an algorithm change — putting the budget in the key makes that automatic
instead of something to remember. It is in bytes rather than tokens because chunking must not
depend on a tokenizer now that the model is behind an HTTP endpoint; English prose runs about four
bytes to the token, so a budget approximates the model's window on purpose. The default was chosen
for a 512-token model and is conservative for the one in use; raising it is a retrieval question —
bigger chunks match more broadly and cite less precisely — which is why it is a knob and not a
constant. Index and query must use the same budget: a query under a different one searches a
regime nothing was written under.

**Reads take the budget, never the key.** `store.chunker_key_for` derives it from the corpus,
because the two corpora's keys are the same string whenever their chunkers are at the same version
under the same budget — as they are today. A reader that passed the other corpus's key would
therefore work until one of the two versions moved, and then match nothing, which is
indistinguishable from a subject that never came up. Writers still pass theirs explicitly: they
record it in the corpus's sync state as well as on the chunk.

The key is serialized from the budget rather than formatted by hand, and is one column rather
than several, for the same reason in both cases: **a regime filter cannot be under-specified.**
A field added to `ChunkBudget` lands in the key automatically, and a query either matches the
whole regime or does not — where separate `target_bytes`/`max_bytes` columns would let a query
forget one and quietly mix two chunkings, which is the failure this schema exists to prevent.
The cost is a ~50-byte string in a primary key; if that ever shows up on a disk graph, the move
is a `chunk_regimes` table with an integer id, which keeps the single-column filter in the hot
table and makes the parameters queryable.

**`corpus` is in that key rather than implied.** Each corpus supplies its own kind of content
address — a git blob sha, the sha256 of a rendered message window — and its own chunker with its
own version line. Tagging the corpus is what keeps those namespaces apart; leaving it to a
convention about hash lengths would work right up until it didn't.

### git: the join is the tip filter

Search joins `git_tip` to `chunks`, so content that is no longer at the branch tip is
unreachable **by construction**, not by a delete pass someone has to remember to run. History is
never indexed — only `git ls-tree -r <tip>` is. A sync is one transaction: a run that dies
halfway — embedder gone, connection lost — leaves the previous tip searchable rather than a
half-swapped one; `test_sync.py` asserts this.

A sync whose commit and regime already match what `git_sync_state` records returns
`AlreadyCurrent` without touching git or the tables, so it costs one `SELECT`. That is what lets
a push-triggered sync and a slow reconciling cron both fire as often as they like — webhooks get
dropped, so you want the belt as well as the braces.

### chat: a chunk holds whole messages

Packing stops at message boundaries rather than at lines, so every window can name exactly which
messages it covers and a hit hands back pointers a caller drills into with the console's own
conversation tools (`haku/console/tools/conversations.py`) rather than trusting the copy in
`chunks.text`. The one exception is a message longer than a whole chunk, which is split — and
each part still holds exactly that one message, so the mapping never becomes approximate.

The unit of skipping is a session: one grouped scan gets every session's message count and newest
message, and a session that matches what `chat_sessions` recorded under the same regime is never
read. A session that has changed is re-windowed **wholesale**, because its trailing window changes
shape as it grows and appending would leave a stale partial window searchable beside the one that
supersedes it. Re-windowing is nearly free — the vectors are cached by content.

**Two differences from the git corpus are worth knowing, because they are weaker properties:**

- **Retraction is a step, not an invariant.** Chat windows are reachable until deleted, so a
  session the console has dropped stays searchable unless the sync sweeps it. `sync_chat` does
  sweep it, but that is a line of code someone can break, where the git corpus's tip join cannot
  be.
- **Only `complete` messages are indexed.** A `pending` or `streaming` row is still being written
  into, and a `failed` one records that nothing was said. So the newest exchange in a live session
  is not searchable until it finishes.

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
**2Gi in total** and shared with the approval ledger. (The "revisit past ~100k chunks" this
paragraph used to carry was arithmetic from the 384-dimension model that preceded Ollama; the
volume runs out around the same place the scan does.)

Two things that revisit would need, in order: a **typmod** — the column is declared without one so
that changing models is not a migration, and an index cannot be built on a column whose dimension
is undeclared — and then an HNSW index, which `halfvec(2560)` is eligible for. Neither requires
re-embedding anything. Dropping to fewer dimensions is the other lever, since Qwen3-Embedding is
Matryoshka-trained; **that one does require re-embedding, and the dimension would have to enter
`model_key`**, because the model name alone would no longer identify the vector space and the
content-addressed cache would silently mix two of them.

**Embeddings come from Ollama**, over its OpenAI-compatible `/v1/embeddings`, so the backend is a
base URL and a model name rather than an implementation — LiteLLM or anything else speaking that
format is a config change. `model_key` is the model, so it invalidates the cache on its own when
the model changes and re-uses every vector when only the address does.

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

## Evaluating it locally

Everything here is runnable against a clone and a throwaway Postgres, which is the point:
whether semantic retrieval over these corpora beats ripgrep and `list_conversations` is an
empirical question, and it gates whether any of this is worth deploying.

```bash
docker run -d --rm -e POSTGRES_PASSWORD=x -p 5432:5432 pgvector/pgvector:pg18
export HAKU_STATE_INDEX_DATABASE_URL=postgresql+asyncpg://postgres:x@localhost:5432/postgres

bb run //haku/state_index:main -- index-git https://git.allegedly.works/haku/haku-state \
    --mirror /tmp/haku-state.git --username haku --password "$FORGEJO_TOKEN"
bb run //haku/state_index:main -- query-git "how do I file an intake item"
bb run //haku/state_index:main -- status
```

`index-git` is idempotent and incremental: re-running it after new commits embeds only blobs it
has never seen.

The chat corpus reads the console's own tables, so `index-chat` wants a database that has them —
the console's, or a restored copy of it. There is no repository to clone and no credential to
pass:

```bash
bb run //haku/state_index:main -- index-chat
bb run //haku/state_index:main -- query-chat "what did we decide about the egress fence"
bb run //haku/state_index:main -- query-chat "intake" --session-id 0e4b…
```

## Not here yet

### Deployment

Deliberately absent — it depends on the evaluation above:

- **Schema ownership.** `store.ensure_schema` creates the extension, schema, and tables for
  the CLI and tests. A deployed index gets them from a migration in the console's Alembic
  chain instead (the console's CNPG cluster is the intended home — no new stateful service,
  and `haku/console/README.md` already names Postgres as an accepted private boundary). The chat
  corpus makes that the obvious home rather than one option: its source tables are already there.
- **The MCP tool surface — built, and off.** `haku_index`
  (<../console/tools/state_index.py>) is an in-process FastMCP server in haku-console: one
  `search` with a `corpus` argument (`haku_state`, `conversations`, or both), plus `index_status`.

  **Search returns pointers, not content, and there are no read tools here.** A haku-state hit
  carries the path, the commit, and the blob sha — Haku reads the file from its own clone. A
  conversation hit carries the session, its room, and the ids of the messages in the window —
  `haku_conversations` already owns reading past sessions. A second reader in this server would be
  a second answer to "what does this file say", and the two would drift.

  Listing the server in `cluster/k8s/haku/console/config.yaml` is what builds it — and the
  console refuses to start if it is listed with no embedder configured, since search embeds its
  query and cannot run without somewhere to do that. Nothing is registered in `cluster/k8s/haku/console/config.yaml` yet — a configured server with no builder
  fails `validate_in_process_server_bindings` at startup — so turning it on is a config change and
  a policy decision together, and **Read scoping** below is the gate on the second.

- **The `vector` extension — settled, and not by an image build.** pgvector 0.8.1 already ships in
  `ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie`, the image the console's CNPG cluster
  already runs; what was missing was only the `CREATE EXTENSION`. That is untrusted, so it needs
  superuser and the migration (running as `approval_store`) cannot do it — hence a CNPG `Database`
  CR (<../../cluster/k8s/haku/console/db/approval-store-database.yaml>) declaring the extension,
  adopting the database `bootstrap.initdb` created, with `databaseReclaimPolicy: retain` so
  deleting the file can never drop the console's database.

  Migration `0037` creates the schema and tables and assumes the extension is there. If it is not,
  the migration fails, the new replica never becomes Ready, and `maxUnavailable: 0` leaves the
  running version serving — so the ordering to verify before merging is that the `Database` CR has
  reconciled first.

- **Sync — done, in the console.** `haku/console/state_index_sync.py` sweeps both corpora from
  the console process: chat every minute over its own tables, haku-state every thirty seconds
  against a bare mirror on the pod's `/tmp`. Each corpus takes its own Postgres advisory lock, so
  exactly one replica syncs it and a slow fetch never delays the other.

  **The git tick is an `ls-remote`, not a fetch.** One round trip returns refs and no objects, so
  the common case — nothing moved — costs almost nothing and can be asked often. The gate is
  `sync.is_current`, the same predicate the sync itself early-outs on, because it must compare the
  whole regime: a new embedding model has to re-index a tip that never moved, and a commit-only
  comparison would skip it forever.

  **A chat session is left to settle before it is indexed.** A changed session is re-windowed
  wholesale, so indexing one mid-exchange re-chunks its whole tail and the next turn does it
  again; `sync_chat` skips a session whose newest message is under `quiet_for` old (30s) and
  reports it as `sessions_settling`. Nothing records the skip, so the next sweep still sees the
  session as changed — the only cost is the delay, and `index_status` therefore has a lag floor of
  the quiet window. The git half needs a credential, and it
  is **Haku's own Forgejo account** (operator, 2026-08-15) rather than a second read-only one — so
  the console holds something that could write haku-state even though nothing in it does. That cost
  is recorded where it is paid: <../console/README.md> and `tf/gitops/haku-state/main.tf`, which
  reflects the Secret into `haku-console`.

  Not done: nothing triggers a sweep on a push, so a haku-state commit is searchable within five
  minutes rather than immediately, and a new message within one.

- **Eviction.** `last_seen_at` is maintained but nothing sweeps it, and the cache keeps every
  vector ever computed — including for content that has left the tip. At ~10 KiB a chunk on a 2Gi
  volume (see above) that is a bounded amount of patience, not an indefinite one. When you do add a sweep, it
  must exclude anything still referenced:

  ```sql
  DELETE FROM state_index.chunks c
   WHERE NOT EXISTS (SELECT 1 FROM state_index.git_tip t WHERE t.blob_sha = c.content_sha)
     AND NOT EXISTS (SELECT 1 FROM state_index.chat_chunks w WHERE w.content_sha = c.content_sha)
     AND c.last_seen_at < now() - interval '90 days';
  ```

  Membership in `git_tip`/`chat_chunks` is the liveness signal, not `last_seen_at`: a sync that
  takes an early-out touches nothing, so an unchanged tip's or an unchanged session's
  `last_seen_at` goes stale while its content is still very much searchable. `last_seen_at` only
  governs how long _unreferenced_ vectors are kept against a future revert.

### Read scoping — before the chat corpus is exposed to any agent

**Decided 2026-08-15: Haku holds `search` and `index_status` unscoped**, auto-approved through
`haku_recall_reads` in `cluster/k8s/haku/console/config.yaml`. What made that an easy call is that
it grants no new reachability — Haku already reads any session through `haku_conversations` and
has a haku-state clone — so search adds discoverability over data it can already reach. The
inventory below stays because the reasoning does not: the moment a second operator or a room Haku
should not see exists, ranked retrieval is where that leaks first, and this is the list of what
would have to change.

**The chat corpus is unscoped: every session, whichever room or operator it served.**
That inherits `haku/plans/matrix_chat_runtime.md` R5.3a, which left the policy deliberately open
for `list_conversations`/`read_rollout` — "the eventual policy about which Haku may read which
past conversation is not settled, and guessing at one here would be a scoping rule nobody
stated."

Semantic search is not the same exposure as that drilldown, and the difference is why this is
written down rather than left inherited. A keyword drilldown makes reading another room's
conversation a deliberate act: you have to name the session. Ranked retrieval surfaces it
**by accident**, at the top of the results, in response to an innocent question. Same data, same
policy gap, materially different odds of tripping over it.

Nothing here is blocked on it — indexing everything and searching everything is right for the
local evaluation, where there is one operator and a restored database. It is a prerequisite for
exposing the corpus through `/mcp` to any agent.

What settling it touches:

- **`haku/state_index/store.py`** — `search_chat` takes an optional `session_id` and nothing
  else. A scope is a `WHERE` over `claude_chat_sessions.operator_id`/`room_id`, which means the
  search joins that table (or `chat_chunks` denormalizes both, the way
  `claude_chat_sessions.room_id` itself is denormalized from `matrix_conversation`).
- **`haku/console/tools/conversations.py`** — `list_conversations`, `read_rollout`, and
  `list_turns` are unscoped by the same open decision. Scoping search but not the drilldown it
  hands off to would be theatre: the message ids in a hit are exactly what `read_rollout` takes.
- **Whatever identity the scope keys on.** An Agent's canonical identity and owning Operator come
  from `haku/console/agents/authorization.py` and `mcp_agent_auth.py`; a room-scoped rule instead
  needs the calling session's own `room_id`, which the in-process server would have to be told.
- **`cluster/k8s/haku/console/config.yaml`** — which agents get the tool at all, and under which
  auto-approval policy. An unscoped read tool on the unconditional auto-approve list is the
  configuration this section exists to prevent.
- **`haku/plans/matrix_chat_runtime.md`** — R5.3a is where the decision belongs once made, and
  its Open questions section already carries the alternative worth weighing: an RLS-scoped
  Postgres role, which pushes scoping into the database instead of into each tool.
- **`haku/docs/security.md`** — if the answer is "an agent may read any conversation", that is a
  confidentiality claim about the console's data and belongs in the enforcement inventory rather
  than in a default nobody chose.

### Frames

`claude_chat_frames` — the console's verbatim record of the agent protocol — is **not indexed**.
`haku/plans/matrix_chat_runtime.md` names frames as the granularity search should eventually use,
and they are the only place a tool call and the result it got both appear. Two reasons to do the
messages first and the frames later, both of which should be re-checked against a real index
rather than argued:

- **A frame's payload is unbounded.** `read_rollout` already clips at 8 KB because one
  `tool_result` can be an entire file (`haku/console/tools/conversations.py`). Embedding those
  verbatim means vectors over file dumps, a corpus that grows with tool volume rather than with
  conversation, and retrieval that returns the file rather than the reasoning about it.
- **The messages are the high-signal half.** `claude_chat_messages` is what was actually said,
  already deduplicated against the delivery mirror by the console. If recall over that is not
  useful, recall over the frames underneath it will not rescue it.

When they are added, they are a **third corpus** (`Corpus.FRAMES`), not a widening of this one:
different unit, different chunker version line, and a `corpus=` filter is what lets a caller ask
for conversation without getting tool output. The likely shape is frames filtered by `kind`
(assistant/user text, and tool calls by name and arguments) with `tool_result` bodies truncated
hard or excluded — which is a decision to make with measurements from the message corpus in hand.

## Test

```bash
bbr test //haku/state_index/...
```
