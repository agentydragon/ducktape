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
own source cells. Missing runtime source definitions use conservative refusal.

The parsed-source invariant tests prepare two sources with the same byte
positions. They check disjoint heap entries and closure captures, rejected
foreign index lookups, qualified effect retention, registry scope reuse, and
fresh standalone scopes. The existing repeated-literal invariant verifies that
re-evaluation does not restore writability.

Program invocation still needs the matching adopter work: choose provider
source inputs before executing its body, bank source-local module binding and
control state, and qualify source-unit/binding effect provenance before merging
it into another module's outcome. `SourceUnitOrdinal`, source `Id`, declaration
coverage receipts and effect-report spans are not runtime allocation addresses.
In particular, the conservative final union of invoked callable heap summaries
must select each key's source inputs, including probe outcomes that were not
otherwise absorbed. A source scope by itself does not solve those obligations.
