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
or `const` declarations initialized with nonnegative safe integers, parameterless functions with a
single `target += source` or `return binding` statement, calls to those functions, and
`globalThis.record(binding)`. It also recognizes a closure factory shaped like
`function make() { let local = seed; return () => ++local; }`, a top-level
`let` or `const` initialized by `make()`, and a no-argument wrapper returning a
call to that closure. Initialization may call the wrapper or record its return.
Parameterless exported functions with a single `throw` of a nonnegative safe
integer are also supported after successful initialization. A throw terminates
the observed call sequence, and the certificate proves matching thrown values.
Top-level calls to such functions remain unsupported. Exported functions may
also take numeric parameters and return expressions composed of those
parameters, readable module bindings, nonnegative integer literals, `+`, `-`,
`*`, `%`, `/` when its result is an integer, and `&`, `|`, `^` when their
signed results are nonnegative, `>>>`, and `<<` and `>>` when their
signed results are nonnegative, numeric conditional expressions,
and numeric `&&` and `||` expressions, and calls to modeled top-level functions with matching
numeric arguments. The checker compares each called body with its declaration;
recursive calls remain unsupported. Function bodies may introduce initialized local `let`
or `const` bindings, mutate local `let` bindings and parameters using `=`, `+=`,
`-=`, `*=`, `%=`, `/=`, `&=`, `|=`, `^=`, `>>>=`, `<<=`, `>>=`, `++` or `--`, and use `if`/`else` when both branches end in a supported return
or throw. A no-`else` `if` may return or throw early before the remaining
statements. Conditions may use numeric `===`, `!==`, `==`, `!=`, `<`, `<=`, `>` and `>=`, boolean literals, `!`,
`&&`, and `||`, including short-circuiting calls. Comparison and logical
results are supported only as conditions, not as returned boolean values.
Guards conservatively reject references to lexical declarations later in the same
block, including behind subsequent guards. Supported thrown
values include safe-integer literals (including negative zero), the constant zero-divisions
`0 / 0`, `1 / 0`, and `-1 / 0`, BigInt literals, booleans, `null`,
`undefined` (written as `void 0`), and strings represented by their UTF-16 code units, including unpaired surrogates. Nonconstant
numeric throw expressions remain subject to the nonnegative safe-integer restriction. A throw stops the observed call sequence
while preserving its preceding trace. The checker conservatively rejects local
temporal-dead-zone reads.
Functions may declare and call nonescaping local functions that capture and
mutate surrounding numeric bindings. The checker rejects recursion, local
function identities that escape, and cases where a captured name or the
function itself could be shadowed at a call site.
Returning or throwing functions may use a terminal `try`/`catch`, with no catch
binding or an unused simple binding, optionally followed by `finally`. Both the
try and catch blocks must return or throw using supported statements. A nonterminal try and catch
may instead consist of supported assignments or updates, optionally ending
in a throw, before execution continues. The finalizer consists of
supported local assignments or updates; it runs after either completion, and
an exception from its expressions replaces the pending completion. Used catch
bindings and direct `return` or `throw` statements in the finalizer remain
unsupported.
Functions also support `for (let i = A; i < N; i++)` and inclusive `<=`
bounds and either `i++` or `i += 1`, with literals `A` and `N` from 0 through
8, at most eight iterations, and a block of supported local assignments or
updates. The body cannot write the loop variable. The frontend unrolls these bounded loops
into the shared expression semantics; dynamic bounds and other loop forms
return `unknown`.
Functions can also allocate nonescaping local object literals and arrays with
nonnegative numeric literal fields, then access existing properties through
static names or indices and immutable aliases. The frontend lowers each field
to a local cell in the shared Lean model. Allocation identity, escaping the
object, dynamic property access, missing fields, getters, setters, and later
allocations are unsupported. This lowering is part of the trusted frontend, not
a formal proof of the full ECMAScript object model.
For these functions the certificate covers finite sequences of calls with
explicit argument lists. Calls must supply exactly the declared number of
nonnegative safe-integer arguments; intermediate results must stay in range.
The model treats subtraction yielding a negative intermediate as unevaluable,
so its natural-number subtraction never silently replaces a negative result
with zero.

