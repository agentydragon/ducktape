# JavaScript module-splitting proof experiment

This spike has narrow ES module subsets for a counter with shared live bindings,
synchronous observable effects, a combined initialization example, and a
statement-based subset with multiple bindings. Names and module filenames come
from the input and assignment. Lean checks the extracted model of the generated
files. Unsupported syntax or assignments return `unknown`.

```sh
nix develop .#js-module-proof
node x/js_module_proof/main.mjs generate x/js_module_proof/example/original.mjs x/js_module_proof/example/assignment.json "$OUTPUT_DIR"
node x/js_module_proof/main.mjs validate x/js_module_proof/example/original.mjs x/js_module_proof/example/assignment.json "$OUTPUT_DIR"
```

Choose a new empty output directory; generation refuses to overwrite files.
The Nix shell supplies Lean, Node, and the same Acorn version pinned by the
repository's npm lockfile. When Lean cannot prove the extracted graphs equal,
the validator searches for a short witness by running the recognized programs
in separate Node processes.

## Effect ordering

The [effect example](example/effects/original.mjs) has a
[safe assignment](example/effects/safe.json) and an
[unsafe assignment](example/effects/unsafe.json). Use either assignment with
the same `generate` and `validate` commands above and a fresh output directory.
`statements` contains zero-based source-statement indices, and `imports` lists
static side-effect imports in source order. Every source statement must be
assigned exactly once; all declared modules must be reachable from `entry`.

This mode accepts one to twelve source statements of the form
`globalThis.record("event")`, up to twelve modules, and static side-effect
imports between those modules. It does not combine effects with counter code.
Cycles, unreachable modules, dynamic imports, top-level await, other statements,
and changes that do not match the assignment return `unknown`.

`Effects.lean` computes the acyclic module graph's trace with a small-step
machine with an explicit task stack and active/completed module sets. The
counter, effects, and combined subsets use this scheduler and the execution
state and transition function in `Semantics.lean`. The machine's correspondence to ECMAScript remains
an assumption. The checker also checks the assignment. For a safe
split, its theorem covers every sequence of synchronous host returns and thrown
values: a throw stops subsequent events and preserves the thrown value. For an
unsafe split, the result includes an independently checkable Lean certificate
of the output trace and its difference from the source, plus a concrete Node
witness. When the trace can be checked independently of the assignment, it is
also returned for unsafe and assignment-mismatch `unknown` results.

This guarantee assumes `globalThis.record` is a stable ordinary data-property
function, the same initial host state and responses to the same ordered call
history, synchronous terminating-or-throwing calls without reentry or
asynchronous effects, and successful ordinary module loading and linking with
no intervening file changes. It considers one fresh evaluation and excludes
observations of module loading, source text, stack traces, and reflection.
Acorn extraction and the model's correspondence to ECMAScript are trusted.

## Combined initialization

The [combined example](example/combined/original.mjs) records a mutable cell
during initialization and exports a function that reads it later. Use its
[safe](example/combined/safe.json) or [unsafe](example/combined/unsafe.json)
assignment with the same CLI commands. Indices assign every non-export source
statement, in source order within each module. The supported shape has one
cell, one increment function and call, one record call, and one exported reader;
the cell and increment function belong to the state module and the reader
function to the reader module. The entry imports both modules.

The validator parses each output and compares its AST to the shape generated
from the assignment; arbitrary rewritten modules are not yet supported. Lean
checks the assignment and runs both source and output through the same state
transitions. For the safe split, the certificate proves equal initialization
states and equal observations for any finite number of subsequent reader calls.
For the unsafe split, Lean proves the modeled states differ and Node confirms
that the recorded value is 0 instead of 1, although the final cell is 1 in both.
This mode assumes the record function returns normally, in addition to the
host, parser and module semantics assumptions above.

The counter-only frontend retains a specialized binding and linking analysis.
Its accepted module shape is fixed; it maps initialization through the shared
scheduler and projects the shared execution state into its counter proof. The
three modes are restricted frontends to a shared model, not independent proofs
of that model's correctness.

## Multiple bindings

