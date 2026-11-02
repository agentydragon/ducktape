# E2E CLI runfile from a downstream workspace

The shared Rust e2e helper used a fixed `_main/...` path for `debundle_e2e`.
In a downstream test, `_main` names the consumer workspace, so the test runner
could not spawn the CLI and failed with `ENOENT` before reaching assertions.
All seven affected suites failed at this same path lookup.

The e2e test macro now declares the CLI as test data and supplies its
`rlocationpath` to the support helper. The helper resolves that label with the
`rules_rust` runfiles API. The one direct `rust_test` caller sets the same
environment variable; Node remains data owned by the shared support library.

Validate from a downstream workspace and from the root workspace. Include the
direct caller alongside the seven suites that exposed the failure:

```bash
bbr test --override_module=ducktape=/path/to/ducktape \
  @ducktape//devinfra/js/debundle/e2e:realizability_test \
  @ducktape//devinfra/js/debundle/e2e:source_match_coalescing_cli_test \
  @ducktape//devinfra/js/debundle/e2e:edit_gate_source_match_cli_test \
  @ducktape//devinfra/js/debundle/e2e:modules_apply_cli_test \
  @ducktape//devinfra/js/debundle/e2e:modules_refine_cli_test \
  @ducktape//devinfra/js/debundle/e2e:effect_translation_validation_test \
  @ducktape//devinfra/js/debundle/e2e:cross_module_emission_test \
  @ducktape//devinfra/js/debundle/e2e:cross_chunk_readable_imports_test
bbr test \
  //devinfra/js/debundle/e2e:realizability_test \
  //devinfra/js/debundle/e2e:source_match_coalescing_cli_test \
  //devinfra/js/debundle/e2e:edit_gate_source_match_cli_test \
  //devinfra/js/debundle/e2e:modules_apply_cli_test \
  //devinfra/js/debundle/e2e:modules_refine_cli_test \
  //devinfra/js/debundle/e2e:effect_translation_validation_test \
  //devinfra/js/debundle/e2e:cross_module_emission_test \
  //devinfra/js/debundle/e2e:cross_chunk_readable_imports_test
```
