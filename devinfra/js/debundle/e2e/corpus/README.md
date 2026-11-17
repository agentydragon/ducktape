# File-backed E2E cases

Run `bbr test //devinfra/js/debundle/e2e:corpus_test`. Each entry of an
`e2e/cases/**/expect.yaml` is a separately named libtest trial. Filter with
`--test_arg=initialization/nested_closure_rebind` or list with `--test_arg=--list`.

A case contains `main.js`, a flat `spec.yaml`, and `expect.yaml`.
Multiple specs can share the same input files. The relative directory and spec
stem form the test name. Bazel declares the files through one corpus glob.
For shared-source examples, see `emission/rename_shorthand` for chunk and module
renames, `emission/return_object_binding` for residual, sibling, and local
ownership, and `initialization/freeze_proxy_receiver` for accepted and rejected
placements of the same program.

The runner stages each run separately and writes `.fixture/js-files.txt` from
its `.js` files in sorted relative-path order. A checked-in `js-files.txt`
overrides discovery for runtime-only siblings, source aliases or deliberate
ordering. An absent `package.json` is supplied with `type: module` for Node execution.

The Rust runner's `apply_defaults` in [run.rs](run.rs) supplies fixture mechanics: `inputs` points at
`.` and `.fixture/js-files.txt`, `write_js_tree.out_dir` is `out`, and
`materialize_logical_modules` uses `target_dir: modules`,
`report_out_dir: out/reports/tree`, and `prune_other_chunks: false`.
Cases only need their semantic spec fields. Explicit fields override these
defaults individually; omitted sibling fields still receive defaults.
These are corpus defaults, not changes to the production spec defaults.
The staged spec passed to the CLI is complete, so retained failures replay
without the corpus runner. To run an authored sparse spec outside the runner,
first supply the missing fields listed above.

`expect.yaml` maps each spec filename to exactly one `accept` or `reject`:

- `accept.stdout` checks original and emitted entry stdout; `entry` defaults to
  chunk `main`. `original_stdout` overrides only the original entry expectation
  when a deliberate rename changes an observable function/class name.
  Optional `exports` maps emitted paths to `include`/`exclude`
  lists. `check_all_js: true` runs Node syntax checks on every emitted JavaScript file.
  Optional `source` maps paths to `contains`, `excludes`, and
  `occurrences_at_least` assertions.
- `accept.probes` maps `*.probe.mjs` paths to expected `stdout`. Each probe
  exports a function taking the entry namespace. It runs after importing the
  entry in a fresh Node process, against both source and output; stdout includes
  entry initialization. Probes are not analyzed chunks.
- `accept.runtime_files` maps emitted paths to case-relative sibling files to
  copy after emission. See `emission/scope_local_naturalization` for an example
  with an explicit analyzed-chunk list.
- `reject.atomic_conflict` names required `modules` and `causes` in the
  structured conflict report.
- `reject.cycle` names required `modules` in one blocking SCC. Optional
  `sequenced: true` also requires a side-effect ordering edge in that SCC's cut.
- `reject.stderr_contains_all` lists the error tokens required from a refusal.
  It covers valid specs with unsupported selectors or inconsistent inputs;
  malformed-spec parsing and multi-step CLI workflows remain Rust tests.

Each rejection names exactly one reason. The CLI must exit with the refusal
status; crashing does not satisfy rejection.

Emitted paths are relative to `write_js_tree.out_dir/app` (`out/app` by default).
Expectations reject unknown fields.
The runner reuses the existing invariant guards, failure retention and command
replay. Multi-step editing and analysis-specific assertions remain Rust tests.

Parameterized initialization families live in `../initialization/` and share
`//devinfra/js/debundle/e2e:initialization_test`. Callback, collection, and
native-integrity builders live in `../callbacks/`, `../collections/`, and
`../native_integrity/` and use area-specific Rust targets. Saved-analysis
callback availability and native own-property families have separate targets
under those areas. These Rust suites retain shared source builders and table
loops where separate files would repeat programs, and retain graph, staged, or
effect-view assertions the corpus cannot express. Concise real-CLI cases use
file-backed entries above. Both forms run through the same CLI and test support.
