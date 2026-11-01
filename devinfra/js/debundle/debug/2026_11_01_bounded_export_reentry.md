# Bounded export-reentry presentation

## Measured issue and boundary

One existing application replay emitted 17,751,915 bytes (16.93 MiB) of
export-reentry JSON across 75 chunks. Its 614 request rows were repeated in all
75 reports (46,050 occurrences); loaded-chunk lists totaled 75,263 entries, and
builtin leaves totaled 20,705 rows. Existing loader failures already aggregate
one first witness and an exact occurrence count per finite reason.

The full `ExportReentryReport` remains unchanged in memory and in the staged
analysis artifact. Policy, loader reachability, reentry names, author inputs and
all gate/effect checks consume that complete representation. A Serialize-only
borrowed output view retains exact collection sizes and aggregate request-site
counts, while taking the deterministic first twenty entries per collection.
Policy name examples are explicitly a presentation sample; the view cannot be
deserialized into a semantic policy. No source, premise, callback timing, heap
locality, purity, acceptance or analyzer precision changes.

Default `export_reentry.json` contains these bounded inventories, explicit
truncation and discoverable full-export instructions. Explicit `run --full-report
export-reentry` streams the original complete report schema to compact
`export_reentry.full.json` through the existing common JSON writer. The writer
propagates serialization and final flush errors before the summary is written.
An export capability does not claim an unrequested file exists. Full diagnostic
selection is independent of full graph export; chunk selection affects sinks
only. Existing atomic/cycle diagnostics, stderr, rejection markers and banners
are preserved.

## Validation scope

The CLI controls compare a 32-request/chunk/builtin summary with every row of
the original-schema full file, exact site counts, and unchanged runtime output.
A typed view control samples 32 blocker/name entries without changing the
complete semantic report, and rejects deserializing the summary as that report.
Direct and staged full exports match. Multi-chunk filters preserve every other
output byte, including summaries, graphs, emitted JavaScript and analysis facts.
Invalid specs and suppressed/unknown sinks remain failures. A common-writer unit
control exercises final-flush failure propagation. Existing loader, host,
callback, vendor, TV, pipeline and CLI suites retain semantic coverage.

No post-change canonical size, runtime improvement or semantic-memory reduction
is claimed here; hosted CI and canonical replay remain separate evidence.

The bounded-default control fails against base production at
`e9586604d42177a077f6bb034c775eb6c313ed83` with “default report retained more
than20 full request rows,” before requesting the new flag. All production
sources were restored after the expected failure.
[Expected failing control](https://app.buildbuddy.io/invocation/7450b6db-c0b4-4d3c-821e-ea87a4687c1d).

Eighteen tests passed: seven report/loader/host/callback/vendor/staged/TV CLI
suites, four library test targets, and seven action-rule controls. The same
command explicitly named artifact, lowering, pipeline, CLI and output-layout
libraries for normal Clippy/Rustfmt aspects.
[Exact source checks](https://app.buildbuddy.io/invocation/a8da5c7e-3d63-4b23-bab9-e3adeb21a5e7).
Normal touched-file hooks and whitespace checks passed. These checks do not
substitute for canonical replay or hosted CI.
