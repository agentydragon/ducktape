# Host behavior

The public engine contract is implemented. The `host_behavior` schema,
validation, loader-closed derivation, and consumed-key reporting live in
`SPEC.md` (§ Host behavior and loader-closed derivation), `pipeline.rs`, and
`lowering/loader_closure.rs`.

Host assertions describe the embedding runtime, not a runtime name or version.
Each consumer owns the audit against its exact pinned executable and must
re-audit on executable changes. The engine reports consumed assertions through
`inference_uses`; the assertion does not prove behavior outside its declared
scope.

No remaining public-engine implementation step is established here. A named
runtime preset is still a separate API decision. Loader reentry that the source
can reach remains conservative unless the explicit policy proves otherwise; the
current boundary is specified in `SPEC.md` (§ Reentry through imported names).
