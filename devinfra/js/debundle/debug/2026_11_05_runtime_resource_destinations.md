# Runtime resource destination boundary

The neutral `url_rebase_test` controls execute a lazy JavaScript chunk and read a
stylesheet through `new URL(..., import.meta.url)` in original, emitted and
browser-harness output. These are Node resource-loading controls; they do not
render CSS or start browser workers.

Before the destination-model change, lowering used the relocated entry directory
as the base for source literals. It also did not resolve captured lazy JavaScript
to its emitted `entry.js`. The dynamic-import and import-meta URL source records feed the
existing artifact import resolver; local asset paths retain source coordinates.
Each chunk's resolved destination map is shared across its logical modules.

Baseline with the new URL controls and unchanged engine at `7fc0470145`:
`53d12828-e462-447e-a1b2-25e9d7021137` failed. The destination implementation's
first focused RBE run `1ddb5596-de6b-4280-888e-7d7959e66040` passed both URL and closure
CLI targets. The closure control distinguishes missing captures, present but
unlisted JavaScript, emitted JavaScript, captured assets, external URLs and
nonliteral uses without weakening the semantic gate.

Remaining obligations: URL search/fragment and encoded path identity, nested CSS asset references,
actual browser worker/style loading, and dynamic consumers of package replacements
need their own runtime controls. The inventory records planned replacement
packages but does not prove that a dynamic call reaches them. Missing-input
reports are not emitter-defect evidence. No private capture or source is included.

The existing direct Worker-string rewrite has only source-shape coverage. Its
browser document base is not the module URL; the resource inventory marks this
case unresolved. The destination fix leaves that transform outside its scope.

Full hosted CI at `f17735cbfd` exposed a fixture dependency on the old wrong
coordinate system: `loader_closed_reentry_test` supplied an extra, hand-written
emitted `lazy.js` whose provider import pointed at `entry.js`. Correct source
rebasing reached the original runtime-only file instead, whose provider source
path was absent from the emitted tree. The fixture now captures the lazy module
as a source chunk and executes that same source in both trees; the artifact
resolver owns both the dynamic target and its static provider import. This
preserves the test's asynchronous-reentry proof without a fixture-only emitted
implementation.
