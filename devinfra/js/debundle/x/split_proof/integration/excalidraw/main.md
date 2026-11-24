# First main.js placement: a retained metadata reader

The [pinned main trial](https://app.buildbuddy.io/invocation/8c4a747d-bf15-47bb-a118-23752040cdd1)
selected `strokeEllipseWithRotation` in its canonical owner
[`packages/excalidraw/renderer/interactiveScene.ts`](../../../../excalidraw/spec/modules/main/packages/excalidraw/renderer/interactiveScene.yaml).
Rust placed unit 1486 of 1,628. Lean checked the original esbuild entry at its public URL with its real providers
and the browser deployment. Before emission it rejected **retained** unit 23: `expected import.meta.url`.
A function there reads `import.meta.env.MODE`. Selecting a different renderer cannot remove that read from the source.

The [reproducer](../../translation/linked/module_metadata_test.mjs) moves only `render`:

```js
var read = () => import.meta.env.MODE;
var render = () => 42;
export { read, render };
```

Changing only `env.MODE` to `url` certifies both on emission and from disk. Keeping `env.MODE` refuses.
In the native counterexample an entry closure exposes its metadata object. We install getters on it and
on the object returned by `env`. Two calls to the original reader see two different object identities
after a write; each getter receives its actual receiver. Moving the reader alone to another module leaves
the entry export names unchanged, but the read throws without calling the entry getter.
The [Chromium control](https://app.buildbuddy.io/invocation/3b5a4b1a-147d-4aab-a304-076b7765cf4d)
also observed a changed `MODE` at an unchanged URL. The original app mounted, drew a rectangle,
and had an empty main namespace. There is no certified main output from this trial.

## What a retained-reader rule must prove

[ModuleLocation.check](../../lean/EsmSplit/CoreJS/ModuleLocation.lean) checks the reader's source ordinal,
retention in the entry, and effective URL. [LinkedDeployment.check](../../lean/EsmSplit/CoreJS/LinkedDeployment.lean)
gives an opaque unit extra live **binding cells** for imports reachable through its dynamic targets.
A checked `import.meta.url` contributes no cell. Neither check supplies the module record's metadata object.
A [CoreJS closure](../../lean/EsmSplit/CoreJS/Value.lean) retains lexical bindings and its receiver;
an opaque closure retains its code and binding slots. The opaque world can store state, but
passing the same code ordinal to that world does not prove how a JavaScript metadata read resolves.
The [stable host read](../../lean/EsmSplit/CoreJS/StableIntrinsic.lean) predicate requires the same result
without effects in every host state. Even a getter that always returns the same value can violate that requirement.

A narrow rule could use a hidden capability for the metadata object:

1. Under the existing native-loader deployment contract, relate the retained entry record in the two
   executions. Give every reader of that record the same metadata object within its execution; keep
   distinct records separate. Reuse this capability across calls, including calls after another unit
   has mutated the object. If the host initializes metadata on first access, preserve that timing.
2. Carry the capability through linkage and closure capture (including opaque calls), and check its
   correspondence alongside live lexical cells. Include the receiver and objects reachable through it
   in the reachability proof for moves. A getter can capture a binding that the reader never names.
3. Use ordinary property reads for both `env` and `MODE`. Prove equivalence from related stores
   and receivers, preserving getter effects, throws, and returned references. Walk nested syntax
   and require the same record at each accepted site. Retain refusal for a moved reader or a changed
   deployment context until a separate rule justifies it.

An implementation can then turn the retained-reader refusal into an emit/disk acceptance and repeat the
full Rust-selected main placement in Chromium. An initialization certificate alone does not check later
calls to exported functions; keep the getter and mutation observations as behavioral controls.
