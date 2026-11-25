# Editor placement inventory at 306 selected bindings

Adding `packages/excalidraw/element/typechecks::isLinearElementType` (source binding
`gs`, unit 621) to [the combined 306-binding selection](https://github.com/agentydragon/ducktape/pull/19787)
certified **307 units in 32 owners**. This was the largest passing placement among nine
proposals on commit `7085c5b7`. The [306 baseline](https://app.buildbuddy.io/invocation/0ce8960d-1c80-4a2e-8853-750df5254da0)
already included the seven utilities at 541–547. The delta here is one binding.

The [nine RBE trials](https://app.buildbuddy.io/invocation/fcefcf77-e3a8-4e93-b0e3-f971df606e0c)
used the same [paired selector exports](https://app.buildbuddy.io/invocation/8cefb09c-f49d-4f23-8257-87ba426be31d),
editor chunk and authored import priority. The source was `chunk-PN7KOLDJ.js`,
SHA-256 `fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`.
The `retry_linear` target certified structural normal completion at emit and disk recheck
with a browser-page host, stable `Math.PI@1`, and the test server's deployment origin.
In Chromium both original and overlay passed the editor smoke: their 611 export names
matched, including the identity of the scroll helper; drawing and undo controls worked.
The other eight targets **observed rejections** and checked that the emitter published no output.

## How candidates were chosen

From the selector-resolved named units, inspect top-level declaration and initializer kinds,
original export names and free references (including nested function bodies). In the earlier
299-binding view, keeping functions, simple allocations and bare declarations, then dropping
units with a private entry dependency outside the candidates, left 24 named units.
Dropping units with earlier retained references and repeating dependency closure left ten.
These screens suggest groups; neither predicate proves a safe move. The [299 trials](https://app.buildbuddy.io/invocation/ef31de92-e98e-433f-b67d-85a70639088e) and
[follow-up](https://app.buildbuddy.io/invocation/b74cd11f-2444-4bdd-88a7-091fdf237cf1) exposed distinct obstacles:
moving `resizeImageFile` changed the location of a relative dynamic import, and the
closure for `copyTextToSystemClipboard` crossed an earlier observer. The single
`isLinearElementType` move passed. Those observations refer to 299, not 306.

On the 306 baseline the trial bank retested that move, then added one small group at
a time from different declaration shapes or dependency relationships. For example,
`COLOR_PALETTE` is an object initializer, the text caches are bare declarations with writers,
and the two `deconstruct…Element` functions are hoisted declarations with private captures.
[trials.json](trials.json) records every exact set of added names, the result and the
checker's first reason. The bank did not enumerate partitions or change owners or priorities.
A named unit can occur in a refused group without being individually impossible to move.

## Accounting and replay

The full selector view assigned 434 of the 1,787 units to 41 owners. In the measured
conjunction 307 were certified; another 13 named units occurred only in refused groups,
and 114 remained untested on this baseline. The remaining 1,353 stayed in entry without
a selector-resolved owner. The inventory makes no upstream ownership inference for them.

At `7085c5b7` build both requested outputs in the same invocation. Keep the resulting
JSON bytes unchanged. The script checks their pinned file hashes and shared source
fingerprint before interpreting the measured outcomes.

```sh
bbr build --remote_build_event_upload=all \
  //devinfra/js/debundle/excalidraw:selected_editor_placement \
  //devinfra/js/debundle/excalidraw:canonical_editor_placement

python3 inventory.py canonical_editor_placement.json selected_editor_placement.json > units.ndjson
python3 inventory.py canonical_editor_placement.json selected_editor_placement.json --trial linear > proposal.json
```

Each inventory row carries the source ordinal, original canonical owner, available
source-to-readable member map, status, and all relevant trial labels. For a fresh check,
use the original hashed editor filename beside its providers and dynamic targets. Supply a
valid deployment origin for that static tree and the same host contract:

```sh
echo '{"profile":"browser-page","stableIntrinsicValues":["Math.PI@1"]}' > host.json
jq --arg path "$(basename "$source")" --arg source "$source" \
  '{entry:$path, chunks:[{path:$path, source:$source, placement:.}]}' proposal.json > deployment.json
lowersplit deployment.json out --host-contract host.json --deployment-origin "$origin"
lowersplit deployment.json out --host-contract host.json --deployment-origin "$origin" --check-output
```

## Units per canonical owner

The final two columns distinguish participation in a refused proposal from a unit for
which no 306-baseline addition was tried. See the corresponding inventory rows for names.

| Canonical owner                                   | Total | In certified 307 | In refusals only | Untested |
| ------------------------------------------------- | ----: | ---------------: | ---------------: | -------: |
| `packages/excalidraw/appstate`                    |     1 |                0 |                0 |        1 |
| `packages/excalidraw/change`                      |     3 |                0 |                1 |        2 |
| `packages/excalidraw/clipboard`                   |    13 |                8 |                0 |        5 |
| `packages/excalidraw/colors`                      |     3 |                2 |                1 |        0 |
| `packages/excalidraw/constants`                   |    78 |               76 |                1 |        1 |
| `packages/excalidraw/data/blob`                   |    21 |                9 |                0 |       12 |
| `packages/excalidraw/data/encode`                 |     4 |                2 |                0 |        2 |
| `packages/excalidraw/data/json`                   |     8 |                7 |                0 |        1 |
| `packages/excalidraw/data/restore`                |    15 |               11 |                0 |        4 |
| `packages/excalidraw/element/bounds`              |     7 |                7 |                0 |        0 |
| `packages/excalidraw/element/collision`           |     9 |                3 |                0 |        6 |
| `packages/excalidraw/element/cropelement`         |     6 |                1 |                1 |        4 |
| `packages/excalidraw/element/linearelementeditor` |     1 |                0 |                0 |        1 |
| `packages/excalidraw/element/newelement`          |    21 |                4 |                0 |       17 |
| `packages/excalidraw/element/resizeelements`      |     5 |                0 |                0 |        5 |
| `packages/excalidraw/element/textmeasurements`    |     7 |                2 |                0 |        5 |
| `packages/excalidraw/element/textwrapping`        |    21 |                2 |                3 |       16 |
| `packages/excalidraw/element/transformhandles`    |     2 |                0 |                0 |        2 |
| `packages/excalidraw/element/typechecks`          |    29 |               29 |                0 |        0 |
| `packages/excalidraw/element/utils`               |     2 |                0 |                2 |        0 |
| `packages/excalidraw/emitter`                     |     1 |                1 |                0 |        0 |
| `packages/excalidraw/fonts/excalidrawfontface`    |     1 |                0 |                0 |        1 |
| `packages/excalidraw/fonts/fonts`                 |     1 |                0 |                0 |        1 |
| `packages/excalidraw/groups`                      |     9 |                9 |                0 |        0 |
| `packages/excalidraw/random`                      |     4 |                2 |                2 |        0 |
| `packages/excalidraw/renderer/renderelement`      |    11 |                6 |                0 |        5 |
| `packages/excalidraw/renderer/staticscene`        |     7 |                4 |                0 |        3 |
| `packages/excalidraw/renderer/staticsvgscene`     |     4 |                2 |                0 |        2 |
| `packages/excalidraw/scene/comparisons`           |     6 |                6 |                0 |        0 |
| `packages/excalidraw/scene/normalize`             |     3 |                3 |                0 |        0 |
| `packages/excalidraw/scene/scene`                 |     1 |                0 |                0 |        1 |
| `packages/excalidraw/scene/scroll`                |     3 |                2 |                0 |        1 |
| `packages/excalidraw/scene/shape`                 |     4 |                4 |                0 |        0 |
| `packages/excalidraw/snapping`                    |    19 |                6 |                0 |       13 |
| `packages/excalidraw/store`                       |     7 |                4 |                1 |        2 |
| `packages/excalidraw/utils`                       |    64 |               62 |                1 |        1 |
| `packages/math/angle`                             |     4 |                4 |                0 |        0 |
| `packages/math/point`                             |    12 |               12 |                0 |        0 |
| `packages/math/utils`                             |     6 |                6 |                0 |        0 |
| `packages/math/vector`                            |    10 |               10 |                0 |        0 |
| `packages/utils/geometry/shape`                   |     1 |                1 |                0 |        0 |
