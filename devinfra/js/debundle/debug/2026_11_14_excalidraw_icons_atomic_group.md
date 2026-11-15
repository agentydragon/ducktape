# Excalidraw icons: an imported-call fallback joins the residual atom

The pinned `@excalidraw/excalidraw` 0.18.1 source is
`packages/excalidraw/components/icons.tsx`. Its editor chunk contains 184
corresponding source units, `unit:677` through `unit:860`. A diagnostic spec
claiming just the first icon, `PlusPromoIcon` (`ry`, `unit:682`), fails the
canonical `//devinfra/js/debundle/excalidraw:debundle` build in
`materialize_logical_modules`. The error is an atomic-group conflict between
`packages/excalidraw/components/icons` and `anon_residual_sentinel`. Claiming
all 184 declarations gives the same conflict. These are input-specific gate
results, not selector-resolution failures or a runtime measurement.

The editor dependency graph has an atomic group of 410 units: 179 icon units
(`unit:682` through `unit:860`) and 231 other editor units. The five preceding
icons declarations (`unit:677` through `unit:681`) are singleton atoms. One
short directed cycle in the group is:

1. `unit:682` (an icon initialized by `createIcon(jsx(...))`) → `unit:675`
   (static field registrations on a class): `binding_timeline`,
   writer-after-writer on `Ed`, from fallback effect coverage. `Ed` is an
   unrelated zlib-state binding.
2. `unit:675` → `unit:1132` (`Sa`, a later function binding): `eager_use`.
3. `unit:1132` → `unit:682`: `binding_timeline`, writer-after-reader on `Sa`.
   The read is promoted through a possible getter in a lazy owner.

The editor imports its field helper `T` from `chunk-DIC2VAWC.js`. That file's
export `p` stores the passed value through a property definition or assignment
and returns it; it does not invoke a callback passed as the value. The graph's
only imported callable summary is for `Ve`, not `T`. `unit:675` consequently
reports nine `unknown_call_target` losses for `T`. The icon initializer also
reports unknown calls and member reads through the JSX namespace and its
`createIcon` call. The callback pool is already above its limit of 64 (the
recorded pool size is 316–322 around these units), widening fallback effects.
The limit amplifies this corpus's SCC; the smaller end-to-end reproduction
below does not need pool exhaustion.

The synthetic CLI test `//devinfra/js/debundle/e2e:interop_factory_icon_test`
has a three-unit atomic conflict: `unit:2` (field registration), `unit:4`
(factory-produced icon), and `unit:5` (later callback target). Its rejected
graph witness contains both `eager_use` and `sequenced` edges. The desired
test asks Debundle to emit the icon module and compare Node output with the
source; today it stops before emission with exit 1. The same source with no
extraction passes and prints `svg`, as does a hand-written ESM split with the
icon in its authored module and the other code in illustrative residual
groups. The hand-written split is a bounded behavioral witness for this input,
not generated output or a general proof of safe reordering.

To make the desired split acceptable, the engine needs sound facts about the
imported field helper's callback timing and about the JSX/icon construction
effects sufficient to remove the spurious ordering cycle. An ordinary purity
assertion on all icon initializers would hide the unresolved call, result and
heap effects. The remaining source/spec pair must then pass the canonical
gate and browser smoke; this investigation has not established that result.

Evidence: `bbr build //devinfra/js/debundle/excalidraw:debundle
--remote_build_event_upload=all` with the one-icon diagnostic spec failed in
`materialize_logical_modules` ([BuildBuddy invocation](https://app.buildbuddy.io/invocation/1cf23234-d11c-4936-abb7-6629c7705da7)); the full claim also failed there
([invocation](https://app.buildbuddy.io/invocation/dd331937-2044-44a5-a5ef-6cd519fb3a11)).
The focused `bbr test //devinfra/js/debundle/e2e:interop_factory_icon_test
--test_output=errors` has two passing controls and the desired split failing
at the same atomic-conflict stage
([invocation](https://app.buildbuddy.io/invocation/525e4a65-4b98-4603-9269-aa0d3c871ca5)).
