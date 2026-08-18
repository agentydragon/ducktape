# haku index

Semantic search over the things a Haku runtime should be able to recall, for runtimes that have
no OpenClaw-style workspace index — and no checkout at all, in the case of the console.

Two corpora, named explicitly everywhere. They answer different questions and are built from
different sources, and a query that silently searched the wrong one would look like a retrieval
quality problem:

| corpus | source                                                          | a hit points at               |
| ------ | --------------------------------------------------------------- | ----------------------------- |
| `git`  | the files at a branch tip of a git repository (haku-state)      | a path and a byte range       |
| `chat` | the console's `session_messages` — Matrix and SPA conversations | a session and its message ids |

The index is derived state: it can be thrown away and rebuilt from git and Postgres at any time.

## Design

The index has globally-addressed semantic content plus source-specific occurrences:

| table                 | keyed by                              | holds                                                       |
| --------------------- | ------------------------------------- | ----------------------------------------------------------- |
| `contents`            | `content_sha`                         | the exact normalized string sent to an embedder             |
| `content_embeddings`  | `(content_sha, model_key)`            | that content's vector in one model's vector space           |
| `git_chunks`          | `(blob_sha, chunker_key, byte_start)` | a Git blob span and its referenced global content           |
| `git_tip`             | `path`                                | the tree at the indexed commit, replaced wholesale per sync |
| `git_sync_state`      | singleton                             | what the branch points at, and which commit `git_tip` holds |
| `chat_chunks`         | `(session_id, window_no)`             | a searchable chat window and its referenced global content  |
| `chat_chunk_messages` | window + ordinal                      | **which messages each window intersects**                   |
| `chat_sessions`       | `session_id`                          | each session's shape and regime as last indexed             |

`contents.content_sha` is the SHA-256 of the exact UTF-8 encoding of `contents.content`.
It has one namespace across Git and chat: the same rendered content appearing in either corpus,
or again in another revision or session, is the same content row. `content_embeddings` then adds
the model identity, because the same content can legitimately have vectors in more than one
vector space. These are durable semantic-index tables, not an evictable cache.

The occurrence rows remain intentionally source-specific. A Git occurrence says which bytes of a
blob yielded one content value; a chat occurrence says which session window and messages yielded
one. That keeps citations and source lifecycle local while semantic materialization is shared.
`byte_start` distinguishes adjacent Git chunks; `chat_chunks.window_no` instead names a window's
position in a session.

**Chunk size and overlap are configurable, and they live inside `chunker_key`** — canonical JSON,
`{"max_bytes":3000,"overlap_codepoints":128,"target_bytes":1500,"version":2}` — rather than beside it. The same blob chunked to a different size or overlap is a different retrieval layout, so a re-tune has to select a distinct regime automatically rather than relying on someone to remember it. Size is in bytes rather than tokens because chunking must not depend on a tokenizer now that the model is behind an HTTP endpoint; English prose runs about four bytes to the token, so a budget approximates the model's window on purpose. Overlap is Unicode code points, so no boundary ever splits UTF-8. The default was chosen for a 512-token model and is conservative for the one in use; raising it is a retrieval question — bigger chunks match more broadly and cite less precisely — which is why it is a knob and not a constant. Index and query must use the same budget: a query under a different one searches a regime nothing was written under.

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

**Source identity and content identity are deliberately different.** A Git `blob_sha` names
source bytes; `content_sha` names the exact string embedded from one chunk. Chat has no blob, but
its windows likewise refer to global `content_sha` values. Neither source namespace leaks into the
content address, which is why an identical embedding input can be shared across corpora.

### git: the join is the tip filter

Search joins `git_tip` to `git_chunks`, then to `contents` and `content_embeddings`, so
content that is no longer at the branch tip is unreachable **by construction**, not by a delete
pass someone has to remember to run. History is never indexed — only `git ls-tree -r <tip>` is. A
sync publishes that tip in one transaction: a run that dies halfway — embedder gone, connection
lost — leaves the previous tip searchable rather than a half-swapped one; `test_sync.py` asserts
this.

**Embeddings commit as they are computed; the tip swap is the atomic step.** A first sync of a
repository this size is minutes of embedding, and as one transaction it could only finish or lose
everything — against an endpoint that occasionally fails a call, that is a run which starts over
forever and never commits (observed in production, 2026-08-15: mirror cloned, `git_tip` empty for
half an hour, no error logged). Content embeddings are unreachable until `git_tip` names their
source occurrences, so committing them early is invisible to searches and makes a retry resume:
the next attempt finds the durable `(content_sha, model_key)` rows and pays only for what is left.
`test_sync.py` asserts both halves — the work survives, and the half-indexed tip is not published.

