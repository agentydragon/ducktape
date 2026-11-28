# When the semver bundle initializes its CommonJS wrappers

The pinned semver 7.8.6 ESM bundle has 59 top-level units and SHA-256
`9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44`.

- `#3` saves `Object.getOwnPropertyNames`. The ordinary named function
  returned by `__commonJS` at `#6` reads this live cell when its cache is
  empty.
- `#9` creates the loader for `internal/constants.js`. Six original callback
  bodies call it: `#11`, `#14`, `#38`, `#40`, `#41`, and `#55`.
- `#56` evaluates `__toESM(require_semver2(), 1)`. It is the only direct
  module-scope call to one of the loaders. It calls `#55`, whose first
  dependency is `#11`; `#11` calls `#9`. Those are the first three cold
  callback lookups in the import trace.

## Reproduce

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:semver_wrapper_node_test
```

The test writes `semver-wrapper-node.json` and the three-unit projection
as undeclared outputs. The evidence covers different inputs and phases:

| Evidence           | What ran or was checked                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Original           | Scoped ESTree identifies 47 loader allocations (`#9`–`#55`) and 125 direct calls to those bindings, including the single module-scope call.                                                                                                                                                                                                                                                    |
| Reached calls      | A separate copy of the same bytes imports with a delegating observer in the ambient `Object.getOwnPropertyNames` slot. The factory captures that observer through unit `#3`. All 47 cold callback lookups occur once during import. Calling `valid`, `satisfies`, and the existing public API smoke suite produces no further callback lookup. This trace counts lookups, not every warm call. |
| Shipped Node API   | A clean import runs the same public smoke suite. Its result matches the observed import, whose only export is `semver`; importing its exact URL again returns the same object.                                                                                                                                                                                                                 |
| Private projection | The printer transplants `#3`, `#6`, and `#9` into a test driver. The test reparses the output and checks the three initializers, including exact factory spelling. The driver checks constants identity and uses a synthetic callback to make throw and reentry observable. It does not add a public loader export.                                                                            |
| Deployment         | The ordinary constructor tries keeping `#0`–`#55` in `prefix.mjs` ahead of `#56`–`#58` in `entry.mjs`, with `all-outcomes` required. It refuses with `no_constructed_rule`, emits no files, and therefore has no split to reread. The test independently checks the staged files and public API if this placement is later certified.                                                          |

A separate import of the shipped bytes throws from the captured helper
on the first `#9` lookup. It observes only `#55`, `#11`, and `#9`, then
rejects with the same error object. Importing that URL again returns
the cached module evaluation error, with no additional lookup. After
restoring the helper, a fresh URL initializes and its public `valid`
call succeeds. ESM import retries and retries of a private loader inside
a surviving module are different observations.

The live-cell control keeps the full bundle and inserts a test hook just
before `#56`. An independent parse checks that all 59 original units remain
in their original order. The hook can replace the actual lexical
`__getOwnPropNames` binding during a lookup. Its replacement delegates except
that it throws if asked for the constants callback.

- Rebinding during the `#11` lookup happens before the first `#9` call.
  The replacement sees `#9` and the import rejects with its error.
- Rebinding during the `#10` lookup happens after `#9` returns to `#11`.
  The later `#9` calls complete without another constants lookup. In
  particular, `#55` reads the constants for the public
  `semver.SEMVER_SPEC_VERSION` export. The import and public API smoke
  complete with the baseline results.

The test records both traces under `liveRebind`. The inserted hook adds no
public export to the bundle; the trace is a behavior control rather than a
certification of the instrumented file.

In the private control, factory allocation calls the callback zero times.
The first call installs the cache before the callback can reenter. Reentry
sees the same partial exports, including a write made before the callback
throws. The caller gets the original error, and the next call allocates a
distinct module and exports object. When that second callback replaces
`module.exports`, both the first successful result and the warm result
are the replacement object.

The observed lookup delegates to the real operation in that run. Neither
the trace nor the projection supplies a general intrinsic integrity premise.
The measured cold path in the shipped bundle is part of initialization;
the tested public API has no cold loader call after that prefix. The
projection's later cold call cannot stand in for such a public entry point.
