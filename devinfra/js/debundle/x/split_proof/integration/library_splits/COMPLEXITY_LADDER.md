# Public placement ladder

These are complete source files and authored placements, not isolated-unit
acceptance counts. The corpus rung uses the placement in
[`certificate_test.mjs`](../../debundle_cases/certificate_test.mjs). The
library rungs use the existing [`certificate_test.mjs`](certificate_test.mjs)
placements. A rung is a separate request; increasing source size or moving
more units does not imply a semantic difficulty ordering.

At `5185a1aa9879e260b986de8e89a3973c943355c9`, the following focused run
passed ([BuildBuddy](https://app.buildbuddy.io/invocation/bc85699f-d95b-4c57-a03b-12489cd17e20)):

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/debundle_cases:corpus_certificate_test" \
  "$p/integration/library_splits:library_certificate_test" --test_output=all
```

The library test prints source SHA-256, original ordinal ranges, owner paths,
entry, authored order, and actual CLI verdict as JSON. It generates the Acorn
8.19.0 and semver 7.8.6 bundles with the pinned dependencies using
[`prepare.mjs`](prepare.mjs). `#a..#b` below is inclusive; these are the
original source ordinals, including export metadata.

| Source and SHA-256                                                                                                                                    | Placement, in authored import priority                                    | Ordinary CLI observation                                                                                                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Corpus `app.js`](../../../../e2e/cases/emission/catchall_export_consumer/app.js), `a7d9a6392dcb7d12f872d2a601d78176b50eae972ef91925e7d7c4a866fe0b23` | `provider.mjs`: `#0`; entry `entry.mjs`: `#1`; order `provider.mjs`       | Compiled constructor certifies at `all-outcomes`. Published files pass disk recheck; Node sees the same namespace and the consumer prints `1`. A changed provider fails readback; a wrong owner refuses without output. The test copies this source to a `.mjs` input. |
| semver, `9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44`                                                                            | `moved.mjs`: `#1`; entry `entry.mjs`: `#0`, `#2..#58`; order `moved.mjs`  | `no_constructed_rule`; no output.                                                                                                                                                                                                                                      |
| semver, same source                                                                                                                                   | `m0.mjs`: `#0..#28`; entry `m1.mjs`: `#29..#58`; order `m0.mjs`           | `no_constructed_rule`; no output.                                                                                                                                                                                                                                      |
| Acorn, `e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee`                                                                             | `moved.mjs`: `#1`; entry `entry.mjs`: `#0`, `#2..#446`; order `moved.mjs` | `no_constructed_rule`; no output.                                                                                                                                                                                                                                      |
| Acorn, same source                                                                                                                                    | `m0.mjs`: `#0..#222`; entry `m1.mjs`: `#223..#446`; order `m0.mjs`        | `no_constructed_rule`; no output.                                                                                                                                                                                                                                      |

The library test separately checks the original bundle's public API smoke
before requesting a split. For a certified split it requires disk recheck and
the same smoke result; no library request above reached those steps. It also
tries moving Acorn's first function declaration (`#10`) and an unsafe
read-before-declaration control; both refused. These are CLI verdicts, not
diagnostic previews. `no_constructed_rule` is the first reported boundary; it
does not identify an unsupported operation, prove the placement safe, or prove
it impossible.

The first two Acorn units are `var` declarations with array initializers;
semver's are `var` declarations with member-expression initializers. Useful
next investigations are, in order: minimize a dependency-safe Acorn array
placement while testing allocation identity and order; isolate a function
declaration with its captures and later calls; then isolate a semver member
read with getter, receiver and throw controls. The full-bundle refusals alone
do not establish that any one of those constructs caused the refusal.
