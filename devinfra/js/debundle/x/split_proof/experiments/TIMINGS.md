# Timings

Every number here was measured on one machine (4 cores, 15 GB, Linux), Lean 4.35.0 from nixpkgs.
Node was v22.23.5 for the first measurements and v24.22.2 for the later ones. Node only parses and runs smoke tests, so it is not in the check times.
Wall-clock unless noted. Raw output is in [`records/`](records) and [`splits/results/`](splits/results).
Where a run did not finish, the table says so and gives the time at which I stopped it.

The checker went through six stages. Names used below:

1. **first version**: the checks as plain statements, list-based.
2. **argument sharing**: `ReadsClosed B := ∀ i, B.Closed (B.reads i)`, so compiled code computes `B.reads i` once.
3. **indexed closure and module tables**: a worklist closure over a `TreeMap` index; tables for `Hygienic`, `Linkable`, `UniqueDefs`.
4. **shared tables**: statements, index and fuel built once; `Permutes` with sets; `mkPlan` with index maps.
5. **`Faithful` tables**: claimants once per statement; a statement set per module. (The final stage.)

The main tables are in [`README.md` § 5](README.md#5-how-far-does-the-check-scale). This file holds the rest.

## Building and running

| What                                                                                                           | Time                               |
| -------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| fetch Lean 4.35 from the Nix cache (first time)                                                                | 38 s                               |
| clean `lake build` (default targets: core library and `render`)                                                | 18.6 s                             |
| then `lake build Experiments check lower lowersplit evalorder`                                                 | 14.7 s                             |
| `./run.sh --controls --fuzz --experiments --lowering --es-order --splits` from a fresh copy in the repository  | 302 s                              |
| kernel check of the real 12-statement example (`lake env lean Experiments/Generated.lean`, with process start) | 1.5 s (0.6 s in the first version) |
| fuzzer, 600 requests (510 unique splits), each rendered and run in Node                                        | 47–53 s                            |
| mutation test, 6 mutants × 400 splits                                                                          | 247 s                              |
| `splits/run_all.sh`: 2,740 splits of six real bundles, each checked and run in Node, 4 jobs                    | 154 s                              |
| `es_order/check.mjs 500 1`: 500 random import graphs, Node against the model                                   | 52 s                               |

## The check on the example, compiled

| Stage                                                | Time |
| ---------------------------------------------------- | ---- |
| everything, `lowersplit` on the 12-statement example | 2 ms |

## Real bundles: lowering to ESTree and the Lean footprint analysis

| Bundle          | Source | Top-level statements | `node dump.mjs` (acorn + eslint-scope + JSON) | Lean footprints (process start + JSON parse + analysis) |
| --------------- | ------ | -------------------- | --------------------------------------------- | ------------------------------------------------------- |
| lodash (subset) | 84 KB  | 670                  | 182 ms                                        | 79 ms                                                   |
| acorn           | 199 KB | 447                  | 238 ms                                        | 92 ms                                                   |
| semver (CJS)    | 72 KB  | 59                   | 154 ms                                        | 78 ms                                                   |
| zod             | 744 KB | 1,072                | 465 ms                                        | 200 ms                                                  |
| acorn, minified | 119 KB | 374                  | 202 ms                                        | 92 ms                                                   |
| zod, minified   | 447 KB | 805                  | 531 ms                                        | 192 ms                                                  |

Best of 2 (dump) and best of 3 (Lean). Inside one process the footprint stage alone took 3 ms (lodash, 670 statements), 6 ms (acorn, 447) and 35 ms (zod, minified, 805).

## Real bundles: how the checks got fast

**Stage 1 to 2.** `ReadsClosed` on lodash prefixes (every other check together stayed under 15 ms):

| Statements          | First version                       | Argument sharing |
| ------------------- | ----------------------------------- | ---------------- |
| 100                 | 3,838 ms                            | 104 ms           |
| 150                 | 24,438 ms                           | 346 ms           |
| 670 (all of lodash) | did not finish; stopped after 685 s | 53,107 ms        |

At 670 statements the whole run was 53.9 s, of which `ReadsClosed` was 53.1 s. Stage 2 did not change the kernel time (the kernel caches repeated terms).
Raw: [`records/real_bundles_1_list_closure.log`](records/real_bundles_1_list_closure.log).

**Stage 2 to 3.** Same runs with the indexed closure and module tables ([`records/real_bundles_2_indexed_closure.log`](records/real_bundles_2_indexed_closure.log)):
lodash 54 s → 0.4 s, acorn 4.2 s → 0.2 s. The seven checks on full zod (1,072 statements) went from 4.3 s to 0.26 s; `Hygienic` alone from 3,911 ms to 4 ms and `Linkable` from 194 ms to 2 ms.

**Stage 3 to 5, and the final table.** Renamed copies of lodash, to reach sizes no real bundle here has:
[`records/replicate_1_tables.log`](records/replicate_1_tables.log), [`replicate_2_shared_arrays.log`](records/replicate_2_shared_arrays.log),
[`replicate_3_faithful.log`](records/replicate_3_faithful.log) and the final run [`replicate_4_final.md`](records/replicate_4_final.md).
Totals per stage are in [`README.md` § 5](README.md#5-how-far-does-the-check-scale).
At 21,440 statements: 252 s after stage 4 (`Faithful` 242 s of it), 10.5 s after stage 5. In the final run, `lowersplit` at 21,440 statements takes 10.3 s and 119 MB.

## Kernel

Synthetic bundles, one `lake env lean` process each, names as strings and as numbers, with peak memory:
[`README.md` § 5](README.md#5-how-far-does-the-check-scale), script [`scaling/nat_names.sh`](scaling/nat_names.sh).
The first version took 0.7 s (12 statements), 5.0 s (48), 19.7 s (96) and 51 s (144), and was killed after 88 s at 192.
`decide +kernel` on the lodash split (670 statements) was killed at the 12 GB limit after 61.7 s.
Elaborating the data takes 0.4–0.6 s of each run. In Lean's interpreter, 192 statements of the first version had not finished after 10 minutes.

## Proof strategies on real splits

[`README.md` § 8](README.md#8-how-a-proof-is-produced); raw output in [`records/ladder_semver.txt`](records/ladder_semver.txt), [`ladder_lodash.txt`](records/ladder_lodash.txt) and [`ladder_acorn_min_rejected.txt`](records/ladder_acorn_min_rejected.txt).

| Split                                                  | Strategy         | Result                                     | Time   |
| ------------------------------------------------------ | ---------------- | ------------------------------------------ | ------ |
| semver, 59 statements                                  | `decide`         | `maximum recursion depth has been reached` | 5.5 s  |
| semver                                                 | `decide +kernel` | proved                                     | 16.6 s |
| lodash, 670 statements                                 | `decide`         | `maximum recursion depth has been reached` | 1.8 s  |
| lodash                                                 | `decide +kernel` | killed at 12 GB                            | 61.7 s |
| lodash                                                 | `native_decide`  | proved                                     | 2.3 s  |
| acorn, minified, 374 statements, rejected by `OrderOk` | `native_decide`  | evaluated the proposition to false         | 1.4 s  |

## Where it still does not finish

| Input                                      | Result                                                  |
| ------------------------------------------ | ------------------------------------------------------- |
| `decide +kernel`, 288 synthetic statements | killed at 12 GB after 66.6 s (names as numbers: 63.6 s) |
| `decide +kernel`, lodash, 670 statements   | killed at 12 GB after 61.7 s                            |
