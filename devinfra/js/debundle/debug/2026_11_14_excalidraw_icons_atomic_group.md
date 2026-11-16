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
and returns it; its body does not directly call a callback passed as the value.
Assignment to a host-provided accessor can still run code. The graph's
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

The reduced fixture has a narrower, experimentally verified cause. In its
shared peer chunk, both helpers are `export const` arrow functions.
`isolated_callable_initializer` accepts a const callable only when its
declaration is the provider's sole declaration, with no other statements or
imports, so `node` cannot get an imported callable summary in this form.
`field` also has no summary; its global assignment has separate host-effect
questions. The `field` and `node` calls are therefore both unresolved;
the opaque-callback fallback can invoke the stored `() => later` while `node`
initializes `icon`. This gives `unit:2` an assumed eager read of `later` and a
writer-after-reader timeline edge from `unit:3`; the analogous opposing edges
between `unit:1` and `unit:3` complete the three-unit atomic SCC. These are
conservative inferences under the missing summary, not evidence that `node`
actually invokes the callback.

A focused CLI probe changed only `node` to `export function node(tag) { return
tag; }` in the same peer chunk. The report then derived `node` as a foreign
call returning argument 0 with no retained arguments, the requested icon
split passed the gate, and original and emitted Node output were both `svg`.
Keeping `node` as a const arrow but placing it alone in its own imported chunk
gave the same summary and accepted output. The original two-const form still
had no imported callable summary and rejected the split. Thus a sound `node`
call summary alone suffices for this fixture; a summary for `field` was not
needed. The probe used an uncommitted diagnostic test in a detached scratch
worktree, not a change to the checked-in regression test
([passing RBE invocation](https://app.buildbuddy.io/invocation/703b105f-8eb9-4031-bfc1-7ae3d30157f0)).

A prospective engine fix is a per-binding initializer-availability proof that
can recognize an independent const callable in a multi-declaration provider,
while retaining the existing body, value-flow, capture and module-order
checks. Simply treating every imported call as pure or every const callable
as available would discard real callback, getter and host effects. The editor
still has unresolved calls through its JSX namespace and `createIcon`, plus
the large fallback callback pool and the `Ed`/`Sa` ordering cycle. The fixture
result does not establish that the pinned editor icons split passes the
canonical gate or browser smoke.

Evidence: `bbr build //devinfra/js/debundle/excalidraw:debundle
--remote_build_event_upload=all` with the one-icon diagnostic spec failed in
`materialize_logical_modules` ([BuildBuddy invocation](https://app.buildbuddy.io/invocation/1cf23234-d11c-4936-abb7-6629c7705da7)); the full claim also failed there
([invocation](https://app.buildbuddy.io/invocation/dd331937-2044-44a5-a5ef-6cd519fb3a11)).
The reduced fixture's focused `bbr test
//devinfra/js/debundle/e2e:interop_factory_icon_test --test_output=errors`
had two passing controls and the desired split failing at the atomic-conflict
stage ([invocation](https://app.buildbuddy.io/invocation/41e57f1d-f76e-4d32-bf98-2bb5a2258a31)).
