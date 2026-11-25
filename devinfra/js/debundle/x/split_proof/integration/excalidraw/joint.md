# Certified Excalidraw deployment

Run `bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_certified_browser_smoke`.

One [ownership subset](../../../../excalidraw/deployment_smoke_subset.json)
selects `strokeEllipseWithRotation` from main and 365 editor bindings. Rust
exports canonical and selected placements from the same selector solve. Lean
checks their source digests and original unit identities, stages both splits
and the seven fixed static providers, reparses the complete output, and
certifies one initialization schedule rooted at `main.js`.

The proof checks both original public namespaces under the recorded
browser-page, native metadata, stable `Math.PI@1`, and empty import-map
premises. A separate disk check verifies the same deployment. Chromium loads
the original and checked trees in fresh contexts, exercises drawing and undo,
and checks extracted renderer, editor, and geometry functions. Lazy files
outside the static overlay resolve against the original bundle.
