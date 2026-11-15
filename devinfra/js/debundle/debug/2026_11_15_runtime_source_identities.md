# Runtime source ownership

The execution domain previously reused `Value::Alloc(kind, u32)` from chunk
value flow. The integer is a byte position in one parse. Running provider code
in an importer's `World` would therefore merge unrelated records, callable
captures, class capabilities and descriptor maps when their sources have equal
positions. This is independent of the export-link resolution problem in
[cyclic export cells](2026_11_14_cyclic_export_cells.md).

The source domain remains `Value<u32>`. The runtime specializes the same value
lattice with a nonserialized `RuntimeAllocationId`: prepared source scope,
local origin, and an optional activation discriminator. The current transfer
retains the established pooled multiplicity for repeated evaluations at one
site. That pooling joins values and loses writability; it supplies neither
fresh-reference equality nor an initializer-completion certificate. Lexical
cells and captured environments retain their existing activation identities.

`ExecutionIndex` source maps check runtime ownership before accessing local
coordinates. Raw syntax coordinates remain source-local. The executable registry
retains the canonical index scope when the same module enters application
analysis; independent standalone preparations receive distinct scopes even if
module names match. Scope membership supplies no readiness or purity proof.

World object surfaces, dense arrays, native retention, closures, class state,
callable invocation keys and native commit plans use qualified addresses.
Descriptor-map handles also include their source scope. Source-derived facts
and persisted gate reports retain their existing local wire representation.

Runtime heap effects retain qualified allocation and binding cells through
invocation outcomes, memo storage and alternative joins. Projection to one
source's gate effects checks ownership. Foreign cells remain in the runtime
ledger and contribute an opaque external frontier instead of silently becoming
local integer cells. A source-owned record-factory heap proof only removes its
own source cells and opaque/external frontiers. Unaddressed frontier flags retain
their source scope, separately from the source receiving the local projection. Missing runtime source definitions use conservative refusal.

The parsed-source invariant tests prepare two sources with the same byte
positions. They check disjoint heap entries and closure captures, rejected
foreign index lookups, qualified effect retention, registry scope reuse, and
fresh standalone scopes. The existing repeated-literal invariant verifies that
re-evaluation does not restore writability.

Live binding effects now carry `RuntimeBindingId` and `RuntimeSourceUnitId`.
Both sides of a raw read/rebind projection must belong to the selected source;
a foreign observer's coincident local ordinal cannot become a local owner.
Potential uninitialized reads and first-assumed-path provenance remain in the
qualified ledger. An observed contribution clears only the matching scoped
binding's assumed tag. Invocation absorption, alternative joins, memo clones
and reentrant fallback merging retain the qualified facts. Source-local owner
lookups filter qualified executed owners before indexing lexical facts.

Finalization transports these ledgers in `SynchronousCalls`, including heap
frontiers and qualified executed owners. The saved source snapshot keeps its
wire format and restores `runtime_effects_available = false`. A portable
program-effect consumer must normalize live scopes to canonical source IDs
before saving, or retain conservative source facts when live effects are
unavailable. An empty restored runtime ledger is not an absence proof.

A separate fallback inventory found that bounded callback pools and raw
callback promotion filtered foreign callable handles through the active source
index. Those paths now preserve the foreign owner, source-derived reads and
rebinds, and an explicit opaque/external frontier. Deferred callback and class
promotion preserve the same facts. Reading foreign source metadata grants no
initialization readiness; fallback foreign reads remain potentially
uninitialized. Shared scheduling may later inspect the correct module bank to
refine these obligations. Source-local accessor-owner traversal must likewise
be interpreted within its bank; the qualified owner ledger preserves the
foreign contribution for the program consumer.

Program invocation still needs the matching adopter work: choose provider
source inputs before executing its body and bank source-local module binding,
reachability and control state. On return, project the compatibility binding
view to the active caller before raw rebind consumers run; project to the root
owner's lexical source at its final output. A shared scheduling owner is
retained across source switches; binding effects use that observer identity,
while executed-code owners remain lexical. Invocation memo signatures include
the scheduling owner so one root cannot replay another root's provenance.
The program consumer must resolve imported
binding aliases to live canonical cells, preserve source ownership of reports,
and enforce readiness against the planned schedule. `SourceUnitOrdinal`, source
`Id`, declaration coverage receipts and effect-report spans are not runtime
allocation addresses. A source scope by itself does not solve those obligations.

The remaining mutable-state bridge is bank-aware application of qualified
fallback rebinds. When a provider cell is observed at a caller's scheduling
owner, no checked raw pair can express both identities. The qualified ledger
retains that rebind; a caller must widen the owning module bank directly
rather than manufacture a local owner ordinal for `apply_rebind_effects`.

