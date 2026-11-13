# Excalidraw linear editor extraction boundary

The original `element/linearElementEditor.ts` class body (`unit:1386`) passes
the full gate in its original home. The adjacent statement (`unit:1387`)
initializes `POINT_HANDLE_SIZE` and six static function properties:
`getEditorMidPoints`, `updateEditorMidPointsCache`,
`getSegmentMidpointHitCoords`, `getBoundTextElementPosition`,
`getMinMaxXYWithBoundText` and `getElementAbsoluteCoords`.

Moving that statement with the class rejects with the existing 410-unit atomic
group. The source selector for the statement resolves; ownership is the rejected
step. The cycle report includes `unit:1387` to `unit:5` (`po`) via a sequenced
binding-timeline edge. Its source uses a fallback summary after
`callback_pool_limit` (limit 64, pool size 479). The initializer's calls to the
bundler's property-definition helper also retain `unknown_call` reasons.
These are current analysis constraints, not proof of an actual read-before-init
in the upstream class.

The canonical spec keeps that statement in the entry. The browser probe checks
the class's constructor, the initialized handle size, lookup, point coordinates,
rotation and normalization after loading the application. To reproduce the
rejection, add one `anonymous_statements` source match for the seven calls in
their listed order, using `LinearElementEditor` as the receiver and `ANYTHING`
for the helper and function values, then build the canonical `:debundle` target.
Reduce the callback publication/fallback witness before changing analysis or
supplying an initialization contract.