The original incrementing factory requires its captured variable to have a
different name from `seed`. The shared Lean lexical lookup treats a nearer
uninitialized binding as a temporal-dead-zone reference rather than falling
through to an outer one. The checker also supports factories with numeric
parameters and initialized lexical locals that return a parameterized arrow,
an anonymous function expression, or a named local function or const holding
one of those expressions. The returned function can mutate captured `let` bindings and parameters through
the supported numeric expression grammar. Top-level `const` declarations call
these factories with numeric literals; each call receives independent captured
cells. A chain of top-level `const` aliases can name one such closure, and
calls through any export share its cells. Every alias must read an already
initialized target; cyclic aliases and unsupported targets are rejected. Reads of
later or uninitialized factory locals and writes to captured `const` bindings
are rejected. Arbitrary escaping and other closure shapes remain unsupported
by the proof. The generator can also emit candidate splits
for some synchronous nested functions, parameters, blocks and branches after
checking their lexical free references and rejecting imported writes. The
validator returns `unknown` for those candidates; generation is not a proof.
Top-level `var` declarations with one nonnegative numeric initializer are
supported. Direct calls to `globalThis.record` observe `undefined` before the
initializer; numeric uses before initialization remain outside the model.
Function bodies may also start with initialized `var` declarations when their
initializers do not read a not-yet-initialized local and their names do not
redeclare parameters. Nested or later function-scoped `var` declarations remain
unsupported.
Discarded string, number, boolean and null literals are modeled as no-ops.
The same holds for `void`, `!`, and `typeof` applied directly to those literals.
Direct `globalThis.record` calls with string literals produce UTF-16 code-unit
observations, including unpaired surrogates; initialization-order counterexamples
include those strings.
Calls with boolean, null, or safe-integer literals, including negative zero, also produce
observations and can appear in initialization-order witnesses. A top-level
`globalThis.record(compute(...))` also supports modeled numeric expression
functions when the argument count matches and every argument is an integer
literal from 0 through 8, as well as the supported counter-closure calls.
The constant forms `record(0 / 0)`, `record(1 / 0)`, and `record(-1 / 0)`
record `NaN`, positive infinity, and negative infinity distinctly.
BigInt literals, including negated literals, can also be recorded; their
observations remain distinct from numeric observations.
`globalThis.record(void 0)` and `void` of another primitive literal record
`undefined` without reading a binding.
This includes `"use strict"`: all source and emitted files are ECMAScript
modules, which are already strict. Discarded RegExp and BigInt literals remain unsupported.
Public exports must be functions. The entry module may rename an exported
function, including under multiple public names and captured closures. A module assignment may set `aliasImports: true` to import dependencies through
generated public aliases while retaining their original local names. A non-entry
module may relay bindings using `reexports: { "dependency.mjs": ["binding"] }`
alongside that dependency in `imports`. Chains are resolved to their original
declarations with a bounded search; missing owners and cycles are refused.
Generated import aliases can travel through those reexport chains. A relay may
set `renameReexports: true` to export generated names; later relays and imports
follow those names back to their original declarations.
A module may also set `renameLocalImports: true` to rename imported bindings
used by simple readers, top-level records and calls, local addition updates,
and pure numeric expressions with numeric parameters. Other renamed local uses currently
return `unknown` from generation.
Source declarations have unique names, function
writes must target a mutable local binding, and each module preserves its assigned
statements' source order. Unsupported syntax, linking, cycles, or uninitialized
reads return `unknown`.