Portable completed-effect transport now maps runtime scopes to canonical source
module names before finalization returns. Allocation origins and source-unit
ordinals remain local coordinates under that module. Binding IDs retain their
SWC identity only in live facts, then use the existing `SavedBinding` context
ordinal codec at artifact save/restore. The saved ledger retains potential
uninitialized reads, first-path fallback reasons, heap frontiers, observed heap
accesses and executed owners. Its field is required in the saved format;
restoring an old artifact cannot create an empty completed ledger. Runtime
arenas remain unavailable after reload, while a successfully normalized
portable ledger retains its own availability. Unknown or ambiguous mappings
retain the known subset and set availability false with an opaque external
frontier. Activation tokens are never exported as source coordinates.

The runtime boundary's full CLI comparison passed 240 of 244 targets, including
canonical Excalidraw. All four failures predate the runtime migration: the exact
`604eaf84b2` base reproduces the imported primitive positive rejection and three
old pending/negative reexport assertions; cross-chunk operation parity is the
known shared-invocation work. Baseline invocation:
`25afd4dd-ebe3-40c8-8a32-44b7928dd728`; full boundary invocation:
`8c8f85b5-97d8-4887-9764-34a530df1c21`.

Root initializer and local body receipts now qualify their source module in
identity equality and the flat call-edge key. Saved root identities and local
body variants require the optional field to be present: plain derived Option
would otherwise deserialize a missing source field as None. The explicit
`deserialize_with` helper distinguishes an unnamed standalone query from an
older unqualified artifact. Reports retain the same qualification. A receipt
producer accepts None only within the active standalone source arena; a foreign
activation whose canonical name is unavailable loses coverage instead. Root,
local callable, constructor and array-callback receipt producers use that check.
Tests distinguish identical local offsets/owners in two named modules, refuse a
foreign root's receipt at the same span, and reject missing saved source fields.

Actual ordered literal and closure creation now instantiates a runtime allocation
event. Source indexes continue to address immutable definitions; flattened
metadata projections remain explicitly pooled. An object method captures the
actual open literal's home object, rather than its source site. Portable
allocation events use canonical scheduling owner, request path and occurrence,
never the runtime counter. Source/control and admission state are copy-on-write
so invocation replay can retain exhaustive unchanged-state witnesses, including
inactive module controls. These carriers precede transition replay; outcome-only
memoization remains the next required change.

Invocation memos now retain an exhaustive world witness, active call context,
COW control/admission state and an explicit post-state transition. Reuse checks
resources before applying the transition; it charges the recorded expansion
cost and preserves allocation/source coverage transfer judgments. One graph
mapper renames fresh allocation tokens, lexical cells and interned captures,
including module banks, descriptor payloads, native revocations, selections,
qualified/portable heap effects and fallback results. Canonical event paths
are rebased to the new request; external/input identities stay unchanged.
Parsed-source invariant tests exercise actual repeated transition reuse,
mutable dependency misses, resource refusal and fresh alias/capture replay.
Real CLI controls exercise repeated factories and second-call mutable observers.

The prepared cross-source execution path exposed an independent read-ledger
gap: imports are correctly absent from `ExecutionInputs.top_bindings`, but
ordinary kernel reads and source fallback promotion used that declaration set
to decide whether to record any module cell read. A transitive observer could
therefore obtain the live export value while omitting its ordering obligation.
The gate accepted source `undefined` versus emitted `function` in the existing
publication-order CLI regression, both before and after the allocation/replay
changes. Read recognition now includes imported bindings and resolves them to
the canonical module/default cell; it never creates a local declaration cell
for an import alias. The direct regression and its saved-analysis counterpart
retain the observer's source-qualified scheduling owner.

## Lossless bounded callback promotion

The shared-source path exposed two independent gaps in conservative callback
promotion. A bank-local `fallback_outcome` accumulator returned only its raw
binding projection to ordinary callers; qualified provider/default-cell reads,
heap effects, precision losses and executed owners could be stranded in another
bank. In addition, `retain_foreign_callback_effects` treated every foreign source
as unavailable after consulting its census, even when the prepared module bank
was present. Profiling the canonical input attributed over 80 percent of sampled
children to that whole-source fallback chain.

Bounded summaries and returned-callback promotion now carry complete `Outcome`
values through ordinary absorption. Cached source read sets are promoted again
for each actual scheduling owner; no per-bank accumulator or once-only returned
owner cache suppresses attribution. Prepared callbacks enter their defining
source bank for finite synchronous read/rebind closure and retained heap callback
traversal. Captured superclass values use ordinary class-member promotion. Only
an unavailable source retains the opaque external fallback; metadata lookup
still grants neither readiness nor purity.

Unsupported callable inputs also retain all actual arguments and captures in the
finite summary. The previous rest-parameter approximation assigned only the
first actual to its formal, dropping the other captured holder graphs before
callback-pool promotion. The existing seventy-holder CLI regression exercises a
safe extraction and a later-cell TDZ refusal through the same factory site.
The unhinted prepared registry control instead checks source/emitted Node parity
and a post-entry provider read: its actual body only stores the callback and
therefore proves deferred timing without an author hint.

The [host-setter initialization counterexample](2026_11_15_host_setter_initialization.md) records a concrete source/emitted divergence that native purity cannot waive.
