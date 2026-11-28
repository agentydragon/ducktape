# Unchanged deferred functions and opaque calls

The model already proves that two occurrences of the same checked closure construction have the same effects. A later call is a separate obligation. Reusing a code ID can prove agreement of two executions of the **same modeled operation**; it cannot explain what a deferred JavaScript body does. This design proposes no new assumptions, host profile, or CLI admission.

The claim should say what was observed: construction and module evaluation, a reached synchronous invocation, or invocation through a specified job checkpoint. A proof about module evaluation does not cover an exported function called later by an unexamined client.

## What the current lemmas buy

| Case                                             | Evidence and exact premises                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Conclusion                                                                                                                                                                                                                                                         |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Allocate or hoist an unchanged closure           | [`makeFunc_agree`](../EsmSplit/CoreJS/Scope.lean) (in namespace `CoreJS`): the same `Func` and display name; `Agree f.free` equates each resolved optional cell address, `this` and home object; apply to the same prestate with the same allocation region. `capture_agree` gives the same captured environment. For opaque hoists, `opaqueCells_agree` and `initClosures_agree` give the corresponding ordered slots and initialization.                                                                             | Equal modeled allocation, identity, prototypes/descriptors and self-binding. These statements do not enter the body. Model equality covers the metadata the model represents.                                                                                      |
| Execute identical CoreJS                         | [`CoreJS.same`](../EsmSplit/CoreJS/Scope.lean) gives equality of expression and statement computations at equal fuel for every world under `Agree` for the same code. The resolved call `callFn` with exactly equal prestate, callee, receiver, arguments and fuel is equal by congruence. [`SameUnits`, `runUnits_same` and `initFunctions_same`](../EsmSplit/CoreJS/Split.lean) lift this to aligned original ordinals, statements and scopes; separately check the initial linked states and live export addresses. | Equal complete modeled results: normal or thrown value and resulting state, unknown with its state, or fuel exhaustion. No body-specific induction is needed once these premises hold.                                                                             |
| Dispatch reaches a deferred body                 | [`dispatchCallWithDeferred`](../EsmSplit/CoreJS/Eval.lean) distinguishes a body tagged with a refusal reason from one with a `World.talk` implementation. Ordinary `callFn` sends the reason-tagged case to `unknown`.                                                                                                                                                                                                                                                                                                 | Equal refusals show matching model boundaries. They give no positive evidence that the native call returns, throws, or makes the same writes.                                                                                                                      |
| Execute an opaque operation in the modeled world | [`Told`, `Request` and `World`](../EsmSplit/CoreJS/Value.lean), [`converse`](../EsmSplit/CoreJS/Eval.lean): same authenticated code/part, slot addresses, callee handle, effective receiver, arguments, call/construct mode, equal full initial state and fuel, and the **same** world on both sides.                                                                                                                                                                                                                  | Same modeled conversation, including nested calls, exceptions, handed handles and changed world state. This is parametric in the modeled interpreter. There is no theorem here that a particular host interprets that opaque body as native JavaScript.            |
| Resume work                                      | [`callFnTurn_overlap`, `performCall_turn` and `converse_turn`](../EsmSplit/CoreJS/CallTurn.lean) describe the synchronous overlap and carry the post-callback state forward. [`Job.capture_corresponds` and `onResume_capture`](../EsmSplit/CoreJS/PrimitiveAwaitCorrespondence.lean) cover the existing concrete continuation and its captured environment.                                                                                                                                                           | Projection to the ordinary result can erase queued jobs. Equal projection or equal immediate return is insufficient for equality through a checkpoint. A new queue-producing opaque edge needs a turn-valued semantics and a correspondence for its continuations. |

In the first implementation experiment, use exact addresses and equality of the **full** linked ready state; existing congruence applies directly. If a later experiment needs different allocations, relate stores by a partial bijection of live cell, object and external handles. It must preserve aliases in both directions, missing and uninitialized cells, read and write permissions, prototypes and descriptors, and extend when fresh observed objects or promises are allocated. Relate the world state, handles already given to it, and pending/running jobs as well. Nothing in `Agree` or `StateMatch` supplies that renaming theorem. In particular, [`link_from_probe`](../EsmSplit/CoreJS/LinkWorld.lean) transports the model's linked heap; it cannot transport an arbitrary heap supplied by a native realm.