A sync whose commit and regime already match what `git_sync_state` records returns
`AlreadyCurrent` without touching git or the tables, so it costs one `SELECT`. That is what lets
a push-triggered sync and a slow reconciling cron both fire as often as they like — webhooks get
dropped, so you want the belt as well as the braces.

### chat: a chunk names the messages it covers

Message ends are the preferred cut rather than line ends, so a window usually stops where a
message does, and every window names each message it **intersects** — the pointers a caller
drills into with the console's own conversation tools (`haku/console/tools/conversations.py`)
rather than trusting the embedded copy in `contents.content`. Two things cut across a message
anyway: the configured overlap, which carries the previous window's tail into the next, and a
message longer than a whole chunk, which is split. So a window's message list is what it overlaps,
not what it wholly contains.

The unit of skipping is a session: one grouped scan gets every session's message count and newest
message, and a session that matches what `chat_sessions` recorded under the same regime is never
read. A session that has changed is re-windowed **wholesale**, because its trailing window changes
shape as it grows and appending would leave a stale partial window searchable beside the one that
supersedes it. Re-windowing is nearly free when it produces content already embedded by the active
model.

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

## An index nobody consults is not memory

An agent that _can_ search does not thereby search. It answers from the context in front of it,
which is the one place the answer reliably is not, and a tool it never reaches for is
indistinguishable from a tool that does not exist. So recall is prompted in two places, both of
which the agent cannot edit:

- **The `search` tool's own description** (<../console/tools/recall_index.py>) states it as a step
  rather than an affordance, and names the question types that trigger it — prior work, decisions,
  dates, people, preferences, commitments, anything asked for earlier. A tool description is what
  a model actually reads; a server's `instructions` frequently are not surfaced by the client at
  all, which is why the same point is not left there alone.
- **The Matrix session prompt** (<../../cluster/k8s/haku/console/matrix_system_prompt.md.j2>),
  which is operator-owned and mounted from the console's ConfigMap. It says that anything older
  than the replayed tail is something to look up here rather than something to ask the operator
  to quote back.

Both say the same third thing: **a search that found nothing must be reported as a search that
found nothing**, not as absence and not as silence. That is the failure the whole surface exists
to prevent, and it is also why a behind corpus attaches its status to the result.

The wording is deliberately close to OpenClaw's `memory-core` prompt section, which is the one
comparable thing in reach and has had far more exposure to real sessions than this has.

## Evaluating it locally

Everything here is runnable against a clone and a throwaway Postgres, which is the point: whether
semantic retrieval over these corpora beats ripgrep and `list_sessions` is an empirical
question, and the answer wants measuring rather than arguing.

The CLI needs an embedder as well as a database — it defaults to `http://localhost:11434/v1` and
`qwen3-embedding:4b`, so either run Ollama locally or port-forward `ollama.ollama:11434`.
`HAKU_STATE_INDEX_EMBEDDER_{URL,MODEL}` override both, and the model must be the one the server
actually reports, since the client fails closed on a mismatch rather than writing a second vector
space into the corpus.

```bash
docker run -d --rm -e POSTGRES_PASSWORD=x -p 5432:5432 pgvector/pgvector:pg18
export HAKU_STATE_INDEX_DATABASE_URL=postgresql+asyncpg://postgres:x@localhost:5432/postgres

bb run //haku/recall_index:main -- index-git https://git.allegedly.works/haku/haku-state \
    --mirror /tmp/haku-state.git --username haku --password "$FORGEJO_TOKEN"
bb run //haku/recall_index:main -- query-git "how do I file an intake item"
bb run //haku/recall_index:main -- status
```

`index-git` is idempotent and incremental: re-running it after new commits embeds only blobs it
has never seen.

The chat corpus reads the console's own tables, so `index-chat` wants a database that has them —
the console's, or a restored copy of it. There is no repository to clone and no credential to
pass:

```bash
bb run //haku/recall_index:main -- index-chat
bb run //haku/recall_index:main -- query-chat "what did we decide about the egress fence"
bb run //haku/recall_index:main -- query-chat "intake" --session-id 0e4b…
```

## Deployed, in the console

- **Schema ownership.** `store.ensure_schema` creates the extension, schema, and tables for the
  CLI and the tests, which own their whole database. The deployed index gets them from the console's Alembic
  baseline — the console's CNPG cluster is the home because the chat corpus's source tables are
  already there.
