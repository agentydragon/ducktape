# haku index

Semantic search over the things a Haku runtime should be able to recall, for runtimes that have
no OpenClaw-style workspace index — and no checkout at all, in the case of the console.

A **logical index** is the durable occurrence and future authorization boundary. Each configured
index has an `index_id` and an `index_type`; it never obtains identity from a Python default or a
conventional name. `git` and `chat` are index types — storage and provenance shapes — rather than
permissions or query scopes:

| index type | source shape                                     | a hit points at            |
| ---------- | ------------------------------------------------ | -------------------------- |
| `git`      | files at a branch tip of a configured Git remote | a path and a byte range    |
| `chat`     | configured console `conversation_item` source    | a session and its item ids |

The deployment registry in `cluster/k8s/haku/console/config.yaml` currently declares two Git
indexes — `haku-state` over Haku's Forgejo remote and `ducktape-public` over the public
Ducktape `devel` branch — plus `haku-conversations` as a chat index. Adding another index is a
reviewed configuration change; it is not an unscoped runtime default.

The index is derived state: it can be thrown away and rebuilt from git and Postgres at any time.

## Source materialization and embedding are separate stages

Git and chat sweeps own their source-specific work: they chunk changed source material, write the
occurrence rows that preserve provenance, and insert the normalized strings into global
`contents`. They never wait for an embedding endpoint. `contents` is consequently the durable,
content-addressed queue shared by every source and logical index.

One independent embedding maintenance loop then selects content that lacks a vector for the active
`model_key`, sends bounded batches to the configured provider, and writes
`content_embeddings`. Search joins source occurrences through that model-specific table, so a
materialized chunk becomes searchable only after its vector arrives. A model change does not
re-fetch or re-chunk Git/chat sources: the worker drains the same global content queue for the new
model. Status distinguishes current source chunks, embedded chunks, and pending chunks so a
partial result cannot be mistaken for an up-to-date empty corpus.

## Design

The index has globally-addressed semantic content plus index-type-specific occurrences:

| table                 | keyed by                                        | holds                                                       |
| --------------------- | ----------------------------------------------- | ----------------------------------------------------------- |
| `indexes`             | `index_id`                                      | one boundary plus its `index_type` (`git` or `chat`)        |
| `contents`            | `content_sha`                                   | the exact normalized string sent to an embedder             |
| `content_embeddings`  | `(content_sha, model_key)`                      | that content's vector in one model's vector space           |
| `git_chunks`          | `(index_id, blob_sha, chunker_key, byte_start)` | a Git blob span and its referenced global content           |
| `git_tip`             | `(index_id, path)`                              | the tree at the indexed commit, replaced wholesale per sync |
| `git_sync_state`      | `index_id`                                      | what the branch points at, and which commit `git_tip` holds |
| `chat_chunks`         | `(index_id, session_id, window_no)`             | a searchable chat window and its referenced global content  |
| `chat_chunk_messages` | index + window + ordinal                        | **which messages each window intersects**                   |
| `chat_sessions`       | `(index_id, session_id)`                        | each session's shape and regime as last indexed             |

### Logical indexes bound occurrences

`contents` and `content_embeddings` are global deduplication layers, but they are not a recall
authority. Every Git and chat occurrence belongs to one durable `index_id`; `indexes` names that
boundary and carries its `index_type`. The deployed registrations are `haku-state` and
`ducktape-public` (Git), plus `haku-conversations` (chat). A second Git index may reuse an
identical content vector, but its tip, revision state, and matches remain a separate set of
occurrences.

An index is its upstream collection: a Git index's configured remote and branch or the Console chat
index's `conversation_item` collection are part of that index's type-specific deployment
configuration, not a second durable database identity. Future Flux artifact ingestion adds an
index type/configuration shape; it does not add a generic source layer. Reader grants and RLS bind
callers to `index_id`, never to the global content cache.

`contents.content_sha` is the SHA-256 of the exact UTF-8 encoding of `contents.content`.
It has one namespace across index types: the same rendered content appearing in either configured
index, or again in another revision or session, is the same content row. `content_embeddings` then adds
the model identity, because the same content can legitimately have vectors in more than one
vector space. These are durable semantic-index tables, not an evictable cache.