The landed [finite static graph constructor](../EsmSplit/CoreJS/FiniteStaticGraph.lean) is an example of the exact-state path: it checks named import addresses, ready state, and the whole schedule before using `runUnits_same`. Its primitive synchronous fragment does not admit deferred calls. The [published Zod v3 diagnostic](../integration/library_splits/ZOD_V3_STATIC_GRAPH.md) separates import placement, namespace imports and star re-exports from a later invocation boundary: `helpers/util.js` can link in isolation, but evaluating its deferred initializer still refuses under the empty profile.

## What to bind before trying to run a body

1. Bind the original code identity to authenticated source and to independently parsed output: ordinal/site and callable part, exact callable source where reflection can observe it, function kind (ordinary, method, arrow, async, generator), parameters and defaults, name/length, constructability, home object/receiver where relevant, and allocation provenance. A hash or names alone cannot witness these equalities. The [artifact comparator](../translation/module_io.mjs) already compares callable source slices at its range-preserving sites; check that the actual move and site use that path.
2. From the actual linker, compare ordered captured slots and the public live cells, including aliasing and binding mode (for example, assigning through an import is different from writing the original local). Use the actual launch state as well: preserving an address is not enough when its contents or initialization state differ. A metadata reader also needs correspondence of the actual module record and its metadata object, and dynamic loading needs the correct referrer/loader state. See [the retained-reader case](../integration/excalidraw/main.md). Source-level callers can also observe function source text and identity without invoking the body.
3. At every invocation, compare the **resolved** callee, invocation kind (include `new.target` for construction), effective receiver or lexical receiver, and arguments at the point of dispatch. Preserve callee/argument evaluation order and the lookup that led there (binding and property lookup, getter, proxy or coercion if reached). Compare the whole result and effects; a getter, callback or a caller that catches a throw can observe intermediate states.
4. Initially require the same sequence of calls and their effects, including intervening writes. Moving a step across an opaque evaluation needs an independent frame or commutation proof. The [opaque frame](../EsmSplit/CoreJS/OpaqueFrame.lean) and [reachable-handle frame](../EsmSplit/CoreJS/Request/Frame.lean) are building blocks: absence of a free variable is no frame if the world can already reach its cell or object through a handle, receiver, getter, prototype or callback.
5. For a relational runner, check a step at each boundary: related configurations issue corresponding requests; request handlers preserve the relation, including across reentry; replies, throws and successors are related, with each next step starting in the updated turn. Maintain FIFO order, promise identity and settlement, rejection reason, and writes before and after resumption. Stop on unsupported behavior and keep language throw, suspension, unknown and exhausted fuel distinct. Do not replace this induction with a premise that the entire native calls already agree.

This separates three useful outcomes. We can often prove construction and non-invoking observations without interpreting a body. We can prove identical modeled behavior parametrically, even for stateful `World.step` / `World.talk` operations, when the same request starts in equal state. To claim positive behavior where ordinary evaluation defers, we need actual semantics for the reached body (or a proved correspondence between an independently specified interpreter and the concrete operations it performs). Recorded replies help test that correspondence but do not prove it. A whole-call uninterpreted return value, an oracle that receives only a spelling, or a pair of equal unknowns would all hide the distinctions below.

## Paired controls

Run each JavaScript block independently as a Node ESM script. In each pair the positive case preserves the relevant input to the operation; the negative changes one thing that a weaker premise would miss.

**Live cells, receivers and identity.** A closure over the same cell observes rebinding; a snapshot initially looks identical. A method extraction changes the receiver. Two separately allocated closures with equal source and captures still have different identities.

