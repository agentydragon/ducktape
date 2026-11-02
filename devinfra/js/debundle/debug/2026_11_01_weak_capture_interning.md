# Capture interner ownership

## Retention and change

The executor's capture-context and callback-set tables owned every interned
description strongly. Retiring a statement's invocation memos could therefore
leave its otherwise unused descriptions, nested captures and value sets alive
until chunk completion. This follows from the ownership graph; no memory or
timing measurement is claimed.

Both tables now index weak references by structural hash and check exact
equality within each bucket. Values and live memo keys own their descriptions.
The numeric identity counter is independent of table size, so deleting a dead
entry cannot recycle an identity still named by another cache or capability.
Descriptions with live owners retain their canonical identity. Owner-boundary
cleanup checks at most 64 hash buckets per table per call, in a rotating sweep;
ordinary lookup also removes expired entries from its bucket. Description
payloads drop with their last strong owner, independently of that cleanup.

Audited-result revocations previously stored only a numeric identity. They now
also retain the revoked description strongly. Otherwise an equal description
created after reclamation could receive a new identity and appear unrevoked.
This preserves the existing conservative sharing of revocation between equal
capture sets; the interner still proves no runtime equality, freshness or
privacy.

Lexical cells still retain their payloads until chunk completion. Captures
reachable through those cells, escaped callbacks, heap properties, accumulated
values, active frames or revoked capabilities remain live. F16's root trace is
still required for reclaiming unreachable cell graphs, including cycles.

## Review

Reviewed all interner consumers, capture equality/hash consistency, collision
checks, invocation memo keys, monotonic identities and numeric-only semantic
roots. This changes internal ownership only. Source execution, effect rules,
premises and serialized formats are unchanged. No tests, builds, corpus jobs,
profiling or benchmarks were added/run locally; normal hosted PR CI supplies
existing checks. No aggregate memory bound or canonical gate acceptance is
established by this change.

## F19: source positions across chunks

The program-execution audit found that `prepare_js_chunks` parses chunks in
separate source maps, while `reparse_unretained_entries` later reparses discarded
ASTs. `analysis_codec` explicitly relies on chunk-local byte positions. Also,
`ParsedJsModule::source_text` and source inspection select the first source-map
file, and line-index construction visits every file in a map. Changing only the
initial parser to share a source map would leave the later parse path and file
ownership inconsistent, and repeated line indexes could retain every file's
lines per chunk.

Before executing foreign bodies, qualify callable, allocation and owner origins
across the entire prepared program, including unretained entries and stored
analysis. Keep source-file ownership and line indexes explicit. Imported
capability summaries currently supply no executable foreign closure environment
or shared initialization/cache state. Raw donor offsets must never be looked up
in an importer's execution index. This is required program semantics and does
not justify a bundler-shape certificate or an extra author premise.