The Acorn frontend converts source and actual outputs into the same small AST.
Lean checks that each emitted statement matches its assigned source statement,
that imports resolve to the correct exported declarations, and that the entry
exports match the source, including their public-to-declaration name mappings.
For every reference it also computes the actual
output owner and its source statement index, and checks that the resolved
declaration equals the source-side declaration. For the original graph check,
the axiom-free general theorem `accepted_reference_owner` proves this for every
reference. For this restricted renamed-local-import subset, a separate axiom-free certificate
resolves each output statement to its source declaration, checks initialization
and public mappings, and proves equal observations for all later calls. Update
targets must be mutable declarations in the updating module.
These are checked sufficient conditions on the restricted AST,
not a proof of the Acorn conversion or ECMAScript's linking rules. The module
scheduler and the shared execution state then model initialization and future
exported calls. Module bindings and captured cells occupy separate stores in
that state, so private cell keys do not become global bindings. Each
recognized closure instance has private modeled cells initialized when its
factory is called; closure identity, arbitrary allocation and escaping beyond
the accepted calls are excluded. The shared captured-frame evaluator has an
axiom-free lemma relating equal captured values under different cell keys when
the globals are the same. Captured-frame writeback checks that reading the cells
reconstructs the updated frame, and an axiom-free lemma relates successful
writebacks of the same frame under different cell keys. A further one-call
lemma connects evaluation and writeback to the machine's return and throw
observations, assuming both writebacks succeed. A finite-sequence lemma relates
interleaved calls to two disjoint captured environments under different cell
keys when both machine runs complete. For exports backed by captured closures, the
validator also tries to include this relation in the generated certificate,
using the actual initialized source and output states; it includes the theorem
only when Lean verifies all its obligations without axioms. This supplementary
theorem relates exported-call runs when both runs complete. Lean checks that
the actual callable tables pair exported names, parameter lists and bodies,
while allowing different capture keys. A further theorem constructs a matching
run from one successful run if writebacks agree for every related state. Equal
loaded values alone do not establish that condition: different alias patterns
can make checked writeback succeed on only one side. An axiom-free writeback
lemma now proves that, for related loaded environments with distinct captured
keys, successful writeback on either side implies successful writeback on the
other. The generated two-closure simulation certificate checks distinct keys
and derives writeback agreement for all related states without assuming it.
When every export is a captured closure, the validator can use the relation for
any finite number of independently captured closures, including aliases of
the same closure, to establish
acceptance: it checks the callable tables, distinct and disjoint cells, and
related initialized states, then proves equal observations for any finite call
sequence. Its output execution allocates modeled captured cells in a separate
namespace, after the checker has compared the extracted source and output
statements. The generated certificate therefore exercises different private
keys for the actual split. Other cases still require equal initialized states and callable tables.
The frontend uses a factory-specific representation. Different closures that
share only some captured cells, and arbitrary escaping closures, are not yet
covered by the relational acceptance proof. A safe
result proves equal modeled initialization and matching return values and
state for any finite sequence of subsequent calls. Function return values are
modeled separately from host-recorded events; ignored top-level call results
are not included in the return trace. An unsafe result includes a Lean proof of different modeled observations during
initialization or later calls, and a Node witness. When initialization traces
match, the validator also searches up to 64 sequences of one or two exported
calls, comparing return prefixes and supported primitive thrown values as well
as recorded events. Before certifying an unsafe witness, the checker also requires
conservative numeric bounds for both extracted programs, including initialization
calls; failure to establish those bounds returns `unknown`. This range analysis is
implemented in the frontend and is part of the trusted boundary, not a Lean proof.
For redirected calls, the checker queries the Lean resolver for a witness call
graph and bounds the reachable functions; incomplete graphs and captured
closures fall back to a whole-program bound.
Semantically equivalent
rewrites beyond this AST subset can return `unknown`. Counterexample lowering
resolves actual imports and public exports, keeps local identities distinct, and
follows calls with bounded expansion. It can return `unknown` for duplicate
output declaration names, redirected captured environments, observed missing
numeric arguments, and other forms it cannot lower faithfully.

This result assumes correct Acorn conversion and correspondence between the
model and ECMAScript. Numeric bindings, exported-function arguments and modeled
numeric expression intermediates must remain nonnegative safe integers excluding
negative zero. Independently recorded integer literals may be negative or negative zero;
the three constant zero-divisions above are also supported when recorded or thrown. Other remainder and division
divisors must be nonzero, division results must be integers, and signed bitwise
results must be nonnegative;
`globalThis.record` must be stable and return normally without reentry or
asynchronous effects, and module loading and linking must succeed.
Loading, reflection, source text and module identity are not observed. The JSON
certificate can be replayed by concatenating `Semantics.lean`, `Effects.lean`,
`Bindings.lean`, and the certificate and running `lean --trust=0 --stdin`.

## Differential fuzzer

To measure coverage on trusted JavaScript files from the repository, run
`node x/js_module_proof/real_corpus.mjs OUTPUT_DIR SOURCE...`. It tries a
single-module baseline plus alternating and contiguous two-module assignments for each file,
reports proved, unsafe and unknown counts, and keeps assignments, output files
and verdicts under the output directory. These are candidate assignments, not
an exhaustive survey; the validator may execute recognized programs when
seeking an unsafe witness.

The [fuzzer follow-ups](TODO.md) track broader JavaScript generation, split
coverage, oracle improvements, and extensions to the proof-supported subset.

