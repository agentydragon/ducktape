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

The four-file layout now certifies with exactly the authored priority:
`newline.mjs`, `scanner.mjs`, `locations.mjs`, then `entry.mjs`.
The scanner imports `isNewLine`; the locations module has no imports and
exports the two original constructor bindings for the entry. `getLineInfo`
uses the imported `Position`, the installed `offset` method captures that
same binding, and `Node` uses the imported `SourceLocation`. The entry keeps
the original public row; neither constructor becomes an entry export.

The constructor admits the third owner only for adjacent concrete constructor
initializations with their prototype write in between. The write runs after
`Position` is assigned, at its original ordinal. The ordered link and
execution proof preserves the captured cells and the public table at every
fuel bound. Swapping the scanner and locations priorities changes the full
ordinal schedule from `[0,1,2,3,4,5,6,7]` to `[0,2,3,4,1,5,6,7]` and is refused.

The four files pass Node and disk readback. Two disk edits are rejected:
rebinding `Node` to an equal-valued copy of `SourceLocation` breaks constructor
identity, and running the prototype write before `Position` initializes
throws in Node. The wrong-priority request also leaves the valid tree intact.

The same projection also certifies `locutil-grouped`, with all four source
members **#35–38** in `locations.mjs`. It keeps the earlier predicate and
scanner in their existing separate projection owners and keeps `Node` and the
selected public export row in the entry. The extra admitted row is a
concretely lowered hoisted function immediately after the constructor block,
capturing exactly the local constructor and the earlier scanner. The same
ordered link, live export and disk readback checks apply. Node confirms that
the entry exposes the same `getLineInfo` function as the helper, exercises
the original constructor/prototype behavior, and detects a changed scanner
binding that also fails disk readback.

This recovers **4/4** of the known surviving `locutil` rows, with no unrelated
body rows in that projection owner. The exact **447-row** workload has a
different authored placement: it leaves #22 and #23 in the entry with the
other retained units. Its empty-profile lowering stops at the unrelated
eager `RegExp` at #8, before group checking. The projection adds no global
premise and does not establish a split or a footprint for that full request.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:acorn_node_location --test_output=all
```
