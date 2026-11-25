# Recovering the pinned Excalidraw editor's authored modules

The canonical Rust editor graph places **272 of 1,787 units in 35 named modules**.
The [current browser smoke](README.md#browser-smoke) resolves the selected owners and
authored import priority in Rust. Lean computes the schedule and checks structural
`normal-completion` at the browser origin. Unselected units stay in the original entry.
Earlier trials below used the Rust emitter's selected schedule; the proposal records
its tested order explicitly.
A refusal of one placement does not establish that moving a binding changes
JavaScript behavior.

The first expanded subset moved **107 bindings
into 23 canonical modules**, up from 16 in four. The
[combined run](https://app.buildbuddy.io/invocation/6777e49b-e517-41bb-ba1e-b1b24dea0b85)
built Rust's selected tree and certified Lean's selected tree both at emission
and from disk. Chromium loaded the certified modules and compared the complete
editor entry namespace with the original. The result is a lower bound for this
checker and input; the candidate filters below are not a maximality proof.

## Hoisted declarations

The [hoisted declaration rule](https://github.com/agentydragon/ducktape/pull/19735)
certifies 23 of the 25 authored function declarations together with their
eligible dependents. The [136-binding replay](https://app.buildbuddy.io/invocation/74aa1a8e-295b-4c8d-a74c-0d07e4ae5aac)
checked Rust emission, both Lean certificates and the browser overlay.
The two remaining hoists, `deconstructRectanguloidElement` and
`deconstructDiamondElement`, depend on private entry bindings.
[Restoring both changed the entry exports](https://app.buildbuddy.io/invocation/45f6d119-2f0f-4d2d-9d9b-3d1fae522121).

## Restoring an upstream dependency

The canonical selectors for [constants.ts](../../../../excalidraw/spec/modules/editor/packages/excalidraw/constants.yaml)
place 73 additional bindings in their [pinned upstream module](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/constants.ts).
Moving `ELEMENT_READY_TO_ERASE_OPACITY` into that module lets the renderer
import it there. The checker can now certify `getRenderOpacity` without exposing
its former private dependency from the editor entry.

Three further trials on the hoist baseline identified separate proof limits:
the linked state exposed [`vectorNormalize`](https://app.buildbuddy.io/invocation/b18c0ffd-ecaf-4e0f-b7db-bf9daae50835);
an earlier unit accessed the moved region of [`hitElementBoundText`](https://app.buildbuddy.io/invocation/1eed0d13-c6ec-46ac-8c67-6ef04cf9b895)
and of [`loadFromBlob`](https://app.buildbuddy.io/invocation/7ad47e3f-24cd-4140-801c-4911859cee5b).

## Applying the generic move proofs together

The [combined 224-binding replay](https://app.buildbuddy.io/invocation/aaca1ba2-7ef6-4c50-aaac-339b715b5bd7)
keeps the full Rust assignment as its reference and exercises these additional moves:

- The regex-literal rule certifies the `COMMON` and `EMOJI` tables in text wrapping.
- The isolated-observer and adjacent-declaration rules allow `isLinearElement`,
  the two free-draw predicates and `isBindingElementType`. Adjacent captures
  also occur in collision, group selection and file-handle helpers.
- The numeric rule certifies `MAX_ALLOWED_FILE_BYTES = 4 * 1024 * 1024` in
  `constants.ts`. Restored dependencies also allow `cappedElementCanvasSize`,
  `restoreAppState` and `restore` in their original owners.

The following screening and refusal counts describe the earlier 107-binding
baseline; they are evidence for the rule changes, not a count of remaining
refusals on the current checker.

The [multiple-observer rule](https://github.com/agentydragon/ducktape/pull/19747)
subsequently certified `vectorNormalize` alongside all 224 bindings above.
Two hoisted geometry helpers capture that binding. The checker accounts for
both observers before restoring the suffix state; a getter that reaches a
mutable observer still prevents the move. [225-binding replay and negative controls](https://app.buildbuddy.io/invocation/b3a3b9cf-e163-47e8-ac5b-43f873525eeb).

## The scroll dependency closure

The pinned [scroll.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/scene/scroll.ts#L14)
uses a private helper, `isOutsideViewPort` (unit 1336, `qb`). It reads only
`sceneCoordsToViewportCoords`, which the editor already exports as `sb`.
[Adding the viewport helper to the named selection](https://app.buildbuddy.io/invocation/c88359e1-9673-4325-9990-1599c71d3220)
passed the canonical and selected Rust gates, both Lean checks and Chromium.

Two other private entry cells prevent moving `calculateScrollCenter`:

- `Lm` (1745) is [getVisibleElements in element/index.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/index.ts#L89).
  Its predicate `isInvisiblySmallElement` already has the public name `zj`.
- `Eb` (1269) is [getClosestElementBounds in element/bounds.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/bounds.ts#L935).
  Its point and map helpers have public bindings. Its private
  [getElementBounds](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/bounds.ts#L816)
  (1264, `xt`) delegates to [ElementBounds](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/bounds.ts#L72).
  The bundle creates the class (1247), initializes its static WeakMap (1248)
  and binds the alias `pb` read by `xt` (1249).

The Rust atomic dependency graph puts 1745 and 1264 in the same 410-unit
component despite their different upstream homes. Assigning the helpers to
their original modules splits that component across the entry, `element/index`
and `element/bounds`; both the [canonical gate](https://app.buildbuddy.io/invocation/bdb36333-a093-4883-82c2-c7376e45b82c)
and the [named-subset gate](https://app.buildbuddy.io/invocation/892125c3-5f45-4cfb-8abc-6c687835303d)
refused it. This is an ordinary Rust gate result. The
[independent Lean replay and selector fixtures](proposals/scroll/README.md)
record the checker's own refusals for the full closure and a certified
236-unit placement for smaller upstream groups.

## Follow-up on the scroll and utility blockers

The [279-binding editor run](https://app.buildbuddy.io/invocation/caefdcf8-0751-4145-bd72-5ab8a61e78a6)
adds 52 bindings in upstream `utils.ts`, the font-family fallback helper in
`constants.ts`, and `isOutsideViewPort` from `scene/scroll.ts`. The
[larger proposal replay](https://app.buildbuddy.io/invocation/42f75333-d929-48d0-b051-bab21229909f)
certified 292 bindings in 30 owners using the same import planner. It also
selected scroll/geometry proposals, `math/utils::average` and its SVG consumer,
and `SHIFT_LOCKING_ANGLE` under the explicit `Math.PI@1` browser contract.

- **Wrong public surface:** requesting `calculateScrollCenter` plus its
  direct private dependencies exposed the class alias `pb$1` at cell
  `@1250.0`. Including the original bounds class, alias, cache write and
  geometry helpers exposed a separate ordering refusal: unit 1338 can access
  the moved region of 1745. The independent result comes from Lean on a
  resolved placement; the Rust emitter's atomic-group report is a separate
  observation. [Placement trials](https://app.buildbuddy.io/invocation/906e8fb4-91cd-410f-85ed-0c0d9896379c).
- **Class cache initialization:** the bundle creates `ElementBounds` (1247),
  allocates a WeakMap and installs it on that class (1248), then aliases the
  class (1249). Moving the write as a local allocation fails because it also
  modifies the earlier class object. Leaving the write in entry fails on the
  alias's binding read; moving just the class exposes a forward capture.
  [Four reduced placements](https://app.buildbuddy.io/invocation/72e03088-de3b-42ae-bcaf-2ace949a10df).
  A proof needs both fresh constructor semantics and a frame that permits
  writing an owned receiver allocated by another unit. The
  [off-region write lemma and runtime controls](https://app.buildbuddy.io/invocation/69da0a60-6026-44e4-b789-c3bea9ca5e79)
  distinguish replacement constructors, reentry and an observable saved
  property-definition hook.
- **Regular expression construction:** the two directionality strings can
  move together in their upstream utilities module. The constructor that
  consumes them and `isRTL` remain in entry. A regex literal allocation
  theorem does not justify invoking a mutable ambient `RegExp` constructor.
  [Partial placement, disk check and Chromium](https://app.buildbuddy.io/invocation/ab384ba5-99e2-429a-a35f-15076d354d23).

### What would justify the constructor in unit 1248?

On the pinned editor (SHA-256
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`),
unit 1248 passes the result of `new WeakMap()` to the static-field helper.
The [neutral replay](https://app.buildbuddy.io/invocation/d2350079-73ab-4040-8ad0-a63e0f554aa8)
isolates the constructor: moving only `var cache = new WeakMap()` ahead of an
unrelated declaration fails the local-allocation check. Concrete linked
execution under the browser-page profile reaches “host value window.WeakMap
is constructed”.

[WeakMapBoundary.lean](../../lean/EsmSplit/CoreJS/WeakMapBoundary.lean) proves
that even an effect-free stable binding read returning a stipulated constructor
identity leads to that unknown result at sufficient fuel, with the input state
unchanged. The world has no native construct operation. A future rule would
need to check the unshadowed binding, require it to yield the realm's intrinsic
constructor without effects throughout initialization, and implement the
zero-argument construct with that same new target. Its only state change should
be allocation at a fresh address, with the realm's intrinsic WeakMap prototype
and an empty `[[WeakMapData]]` slot. These premises do not apply to
`new WeakMap([])`, which processes an iterable.

The [runtime controls and kernel audit](https://app.buildbuddy.io/invocation/261231e7-a949-4131-ba1d-0ebdfea74647)
distinguish a native map from an ordinary object with identical own properties
and prototype. After removing both prototypes, the saved native `has` still
returns false for the map and throws for the lookalike. The current object
record cannot make that distinction from its fields alone. Supporting
allocation requires preserving the brand independently of those properties;
reached collection operations need corresponding semantics or an unknown
result. A replaced or shadowed constructor can return an existing sentinel;
even a getter returning the native constructor, or a stable constructor that
reenters, can observe the order of initialization.

This boundary adds no certified editor units. The other standalone
zero-argument declaration, `elementWithCanvasCache` (1206), is captured by
unit 675's deferred callback, which calls `delete` on the cache. It does not
isolate the constructor move. In 1248 the helper also writes an already
allocated class object in another unit's region; the constructor premise
alone cannot justify that write.

The [306-binding audit](audits/editor_306/README.md) accounts for every editor unit by canonical
owner and records nine retries. The largest passing addition on that baseline was `isLinearElementType`.

## Heading helpers and the linear-element predicate

The [pinned heading source](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/heading.ts)
defines the four direction vectors and the functions that use them. The
[production selector](proposals/heading/packages/excalidraw/element/heading.yaml)
pins the direction order and the diamond angle boundaries. Nine heading
bindings retain that owner; the smoke also selects `isLinearElementType` in
the canonical type-check module. [All 316 bindings in one placement](https://app.buildbuddy.io/invocation/06adc7df-16c6-49b6-b80f-1082a9c6fefa)
passed the direct selector handoff, structural certificate, disk recheck and
Chromium replay.

## What limited the candidate set

To select a trial, join the canonical Rust graph to the scope-annotated original
editor by source ordinal. For each canonical unit, record its declaration kind,
module bindings it references (including in closures), and which bindings the
original entry exports. Apply the following filters in order. These are candidate
selection counts, not 158 independently rejected Lean runs.

| Filter                                               | Units | Example and reason                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------------------------------- | ----: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Hoisted function declarations                        |    25 | Moving the full angle module reached `degreesToRadians`. The structural move rule expects a local allocating declaration; a hoisted function is already present before evaluation. [Lean refusal](https://app.buildbuddy.io/invocation/10de4651-3fb8-4976-9254-126dd29cafb6)                                                                                       |
| An earlier source unit refers to the binding         |    42 | `isFreeDrawElement` captures `isFreeDrawElementType`. The [move checker](../../lean/EsmSplit/CoreJS/LinkedPivotCheck.lean) requires earlier units to have no access to the moved region. A reference in a deferred body counts. [Lean refusal](https://app.buildbuddy.io/invocation/69a56768-5f93-452b-8a9d-8ea7727ea7fd)                                          |
| Needs a private binding that would stay in the entry |    91 | Iterate to a fixed point: if a selected unit needs a private entry binding whose owner cannot move with it, drop that unit and revisit its consumers. For example, moving `calculateScrollCenter` adds three editor exports; moving only `centerScrollOn` preserves the namespace. [Scroll export comparison](README.md#strict-certification-of-the-scroll-module) |

This left 114 candidates. The Rust gate and Lean then checked concrete subsets.
The following blockers came from those runs:

| Bindings                                                     | Observed blocker                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cachedCjkRegex`, `cachedLineBreakRegex`, `cachedEmojiRegex` | Rust requires each cache and the function that assigns it to share an owner. The 114-binding trial split all three pairs. The corresponding writers are `containsCJK`, `getLineBreakRegex` and `getEmojiRegex`; they have private dependencies in the entry. [Rust rejection and unit pairs](https://app.buildbuddy.io/invocation/92f3992e-746a-41b5-b040-148b68ca3755) |
| `resizeImageFile`                                            | The helper contains relative dynamic imports. The deployment check rejected moving it away from the original editor URL. [Lean refusal](https://app.buildbuddy.io/invocation/b76a3d3e-7ede-4690-ba11-978fcaafaede)                                                                                                                                                      |
| `COMMON`, `EMOJI`                                            | These text-wrapping regex tables did not pass the local-allocation check. [COMMON refusal](https://app.buildbuddy.io/invocation/70798918-ca88-4af1-8320-ebf95a3d2839) · [EMOJI refusal](https://app.buildbuddy.io/invocation/736a4877-db12-4d6d-b756-c220a4883473)                                                                                                      |
| `isLinearElement`                                            | Its region is already reachable in the initial linked state. A function declaration later in source order captures this binding, so checking earlier source references alone did not screen it out. [Lean refusal](https://app.buildbuddy.io/invocation/4c804fb2-54b2-4118-a811-f5f29bf3261d)                                                                           |

### Why an object allocation and a later property read cannot simply move as a group

`DEFAULT_FONT_FAMILY = FONT_FAMILY.Excalifont` needs a fact about the object at
the instant of the read. Consider `const table = {value: 1}; const alias = table;
alias.value = 2; const chosen = table.value;`. Moving the first and last
declarations together changes `chosen` from 2 to 1 even though both final
tables contain 2. Replacing the write with a read makes the same placement benign.
[Neutral runtime comparison and checker refusals](https://app.buildbuddy.io/invocation/1dede4aa-a7ef-4e91-b0ac-9d064006de79).

The current frame rule can equate cells holding an external address without
equating its property values. A proof for the benign case must establish an own
data descriptor and value after the common prefix, then show that the crossed
code preserves that lookup through every alias. Merely selecting the allocator
in the same module does not establish either condition.

## Character ranges and the RTL constructor

On editor SHA-256 `fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`,
full-context Rust selectors for the upstream `packages/excalidraw/utils.ts` resolve
`RS_LTR_CHARS` (`r7`, #562), `RS_RTL_CHARS` (`U7`, #563), `RE_RTL_CHECK`
(`l7`, #564) and `isRTL` (`hs`, #565). Adding just the first two to the
278-binding utilities proposal [passed emission, disk checking and Chromium](https://app.buildbuddy.io/invocation/ab384ba5-99e2-429a-a35f-15076d354d23).
This uses the existing rule for string literals.
The [four-binding trial](https://app.buildbuddy.io/invocation/b584ed7c-8f10-48af-a37b-725df6b5f69a)
refused the move of #564.

The original initializer is:

```js
new RegExp(`^[^${r7}]*[${U7}]`);
```

`linkedTemplate` converts each substitution to a string before adding the
next cooked segment. [StringInitializer](../../lean/EsmSplit/CoreJS/StringInitializer.lean)
proves the result and absence of effects **given the values of the reads**.
The two inputs are module `var` cells in different regions from the result.
A move proof still needs to show the corresponding writes have run in both
schedules and all crossed code preserves those cell values, including code
with previously captured or published handles. Moving a template ahead of
`var text = "a"` can otherwise substitute `"undefined"`.

The browser-page profile currently stops at the ambient `RegExp` callee,
before evaluating the argument. Even a stable binding identity would need
a separate construction rule: given the actual intrinsic as callee and new
target, compile the established string, throw on an invalid pattern, or
allocate a **fresh** object with the intrinsic prototype, matcher data and
its own writable `lastIndex` initialized to zero. The object's `.source`
must use the escaped pattern representation (including `(?:)` for the
empty pattern). CoreJS already allocates that object for a _parsed literal_;
copying an arbitrary constructor argument into its literal data would skip
both syntax errors and escaping. A replacement constructor or callee getter
can run code before or instead of native construction. The RTL placement
does not justify treating the global name as a built-in.

The [linked record-prefix rule](https://github.com/agentydragon/ducktape/pull/19813)
now establishes that the `FONT_FAMILY.Excalifont` own data property has the
same value at the two reads. `DEFAULT_FONT_FAMILY` therefore moves to its
upstream [constants.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/constants.ts#L181)
owner alongside all 316 earlier bindings, with the same browser contract.
[317-binding certificate, disk check, and Chromium replay](https://app.buildbuddy.io/invocation/32ce1694-d870-449f-ba52-2de1080bc4d1).

The earlier [consumer trials](https://app.buildbuddy.io/invocation/1e01909b-5cc5-4816-a7f9-8de5a1d20b8a)
still required additional entry exports for `newTextElement` (its
`_newElementBase` dependency) and `getDefaultAppState` (two private cells).
Certifying the font-family read does not recover those dependencies.

The [combined size, resize and change replay](https://app.buildbuddy.io/invocation/4b46901a-e613-4f07-942e-ff1ad7b08739)
selects 324 bindings in 36 owners. It moves `Delta` back to
[`change.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/change.ts)
and the [six size and resize bindings](proposals/size_resize/README.md)
to their original modules. Chromium checks line and square sizing and the
cursor returned for rotation, a rotated element, and a reflected element.

The [327-binding replay](https://app.buildbuddy.io/invocation/d70c9190-b6e9-440e-b47f-24bea32a4efc)
adds `SIDE_RESIZING_THRESHOLD`, `EPSILON`, and `DEFAULT_COLLISION_THRESHOLD`
in upstream [constants.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/constants.ts#L185).
The prefix establishes the numeric spacing before the first multiplication;
the later subtraction uses the initialized side threshold and epsilon. The
[proof for numeric cell reads](https://github.com/agentydragon/ducktape/pull/19831)
uses the existing deployment contract. The production selector spells the
epsilon literal `1e-5`, as it appears in the pinned bundle.

## Recovering the geometry constructors

The pinned source defines `line`, `lineSegment` and `rectangle` as two-element
array constructors in three different modules. The
[selector fragments](proposals/math_geometry) distinguish them by the adjacent
intersection or distance calculation. The fourth module,
[`math/curve.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/math/curve.ts),
contains the four-point constructor, finite-difference gradient, solver and bounds.
Moving the constructors to their upstream homes lets
[`deconstructRectanguloidElement`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/utils.ts)
import them without adding any public editor name. The
[combined placement](https://app.buildbuddy.io/invocation/d9a99730-6907-455f-b609-fc2c90beeb42)
certified nine additional bindings.

The same full-context solve resolves additional geometry bindings. Moving
`pointOnLineSegment` with `distanceToLineSegment` in the
[larger trial](https://app.buildbuddy.io/invocation/fb144d58-fb9a-4c64-b3e9-de19b4e6620c)
failed: the linked initial state already exposes the region of
`pointOnLineSegment` (517). Leaving the segment predicates and their private
consumers in the entry then exposed the same proof condition for
[`bezierEquation` (525)](https://app.buildbuddy.io/invocation/78f1f928-788b-43b5-9a68-f2bf165df18d).
These are proof refusals, not observed behavioral differences.

The [observer-chain proof](https://github.com/agentydragon/ducktape/pull/19889)
accounts for hoisted observers in dependency order. For example, the
`curveIntersectLineSegment` closure can reach `rectangleIntersectLineSegment`,
which can reach `lineSegmentIntersectionPoints`, which captures
`pointOnLineSegment`. The [linked probe](https://app.buildbuddy.io/invocation/173feb9b-e2dc-4d81-b7e6-b7839b698081)
checked that the three observers could be isolated, starting with the outermost.
The [combined 344-binding replay](https://app.buildbuddy.io/invocation/460d6629-1056-48f1-aefb-19f483b54515)
then certified all eight previously omitted segment, rectangle and Bézier helpers
together. The proof does not require an additional host assumption or a different
module order.

## Transform handles and the resize hit tests

The [353-binding replay](https://app.buildbuddy.io/invocation/b8bd7863-1139-4bf2-ac9f-eb005e5c2f18)
adds nine declarations from upstream [transformHandles.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/transformHandles.ts):
handle sizes, the rotation gap, three omission masks, three handle builders,
and `shouldShowBoundingBox`. Chromium constructs handles for a desktop
rectangle and checks its northwest corner and east side through the editor's
public hit-test functions.

The resize hit tests themselves still need private entry bindings. Including
`getSelectionBorders` and `canResizeFromSides` leaves `isAndroid` in entry.
[Separate hit-test placements](https://app.buildbuddy.io/invocation/0c429b2e-0fc7-4121-8826-a7e0cdfd1651).
A stable user-agent string alone does not justify moving the regex test that
initializes this flag. In a [Chromium control](https://app.buildbuddy.io/invocation/61053222-ff13-406a-9b44-61c9278006c5),
exchanging two native matches changes the realm's legacy match captures even
when the initializer returns the same boolean and leaves the same `lastIndex`.

A separate trial assigned `deconstructDiamondElement` to `element/utils` and
`getDiamondPoints` to its upstream `element/bounds` module. Both selectors
resolved. Lean refused before emission because the linked initial state already
exposes the region of `getDiamondPoints` (unit 1252).
[Diamond placement refusal](https://app.buildbuddy.io/invocation/6636793f-2c68-4853-ae8b-f3175fcb3d8a).

## Computed font keys and the default sidebar

`FONT_FAMILY_FALLBACKS` constructs a fresh object using two previously
initialized font names as property keys. `DEFAULT_SIDEBAR` uses an earlier
string binding as its `defaultTab` value. Both belong to upstream
[constants.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/constants.ts).
The [record creation proof](https://github.com/agentydragon/ducktape/pull/19900)
checks the actual linked prefixes before specializing either read. The
[355-binding combined replay](https://app.buildbuddy.io/invocation/50fd3de4-ed08-45b7-bab6-d6c47ba006b8)
uses the same host contract and upstream module owners as the transform-handle
placement. The selector requests both declarations by their upstream names.