- **The MCP tool surface.** `haku_index` (<../console/tools/recall_index.py>) is an in-process
  FastMCP server in haku-console: one `search` with a `corpus` argument (`haku_state`,
  `conversations`, or both), plus `index_status`.

  **`index_status` answers before there is anything to search.** It reports `remote_commit` —
  what the last sweep saw on the branch, recorded on every tick including the ones that decide
  there is nothing to do — alongside `indexed_commit`, which is null until a first sync has
  completed, and `embedded_chunks`, which climbs while one is running. The three together say
  which of "never configured", "never reached the repository", "indexing right now" and "behind
  by a commit" is true. That distinction is not hypothetical: on 2026-08-15 the corpus sat empty
  for an hour while the status surface returned a bare `null`, and diagnosing it took a psql
  session against the production database.

  Both facts live in one `git_sync_state` row rather than two tables: they are two things about
  the same branch, every reader wants both, and "is the index behind" should be a comparison
  within a row. The indexed half is nullable because it becomes true later, and a check keeps it
  all-or-nothing so a commit can never be recorded without the regime it was indexed under.

  **A search over a corpus that is behind carries the status back with it**, in `SearchResults.index`,
  rather than relying on the caller to go ask. Being told to check a second tool before
  believing an empty result only works on a caller that reads an empty result as suspicious,
  which is exactly the caller that does not need telling. What rides along is the whole status
  object and not a `stale: true` flag, because the useful question is _by how much_: four
  messages waiting is a different answer from a tip that is nine commits behind. It is attached
  only when a searched corpus is actually behind — a chat lag inside the sweep's own window
  (`_SETTLED_WITHIN`, two minutes) is the pipeline working, and a field present on every search
  is a field a reader learns to skip.

  **Search returns pointers, not content, and there are no read tools here.** A haku-state hit
  carries the path, the commit, and the blob sha — Haku reads the file from its own clone. A
  conversation hit carries the session, its room, and the ids of the messages in the window —
  `haku_conversations` already owns reading past sessions. A second reader in this server would be
  a second answer to "what does this file say", and the two would drift.

  Listing the server in `cluster/k8s/haku/console/config.yaml` is what builds it — a configured
  server with no builder fails `validate_in_process_server_bindings` at startup — and the console
  refuses to start if it is listed with no embedder configured, since search embeds its query and
  cannot run without somewhere to do that. It is listed there, and Haku holds both tools unscoped
  through the `haku_recall_reads` policy; **Read scoping** below is what that decision rests on.

- **The `vector` extension — not an image build.** pgvector is untrusted, so `CREATE EXTENSION`
  needs superuser and the migration (running as `approval_store`) cannot do it — hence a CNPG
  `Database` CR (<../../cluster/k8s/haku/console/db/approval-store-database.yaml>) declaring the
  extension, adopting the database `bootstrap.initdb` created, with `databaseReclaimPolicy: retain`
  so deleting the file can never drop the console's database. pgvector 0.8.1 already ships in
  `ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie`, the image the console's CNPG cluster
  runs, so nothing had to be rebuilt.

  The migration that builds the derived schema assumes the extension is there. If it is not, the
  migration fails, the new replica never becomes Ready, and `maxUnavailable: 0` leaves the running
  version serving — so a change to either side wants the `Database` CR reconciled first.

- **Sync.** `haku/console/recall_index_sync.py` sweeps both corpora from the console process:
  chat every minute over its own tables, haku-state every thirty seconds against a bare mirror on
  the pod's `/tmp`. Each corpus takes its own Postgres advisory lock, so
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
  the quiet window. The git half needs a credential, and it is **Haku's own Forgejo account**
  (operator, 2026-08-15) rather than a second read-only one — so
  the console holds something that could write haku-state even though nothing in it does. That cost
  is recorded where it is paid: <../console/README.md> and `tf/gitops/haku-state/main.tf`, which
  reflects the Secret into `haku-console`.

## Not here yet

- **A push-triggered sweep.** Nothing fires one, so a haku-state commit waits for the next
  thirty-second git tick and a new message for the next minute-long chat tick plus its quiet
  window.

- **Retention is not a cache policy.** `contents` and `content_embeddings` are durable semantic
  index data, including content that has left the current tip or a deleted chat session. No
  retention/garbage-collection policy exists yet. If one is added, it must remove a content row
  and all of its model embeddings only when neither `git_chunks` nor `chat_chunks` references it;
  the source-occurrence tables, not a last-seen timestamp, are the liveness signal.

## Read scoping

**Decided 2026-08-15: Haku holds `search` and `index_status` unscoped**, auto-approved through
`haku_recall_reads` in `cluster/k8s/haku/console/config.yaml`. What made that an easy call is that
it grants no new reachability — Haku already reads any session through `haku_conversations` and
has a haku-state clone — so search adds discoverability over data it can already reach. The
inventory below stays because the reasoning does not: the moment a second operator or a room Haku
should not see exists, ranked retrieval is where that leaks first, and this is the list of what
would have to change.