```js
import assert from "node:assert/strict";

function readCell(get) {
  return function read() {
    return get();
  };
}
let live = 0;
const original = readCell(() => live);
const kept = readCell(() => live);
const snapshot = live;
const wrong = readCell(() => snapshot);
assert.equal(original(), wrong());
live = 4;
assert.deepEqual([original(), kept(), wrong()], [4, 4, 0]);
assert.equal(original.toString(), kept.toString());
assert.notEqual(original, kept);
assert.equal(new Set([original, original]).size, 1);
assert.equal(new Set([original, kept]).size, 2);

function readReceiver() {
  return this?.x;
}
const obj = { x: 7, f: readReceiver };
const { f } = obj;
assert.deepEqual([obj.f(), f.call(obj), f()], [7, 7, undefined]);
const makeArrow = function () {
  return () => this.x;
};
const arrow = makeArrow.call(obj);
assert.equal(arrow.call({ x: 9 }), 7);

const base =
  "data:text/javascript," + encodeURIComponent("export let state = 0; export function bump() { return ++state; }");
const bad =
  "data:text/javascript," +
  encodeURIComponent("import { state } from " + JSON.stringify(base) + "; export function bump() { return ++state; }");
assert.equal((await import(base)).bump(), 1);
const readOnly = await import(bad);
assert.throws(() => readOnly.bump(), TypeError);
```

**Reflection and loader metadata.** A call returning the same value can still be distinguishable by callable source or descriptor configuration. Two modules with byte-identical bodies have distinct module records; changing a URL or resolving a different record affects `import.meta` even if the captured lexical list is empty.

```js
import assert from "node:assert/strict";

const a = function read(x = 0) {
  return x;
};
const b = function read(x = 0) {
  /* changed source */ return x;
};
assert.deepEqual([a(), a.name, a.length], [b(), b.name, b.length]);
assert.notEqual(Function.prototype.toString.call(a), Function.prototype.toString.call(b));
Object.defineProperty(b, "length", { configurable: false });
assert.notDeepEqual(Object.getOwnPropertyDescriptor(a, "length"), Object.getOwnPropertyDescriptor(b, "length"));

const body = "export function read() { return import.meta; }";
const url = "data:text/javascript," + encodeURIComponent(body);
const left = (await import(url)).read;
const same = (await import(url)).read;
const other = (await import(url + "#other")).read;
assert.equal(left.toString(), other.toString());
assert.equal(left(), same());
assert.notEqual(left(), other());
assert.notEqual(left().url, other().url);
```

**Accessors, rebinding and random.** Save-once and read-again are different operations. Use a controlled stand-in for the selected random function to show the exact consumption and the order of lookup effects; never expect independent native `Math.random` runs to sample the same numbers.

```js
import assert from "node:assert/strict";

function sample(mode) {
  const oldMath = globalThis.Math;
  const trace = [];
  let cursor = 0;
  const draws = [0.125, 0.75, 0.5];
  const math = {
    get random() {
      assert.equal(this, math);
      trace.push("get");
      return () => {
        trace.push("draw");
        return draws[cursor++];
      };
    },
  };
  globalThis.Math = math;
  try {
    const run = () => Math.random();
    const saved = mode === "saved" ? Math.random : run;
    const first = saved();
    if (mode === "reordered") run(); // consume the draw belonging to the next call
    const second = saved();
    return [first, second, cursor, trace];
  } finally {
    globalThis.Math = oldMath;
  }
}
const kept = [0.125, 0.75, 2, ["get", "draw", "get", "draw"]];
assert.deepEqual(sample("original"), kept);
assert.deepEqual(sample("retained"), kept);
assert.deepEqual(sample("saved"), [0.125, 0.75, 2, ["get", "draw", "draw"]]);
assert.deepEqual(sample("reordered"), [0.125, 0.5, 3, ["get", "draw", "get", "draw", "get", "draw"]]);

const holder = { draw: () => 0.125 };
const saved = holder.draw;
holder.draw = () => 0.75;
assert.deepEqual([saved(), holder.draw()], [0.125, 0.75]);
```