The occurrence rows remain intentionally source-specific. A Git occurrence says which bytes of a
blob yielded one content value; a chat occurrence says which session window and messages yielded
one. That keeps citations and source lifecycle local while semantic materialization is shared.
`byte_start` distinguishes adjacent Git chunks; `chat_chunks.window_no` instead names a window's
position in a session.

**Chunk size and overlap are configurable, and they live inside `chunker_key`** — canonical JSON,
`{"max_bytes":3000,"overlap_codepoints":128,"target_bytes":1500,"version":2}` — rather than beside it. The same blob chunked to a different size or overlap is a different retrieval layout, so a re-tune has to select a distinct regime automatically rather than relying on someone to remember it. Size is in bytes rather than tokens because chunking must not depend on a tokenizer now that the model is behind an HTTP endpoint; English prose runs about four bytes to the token, so a budget approximates the model's window on purpose. Overlap is Unicode code points, so no boundary ever splits UTF-8. The default was chosen for a 512-token model and is conservative for the one in use; raising it is a retrieval question — bigger chunks match more broadly and cite less precisely — which is why it is a knob and not a constant. Index and query must use the same budget: a query under a different one searches a regime nothing was written under.

**Reads take the budget, never the key.** `store.chunker_key_for` derives it from the index type,
because `git` and `chat` keys may be the same string under the same budget. A reader that passed
the other index type's key would work until one version moved, and then match nothing, which is
indistinguishable from a subject that never came up. Writers still pass theirs explicitly: they
record it in the index's sync state as well as on the chunk.

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
supersedes it. Re-windowing is nearly free when it produces content already present in the global
content queue; the shared worker decides later whether that content still needs a vector.

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

## Deployed, in the console

- **Schema ownership.** `store.ensure_schema` creates the extension, schema, and tables for the
  tests, which own their whole database. The deployed index gets them from the console's Alembic
  baseline — the console's CNPG cluster is the home because the chat corpus's source tables are
  already there.
- **The MCP tool surface.** `haku_index` (<../console/tools/recall_index.py>) is an in-process
  FastMCP server in haku-console: one `search` with optional `index_ids` (omitted means every
  configured index), plus `index_status`.

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

  **A search over an index that is behind carries the status back with it**, in `SearchResults.index`,
  rather than relying on the caller to go ask. Being told to check a second tool before
  believing an empty result only works on a caller that reads an empty result as suspicious,
  which is exactly the caller that does not need telling. What rides along is the whole status
  object and not a `stale: true` flag, because the useful question is _by how much_: four
  messages waiting is a different answer from a tip that is nine commits behind. It is attached
  only when a selected index is actually behind — a chat lag inside the sweep's own window
  (`_SETTLED_WITHIN`, two minutes) is the pipeline working, and a field present on every search
  is a field a reader learns to skip.

  **Search returns each matching indexed chunk by default, plus its pointer.** Set
  `include_content=false` to return provenance only. A Git hit always carries the index id, path,
  commit, and blob sha; a conversation hit carries the session, its room, and the ids of the
  messages in the window. The chunk is useful retrieval context, not an authoritative replacement
  for the source: callers that need a whole Git file or wider conversation read it through that
  source's reader. A second whole-source reader in this server would be a second answer to "what
  does this file say", and the two would drift.

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