**The chat corpus is unscoped: every session, whichever room or operator it served.**
That inherits the Matrix channel's own rule (<../console/x/channels/matrix/SPEC.md> § The agent's
own view), which leaves the policy deliberately open for `list_sessions`/`read_rollout` — the
eventual policy about which Haku may read which past conversation is not settled, and guessing at
one here would be a scoping rule nobody stated.

Semantic search is not the same exposure as that drilldown, and the difference is why this is
written down rather than left inherited. A keyword drilldown makes reading another room's
conversation a deliberate act: you have to name the session. Ranked retrieval surfaces it
**by accident**, at the top of the results, in response to an innocent question. Same data, same
policy gap, materially different odds of tripping over it.

Nothing was blocked on it while there was one operator and one agent. It is the prerequisite for
exposing the corpus through `/mcp` to a **second** agent.

**The trigger condition has since arrived, and the intended answer is not a filter.** Several
agent kinds at several information trust levels (<../plans/information_trust_tiers.md>) is exactly
the "a room Haku should not see" case above. The direction chosen there: `Corpus` stays the
**type** (`git`/`chat` — how content is chunked and addressed) and gains named **instances**
configured per repo and per tier, so the gate is which indexes a caller may search rather than a
predicate every read path has to remember.

Consequences for this package, and note where they do **not** land: globally-addressed
`contents` and `content_embeddings` are unchanged by source-instance scoping. Two instances can
share the same exact content and its semantic representation, while identity and access policy
live on source occurrences — the rows a hit hands back. `git_tip` becomes keyed `(index, path)`,
`git_sync_state` gains a row per index (retiring its `id = 1` singleton CHECK), chat occurrences
derive their instance from the session, and the permitted-instance join belongs in the same
materialized CTE that already filters the current chunker and model before the distance operator.

What settling it touches:

- **`haku/recall_index/store.py`** — `search_chat` takes an optional `session_id` and nothing
  else. A scope is a `WHERE` over `sessions.operator_id`, or over the address of the
  `chat_attachment` on the session's conversation, which means the search joins those tables (or
  `chat_chunks` denormalizes both).
- **`haku/console/tools/conversations.py`** — `list_sessions`, `list_turns`,
  `read_transcript` and `read_rollout` are unscoped by the same open decision. Scoping search but not the drilldown it
  hands off to would be theatre: the message ids in a hit are exactly what `read_rollout` takes.
- **Whatever identity the scope keys on.** An Agent's canonical identity and owning Operator come
  from `haku/console/agents/authorization.py` and `mcp_agent_auth.py`; a room-scoped rule instead
  needs the calling session's own conversation, which the in-process server would have to be told.
- **`cluster/k8s/haku/console/config.yaml`** — which agents get the tool at all, and under which
  auto-approval policy. An unscoped read tool on the unconditional auto-approve list is the
  configuration this section exists to prevent.
- **<../plans/information_trust_tiers.md>** — where the decision belongs once made, since the fence
  that replaces "unscoped" is the information tier rather than the room. The alternative worth
  weighing against it is an RLS-scoped Postgres role, which pushes scoping into the database
  instead of into each tool.
- **`haku/docs/security.md`** — if the answer is "an agent may read any conversation", that is a
  confidentiality claim about the console's data and belongs in the enforcement inventory rather
  than in a default nobody chose.

## Frames

`session_frames` — the console's verbatim record of the agent protocol — is **not indexed**.
Frames are the granularity search should eventually use, and they are the only place a tool call
and the result it got both appear. Two reasons to do the
messages first and the frames later, both of which should be re-checked against a real index
rather than argued:

- **A frame's payload is unbounded.** `read_rollout` already bounds a page in bytes because one
  `tool_result` can be an entire file (`haku/console/tools/conversations.py`). Embedding those
  verbatim means vectors over file dumps, a corpus that grows with tool volume rather than with
  conversation, and retrieval that returns the file rather than the reasoning about it.
- **The messages are the high-signal half.** `session_messages` is what was actually said,
  already deduplicated against the delivery mirror by the console. If recall over that is not
  useful, recall over the frames underneath it will not rescue it.

When they are added, they are a **third corpus** (`Corpus.FRAMES`), not a widening of this one:
different unit, different chunker version line, and a `corpus=` filter is what lets a caller ask
for conversation without getting tool output. The likely shape is frames filtered by `kind`
(assistant/user text, and tool calls by name and arguments) with `tool_result` bodies truncated
hard or excluded — which is a decision to make with measurements from the message corpus in hand.

## Test

```bash
bbr test //haku/recall_index/...
```