In the existing [intrinsic table](../EsmSplit/Intrinsics.lean), `Math.random` is a world operation. Once lookup has actually selected the supported intrinsic, [`hostCall`](../EsmSplit/CoreJS/Value.lean) consumes a host-state transition even when the returned number is unused. The [record/replay harness](../experiments/README.md) can replay samples in order. A real semantics must account for the relevant realm, consumed draw, exact Number result, and the successor state seen by later calls. An appropriate relation could couple the same draw sequence; equality across independently seeded executions is not the claim. A replaced `Math`, accessor, shared host state, or an intervening random consumer invalidates a proof that used only the intrinsic name.

**Reentry, order, exception identity and resume.** A callee invokes a callback that reads the live state and schedules another job. The same thrown sentinel and final scalar can hide changes in effects. Awaiting one promise also allows another job to run first; an observation before the checkpoint cannot establish resume behavior.

```js
import assert from "node:assert/strict";

async function run(mode) {
  const log = [];
  const sentinel = {};
  let shared = 0;
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  function opaque(cb) {
    shared = 1;
    cb();
    shared = 2;
    throw sentinel;
  }
  const resume = (async () => {
    await gate;
    log.push("resume:" + shared);
    shared = 3;
    throw sentinel;
  })();
  try {
    const cb = () => {
      log.push("reenter:" + shared);
      if (mode !== "resume-first") queueMicrotask(() => log.push("job:" + shared));
    };
    if (mode === "late-callback") {
      try {
        opaque(() => {});
      } finally {
        cb();
      }
    } else {
      opaque(cb);
    }
  } catch (error) {
    assert.equal(error, sentinel);
    log.push("catch:" + shared);
  }
  release();
  if (mode === "resume-first") queueMicrotask(() => log.push("job:" + shared));
  await assert.rejects(resume, (error) => error === sentinel);
  return [log, shared];
}
const kept = [["reenter:1", "catch:2", "job:2", "resume:2"], 3];
assert.deepEqual(await run("original"), kept);
assert.deepEqual(await run("retained"), kept);
assert.deepEqual(await run("late-callback"), [["reenter:2", "catch:2", "job:2", "resume:2"], 3]);
assert.deepEqual(await run("resume-first"), [["reenter:1", "catch:2", "resume:2", "job:3"], 3]);
```

## Where the real workloads stop

| Workload and boundary                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Smallest responsible next experiment                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Retrier 0.4.3](../integration/library_splits/retrier/README.md): the full pinned `dist/retrier.js` placement retains all ten ordinals, keeping the public class/export in the entry. Linked classification first stops at `#7`, the `RetryTask.id = Math.random().toString(36).slice(2)` initializer. The existing non-retry Node discriminator does not construct a `RetryTask`. It holds one pending task, checks the exact rejection object and retained writes, then observes another queued task finishing. | First separate construction of the class and its deferred initializer from reaching `new RetryTask`. Keep the classifier refusal distinct from evaluating that initializer. On a path that reaches it, check the full lookup, draw and radix/slice chain, returned ID and subsequent world state. Run a matched retry path where the predicate accepts; a false predicate, thrown sentinel, timer interleaving or a changed draw stream should distinguish the controls. `Date`, `Promise`, timer delivery, and the queue/working count across resumption need live semantics or a proved bridge. |
| Excalidraw [retained main reader](../integration/excalidraw/main.md): unchanged render code at `#1379` starts a bare dynamic import, and the pinned control changes its outcome with unchanged bytes under a different map. [Editor cache](static_field_locality.md): class, field call and alias are `#1247–1249`. A call through `publicField` installs a new `WeakMap` on an **earlier** allocation.                                                                                                           | For the main reader, relate the module record, actual loader resolution and the returned distinct promises, cache and rejection event. For the editor, check the real helper, saved definition function, constructor identity, and native WeakMap brand/freshness, plus the field write on the original class and its descriptors. Add controls for an inherited accessor, a replaced helper that reenters, and an alias observing the field before the write. The existing `Local` result rules out treating this as a write confined to the field initializer's region.                         |
| [semver 7.8.6](global_member_reads.md): the full esbuild bundle starts with `#0 var __create = Object.create` and `#1 var __defProp = Object.defineProperty`. The eager binding lookup and property read fail before a later call or unchanged closure is reached. The [full workload ladder](../integration/library_splits/COMPLEXITY_LADDER.md) records refusals.                                                                                                                                               | Preserve the lookup and save the returned reference in its original live cell; move on to consumers only after connecting this read to the mounted environment. Compare getter receiver and order, binding shadowing, throwing lookup, replacement **after** capture, and a later helper invoking the saved definer or copying an accessor. Reusing an unchanged `__commonJS` closure cannot justify moving its first invocation or resetting cached/exported identities.                                                                                                                         |

