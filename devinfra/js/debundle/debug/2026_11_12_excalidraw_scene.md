# Excalidraw Scene: class relocation and residual initialization

The Excalidraw 0.18.1 `Scene` class passes ordinary extraction to its original
`packages/excalidraw/scene/Scene.ts` home when its generated static-map writes
remain in the editor entry. The emitted entry imports `Scene`, initializes
`sceneMapByElement` and `sceneMapById` at their original position, then assigns
the original class aliases. The class imports its still-residual source helpers.
This recovers the class body without claiming the complete upstream module.

The full source-family candidate resolves five bindings and one anonymous
statement: `getNonDeletedElements`, `validateIndicesThrottled`,
`hashSelectionOpts`, `isIdKey`, `Scene`, and the combined static writes.
Assigning all of them to the Scene home rejects with a 410-unit atomic group.
Removing only the throttled initializer still rejects. The class plus static
writes also rejects, with only the static-write statement (`unit:1693`) in
that group; the class (`unit:1692`) is outside it. The three ordinary helpers
also reject when selected without the class or static writes.

One helper-only witness runs from residual `unit:675` through `unit:1206`
to `getNonDeletedElements` (`unit:1688`), then back through sequenced fallback
edges. The report cites callback-pool limits (pool sizes 398 and 316).
The Blob/File investigation records a related path through
`validateIndicesThrottled` in the same group. These observations identify the
current assignment constraint, not a demonstrated runtime dependency or an
engine defect.

To continue, add structural claims for these helpers and the static-map writes
to `excalidraw/spec/modules/editor/packages/excalidraw/scene/Scene.yaml`, then
build `//devinfra/js/debundle/excalidraw:debundle`. Trace the report's callback
publication path and reduce it to a neutral CLI fixture before changing
analysis or adding a trust annotation. Preserve the passing class relocation
and test static lookup, scene updates and cleanup in the emitted browser app.
