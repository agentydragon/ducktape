# Shape matcher versus native lowering, head to head

Direct comparison of the two ways a `source_match` selector can reach the
selector IR, measured on the same input in one session. This is the measurement
behind <../../docs/selector_resolution.md> § "Rejected: let the solver consume
AST facts natively instead of candidate rows".

## Input

The largest known downstream chunk: 7,139,070 bytes (6.81 MiB), 204,235 lines,
1,190,984 AST nodes. The spec over it carries 6,179 `source_match` selectors
across 1,754 module YAML files.

Binary built `-c opt`. All runs on one host (a 4-core container), so the
absolute numbers are not comparable to other hosts — the ratio is the result.

Never compare a `fastbuild` selector timing to anything: the same
`match-selector` probe measured 19.80s fastbuild against 3.86s optimized
(<2026_07_13_match_selector_full_domain_profile.md>).

## Measurements

Matcher path — `spec validate --modules <spec> --source-file <chunk>`, which
builds one `ChunkResolver` and resolves every module's selectors against it:

| Run    |  Wall |
| ------ | ----: |
| cold   | 14.0s |
| warmed | 10.7s |
| warmed | 11.1s |
| warmed | 11.2s |

7 unresolved selectors out of 6,179.

Native path — `spec match-selector --source-file <chunk> --match <selector>
--no-slack`, which lowers one selector natively and solves it through the
CP-SAT sidecar:

| Selector                             | Wall (warmed) |
| ------------------------------------ | ------------: |
| `const NAME = ["…", "…", "…", "…"];` |          7.0s |
| a four-`ANYTHING` function body      |          7.1s |
| cold, same function body             |         11.8s |

## Reading

**One native selector costs what the whole spec costs through the matcher.**
6,179 selectors resolve in 11.1s one way; one selector takes 7.1s the other.

**The native cost is not matching work.** A trivial array-literal selector and a
four-hole function body cost the same, so the time is per-chunk fact extraction
and domain construction, not the match. That also means it does not amortize
away in a joint solve: it is the floor, and a production-sized program adds
per-selector model on top of it. The recorded production-sized attempt timed out
at 120s inside `FactDomains::from_program_and_facts` without reaching the solver
(<2026_06_27_large_bundle_selector_csp_profile.md>).

**The solver is not the cost.** Feeding the saved request for one selector to
the optimized sidecar takes 0.02s against 1.91s of model construction. The
encoding, not the search, is what is expensive.

## Not measured

The full `run` pipeline end to end. Reproducing it needs the downstream vendor
swap's package trees, which this environment could not fetch. Selector
resolution is the largest single component of that wall on a
`source_match`-heavy spec (~92% on the 2026-06-21 callgrind workload,
<2026_06_21_fact_resolver.md>), but the end-to-end number is unmeasured and
should not be inferred from these figures.

## Reproducing

```sh
bbr build -c opt //devinfra/js/debundle:debundle \
  //devinfra/js/debundle/solver_backends/ortools_cpsat:selector_cpsat_solver

# matcher path — whole spec, no solver backend needed
time debundle spec validate --modules <spec>/modules --source-file <chunk>.js \
  --format ndjson

# native path — one selector, needs the sidecar
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER=<path>/selector_cpsat_solver
time debundle spec match-selector --source-file <chunk>.js \
  --match '<selector>' --no-slack --format json
```
