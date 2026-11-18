# Excalidraw bounds extraction boundary

Six original exports from Excalidraw 0.18.1 `element/bounds.ts` resolve through
structural source selectors. `getVisibleSceneBounds` and `doBoundsIntersect`
pass the full gate together in their original home. The other four candidates
remain residual: `getBoundsFromPoints` (`unit:1257`), `getArrowheadSize`
(`unit:1259`), `getArrowheadAngle` (`unit:1260`) and `getCenterForBounds`
(`unit:1272`). Other exports from that source file have not been tested here.

Assigning all six rejects with a 410-unit atomic group crossing the bounds
home and residual editor entry. One witness runs from `unit:675` through
`unit:1206` to `unit:1257` (`getBoundsFromPoints`), then through sequenced
edges back to the residual. The Scene and Blob/File notes record related
constraints in that group. This is the current gate's constraint, not proof
that the upstream bounds functions have an actual initialization cycle.

To continue, add structural selectors for the four upstream functions to
`excalidraw/spec/modules/editor/packages/excalidraw/element/bounds.yaml` and
build `//devinfra/js/debundle/excalidraw:debundle`. Trace the witness's callback
publication path and reduce the rejection to a neutral CLI fixture before
changing analysis or supplying an initialization contract. Preserve the
passing viewport/intersection module and its Node behavior checks.
