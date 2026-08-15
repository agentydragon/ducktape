# What OpenClaw's memory actually does, and what of it we want

Read of the OpenClaw source in aid of one question: with the index built and current, what
memory affordances do they have that we don't? Everything below is against
`openclaw/openclaw@de4104c8` (2026-08-15); line numbers are from that tree.

The short version: their retrieval is not better than ours, their **prompting** was (fixed —
#4072), and their consolidation ("dreaming") is a real thing we deliberately do not have.

## The pieces

Four workspace files, of which two are pushed into every session and two are pulled:

| file                          | how it reaches the agent                           |
| ----------------------------- | -------------------------------------------------- |
| `USER.md`                     | loaded at session start, own small budget          |
| `MEMORY.md`                   | loaded at session start                            |
| `memory/YYYY-MM-DD[-slug].md` | today's and yesterday's load on `/new` or `/reset` |
| `DREAMS.md`                   | never — human reading, in the Dreams UI            |

Plus two search corpora (`memory.search.sources`): `memory` (those files) and `sessions`
(transcripts). And two writers: the bundled `session-memory` hook, and dreaming.

## Finding 1: the hook and the sessions corpus overlap, and they know it

The `session-memory` hook writes the last 15 user/assistant messages to
`memory/YYYY-MM-DD-HHMM.md` on `/new`, `/reset`, daily reset or idle expiry. The `sessions`
source indexes the same transcripts. Both are live on a default personal install:

- `sources` defaults to `["memory"]`, but `rememberAcrossConversations` defaults to **true**
  when there is no DM isolation (`packages/memory-host-sdk/src/host/config-utils.ts:102`),
  and that flag appends `"sessions"` regardless (`src/agents/memory-search.ts:269`).
- Their own docs say so, in both `docs/automation/hooks.md` and
  `docs/reference/memory-config.md`: the same conversation can appear from both sources,
  "producing overlapping search results and additional embedding work… Enable both only when
  you intentionally want both representations."

It is not _pure_ redundancy, for three reasons worth keeping:

1. **Push vs pull.** The hook's output is auto-loaded into the next session. The corpus only
   surfaces if the model searches. Different reliability, and the reason their prompting
   matters as much as their index.
2. **It degrades without an embedder.** The `memory` source is files. The sessions corpus
   needs an embedding provider, is still behind `experimental.sessionMemory`, and indexes
   async. A reset always leaves a durable artifact even when retrieval is down.
3. **It is substrate the agent owns.** Markdown in the workspace can be corrected and
   consolidated — dreaming reads `memory/*.md` forward into `MEMORY.md`. An append-only
   transcript store cannot be revised.

**We need none of it.** `recent_messages` in the Matrix session prompt already replays the
tail into the next session, harness-side, from the same tables the chat corpus indexes. One
source of truth covers both roles, with no second representation to drift or double-embed.

## Finding 2: dreaming, and what actually drives it

Background consolidation in `memory-core`. On by default, cron `0 3 * * *`. Three phases per
sweep — light → REM → deep — of which only deep writes `MEMORY.md`.

### The promotion signal is retrieval frequency, not outcome

Every `memory_search` records what it surfaced — query hash, snippet, path, day — into a
short-term recall store (`extensions/memory-core/src/tools.ts:286`). That store, not the
transcript, is what deep ranks. A fact becomes durable because it kept being retrieved.

Worth being precise about what that does and does not measure, because the name oversells it:

- Six weighted signals (`short-term-promotion.ts:151-177`): relevance .30, frequency .24,
  query diversity .15, recency .15, multi-day consolidation .10, concept richness .06.
- **"Relevance" is the retriever grading itself** — `avgScore = totalScore / signalCount`,
  accumulating the similarity score the search engine assigned at retrieval time
  (`short-term-promotion-record.ts:266`). A confidently retrieved irrelevant chunk scores
  like a confidently retrieved correct one.
- **There is no outcome signal anywhere.** Nothing records whether the agent used a retrieved
  chunk, whether the answer was right, or whether the user pushed back. Grep the recall lane
  for `helpful|useful|feedback|reward` — nothing.
- The light/REM "phase boost" is rich-get-richer: what scores well enough to make today's
  shortlist gets a recency-decayed bump toward promotion tomorrow, for having scored well.

So the real predicate is **retrieved often, by varied queries, across multiple days, with
decent similarity** — an engagement proxy. Defensible (a chunk that keeps surfacing under
genuinely different questions is probably about something that keeps mattering) but it cannot
tell a fact the agent kept needing from a semantic attractor.

Gates, all of which must pass (`src/memory-host-sdk/dreaming.ts:46`): `minScore` 0.75,
`minRecallCount` 3, `minUniqueQueries` 3.

### Which parts are code and which are a model

Exactly two model calls in the whole system.

Mechanical: recall recording; transcript ingestion (eligibility filter, redaction, then
_synthetic_ recalls under `__dreaming_sessions__:<day>` — feedstock, not evaluation,
`dreaming-phases.ts:735`); concept tags by stop-word tokenization; light's rank-cap-stamp;
REM's "reflections", which are a concept-tag `GROUP BY … ORDER BY count` with a blacklist and
a strength threshold (`dreaming-phases.ts:1200`); deep's ranking and gates; the provenance
taint gate; snippet rehydration from live files; the rewrite validator; the cron.

Model, 1 — **consolidation subagent** (`dreaming-consolidation.ts:620`), deep phase only. Gets
the gated candidates plus the current `MEMORY.md`, returns a rewritten file. The only model
output that reaches durable memory, and it is boxed on both sides: it does not choose what it
sees, cannot drop more than `maxPriorEntryLossFraction` (0.25) of prior entries, cannot
promote anything lacking a `Source: path#Lx-Ly` reference, cannot exceed the file budget, and
is discarded wholesale for a deterministic append-only fallback if the validator says no
(`dreaming-consolidation.ts:225-302`). The previous file is stored as a preimage in SQLite
first.

Model, 2 — **diary narrative subagent** (`dreaming-narrative.ts:241`), once per phase,
best-effort, writes `DREAMS.md`. Explicitly not a promotion source; on failure it falls back
to generic text so the diary "never leaks staging content".

**The transferable part is the sandwich**: deterministic filter in front, model confined to
the one job models are good at (merging a new fact into existing prose without duplicating or
contradicting it), mechanical validator behind, non-LLM fallback if it fails. Nothing that
decides _what becomes memory_ is a model, which is why `memory promote-explain "router vlan"`
can answer why something did or didn't qualify.

## Where that leaves us

| capability                        | them                                  | us                                       |
| --------------------------------- | ------------------------------------- | ---------------------------------------- |
| conversations indexed             | opt-in-ish, per workspace             | all Matrix/SPA sessions, always          |
| repo/notes indexed                | workspace files                       | haku-state at the indexed tip            |
| context carried across a reset    | `session-memory` hook writes a file   | `recent_messages` in the session prompt  |
| told to search                    | tool description + `## Memory Recall` | same, as of #4072                        |
| staleness visible to the agent    | none                                  | `index_status`, and it rides on a search |
| consolidation into a durable file | dreaming                              | **nothing**                              |

Decisions taken (operator, 2026-08-15): no dreaming; drop the `tools/semsearch/` line in
haku-state; ship the prompting, which was the whole gap that mattered.

## If we ever revisit consolidation

The prerequisite is the signal, not the phases. Our `search` records nothing about what it
returned, so we could not rank candidates the way deep does — and dreaming without that signal
degrades into "a model summarizes recent chat nightly", which is the version that writes
hallucinations into permanent memory.

The cheap first step is a recall log: `(chunk_id, query_hash, score, searched_at)` on every
hit we return. Small table, no schema drama, and independently useful — it is also the only
way to answer "is the index any good", which we currently cannot. Everything else (scoring,
gates, a consolidation step) is downstream of having it, and can wait until there is enough
data to say whether the ranking would mean anything.

Do not copy the outcome-blindness if we do build it. Knowing which retrieved span actually
fed a good answer is the strictly better signal; it is also a research project, not a cron
job, so the honest options are "engagement proxy, labelled as such" or "not yet".
