# Framework-neutral test outputs and visual reviews

Implementation is in progress. The chosen comparison profile is
`bazel-configuration-exact-v1`: only equal, known configuration IDs are
comparable. The cutover intentionally starts fresh baseline pointers in a new
namespace; preserving the old filename-based comparisons is not required.
Keep this plan until the pinned publisher has shipped and end-to-end publication
has been verified. References to the previous protocol below describe the
starting point.

## Goal

Give tests a shared way to identify and publish their outputs, and collect those
outputs independently of the test framework and presentation. The same logical
screenshot must line up across Bazel invocations even when its shard, run, or
attempt changes. Different cases and checkpoints must not be silently treated as
the same screenshot.

The current `visual-review.json` protocol identifies a review image by target and
PNG basename. `VisualPage.capture()` can derive a basename from the pytest case
name, but an explicit checkpoint replaces that name. Shards have separate output
directories, so the same basename can be declared by different cases without a
local overwrite. The publisher currently unions those declarations. The
cross-shard guard in [PR #13692](https://github.com/agentydragon/ducktape/pull/13692)
is an interim protection if it lands before this work; the protocol must uphold
the invariant regardless.

## Identity and protocol

Define a versioned, framework-neutral test-output record. Its logical identity is
`(collection scope, case ID, checkpoint, output role)`:

- For Bazel, the collector supplies the repository and canonical test label as
  the collection scope. A producer does not need Bazel or pytest to construct its
  case and output identity.
- The case ID is an opaque, stable value unique within the collection scope;
  the collector does not parse framework names, paths, classes, or parameter
  IDs. A producer must provide enough context to avoid collisions. A deliberate
  framework migration can retain an existing case ID. Duplicate claims in one
  collection are detectable; reuse by an unrelated case in a later revision
  cannot be inferred from an equal ID alone.
- A checkpoint is an optional opaque string scoped to the case. A `null`
  checkpoint represents the default capture and is distinct from any named
  checkpoint. An output role distinguishes a screenshot from its trace,
  diagnostics, or another artifact.
- Display labels, file paths, dimensions, and content digests are metadata,
  not the identity. Distinct logical outputs remain distinct even if
  they have identical bytes or display labels. Identical logical identities with
  incompatible kinds or metadata are an error, not a new identity.

Encode identity unambiguously and retain its full structured form in metadata.
Physical file names may be readable encodings or hashes of the identity, with
collision detection; they must not become the identity. Do not concatenate
unescaped user strings into a path. Validate relative paths and references,
including traversal, symlinks, and malformed or missing files. The visual review
is an explicit role: collecting a diagnostic must not automatically publish it
as a review image. The screenshot role specifies PNG validation and the
`image/png` publication content type. The generic collector treats payloads as
bytes; future roles can define their own handling. This plan implements diffing
only for screenshots.

Bazel invocation, configuration, shard, run, attempt, result status, and CAS
URI are execution provenance. Preserve them for every record and staged file,
but keep them out of logical identity. Resharding must not rename an output.
Configurations can be semantically different even when they produce the same
case; define a named comparison profile for intended compatible executions and
fail closed on ambiguous or unknown comparisons. Do not use an opaque Bazel
configuration hash as a permanently stable screenshot name. The collector must
report provenance and the reason it accepts or rejects a comparison.

## Producers

Provide a framework-independent output writer accepting an explicit case ID,
checkpoint, role, and bytes or file. Add a pytest fixture
that binds the case ID automatically to the full node ID, normalized to a
verified stable repository-relative root under Bazel. `VisualPage.capture()`
uses the bound case and treats its optional name as a local checkpoint, not a
replacement for case identity. The default capture needs no author-supplied
name; repeated captures require distinct checkpoints and fail on duplicates.
Keep readable, stable pytest parameter IDs; a file, case, or parameter rename
changes identity and needs deliberate migration if old visual history matters.

Define the protocol so other languages and frameworks can implement writers
without importing Python or depending on pytest conventions. Framework-specific
adapters and their identity stability are separate from the collector. Do not
require or build a particular non-pytest adapter just to establish neutrality;
validate it with a producer that writes the documented protocol directly.

Support concurrent tests and processes within a Bazel result. Replace the
current shared read-modify-write manifest pattern with independently claimed,
atomically completed output records or an equally safe mechanism. Publish a
record only after its referenced file is complete. Duplicate claims for the
same identity fail even if the bytes match. Keep unrelated diagnostic outputs
available in the test artifacts; the publication policy explicitly selects
which roles become public review assets.

## Example output layout

One possible implementation uses a record per output, with the record as the
atomic declaration. Each Bazel result has its own undeclared-output directory;
these files are not shared by shards, runs, or attempts. The placeholders below
stand for deterministic encodings of the local `(case ID, checkpoint, role)`
identity, not counters or hashes of image bytes:

```text
$TEST_UNDECLARED_OUTPUTS_DIR/
└── test-outputs/
    ├── records/
    │   ├── <mobile-expanded-screenshot-key>.json
    │   ├── <mobile-expanded-diagnostics-key>.json
    │   └── <desktop-expanded-screenshot-key>.json
    └── files/
        ├── <mobile-expanded-screenshot-key>.png
        ├── <mobile-expanded-diagnostics-key>.json
        └── <desktop-expanded-screenshot-key>.png
```

For example, the screenshot record could contain the following. The case ID is
an opaque producer-supplied string; the format does not require a test framework
or interpret the slash. `path` is relative to `test-outputs/`.

```json
{
  "schema": "ducktape.test-output.v1",
  "case_id": "dialog/mobile",
  "checkpoint": "expanded",
  "role": "screenshot",
  "path": "files/<mobile-expanded-screenshot-key>.png",
  "label": "Expanded dialog on mobile"
}
```

Its diagnostics record uses the same case and checkpoint with a different role
and file, so it cannot be confused with the screenshot:

```json
{
  "schema": "ducktape.test-output.v1",
  "case_id": "dialog/mobile",
  "checkpoint": "expanded",
  "role": "diagnostics",
  "path": "files/<mobile-expanded-diagnostics-key>.json"
}
```

The desktop screenshot would have `case_id` `dialog/desktop` and can reuse the
`expanded` checkpoint. A default capture uses `"checkpoint": null`. A pytest
adapter would instead derive its case ID from the full node ID. A writer must
create each payload completely before atomically publishing its record and fail
a duplicate claim; the collector validates the full identity rather than
trusting the placeholder key. This is a proposed layout and schema to validate,
not the current `visual-review.json` format.

The collector attaches the target and result provenance from its source, not
from producer-controlled fields. Its materialized observation could look like:

```json
{
  "scope": { "repository": "agentydragon/ducktape", "target": "//example:visual" },
  "case_id": "dialog/mobile",
  "checkpoint": "expanded",
  "role": "screenshot",
  "source": {
    "invocation": "<invocation-id>",
    "configuration": "<configuration-id>",
    "run": 1,
    "shard": 3,
    "attempt": 1,
    "status": "PASSED",
    "uri": "<buildbuddy-cas-uri>"
  }
}
```

The corresponding publication has a separate content identity. For example:

```text
s3://pr-visuals/
├── assets/
│   ├── <candidate-png-sha256>.png
│   ├── <baseline-png-sha256>.png
│   └── <diff-png-sha256>.png
└── reviews/<commit-sha>/
    ├── metadata.json
    ├── index.html
    └── tests/<target-slug>/
        ├── metadata.json
        └── index.html
```

Review metadata links the logical identity to the candidate, baseline, and diff
blob digests. Equal PNG bytes can share one blob without merging their logical
identities. Records and diagnostics are not implicitly made public.

## Collection, storage, and presentation

Keep these boundaries independently testable:

1. A Bazel/BuildBuddy source lists the result artifacts with exact invocation,
   target, configuration, run, shard, attempt, and status. It fetches records
   and referenced files by their advertised CAS URIs into result-scoped staging;
   it must not guess by basename or discard result identity. Define a source
   interface that can also accept already-materialized outputs. Keep records and
   files in Bazel undeclared outputs and preserve the download configuration
   that makes cached test outputs visible in BES.
2. A framework-neutral collector validates records, binds collection scope and
   provenance, groups by logical identity, and reports conflicts and incomplete
   results. The same logical identity emitted by distinct shards of the same
   configured run is a conflict even when bytes match. Retries and repeated
   runs stay distinct observations; never silently union failed or incomplete
   attempts into a complete roster. Apply the result selection policy below
   while retaining all observations for repeated run determinism checks. Union
   disjoint shard outputs within an invocation;
   do not union different whole rosters from linked invocations. Missing data
   remains an explicit incomplete/error state rather than evidence that an image
   was removed.
3. A publication layer materializes selected artifacts into an object store by
   content digest and writes indexes preserving their logical identity and
   provenance. Today visual PNGs are deduplicated individually at
   `assets/<sha256>.png`; review metadata and pages are commit-scoped and refer
   to those blobs. Preserve this per-blob addressing for candidate, baseline,
   and generated diff images. Collection does not depend on S3 or HTML, and
   upload policy need not publish every collected diagnostic.
4. The visual review consumes the collected screenshot records and their
   published blob references to resolve baselines, compare pixels, render
   galleries, and update the PR comment and checks. The visual determinism
   checker consumes the same collection semantics but compares repeated
   screenshot observations; it may use CAS digests without downloading PNGs.
   Neither consumer reconstructs identity from filenames, display text, or a test
   framework. Traces and diagnostics remain inspectable artifacts; adding
   differs for them or other output types is separate work.

Preserve per-target baseline provenance, explicit stale-fallback warnings,
commit-pinned review pages, shared-blob upload ordering, current affected-test
selection, and human review of pixel changes. Behavioral and render-health tests
remain hard gates.

## Repeated runs and attempts

Collect every result with its status and provenance. For visual review, process
each invocation and configured run independently:

1. For each shard in a run, select its confirmed terminal successful attempt.
   Keep earlier failed attempts and their outputs for diagnosis and report that
   a retry occurred. The record and payload must come from the same attempt. A
   shard with no confirmed successful terminal result is failed or incomplete;
   do not substitute a failed attempt's partial outputs or guess from attempt
   numbers when terminal status is unknown.
2. Assemble the selected shard outputs into that run's roster. Preserve a
   conflict if distinct shards claim the same logical identity, even if their
   payload bytes match. Do not use a missing result or missing declared file as
   evidence that a screenshot was removed.
3. Examine the successful runs for each logical screenshot. If all complete runs
   have identical screenshot bytes, a normal review may show one representative
   image while retaining every run's provenance and separately reporting any
   retries or failed/incomplete runs. Missing observations remain visible; do
   not silently treat an incomplete run as agreement.
4. If successful runs produce different screenshot bytes, mark the screenshot
   unstable and show the variants with their run provenance. The review may
   show individually labelled baseline diffs, but must not silently select one
   as the definitive result. The determinism checker compares the successful
   repeated observations using the same collected identities and reports the
   incomplete or failed runs separately.

When comparing two invocations, apply this policy to each separately before
matching logical screenshots. A prior failed attempt is never a baseline or a
replacement for a missing successful result. A complete run's genuinely absent
output can be classified as removed only when the relevant collection coverage
is known; unknown or partial coverage remains incomplete.

## Migration and validation

1. Inventory all producers, diagnostics, visual consumers, release/pin paths,
   and concurrently running publisher versions. Define the protocol and
   comparison profile with fixtures covering distinct identities, ambiguous
   configuration, and deterministic serialization across implementations.
2. Implement and validate the generic writer, pytest adapter, and collector;
   update all callers and visual/determinism consumers together, without
   transitional source shims. Coordinate the trusted publisher wheel and pin
   rollout and prove the cutover before changing production publication. Do not
   assume a source merge immediately deploys the wheel.
3. Start fresh baselines in the new namespace; old filename-based baselines are
   not mapped to structured identities. Clear old bundle and pointer namespaces;
   historical links will expire. Devel runs must repopulate the new per-target
   pointers before comparisons become available.
4. Verify the pytest adapter and a producer that writes the protocol directly
   without pytest or its identity scheme; exercise parallel writers,
   multiple checkpoints and roles, stable pytest roots and parameter IDs, and
   duplicate rejection. Exercise independent shards, multiple configurations,
   retries, repeated runs with equal and different screenshot bytes, cache hits,
   partial failures, and missing files. Confirm a resharded case matches its
   baseline and different cases never do; failed attempts must not supply the
   selected image or hide instability in successful runs.
5. Run an end-to-end RBE visual test through BuildBuddy collection, per-blob S3
   publication, a before/after gallery, PR comment and determinism check. Verify
   published pages and images, historical and fallback baselines, and both
   changed and unchanged results. Update owning specifications and operational
   docs, and delete this plan when the migration is complete.

The released wheel and its Nix pin must still advance after merge. Until then,
the old publisher and migrated producers cannot exchange records; monitor the
pin rollout and allow devel runs to populate the new baseline namespace.
