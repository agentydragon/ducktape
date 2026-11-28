# Whole Lodash join and its public caller

The [npm lock](../../pnpm-lock.yaml) supplies `lodash-es@4.18.1`. The
fixture verifies all 691 bytes of the shipped `join.js`
(SHA-256 `2f791dd3d33d7033f23af1bcb523df3910f26ed52a4eda7a098e0e30ca55496d`)
and its 724-byte package manifest. Both resolve into the same real npm
package directory. There are no source imports. The only free source name
is `Array`; the fixture makes no assumption that it or its methods will
have their initial realm values.

| Row | Original operation                        | Requested owner           |
| --: | ----------------------------------------- | ------------------------- |
|   0 | Read `Array.prototype` into `arrayProto`  | `join-implementation.mjs` |
|   1 | Read `arrayProto.join` into `nativeJoin`  | `join-implementation.mjs` |
|   2 | Hoisted function `join(array, separator)` | `join-implementation.mjs` |
|   3 | Executable `export default join`          | `join.js`                 |

The [spec](lodash_join/split.json) keeps the prefix in source order and
leaves the default snapshot at the original public route. In the function
body, a nullish argument returns an empty string without a method lookup.
The other path looks up `.call` on the saved value and dispatches with the
actual array-like value and separator. Nothing in the fixture implements a
special Lean result for a function named `join`.

The same [public caller](lodash_join/caller.mjs) statically imports each
entry. It makes ordinary calls, then calls with an array-like object whose
first element updates an exported caller cell and whose second element
updates it again and throws a unique object. The caller catches that exact
object and observes a final write count of 11. The oracle replaces
`Array.prototype.join` **after** importing the entry and installs an
observable `.call` getter on the originally saved value. It checks that
both nullish calls skip the getter; the nonnullish call reads it exactly
once, invokes it on the saved function, passes the original receiver and
separator, and visits the length, two slots, and two coercions in order.
The public default is the exact function used by the caller and, in the
split reference, is the function exported by the implementation file.

Two negative reference trees keep the same callable public entry. Reading
`arrayProto.join` at call time selects the replacement and never reaches
either write. Passing an empty array instead of the original receiver
still reaches the saved function's `.call` getter, but returns the empty
string and never enters the original receiver. Neither negative catches
the original thrown object.

| Boundary                  | Evidence                                                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Whole original            | One, all four rows and no module dependencies; exact byte pins                                                                                                               |
| Source projection         | None                                                                                                                                                                         |
| Ordinary CLI              | On the wrapper repair, rejects the whole request at `all-outcomes`, with no files. A separate authenticated source probe reports `statement 0: unsupported global Array`.    |
| Independent disk readback | No CLI artifact to read back. If a later run emits one, the fixture checks it from disk and rejects the two modified helper trees.                                           |
| Reached-call proof        | Pending the shared caller relation and the admission of the original lookup/dispatch; the Node trace is an observation.                                                      |
| Node                      | The exact original and separately staged reference agree; both altered references fail the distinguished observations.                                                       |
| Deployment                | Both Node trees have the exact shipped ESM package manifest. Node syntax checking rejects the staged entry under explicit CommonJS. No strict deployment receipt is claimed. |

The reference stages the original three prefix rows and the original default
row at their requested destinations; only boundary import/export glue is
authored by the fixture. It is not an ordinary CLI output.

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test "$p:lodash_join_test" "$p:lodash_join_node_test" "$p:eslint_test" --test_output=all
```

[Strict baseline and Node replay](https://app.buildbuddy.io/invocation/433d0e7a-571f-4b84-87c4-7125d0bd6679),
using the upstream wrapper build repair from #20731. The earlier
[pre-async replay](https://app.buildbuddy.io/invocation/fe6836a7-a693-4170-bbfd-98448af6b80e)
observed the same constructor and source-classification refusals.
