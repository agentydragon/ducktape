# Locating the pinned editor's heading helpers

Nine bindings from [`packages/excalidraw/element/heading.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/heading.ts) pass Lean certification in their upstream owner. The [named delta](named_delta.json) adds the four cardinal directions, `headingForDiamond`, `vectorToHeading`, `headingForPoint`, `compareHeading`, and `flipHeading`. The complete context resolves three further names whose moves did not certify.

## Start with the pinned source

Read [the declarations in `element/heading.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/heading.ts#L19-L74) in the upstream v0.18.1 checkout (commit `a2ec2889babf7d2295469c6d90ebe77fae57df84`). The file declares the four direction vectors. Both the diamond-angle and vector classifiers return those same vectors; `headingForPoint` passes a point difference to the vector classifier. `compareHeading` compares both components, and [`flipHeading`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/heading.ts#L198-L202) reverses each nonzero component.

Two callers explain how the declarations fit together. [`element/elbowArrow.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/elbowArrow.ts#L418-L445) derives the headings of consecutive segments and compares them directly and after a flip to distinguish straight runs from reversals. [`scene/Shape.ts`](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/scene/Shape.ts#L544-L554) uses `headingForPointIsHorizontal` on both sides of a bend. These consumers import helpers whose declarations belong to `element/heading.ts`.

## Production evidence

The [production selector](packages/excalidraw/element/heading.yaml) matches a connected group: the directed vectors in right/down/left/up order; `atan2` of the two point differences with 45°, 135°, 225° and 315° cutoffs; the named vectors actually returned by each branch; the component-wise equality used on both horizontal directions; and the sign reversal of each nonzero component. The point helper calls the recovered vector classifier on the result of `vectorFromPoint`. The diamond shape test refers back to the same four vectors. An isolated two-number array does not establish a direction's upstream name.

The [joint resolver](https://app.buildbuddy.io/invocation/4ccea516-3494-420e-8904-63ec516eb51a) identified the twelve declarations at production ordinals 1225–1236 in the full authored context, on editor SHA-256 `fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`. Changing only the 315° cutoff to 314° gave [`no_match` on the same production bytes](https://app.buildbuddy.io/invocation/ef49a602-3a37-4b4b-b459-99b5469e8c82). Ordinals here report the result of resolution; the proposal uses names.

For the passing replay, use the 279-name selection from [#19771 at e9f696f3](https://github.com/agentydragon/ducktape/blob/e9f696f311224a7d03915560f9e59b3e5098839f/devinfra/js/debundle/excalidraw/editor_smoke_subset.json). In a scratch copy at `cf849003`, place the selector under the editor spec's `packages/excalidraw/element/` and insert `packages/excalidraw/element/heading` immediately after typechecks in the editor import priority. Append the delta module to the selected modules and give that separate subset to `selected_editor_placement`. Retain the existing scroll proposal fragments. The full context and subset must resolve on the same production source.

Run `bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_certified_editor_browser_smoke`. [Lean emission, separate disk verification and Chromium](https://app.buildbuddy.io/invocation/8864858f-980e-4b5a-bcb1-54e7c882a941) passed for **288 of 1,787 units in 29 owners**. The checker proved normal-completion equivalence under the ordinary browser-page contract and checked deployment origin. The browser compared the original and split editor's 611-name public namespace and exercised drawing and Undo. The replay additionally verified that the heading module exported the four distinct direction vectors and that the entry exposed the same array objects.

[Moving all twelve](https://app.buildbuddy.io/invocation/e573fccf-e7fa-4d7b-a48f-e4bfaa2008ca) would expose `triangleIncludesPoint` (540) and `getCenterForBounds` (1272) as extra entry exports through `headingForPointFromElement` (1235). [Removing only 1235](https://app.buildbuddy.io/invocation/6b2dd8da-d838-4d03-8af5-2de04afa1ffb) reaches a different refusal: `headingForPointIsHorizontal` (1232) has access to the moved region of `headingIsHorizontal` (1234). The nine-member placement leaves all three in the entry. These refusals describe the tested placements.

The production replay needs the pinned JS, the named selection, the full Rust selector context, and the host and deployment contracts. Its [recorded run](https://app.buildbuddy.io/invocation/8864858f-980e-4b5a-bcb1-54e7c882a941) completed before either development-map target was present in the checkout.

## Optional development-map diagnostic

In the lockfile-pinned `@excalidraw/excalidraw@0.18.1` tarball, all 65 development JS files have sibling maps; none of the 65 production JS files do. To inventory a development chunk:

```sh
p=//devinfra/js/debundle/x/split_proof/integration/excalidraw
bb run "$p:mapped_bindings" -- "$PWD/package/dist/dev/chunk-4FTI6OG3.js" | jq 'select(.source == "../../element/heading.ts")'
```

The tool asks the map for the location of each simple top-level declaration identifier, retaining exact generated-position hits. It prints the development binding and the one-based source line and column. [The real-map check](https://app.buildbuddy.io/invocation/3674b793-ddf8-405e-b264-46e6927c29b0) found **1,193 bindings in 100 sources**, including twelve in heading.ts. The map's heading source content and the upstream tag file both have SHA-256 `eca58a7ecc9f4fbfe14561ce4887c2593c13e1842738bbbf2dabcafc19f679ce`.

Development line numbers do not transfer to production. For example, development emits separate variable declarations and `if` branches for the diamond angle; production joins the declarations and uses nested ternaries. The TS export `headingIsVertical` has no standalone declaration in the mapped development group.
