# Readable names for emitted entry imports

A downstream extraction exposed a missed readability rewrite: a selected
provider defines a descriptive name and adds it as an entry export, while a
selected consumer still imports the captured short export name from that
entry. The useful recovered name exists; it is not a missing source audit.

The source-backed mechanism is in
`lowering/imports/cross_chunk_imports.rs`. It runs after materialization and
uses `ArtifactSourceImportResolver`, which interprets specifiers against the
original source path. Lowered files already contain output-relative imports,
for example `../../provider/entry.js`. That path resolves through the emitted
artifact index but need not identify any original source chunk. The old pass
skips the candidate before the rename ledger can consider it.

Keep original-source resolution first. If it fails, use the existing runtime
import resolver and accept only the resolved provider entry file. The alias
table describes entry exports, so an import from another logical-module file
must not reuse that table just because it belongs to the same chunk. Retain
the existing same-chunk, processed-chunk, ambiguity, collision and hygiene
checks. This changes the named import/local binding to an already emitted
alias of the same live binding; it does not redirect the import path or bypass
entry initialization.

Changed-file Nix pre-commit and diff checks pass. Existing hosted CI provides
compilation and its current controls; no tests were added or run locally.
Production output confirmation for a newly compiled engine needs its matching
analysis. The compiled analysis identity currently includes the whole pipeline
closure, including emission code. This broader iteration cost is recorded as
F4 in `TODO.md`; do not bypass compatibility checks to reuse old artifacts.
