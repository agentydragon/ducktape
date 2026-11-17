# File-backed E2E cases

Run `bbr test //devinfra/js/debundle/e2e:corpus_test`. Each entry of an
`e2e/cases/**/expect.yaml` is a separately named libtest trial. Filter with
`--test_arg=initialization/nested_closure_rebind` or list with `--test_arg=--list`.

A case contains `main.js`, an ordinary flat `spec.yaml`, and `expect.yaml`.
Multiple specs can share the same input files. The relative directory and spec
stem form the test name. Bazel declares the files through one corpus glob.

The runner stages each run separately and writes `.fixture/js-files.txt` from
its `.js` files in sorted relative-path order. A checked-in `js-files.txt`
overrides discovery for runtime-only siblings, source aliases or deliberate
ordering. Specs use `inputs: {input_root: ., js_list_path: .fixture/js-files.txt}`,
`write_js_tree: {out_dir: out}`, and
`materialize_logical_modules: {target_dir: modules, report_out_dir: out/reports/tree}`.
An absent `package.json` is supplied with `type: module` for Node execution.

`expect.yaml` maps each spec filename to exactly one `accept` or `reject`:

- `accept.stdout` checks original and emitted entry stdout; `entry` defaults to
  chunk `main`. Optional `exports` maps emitted paths to `include`/`exclude`
  lists. Optional `source` maps paths to `contains`, `excludes`, and
  `occurrences_at_least` assertions.
- `accept.probes` maps `*.probe.mjs` paths to expected `stdout`. Each probe
  exports a function taking the entry namespace. It runs after importing the
  entry in a fresh Node process, against both source and output; stdout includes
  entry initialization. Probes are not analyzed chunks.
- `accept.runtime_files` maps emitted paths to case-relative sibling files to
  copy after emission. See `emission/scope_local_naturalization` for an example
  with an explicit analyzed-chunk list.
- `reject.atomic_conflict` names required `modules` and `causes` in the
  structured conflict report. The CLI must exit with the refusal status;
  crashing does not satisfy rejection.

Emitted paths are relative to `out/app`. Expectations reject unknown fields.
The runner reuses the existing invariant guards, failure retention and command
replay. Multi-step editing and analysis-specific assertions remain Rust tests.
