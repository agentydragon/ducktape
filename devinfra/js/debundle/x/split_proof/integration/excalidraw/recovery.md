# Recovering the pinned Excalidraw editor's authored modules

The canonical Rust editor graph places **272 of 1,787 units in 35 named modules**.
The browser smoke selects readable binding names from those owners. Each trial
keeps all other units in the original entry, uses Rust's selected schedule, and
requests the structural `normal-completion` guarantee at the browser origin.
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
