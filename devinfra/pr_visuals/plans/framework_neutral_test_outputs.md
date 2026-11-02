# Framework-neutral test outputs and visual reviews

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
`(collection scope, case namespace, case ID, checkpoint components, output role)`:

- For Bazel, the collector supplies the repository and canonical test label as
  the collection scope. A producer does not need Bazel or pytest to construct its
  case and output identity.
- The case namespace identifies the adapter's identity scheme. The case ID is an
  opaque, stable value in that scheme; the collector does not parse framework
  names, paths, classes, or parameter IDs. Distinct namespaces cannot collide.
- A checkpoint is a sequence of explicit components scoped to the case. The
  default checkpoint has its own unambiguous representation. An output role
  distinguishes a screenshot from its trace, diagnostics, or another artifact.
- Display labels, media types, file paths, dimensions, and content digests are
  metadata, not the identity. Distinct logical outputs remain distinct even if
  they have identical bytes or display labels. Identical logical identities with
  incompatible kinds or metadata are an error, not a new identity.

Encode identity unambiguously and retain its full structured form in metadata.
Physical file names may be readable encodings or hashes of the identity, with
collision detection; they must not become the identity. Do not concatenate
unescaped user strings into a path. Validate relative paths and references,
including traversal, symlinks, and malformed or missing files. The visual review
is an explicit role: collecting a diagnostic must not automatically publish it
as a review image. Keep roles and media types open to other comparison consumers
in the future; this plan implements diffing only for screenshots.

Bazel invocation, configuration, shard, run, attempt, result status, and CAS
URI are execution provenance. Preserve them for every record and staged file,
not as checkpoint components. Resharding must not rename a logical output.
Configurations can be semantically different even when they produce the same
case; define a named comparison profile for intended compatible executions and
fail closed on ambiguous or unknown comparisons. Do not use an opaque Bazel
configuration hash as a permanently stable screenshot name. The collector must
report provenance and the reason it accepts or rejects a comparison.

## Producers

Provide a framework-independent output writer accepting an explicit namespace,
case ID, checkpoint, role, media type, and bytes or file. Add a pytest fixture
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
   attempts into a complete roster. Define and test selection of the authoritative
   successful result for review, while retaining all observations for repeated
   run determinism checks. Union disjoint shard outputs within an invocation;
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

## Migration and validation

1. Inventory all producers, diagnostics, visual consumers, release/pin paths,
   and concurrently running publisher versions. Define the protocol and
   comparison profile with fixtures covering distinct identities, ambiguous
   configuration, and deterministic serialization across implementations.
2. Implement and validate the generic writer, pytest adapter, and collector;
   update all callers and visual/determinism consumers together, without
   transitional source shims. Coordinate the trusted publisher wheel and pin
   rollout so old jobs and retained published metadata remain readable, and
   prove the cutover before changing production publication. Do not assume a
   source merge immediately deploys the wheel.
3. Map existing target/path baselines to new logical identities only where a
   one-to-one correspondence is verified. Otherwise classify the new and old
   images separately rather than guess. Report the scope of lost continuity
   before rollout and preserve retained content-addressed blobs and commit pages.
4. Verify the pytest adapter and a producer that writes the protocol directly
   without pytest or its identity scheme; exercise parallel writers,
   multiple checkpoints and roles, stable pytest roots and parameter IDs, and
   duplicate rejection. Exercise independent shards, multiple configurations,
   retries, repeated runs, cache hits, partial failures, and missing files.
   Confirm a resharded case matches its baseline and different cases never do.
5. Run an end-to-end RBE visual test through BuildBuddy collection, per-blob S3
   publication, a before/after gallery, PR comment and determinism check. Verify
   published pages and images, historical and fallback baselines, and both
   changed and unchanged results. Update owning specifications and operational
   docs, and delete this plan when the migration is complete.

Before implementation, settle how the canonical comparison profile is declared
and how the pinned publisher cutover avoids mixed-protocol gaps. Those are
release and identity decisions, not reasons to couple the core protocol to a
test framework.
