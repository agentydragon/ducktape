# Certified main.js renderer split

The main selection moves `strokeEllipseWithRotation` from the canonical
renderer owner. It was included in the [historical joint deployment replay](joint.md).
[SHA-256 pins](main_static_sha256.json) identify the original main source and
its static closure. The renderer probe checks the issued `beginPath`,
`ellipse`, and `stroke` calls.

## Earlier proof boundaries

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

## Native browser initialization

With the `native-browser-record@1` deployment premise, the [reduced reader certifies on emission and from disk](https://app.buildbuddy.io/invocation/6e7fb1dd-5286-4df9-880e-1d9e3c942e2b). The [pinned Chromium measurement](https://app.buildbuddy.io/invocation/708fa6a9-9448-4574-b836-17dd8d170c5f) observed the fresh null prototype, native data descriptors, a distinct resolver per module, and a later getter returning two different results after a write. The preset concerns creation; it does not make `env.MODE` a stable read. The generic host counterexample below still applies outside that premise.

The [main replay with the preset](https://app.buildbuddy.io/invocation/5d282780-a1a3-46be-bbcc-5c9f43c4fecb) matched the same source digest and selected only renderer unit 1486 through the neutral resolver. Deployment checking passed unit 23 and stopped at unit 1379: `expected a relative module request`. No full main output was published.

## The text-to-diagram request

At ordinal 1379 in the [pinned main AST](https://app.buildbuddy.io/invocation/a397565d-7c20-41cd-b2b4-56062d10e7da), `TTDDialogBase` passes this initializer to React:

```js
useState({ loaded: false, api: import("@excalidraw/mermaid-to-excalidraw") });
```

The operand is a string literal with a bare package name. The containing component function is deferred at module initialization. On render, JavaScript evaluates the import while constructing the argument to `useState`; an effect subsequently awaits the promise. Esbuild explicitly leaves this package external. In the [original Chromium run](https://app.buildbuddy.io/invocation/21f9b802-4f6b-4f52-9ce1-1f9c4dc712bc) the page had no import map or corresponding startup rejection. Opening **More tools → Mermaid** produced unhandled `TypeError` rejections naming that specifier.

The [changed-map control](https://app.buildbuddy.io/invocation/3100e26d-af5c-4ea5-a49c-e0c3e5db1f68) serves identical entry and target bytes at the same URLs. Without a map, two calls return distinct promises that reject without fetching the target. An unhandled call reports the actual promise and reason. With a mapping, both calls fulfill and the target evaluates once. Only the unmapped deployment is certified under `empty@1`.

The [selected main replay](https://app.buildbuddy.io/invocation/f827354b-83ae-46d2-b84c-23a5b060ba7f) used both the native metadata preset and the empty-map premise. It selected only `strokeEllipseWithRotation` at unit 1486 against the same digest and original providers. Linked lowering then refused statement 1256: `unsupported opaque generator function declaration`. No full main output was available to reparse or replay.

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

## Receiver-cell experiment

The [Lean prototype](../../lean/EsmSplit/CoreJS/MetadataCell.lean) checks a hidden immutable cell and its current heap object in both scopes. Given a common starting state, its certificate equates the results of the two captured reads for every world and fuel bound sufficient to reach the property expressions. Both reads dispatch through the ordinary evaluator: the outer getter can change state before the inner lookup. The fixture changes a getter's captured binding to a second object and observes its distinct address. A getter that reaches unsupported code produces `unknown` even when the receiver check succeeds.

The remaining obligation is to **supply** the root. In `missing_linked_root`, the actual linker succeeds and leaves the hidden cell absent, even though a hoisted opaque function requests it. A loader extension needs a record-to-cell mapping shared across source and candidate, with an immutable binding to the metadata object and world-specific property state. It must account for initialization on first access and for any already reachable references. In particular, [link transport](../../lean/EsmSplit/CoreJS/LinkWorld.lean) currently reuses the probe's heap across worlds. Adding an arbitrary host-supplied metadata heap to the probe is not justified by that theorem. Until the producer and its transport are proved, the prototype does not participate in the splitter's verdict.

The [neutral resolver replay](https://app.buildbuddy.io/invocation/7f4592ee-b421-463a-a1d4-9290aedd27f4) checked the canonical and selected owner maps against original `main.js` SHA-256 `226df77e1ed19405ba8bcdcf9172aa8b2050d25877e2fd472ebd3864f0cd33d9`. The selected owner contains only unit 1486. With the original providers, Lean still refused retained unit 23 before publication.

## Linking the receiver before hoisting

[MetadataLink.linkRecord](../../lean/EsmSplit/CoreJS/MetadataLink.lean) installs an immutable receiver cell in the entry's actual linked scope after imports resolve and before functions capture bindings. The caller supplies an already provisioned record; no stable-read premise is used. This solves capture for a given world. It also makes the limitation of the production [link transport](../../lean/EsmSplit/CoreJS/LinkWorld.lean) observable.

A retained function reads `import.meta.env.MODE` before an exported `var pivot = "ready"` initializes. The two layouts have the same entry URL and public names. One host supplies a data property containing `undefined`. The other installs a getter for `MODE` that reads the entry's live `pivot` export. Moving the initializer to an earlier module changes the getter's answer from `undefined` to `"ready"`; the data property's answer stays `undefined`. The [native ESM control](../../translation/linked/metadata_link_worlds_test.mjs) exercises the same two hosts using the module record passed to the loader's metadata hook.

Lean checks both executions. It also proves `no_common_probe`: the metadata getter captures the cell of `pivot`, so the frame check excluding that region fails in the getter world and passes in the data world. Both hosts allocate at the same addresses. Transporting one probe snapshot copies its heap and cells, giving the same answer in both worlds. Consequently the experimental linker cannot supply the existing structural certificate for arbitrary metadata.

The next step is to parameterize the **ready heap and the frame proof**, not just the receiver address. For example, derive a bound on the binding cells that host callbacks can reach from each protected record (including through the module namespace); prove it contains every reachable edge for every admitted world, and reject any pivot it intersects. Capture the same record in both layouts and preserve first-access timing if provisioning has effects. The [direct resolver retry](https://app.buildbuddy.io/invocation/3427ec99-a05e-48b0-a358-7ddac381a4a9) used the pinned digest above and the actual providers with `--resolved-placement main`. The production decoder still rejects unit 23 before publication.

## Requests from an unchanged provider

In the [original editor provider](https://app.buildbuddy.io/invocation/4aefef28-161c-4bb7-b15b-a182849456e9), unit 354 conditionally imports either `./file-open-002ab408-75HCAGST.js` or `./file-open-7c801643-M7Y3IQ2R.js`. Both resolve from the URL of `chunk-PN7KOLDJ.js`. Another lazy import in this provider re-enters its linked exports through the image module.

The checker collects these dependency graphs from the actual provider files and assigns their live import cells to the originating provider units. With native metadata enabled, each referring module gets its own metadata object. The [provider fixture](../../translation/linked/provider_location_test.mjs) distinguishes same-named targets in two directories, exercises a lazy cycle, and rejects moving a binding captured by a provider hoist through a lazy dependency.

The [full main replay](https://app.buildbuddy.io/invocation/77fdabaf-e337-45e6-aab3-39a36b7149ae) selected renderer unit 1486 at the original main digest, with generator hoists, native entry metadata and an empty browser import map. Both graphs decoded and passed the scope comparison. The structural schedule checker did not return within the 900-second diagnostic budget. This establishes neither a failed move nor a certified main output.