The [multiple-binding example](example/bindings/original.mjs) has a
[safe](example/bindings/safe.json) and an [unsafe](example/bindings/unsafe.json)
assignment. `statements` are zero-based indices of non-export statements, and
`imports` lists module dependencies used by the generator. Lean checks the
actual output imports and reexports, rather than assuming those in the spec.

This subset supports up to 20 statements and 12 modules: `let` declarations
initialized with nonnegative safe integers, parameterless functions with a
single `target += source` or `return binding` statement, calls to those functions, and
`globalThis.record(binding)`. Public exports must be functions. Imports and
exports use unaliased names, source declarations have unique names, function
writes must target a local binding, and each module preserves its assigned
statements' source order. Unsupported syntax, linking, cycles, or uninitialized
reads return `unknown`.

The Acorn frontend converts source and actual outputs into the same small AST.
Lean checks that each emitted statement matches its assigned source statement,
that imports resolve to the correct exported declarations, and that the entry
exports match the source. For every reference it also computes the actual
output owner and its source statement index, and checks that the resolved
declaration equals the source-side declaration. The axiom-free general theorem
`accepted_reference_owner` proves that this holds for every reference in any
accepted graph. This is a checked sufficient condition on the restricted AST,
not a proof of the Acorn conversion or ECMAScript's linking rules. The module
scheduler and the shared named-cell
execution state then model initialization and future exported calls. A safe
result proves equal modeled initialization and matching return values and
state for any finite sequence of subsequent calls. Function return values are
modeled separately from host-recorded events; ignored top-level call results
are not included in the return trace. An unsafe result includes a Lean proof of
different modeled initialization and a Node witness. Semantically equivalent
rewrites beyond this AST subset can return `unknown`.

This result assumes correct Acorn conversion and correspondence between the
model and ECMAScript. All values and intermediate sums must remain nonnegative
safe integers, `globalThis.record` must be stable and return normally without
reentry or asynchronous effects, and module loading and linking must succeed.
Loading, reflection, source text and module identity are not observed. The JSON
certificate can be replayed by concatenating `Semantics.lean`, `Effects.lean`,
`Bindings.lean`, and the certificate and running `lean --trust=0 --stdin`.

## Differential fuzzer

The [fuzzer follow-ups](TODO.md) track broader JavaScript generation, split
coverage, oracle improvements, and extensions to the proof-supported subset.

`fuzz.mjs` constructs seeded ESTree programs and module assignments, invokes the
actual multiple-bindings splitter, and executes the source and generated entry
in separate fresh Node processes. It compares recorded events, public exports,
return values and exceptions after a generated sequence of exported calls. It
also runs the Lean-backed validator and checks that a proved result agrees with
Node and that an unsafe result's witness matches the independently observed
initialization. Some trials mutate generated modules to exercise the validator;
other trials generate programs with nested, escaping closures, parameters,
shadowed bindings, captured writes, bounded loops and branches, plus cyclic
graphs or invalid assignments.
Closure programs are independently executed in Node even when the splitter
refuses them.

```sh
nix develop .#js-module-proof
node x/js_module_proof/fuzz.mjs --seed 123 --cases 1000 --output-dir "$OUTPUT_DIR"
```

The seed and case index determine each case independently; `--start N --cases 1`
replays one case. Failure artifacts include source, assignment, generated files,
Node observations and, if available, the validator verdict. Cases with a Node difference and an unknown or unvalidated verdict are retained
for investigation; other non-failing cases are removed. The fuzzer keeps files only inside the chosen output directory.
Lean checking is expensive: `--validate-every N` selects a reproducible
approximately 1/N sample for validation while still splitting and running all
supported cases in Node. The summary distinguishes unvalidated cases, unknowns,
proved results, unsafe witnesses and Node differences. A Node difference in an
unvalidated or unknown case is useful evidence, not itself a validator error.

The generator covers a deliberately small, extensible ESTree grammar: integer
bindings, functions, calls, records and exports, and a separate scope-aware
generator for nested closures and bounded control flow outside the currently
supported splitter subset. It varies values, statement orders, effect placement, module
assignments, graph edges and public call sequences; it does not cover arbitrary
JavaScript or establish the JavaScript semantics assumed by the proof. Values
and call counts are bounded to stay within the safe-integer assumption. Node
runs are empirical checks, not a second proof of all executions.

