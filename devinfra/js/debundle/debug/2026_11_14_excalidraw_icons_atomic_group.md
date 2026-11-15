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

The reduced CLI test `//devinfra/js/debundle/e2e:interop_factory_icon_test`
has a three-unit atomic conflict: `unit:1` (field registration), `unit:2`
(imported `node("svg")` icon initializer), and `unit:3` (later callback
target). Its [conflict report](https://app.buildbuddy.io/invocation/57474597-a335-4197-8366-77cd977de7d6)
shows `eager_use` through `later` and `sequenced` `binding_timeline` edges
whose fallback path is `opaque_callback_invocation`. The desired test asks
Debundle to emit only `icon` in `icons.js` and compare Node output with the
source; today it stops before emission with exit 1. The same source with no
extraction passes and prints `svg`, as does a hand-written ESM split that
executes the icon module before residual field registration, then invokes the
stored callback and the returned `later` function. The hand-written split is
a bounded behavioral witness for this input, not generated output or a general
proof of safe reordering.

CLI reduction sweeps ([first](https://app.buildbuddy.io/invocation/226795fa-f218-4328-b760-747f893d7a86),
[second](https://app.buildbuddy.io/invocation/6cd623e3-aa05-43b9-b11e-9ec68bf2d296),
[third](https://app.buildbuddy.io/invocation/9e48e54e-411f-41ae-b358-9b6eb7d1d244))
tested individual source and spec changes. Removing the original local
`create` call, class target, or output did not remove the conflict. Replacing
the field helper with a one-argument imported callback storer also retained
the three-unit conflict; that is the smallest tested three-unit fixture with
observable `svg` output. In the class-based three-unit candidate, removing
the callback, replacing the imported `node` call with a literal, making
`node` local, or moving field registration after icon initialization allowed
extraction. Making `field` local still failed, now with a two-unit group;
making both helpers local in the original fixture allowed extraction.
Changing the original residual mode among coarse, per-group, catchall, and
inline did not remove the conflict. Extracting `create` instead of `icon`
was accepted, so the selected member matters.

A still smaller direct-capture variant, `field({}, "get", () => icon); const
icon = node("svg")`, fails with a two-unit group. In that variant even a
literal icon value remains stuck: it no longer tests the intervening `later`
binding or the imported JSX-call fallback of the retained three-unit shape.
The checked-in fixture invokes its stored callback to make storage
observable. Its one-argument helper captures `__publicField`'s callback
timing, not its full class/property behavior. This is a minimum along tested
reductions of the chosen three-unit causal shape, not a proof of global
minimality. The synthetic fallback path also differs from the editor's `Ed`
effect and possible-getter path; matching edge kinds do not establish
identical causes.

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
The reduced fixture's focused `bbr test
//devinfra/js/debundle/e2e:interop_factory_icon_test --test_output=errors`
had two passing controls and the desired split failing at the atomic-conflict
stage ([invocation](https://app.buildbuddy.io/invocation/41e57f1d-f76e-4d32-bf98-2bb5a2258a31)).
