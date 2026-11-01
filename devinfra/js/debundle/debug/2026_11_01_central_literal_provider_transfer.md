# Central literal transfers for strict provider summaries

## Bounded migrated slice

The canonical execution index identifies a simple-parameter function whose
complete body is one return expression. The existing recursive ValueExpr
lowering retains syntax coverage before any information can be erased. Only
input references, primitives and static ordinary record/array literals enter
the migrated query. A failed query does not retry the unmigrated evaluator.

The same allocation_transfer kernel now serves ordinary Evaluate transfers and
the strict provider query. It owns primitive/reference/literal evaluation and
the real literal heap merging. Application binding resolution keeps its existing
semantics but contributes opaque access evidence. Application children continue
through RuntimeOperation::Evaluate; strict children call the same kernel and
decline unsupported operations. Neither path has a separate literal evaluator.
Spread/holey arrays and exact descriptor/spread models remain application
operations, while their dense-array heap merging uses the shared implementation.

The strict query supplies scoped opaque parameters and a real empty heap. Its
symbolic scope identifies query ownership, not a stable provider or runtime
allocation. Symbolic values prove no kind, native identity or member behavior;
they never enter importer state. The result is consumed into the existing
ForeignReturn and retained argument positions. Importers retain actual live
Values through the opaque carrier; no provider allocation ID crosses chunks.

## Certificate boundary

CoveredAllocationOnly requires no external access, write, publication,
invocation or throwing operation. Root source coverage composes with binding
access evidence and every child judgment. RuntimeResult carries the judgment
and dependencies even when a parent consumes only Values. Non-Evaluate and
unsupported transfers are opaque. Current application judgments stay opaque
without whole-source coverage; this is not a general execution effect ledger.
The initial strict consumer uses no external premise and declines unexpected
nonempty dependency sets rather than losing their conditions.

Source coverage rejects extra statements, defaults/rest/destructuring,
async/generator/decorator forms, operators, sequence/assignment/template/
conditional expressions, member reads, computed/spread/accessor/prototype
properties, calls, throws and control flow. Source coverage is collected in the
existing lowering traversal. It is not inferred from an empty SynchronousCalls
set or Outcome.complete. Local assignment pre-evaluation remains outside this
slice and cannot establish source-order evaluation.

The old entry is explicitly named derive_unmigrated_foreign_call. Its literal
arms remain necessary for multi-statement/local-assignment/class bodies, and
its source proof remains responsible for those unmigrated cases. The overlapping
direct argument-return standalone assertion moved to existing CLI coverage.

## Controls

CLI migration controls preserve runtime output and retained input positions for
records, dense arrays and nested literals. Conservative controls check observed
global writes and absence of summaries for erased or uncovered operations.
These controls are not claimed as baseline-failing regressions. A focused
canonical-key unit control proves both function and const-arrow declarations
reach the new query without legacy fallback; initializer export publication
restrictions remain independent.

The initial focused CLI check passed in BuildBuddy invocation
`a2ba48c3-3c04-486f-8b88-7f5e79888b1b`; it preceded final API naming and the
canonical-key unit control. No corpus acceptance improvement or general
provider reachability coverage is claimed. Namespace member identity and wider
provider/global effects require subsequent work.