- **Sync.** `haku/console/recall_index_sync.py` sweeps every configured index from the
  separately deployed `haku-indexer` worker (`haku/console/indexer.py`) in its `chunk` role
  (`cluster/k8s/haku/console/indexer-deployment.yaml`); the same image's `embed` role drains the
  shared embedding queue (`indexer-embed-deployment.yaml`). The console process only reads the
  committed index state for search and status, so index maintenance failing or rolling never
  touches the console's own availability. The current chat index runs every minute over the
  console's tables (which the worker's narrow database role may only read); each configured
  Git index runs every thirty seconds against its own bare mirror on the chunk pod's `/tmp`.
  Each logical index takes its own Postgres advisory lock, so exactly one replica — of either
  deployment, during a rollout that still has a loop-carrying console — syncs it and a slow
  fetch never delays another index. The embedding drain needs no such leadership: every batch is
  claimed `FOR UPDATE SKIP LOCKED`, so concurrent drains split the queue instead of electing one
  worker.

  **The git tick is an `ls-remote`, not a fetch.** One round trip returns refs and no objects, so
  the common case — nothing moved — costs almost nothing and can be asked often. The gate is
  `sync.is_current`, the same predicate the sync itself early-outs on, because it must compare the
  source regime: a chunker change has to re-materialize a tip that never moved, while a new
  embedding model is handled independently by the shared worker's model-specific queue.

  **A chat session is left to settle before it is indexed.** A changed session is re-windowed
  wholesale, so indexing one mid-exchange re-chunks its whole tail and the next turn does it
  again; `sync_chat` skips a session whose newest message is under `quiet_for` old (30s) and
  reports it as `sessions_settling`. Nothing records the skip, so the next sweep still sees the
  session as changed — the only cost is the delay, and `index_status` therefore has a lag floor of
  the quiet window. Git credentials are per-index: `haku-state` uses **Haku's own Forgejo account**
  (operator, 2026-08-15), so the indexer worker holds something that could write haku-state even
  though nothing in it does — the console API pod no longer mounts it. `ducktape-public` needs
  none: it clones the canonical public GitHub remote anonymously. The Forgejo credential cost is
  recorded where it is paid: `tf/gitops/haku-state/main.tf`, which reflects the Secret into
  `haku-console`, and the indexer Deployment that consumes it.

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

**The chat corpus is fenced per conversation.** Two layers, decided at two times:

- **Which indexes a caller may search at all** is the per-profile `recall_index_ids` grant,
  enforced server-side in the console (`recall_index_access.py`). The gate is the named logical
  index, not a predicate every read path has to remember.
- **Which conversations a caller's chat hits may come from** (#4431 stage 5): every chat
  occurrence names its `conversation_id`, search joins it to the conversation's pinned
  `access_profile_id`, and candidates outside the caller's profile-DAG read closure
  (`haku/console/conversation_read_access.py` over `can_read_profiles`) are excluded in the same
  materialized CTE that already filters chunker and model — **before** the distance operator, so
  an unauthorized window loses by exclusion, never by rank. `search_chat` therefore requires a
  `readable_profiles` decision from every caller; `None` is the browser Operator's whole-corpus
  scope, and conversations predating pinned identity match no profile list.

The drilldown shares the fence: `haku_conversations`'s `list_sessions`, `list_turns`,
`read_conversation_items` and `read_session_frames` apply the same scope, because scoping search but not
the drilldown it hands off to would be theatre — the ids in a hit are exactly what those reads
take. Why ranked retrieval was the urgent half is worth keeping: a drilldown makes reading
another conversation a deliberate act (you have to name the session), while ranked retrieval
surfaces it **by accident**, at the top of the results, in answer to an innocent question.

Globally-addressed `contents` and `content_embeddings` are deliberately outside the fence: two
indexes, or two conversations, can share exact content and its vector, while identity and access
policy live on source occurrences — the rows a hit hands back. The occurrence links to the
conversation and never duplicates a profile label, so revoking or re-pinning reads requires no
re-index.

What remains open here is the trust-tier generalization
(<../plans/information_trust_tiers.md>): tier labels for Matrix rooms and agent kinds, and
per-tier chat indexes if the label ever needs to move off the conversation's pinned profile.
`cluster/k8s/haku/console/config.yaml` still decides which agents get the tools at all and under
which auto-approval policy — an unscoped read tool on the unconditional auto-approve list is the
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

- **A frame's payload is unbounded.** One `tool_result` can be an entire file. Embedding those
  verbatim means vectors over file dumps, a corpus that grows with tool volume rather than with
  conversation, and retrieval that returns the file rather than the reasoning about it.
- **The prompts and answers are the high-signal half.** `conversation_item` is what was actually
  said, folded out of the frames by the console. If recall over that is not useful, recall over the
  frames underneath it will not rescue it.

When they are added, they are a **third index type** (`IndexType.FRAMES`), not a widening of
this one: different unit, different chunker version line, and explicit index selection is what
lets a caller ask for conversation without getting tool output. The likely shape is frames filtered by `kind`
(assistant/user text, and tool calls by name and arguments) with `tool_result` bodies truncated
hard or excluded — which is a decision to make with measurements from the message corpus in hand.

## Test

```bash
bbr test //haku/recall_index/...
```