## What the certificate says

The Acorn frontend converts the supported syntax into `SourceAst` and
`OutputAst` records. Lean's `analyzeSource` and `analyzeOutput` check exports,
imports and binding names and construct graphs; Lean then resolves aliases,
classifies reads and writes, and compares the resulting counter programs.
Initialization takes a link step followed by an initialization step scheduled
by the shared machine; calls read and update modeled cells using the shared
execution transitions. The snapshot field is ghost state when no snapshot exists.
For each accepted input/output pair, the certificate checks that initialization
matches the frontend model and proves matching operational steps and complete
finite call sequences. Generic Lean theorems establish initialization and
matching call steps for every source/output AST pair accepted with the same
program. The output frontend enforces agreement with the direct model and
initialization as acceptance checks. The certificate contains the actual AST
records. Lean checks
it with trust level zero, and the checker requires Lean to report that the
resolution and equivalence theorems have no axioms. Source and output hashes
are comments identifying the files checked, not Lean-verified facts.
The JSON response contains the certificate text; concatenate `Semantics.lean`,
`Effects.lean`, `Counter.lean`, and then the certificate, and run
`lean --trust=0 --stdin` to check it independently.

The claim about JavaScript is **conditional on the unverified Acorn frontend
correctly converting the supported syntax into the Lean AST, and on the Lean
model matching ECMAScript module linking, initialization and binding behavior**.
There is no complete ECMAScript semantics or proof relating the model to one.
The model uses natural numbers. The reported JavaScript claim
limits increments to keep the counter within the exact integer range, assumes
calls only through the public entry point after initialization, and excludes
source reflection, module identity, loading and stack traces from observations.
Lean does not independently establish these assumptions or IEEE-754 arithmetic.

The frontend does not implement a general scope resolver or arbitrary
partitions. It accepts the cell and increment in one module, the reader in
another, and an optional immutable snapshot of the imported cell. For a
snapshot split the validator finds `increment(); current()` and reports
`unsafe` only after Node observes different results. Unsupported programs,
unresolved references and failed Lean invocations do not report success.

## End-to-end test

From a checkout containing the experiment, run:

```sh
bash x/js_module_proof/standalone/run-remote.sh
```

The script uses `bbr` and stages the experiment into a separate Bazel
workspace on the remote runner. Its pinned Node, Acorn, and Lean inputs are
declared in `standalone/`. The suite checks the CLI proof, unsupported input,
missing references and a missing Lean executable. An eight-case generated corpus
compares Node executions for renamed bindings, aliases, declaration ordering,
the safe-integer boundary, and a snapshot witness. The effect tests cover safe
and unsafe orderings, throws, shared dependencies, assignment mismatch and
cycles. The workspace uses Linux x86-64 Lean.

A separate generated corpus checks twelve four-module graphs, including
diamonds, reversed imports, repeated imports, and assignment mismatches. It
compares traces computed in Lean directly with actual Node execution, both
with normal host returns and with a throw at each event. It covers safe,
unsafe, and `unknown` verdicts. The combined test exercises generation and
validation of both splits, rechecks their Lean certificates, and rejects a
modified output.
The multiple-binding test exercises the safe and unsafe assignments, replays
their certificates, rejects wrong imports, changed bodies and missing statements,
and accepts another program with more declarations, functions and calls.
An additional 36-case sharded corpus constructs emitted files independently of
the generator and varies declaration and import order, values, initialization
order and malformed outputs. It compares proved cases and unsafe witnesses with
Node, including return values after calls, and checks that unsupported aliases,
cycles, temporal dead zone reads, and writes through imports are not proved
safe. For safe and unsafe cases, Lean also checks the concrete Node-observed
event and return traces directly.

BuildBuddy includes untracked files in its patch. Use a clean or sparse
checkout if the working checkout contains unrelated untracked material.

## Remaining work

- Relate the operational model to ECMAScript, including its module linking and
  initialization rules. The model's snapshot slot is ghost state when absent.
- Verify or validate the Acorn-to-Lean AST conversion and source-text parsing.
- Extend the supported language to cycles, reentrancy, and IEEE-754 arithmetic
  before claiming support for them.
