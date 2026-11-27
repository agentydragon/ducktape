# Acorn node locations across module owners

The pinned Acorn bundle is 204,565 bytes and has 447 original source units.
This projection extends the six-row line-scanning projection with Acorn's
original `Node` constructor and its lexical dependency `SourceLocation`.
The seven body statements are copied verbatim; the original standalone
export row retains `Node`, `getLineInfo`, and `isNewLine`. The resulting
1,413-byte module has SHA-256
`1d42bb7d9c0196ebb76178a7ce8135fc308e720f54987d2e297a8e395d24f044`.
The source has no free ambient bindings.

| Projected ordinal | Original ordinal | Unit                        |
| ----------------- | ---------------- | --------------------------- |
| #0                | #22              | `isNewLine`                 |
| #1                | #23              | `nextLineBreak`             |
| #2                | #35              | `Position`                  |
| #3                | #36              | `Position.prototype.offset` |
| #4                | #37              | `SourceLocation`            |
| #5                | #38              | `getLineInfo`               |
| #6                | #269             | `Node`                      |
| #7                | #446             | Standalone export row       |

The Node control parses JavaScript with a CRLF, an astral character, and a
Unicode line separator using the full pinned Acorn API. In the projection,
`getLineInfo` calls the extracted scanner and predicate. `Node` receives the
resulting position and constructs a `SourceLocation` that contains that exact
object. The control observes line 2, column 0, range `[18, 0]`, and the same
source-file fields as the full module. The position's `offset(4)` method
returns a fresh position at line 2, column 4.

Both authored layouts passed those observations in independent Node module
graphs. The ordinary CLI certified **0/2** on
[#20630 at `9309d55f`](https://app.buildbuddy.io/invocation/04028c08-5f2b-4947-89f1-c8156b51ca5a):

| Priority, with entry last                     | Original units outside entry | First CLI refusal                                                                                                                                     |
| --------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `newline.mjs`, `scanner.mjs`                  | #22, #23                     | `entry unit 6: expected concrete retained code`; `Node` uses `[pos, 0]` in the range assignment, and the retained expression check has no array case. |
| `newline.mjs`, `scanner.mjs`, `locations.mjs` | #22, #23, then #35–37        | `expected a predicate module and at most one reader module`                                                                                           |

Both returned `no_constructed_rule` without writing a destination. The earlier
six-row projection's 2/3 result is a separate measurement.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:acorn_node_location --test_output=all
```