`fuzz.mjs` constructs seeded ESTree programs and module assignments, invokes the
actual multiple-bindings splitter, and executes the source and generated entry
in separate fresh Node processes. It compares recorded events, public exports,
return values and exceptions after a generated sequence of exported calls. It
also runs the Lean-backed validator and checks that a proved result agrees with
Node and that an unsafe result's witness matches the independently observed
initialization. Unsafe witnesses include a Lean certificate of the exact Node
trace and its difference, including later argument-bearing calls when supported.
Some trials mutate generated modules to exercise the validator;
mutations cover binding initializers, function bodies, recorded effects,
exports and import order, with separate counts for each kind. Coverage records
renamed and multiply exported declarations, requested import aliases, and how
many generated import aliases, top-level `var`, and local `var` cases were
compared and proved.
Some trials generate supported parameterized captured closures or counter
closures and check safe and unsafe
splits against Node and Lean. Other trials generate programs with nested,
escaping closures, parameters,
shadowed bindings, captured writes, bounded loops and branches, plus cyclic
graphs or invalid assignments. Separate generated cases exercise object and
array allocation, indexed property reads and mutation through aliases in Node
and the Lean-backed checker within that restricted local-data subset.
Parameterized programs also compose bounded numeric expression trees from
visible parameters, literals, addition and multiplication; the campaign
summary reports their generated depths.
Some nested closure cases also execute a `try`/`finally` block in Node and
check that its finalizer runs once per call. The proof frontend currently
refuses these cases; the summary reports their Node executions separately.
More general closure programs are executed independently in Node; when candidate
generation succeeds, the fuzzer also compares the emitted modules. The summary
reports generated and compared closure cases separately from refusals. It
separates refusals of cases designated with invalid assignments from refusals on
broader-syntax cases, and records validator verdicts for mutated and unmodified
output independently. A broader-syntax case can also have an invalid split, so
its refusal does not by itself identify the cause.
The summary counts cases containing each ESTree node type and groups cases by
module and import counts, including cases that are later refused. It also counts
the actual generation refusal messages, separate from requested split categories.
Use `--splits independent` to repartition generated statements independently of
their grammar and sample acyclic graphs, cycles, and deliberately duplicated
statement assignments, some ordered partitions with imports of every earlier
module, and independent choices of import aliases. Some labeled cases deliberately
place an early `var` observation after its initializer, so the fuzzer checks a
real unsafe ordering. The summary records requested categories, ordered
partitions, and refusals;
an acyclic graph can still have missing dependencies or other invalid links.
Use `--grammar composed` to generate bounded expression trees and nested
returning branches and catch clauses from visible lexical names, with
independently generated function bodies. It can be combined with either split mode; it currently covers only a subset of the planned grammar.
`--grammar async` generates promise-based functions and observes each call to
completion in Node. Pending promises and timeouts are inconclusive. Async
execution is not yet part of the Lean proof-supported subset.
Use `--grammar alias` to generate simple readers, initialization observations,
local updates, and pure numeric expressions with renamed local imports,
varying reexport chains and partitions. Some cases redirect an import and are
checked as Node-reproduced unsafe witnesses, during initialization and later
calls; replay with `--mutations disabled` to check the corresponding unchanged split.

```sh
nix develop .#js-module-proof
node x/js_module_proof/fuzz.mjs --seed 123 --cases 1000 --output-dir "$OUTPUT_DIR"
```

The Bazel fuzzer test defaults to 128 bulk cases across its shards. For a
larger deterministic Node-focused campaign, set a multiple of 64, for example
`bbr test //x/js_module_proof:fuzz_test --test_env=JS_MODULE_PROOF_FUZZ_CASES=512`.
The test still runs its fixed Lean-backed cases; the additional bulk cases use
sparse independent validation sampling by default. Set
`--test_env=JS_MODULE_PROOF_FUZZ_VALIDATE_EVERY=16` to sample about one in 16
generated cases for Lean validation. Bulk summaries and retained differences
are written to the Bazel undeclared outputs for each shard. Summary entries'
`artifactPath` values are relative to the campaign output directory.

The seed and case index determine each case independently; `--start N --cases 1`
replays one case. `--shard I --shards N` selects the indices congruent to I
modulo N, so independent workers cover the same range without overlap.
Failure artifacts include source, assignment, generated files,
Node observations and, if available, the validator verdict. Unexpected failures
also include a phase and error record. Cases with a Node difference and an unknown or unvalidated verdict are retained
for investigation. Certified unsafe cases are retained with their Lean verdict and
independently reproduced Node witness; `certifiedWitnesses` lists their relative
artifact paths and calls. Other non-failing cases are removed. Set
`--shrink-unsafe-attempts N` to try at most N reductions of a certified unsafe
case; the retained `minimized/` directory includes the resulting verdict and a
freshly reproduced Node witness. A split that
is proved but differs in Node, whether mutated or unmodified, is reduced by removing statements, calls, empty
modules and imports while preserving that failure, with the result saved under
`minimized/`. The fuzzer keeps files only inside the chosen output directory.
Lean checking is expensive: `--validate-every N` selects a reproducible
approximately 1/N sample for validation while still splitting and running all
supported cases in Node. Use `--mutations disabled` to replay the same generated
inputs and splits without deliberate output changes. The summary distinguishes unvalidated cases, unknowns,
proved results, unsafe witnesses and Node differences. A Node difference in an
unvalidated or unknown case is useful evidence, not itself a validator error.
The oracle records primitive returns, thrown values and exception names, and
retains the trace prefix before a throw. It distinguishes `NaN`, infinities,
negative zero and big integers. Timeouts, output limits, and opaque object
observations are inconclusive, with artifacts retained; object identity and
exception messages are not compared.

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
