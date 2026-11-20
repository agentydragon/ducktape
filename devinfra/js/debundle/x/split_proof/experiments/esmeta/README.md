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
