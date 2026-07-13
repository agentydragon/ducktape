# `match-selector` full-domain profile

## Finding

A single `spec match-selector` probe over a 7.14 MB downstream chunk took 19.5
seconds and peaked at 1.56 GB RSS. The selector found one unique target. The
dominant demonstrated cost is full-chunk fact-domain construction, not search
ambiguity and not the statement-list hole by itself.

This is far too close to the intended sub-20-second budget for an entire large
debundle run. Standalone probes and production selector resolution need a
query-local domain-slicing boundary; production must also reuse chunk analysis
across selectors.

## Reproduction

Build the debundler and its CP-SAT sidecar:

```bash
bbr build //devinfra/js/debundle:debundle \
  //devinfra/js/debundle/solver_backends/ortools_cpsat:selector_cpsat_solver
```

For direct binary profiling, point the runtime at the sidecar, enable its summary,
and time the command:

```bash
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER=/path/to/selector_cpsat_solver
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR=/tmp/selector-summary
/usr/bin/time -f 'elapsed=%e maxrss_kb=%M' \
  /path/to/debundle spec match-selector \
  --source-file /path/to/7mb-primary-chunk.js \
  --match 'function decodeEntry(bytes) { let offset = 0, label, children; STMT_LIST_BODY; return [label, children]; }' \
  --no-slack \
  --format json
```

Observed result:

```text
unique target at body index 1649
elapsed=19.51
maxrss_kb=1560536
```

A control probe containing the function's full readable body returned no match
in 33.36 seconds with 1.57 GB RSS. That falsifies the earlier hypothesis that
the broad `STMT_LIST_*` hole was sufficient to explain the latency.

## Build summary

The compiled model summary reported:

| Measure                                |           Value |
| -------------------------------------- | --------------: |
| extracted facts                        |       3,131,689 |
| AST nodes in the full domain           |       1,190,984 |
| selector variables                     |              25 |
| selector atoms                         |              62 |
| compiled variables                     |              27 |
| variables with 1,190,984-value domains |               2 |
| variables with 903-value domains       |               2 |
| singleton variables                    |              23 |
| allowed-table rows                     |               2 |
| serialized request size                | 5,194,536 bytes |
| total serialized domain values         |       2,383,797 |

Model construction took 10.92 seconds:

| Phase                        | Elapsed |
| ---------------------------- | ------: |
| fact domains                 |   7.21s |
| atom lowering                |   2.10s |
| allowed-tuple simplification |   1.11s |
| variables and targets        |   0.48s |

Only two allowed rows survived structural lowering, yet two variables still
carried the full 1.19-million-node domain into a 5.19 MB backend request. CP-SAT
is therefore receiving a needlessly large representation of a narrowly
constrained query.

## Required boundary

The fix should reduce work before backend serialization:

1. Apply declaration-shape and fixed-anchor candidate indexes before assigning
   domains to selector variables.
2. Propagate allowed-table support into those domains so values absent from all
   surviving rows are removed.
3. Build the parsed chunk, fact store, and stable indexes once per chunk and
   reuse them across the production selector program.
4. Add a large-chunk regression that checks maximum variable-domain cardinality
   and serialized request size. Retain elapsed and RSS as outer guardrails.

Per-selector timing remains useful diagnosis, but a timeout or match budget
would only cap the symptom. It would not make the common unique-match path fit
the whole-pipeline budget.
