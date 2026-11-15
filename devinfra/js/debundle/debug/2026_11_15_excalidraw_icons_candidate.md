# Excalidraw icons extraction candidate

The pinned `@excalidraw/excalidraw` 0.18.1 source map
`dist/dev/chunk-4FTI6OG3.js.map` contains the original
`packages/excalidraw/components/icons.tsx`. It has 184 top-level declarations:
179 exports (the two helpers `iconFillColor` and `createIcon`, plus 177 icon
values) and five private helpers. In the editor chunk these map in source order
to `unit:677` through `unit:860`. The separate dependent candidate
[`icons.yaml`](https://github.com/agentydragon/ducktape/blob/codex/debundle-upstream-icons/devinfra/js/debundle/excalidraw/spec/modules/editor/packages/excalidraw/components/icons.yaml)
claims all 184 declarations. Its binding selectors pin the current bundle's
minified names and explicitly make no stability claim across a rebuilt bundle.

The original project analysis resolved the full candidate (`:analysis`, [invocation](https://app.buildbuddy.io/invocation/dfe63b40-6733-4905-bde7-82f5e2bde28a)). The original
`//devinfra/js/debundle/excalidraw:debundle` gate rejects it in
`materialize_logical_modules`: a 410-unit atomic group is split between
`anon_residual_sentinel` and `packages/excalidraw/components/icons` ([invocation](https://app.buildbuddy.io/invocation/d501496a-3291-4ec8-9cfb-5d879171934d)). This matches the prior one-icon candidate's group conflict, so this full candidate does not produce emitted modules yet.

The smallest current graph witness is the three-unit cycle `unit:682` →
`unit:675` → `unit:1132` → `unit:682`. `unit:682` is the `PlusPromoIcon`
initializer; `unit:675` is an unrelated class-field registration; and
`unit:1132` is a later function. The first edge is a sequenced
`binding_timeline` edge on `Ed`, promoted through a possible getter and widened
fallback effects. The second is `eager_use` of `Sa`. The last is a sequenced
`binding_timeline` edge on `Sa`, with a possible-getter fallback. The whole
atomic group also included 176 other public icon values, two private SVG
fragment helpers, and 231 unrelated editor units.

This resembles the reduced real-CLI case in
[`cross_chunk_operation_parity_test.rs`](../e2e/cross_chunk_operation_parity_test.rs):
an imported `field` stores a callback, an imported `node` returns the icon
value, and a later callback target closes the inferred ordering cycle. That
synthetic case demonstrates the same broad fallback edge pattern, not the same
underlying cause. Excalidraw's field helper is `T` from
`chunk-DIC2VAWC.js`; its icon initializers also call the bundled JSX runtime
and `createIcon`, while the graph reports a callback-pool-limit loss.

The candidate's
[`spec_output_test.rs`](https://github.com/agentydragon/ducktape/blob/codex/debundle-upstream-icons/devinfra/js/debundle/excalidraw/spec_output_test.rs) checks the
expected module path, exact 184-unit ownership range, representative exports,
and the `PlusPromoIcon` React element under Node. The existing browser smoke
[`test_smoke.py`](https://github.com/agentydragon/ducktape/blob/codex/debundle-upstream-icons/devinfra/js/debundle/excalidraw/test_smoke.py) probes the same icon from the
emitted module. Both targets depend on the real `:debundle` result, so they
have not passed on the shared-execution engine; they are not evidence of
accepted output today. Canonical analysis did not produce an artifact within
the bounded runs recorded in
[the execution profile](2026_11_15_availability_trace_storage.md). This prevents
checking whether the original atomic-group conflict persists. The engine PR
does not include the candidate spec or its output/browser assertions.

The five upstream-private helper values
must be named as members for this candidate to claim their source units, which
also exposes those names as generated module exports.
