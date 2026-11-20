# ESMeta and module splitting: feasibility spike

Can an existing ECMAScript semantics justify the split checker's assumptions and
prove that an emitted ES module graph behaves like its source bundle? This spike
tried [ESMeta](https://github.com/es-meta/esmeta), which extracts an executable
semantics from ECMA-262. It does **not** prove such a theorem.

## Result

At ESMeta commit [`b4a787f2716523bf76d4a897068bbd5b1fc8ef2d`](https://github.com/es-meta/esmeta/tree/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d)
(version 0.8.2), with its pinned ECMA-262 submodule, the existing Lean renderer
accepted the checked-in example and emitted four modules. Node ran the bundle
and emitted entry with the same stdout. ESMeta evaluated a simple JavaScript
script, and its `Module` parser accepted the emitted entry, but its evaluator
failed to parse that entry as a script. No module evaluation or equivalence
proof was obtained. The example uses `console.log`, so it is not a host-free
semantics experiment; Node is a behavioral smoke test, not a proof.

This is an execution limitation, not just a missing command-line option:
ESMeta's [`Initialize.fromFile`](https://github.com/es-meta/esmeta/blob/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d/src/main/scala/esmeta/es/Initialize.scala#L23)
uses a Script parser, its [`RunJobs`](https://github.com/es-meta/esmeta/blob/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d/src/main/resources/manuals/algos/RunJobs.algo#L6)
invokes `ParseScript` and `ScriptEvaluation`, and its
[`HostLoadImportedModule`](https://github.com/es-meta/esmeta/blob/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d/src/main/resources/manuals/funcs/fallback/HostLoadImportedModule.ir)
is a no-op. Upstream [issue #372](https://github.com/es-meta/esmeta/issues/372)
tracks module-goal execution, missing module algorithms and a loader. Its
[Test262 filter](https://github.com/es-meta/esmeta/blob/b4a787f2716523bf76d4a897068bbd5b1fc8ef2d/src/main/scala/esmeta/test262/util/TestFilter.scala#L62)
excludes module tests. Parsing module syntax alone does not model loading,
linking, initialization or evaluation.

ESMeta is a Scala interpreter for extracted specification IR, not a Lean
semantics. Executing both programs in it, even after adding modules, would test
particular runs; it would not establish equivalence for all inputs. A proof
would also need a trustworthy bridge from its semantics into a proof assistant,
or a separate formal semantics with a justified relation to ESMeta. The
current `EsmSplit` theorem remains conditional on its abstract footprints and
module model; this spike does not discharge those conditions.

## Reproduce the probe

The following uses a scratch directory and the repository devshell. Build the
actual example renderer and run its output with Node:

```sh
OUT_DIR="${TMPDIR:-/tmp}/agents/$(date +%Y_%m_%d)/esmeta-split-example"
bb run //devinfra/js/debundle/x/split_proof/lean:render -- "$OUT_DIR"
node devinfra/js/debundle/x/split_proof/example/input/bundle.mjs
node "$OUT_DIR/main.mjs"
```

Both Node runs printed `<circle> 12.56636 true` and `area,label` on separate
lines. The renderer reported that all eight checks passed.

Build ESMeta with JDK 21 and sbt as described by its upstream README. Check out
the commit above and initialize its `ecma262` submodule. From the ESMeta root,
with `ESMETA_HOME` set to that directory and `esmeta` on `PATH`:

```sh
printf 'print(40 + 2);\n' > "$OUT_DIR/control.js"
esmeta eval "$OUT_DIR/control.js" -silent
esmeta parse "$OUT_DIR/main.mjs" -parse:goal=Module -silent
esmeta eval "$OUT_DIR/main.mjs" -silent
```

The first command exited 0 and printed `42.0f`; the second exited 0; the third
exited 1 with `No result when parsing failed` from `Initialize.fromFile`. The
`print` control is provided by ESMeta's host; it is only a basic interpreter
smoke test. The renderer's emitted modules were not executed by ESMeta.

## What a stronger result needs

A useful restricted theorem would quantify over supported source bundles,
assignments, initial states, and subsequent observable behavior, and say that
whenever the actual emitter accepts and emits a graph, the graph and bundle
have the same relevant behavior. Initially it could exclude renaming,
source-text inspection, dynamic imports, top-level await, and host-specific
calls. It must still address lexical bindings, live imports, module
initialization, cycles, exceptions and divergence, not only final values from
a single execution.

To use ESMeta for that theorem, first establish an executable module graph:
implement or obtain module records, a deterministic loader for a closed graph,
linking and evaluation, and run appropriate Test262 module cases plus the
renderer's real output. Then investigate a mechanically checked semantic
bridge and prove the frontend's footprint soundness and the emitter's
correspondence to the checked graph. The module implementation and bridge are
substantial unimplemented work; the ESMeta parse experiment does not establish
their feasibility or cost. A restricted proof-assistant-native semantics is an
alternative to investigate, with a separate burden of validating its relation
to ECMAScript.

## Extracted module algorithms and proof bridge

To distinguish missing module semantics from the Script-only entry point, the
spike also ran `esmeta compile -compile:log -silent` at the revisions above.
It dumped 2,998 IR functions and a 644-line report of `yet` expressions. These
are report entries, not a count of distinct missing algorithms; some are
assertions and the same expression can be reported more than once. ESMeta's
interpreter throws `NotSupported` when it evaluates a `yet` expression.

Inspect the dumped functions from the ESMeta directory:

```sh
esmeta compile -compile:log -silent
cat logs/compile/func/HostLoadImportedModule.ir
cat logs/compile/func/CreateImportBinding.ir
cat 'logs/compile/func/Record[ModuleEnvironmentRecord].GetBindingValue.ir'
```

The loader returns without loading anything. More specifically, even if a
loader populated the module graph, `CreateImportBinding` contains a `yet`
where it must create an indirect binding; `GetBindingValue` also contains
`yet`s for recognizing and following that binding. These are relevant to the
renderer output above, whose entry imports named bindings from `utils.mjs`.
We did not dynamically reach them: execution stops earlier at the Script
parser. Other `yet`s are assertions, such as linking invariants; they need
proof or a justified treatment, and are not automatically the same as an
unimplemented state update.

A concrete proof path, after restricting the supported language and module
features, would require these separate links:

1. **Executable module semantics.** Replace the host loader with a deterministic
   closed-graph loader, implement live indirect bindings and the required
   module steps, and exercise TDZ, cycles, and linking errors against module
   conformance tests. This is an ESMeta implementation task, not a theorem.
2. **Proof-assistant semantics.** Give the relevant ESMeta IR and machine
   states a Lean semantics, and establish a simulation between it and the
   extracted functions. Alternatively, use a semantics already defined in a
   proof assistant and justify its correspondence with ECMA-262. Translating
   selected algorithms by hand without such a correspondence adds trusted
   assumptions; running the Scala interpreter on examples does not prove it.
3. **Connect the actual tool.** Prove that parsing and analysis safely
   overapproximate reads, writes and effects, and that emitted module text
   resolves to the checked units, bindings and order. Apply the theorem to the
   real emitter and its accepted assignment, not a separate handwritten split.
   Parsing and code generation may initially be explicit trusted assumptions,
   but must appear in the theorem's statement.
4. **State observations.** Model a restricted closed module graph with no
   name reflection, dynamic import, top-level await or host calls. Relate its
   termination, exceptions, externally visible state and later calls through
   exported bindings. Expanding to browser behavior needs a host model.

This is a proposed decomposition, not a demonstrated translation to Lean or
an estimate of implementation effort. Extracting the IR provides a concrete
input to a bridge, but this spike found no existing proof-assistant bridge and
built none. In particular, the current `MiniJS` proof is a proof about a small
separate language, not an ESMeta-to-ECMAScript correspondence.