The [landed census](../integration/library_splits/FULL_BLOCKER_CENSUS.md) records 11 refusals across the pinned full-source placements. Its per-unit opaque fallbacks are a separate diagnostic; they supply no evidence of construction, execution, or publication for any of those full placements.

## Construction-only Retrier slice

The [scratch replay on `d7d1294b`](https://app.buildbuddy.io/invocation/108b4c6e-cdab-4993-a059-d7b81e2bef4e) reproduces the [original experiment](https://app.buildbuddy.io/invocation/930e14fc-1065-4c4e-932d-fdbe68216d79). It extracts only the exact expression `Math.random().toString(36).slice(2)` from hash-checked `@humanwhocodes/retry@0.4.3`. In the original it is an instance field initializer. The wrapper and lexical capture below are the experiment; it does not move the original class or prove its field initialization.

```text
const suffix = ":";
const decoy = ":";
function retryId() {
  return Math.random().toString(36).slice(2) + suffix;
}
export const retained = retryId;
```

The authored request assigns original ordinals 0–2 to `retained.mjs` and leaves ordinal 3 at `entry.mjs`, with the helper first. The original function at ordinal 2 therefore has a different owner. The test uses the parser, planner and ESTree printer, reparses the staged files, resolves import and export cells, and checks the original ordinals, linked order, statements and captures. The initial linked state check is full equality. With those concrete checks, the proof in the scratch runner is:

```lean
private theorem sameConstruction (before after : Program) (pair : CheckedProbePair before after)
    (ready : pair.before.ready = pair.after.ready)
    (units : SameUnits (unitsOf pair.before.linked) (unitsOf pair.after.linked)) :
    ∀ w fuel, runProgram w fuel before = runProgram w fuel after := by
  intro w fuel
  rw [run_after_link fuel (pair.before.forWorld w), run_after_link fuel (pair.after.forWorld w)]
  change runUnits fuel (unitsOf pair.before.linked) (linkState w pair.before.ready) =
    runUnits fuel (unitsOf pair.after.linked) (linkState w pair.after.ready)
  rw [ready, runUnits_same fuel units]
```

The proof uses the existing link and schedule lemmas. A separate `exportsMatchFrom` check gives `SameExports` via `CheckedProbePair.sameExports`. In the construction run, both layouts complete at fuel 128 in `World.probe`, export the closure from the same modeled address, and capture the original `suffix` cell. Node observes a function and zero random draws in both layouts.

- **Changed capture:** keep the same statement, code and ordinal in the checker input, but redirect its resolved `suffix` slot to the distinct `decoy` cell. Both cells hold `":"` after initialization. `sameUnits?` rejects this hypothetical rebinding by address.
- **Reached call:** append `export const value = retryId();` in the entry. Both model runs now stop at `deferred function retryId: unsupported global Math`. Both Node runs with a controlled draw of `0.5` observe one draw and return `"i:"` for that export. The modeled equality theorem still holds, but this time it equates refusals; it says nothing about producing the ID.

The full Retrier fixture still refuses before publication. The experiment changes neither the model nor any admission rule.

For the next prototype, keep the existing constructors untouched. Reuse `makeFunc_agree` and the linked schedule lemmas for an authenticated construction-only fixture; distinguish it in the test matrix from a reached deferred-call refusal. Then exercise existing world calls with an explicit state transition and a nested callback, requiring positive completion or the expected language throw and full state in a concrete control. Only after that is it useful to try a proof for a single live operation from a real workload. Record decode, model completion, and ordinary native observations separately at each step.
