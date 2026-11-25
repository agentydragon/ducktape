# Joint main and editor deployment trial

Run `bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:joint_deployment_test`.
The input is one named binding from each canonical Rust placement:
`strokeEllipseWithRotation` from main and `centerScrollOn` from editor. The
fixture checks both source digests, resolves their original filenames from
[the pinned static closure](main_static_sha256.json), and retains each original
public path. The generated private modules have distinct paths.

The test stages both planned outputs in one directory and copies each of the
seven unchanged static providers once. It checks the complete file set and
compares each split's syntax and callable source spelling against that split's
original source. It reparses the staged files for the proof. Lazy resources
outside the overlay still come from the original bundle.

[The proof](../../lean/EsmSplit/CoreJS/JointStatic.lean) uses a single graph
rooted at main.js. Original ordinals occupy disjoint ranges for main, editor,
then the shared providers in path order. Emitted units keep their original
identities. Each opaque unit receives the loader cells of its own original
referrer and ordinal. Both layouts use the same checked resource inventory;
location-sensitive units stay at their original effective URLs. The checker
compares the actual public cells at both original paths and certifies the
whole initialization schedule.

The verdict is equality of normal CoreJS final states under the browser-page
interpretation, the native metadata record initialization premise and the
fixed empty import map. It assumes the indicated origin and paths resolve to
these assets with one module record per effective URL. The test's undeclared
outputs contain both selected placements, the proof log and `inventory.json`:
source hashes and effective URLs, linked unit ranges, per-referrer loader
imports and metadata, and the staged file hashes. This trial has no browser
runtime assertion. The separate single-chunk Chromium tests exercise their
respective deployments.
