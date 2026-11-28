# Acorn's location group in the full bundle

All four surviving units of `acorn/src/locutil.js` land together in the Rust
output for the existing source-files request. A second, two-owner request
extracts exactly those four units and leaves the rest in entry. Rust and
strict LowerSplit both refuse that concentrated request. The location
consumer matches on the first output, but the complete entry namespace
exposes six additional binding names.

The input is the complete 447-unit, 204,565-byte Acorn 8.19.0 bundle pinned
at `e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee`.
The test checks its ordinal spans against the
[blocker census](FULL_BLOCKER_CENSUS.md) and composes the esbuild map with
[the byte-verified Rollup map](SOURCE_OWNERSHIP.md). The upstream file
[`acorn/src/locutil.js`](https://github.com/acornjs/acorn/blob/0e0ece7cc7202624d4aca531be4b515b0114b5d0/acorn/src/locutil.js)
has SHA-256 `a546816b2e0936baf39cfee6d5116f62f3b75029780d6c7c64de036c339aba58`.

| Ordinal | Full bundle UTF-8 and UTF-16 range (half-open) | Upstream anchor lines | Bundled unit                             |
| ------: | ---------------------------------------------- | --------------------: | ---------------------------------------- |
|      35 | [19036, 19126)                                 |                  6–10 | `Position` initialization                |
|      36 | [19127, 19229)                                 |                 12–14 | `Position.prototype.offset` installation |
|      37 | [19230, 19402)                                 |                 17–22 | `SourceLocation` initialization          |
|      38 | [19403, 19651)                                 |                 31–38 | `getLineInfo` declaration                |

These distinct units cover 612 input span bytes, with 573 bytes mapped back
to the upstream file. The Rollup/Bublé output lowers the two upstream
classes to function initializations and splits out the prototype write.
The installation at #36 needs the already initialized #35; calling
`offset` later uses the same `Position` cell. The hoisted `getLineInfo`
function captures `Position` and the private `nextLineBreak` (#23) from
`whitespace.js`; the scanner calls `isNewLine` (#22). None of #35–38
reads an ambient name. The concentrated request keeps #22–23 in entry, so
`getLineInfo` still needs the scanner cell from the original program.

The [ledger](results/acorn-locutil.json) lists exact lexical references by
ordinal, independently of the Rust dependency gate. In particular, parser
position creation (#258), AST node construction (#269), token construction
(#399), and parse-error reporting (#256) exercise the group. The original
`Parser.acorn` assignment (#442) and entry export row (#446) both publish
`Position`, `SourceLocation`, and `getLineInfo`. The scanner is private.

| Full-bundle request     | Requested group / total owners                                           | Rust output                                                                                                                                                   | Strict `all-outcomes` output  |
| ----------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| Existing `source_files` | 4/4 in one home; 26 owners                                               | 4/4 in `app/main/owners/8.js`, zero unrelated original units and no residual or unknown group members. Owner file: 1,103 bytes; whole tree: 234,384 JS bytes. | Refused; 0 files; no readback |
| `locutil`               | 4/4 in `locutil.mjs`; other 443 units in entry, including #446; 2 owners | Refused; 0 files; no output group to measure                                                                                                                  | Refused; 0 files; no readback |

The test verifies the Rust selector resolution against the requested
assignment before running the gate. The single-group request has spec
SHA-256 `27797b85a670d3f4b70d9fe8fe76fb8fd82547cadff80999edba6cc59000bf05`.
Its Rust rejection reports a two-owner cycle: #36 reaches entry's
`TokenType` (#13) through the conservative at-init closure of an unresolved
call, while the entry's `Parser.prototype.parse` installation (#64)
reaches `Position` (#35). These edges are gate diagnostics. The reported
cycle does not establish that the prototype method calls `TokenType`.
Both ordinary strict requests returned `no_constructed_rule`. The separate
source-shape probe stopped at the eager `RegExp` in #8, before reaching
the location group.

The [eight-row projection from #20713](ACORN_NODE_LOCATION.md) certifies
all four location units in `locations.mjs` at `all-outcomes`, passes disk
readback, and rejects a changed scanner binding. It puts #22 and #23 in
separate earlier owners and exports only `Node`, `getLineInfo`, and
`isNewLine` from entry. The full concentrated request keeps the predicate
and scanner in entry and preserves the original public constructors and
`Parser.acorn` requirements. The existing full source-files request
places #22 and #23 together in the `whitespace.js` owner. The proof of
the projected placement does not test these full-bundle layouts.

The public consumer parses CRLF, an astral string, and a Unicode line
separator. It checks token, comment, AST and syntax-error locations,
per-parse `sourceFile` and `directSourceFile`, UTF-16 columns, and
a fresh position from `offset`. It confirms that parser results, the
emitted locutil owner, `Parser.acorn`, and the entry exports use the same
constructors and function. A wrong source label and an equal-fields object
with a different prototype both fail. The existing Rust source-files output
passes those observations and keeps `nextLineBreak` private, but gains six
entry exports: `keywords`, `parse3`, `parseExpressionAt2`, `tokenizer2`,
`types`, and `types$1`. The older smoke test did not compare the whole
namespace. The concentrated request has no artifact on which to make a
public surface claim.

## Exact public entry for the source-files layout

A separate Node replay puts the 26 Rust-emitted files under
`implementation/` byte for byte and puts one explicit re-export at the
original `app/main/entry.js` path. It forwards the names from the pinned
source namespace; generated bindings stay inside the implementation. Both
trees run under the test's explicit ESM package scope. The graph check
verifies that all implementation requests are relative and closed, rejects
dynamic imports and URL metadata, and compares the two depth-first orders.
The original implementation is acyclic. The wrapper adds only the final
public entry to that order and introduces no back edge.

The wrapper exposes exactly the original namespace. The three location
exports are the same functions as in the implementation and in
`Parser.acorn`. The location consumer still passes and `nextLineBreak`
stays absent from the public namespace. A star re-export of the same tree
exposes the six glue names again. All four locutil body units stay together
with zero unrelated body units; the wrapper contributes one generated file
and no original units. This is an alternative entry layout for the
source-files artifact. The concentrated request still fails the Rust
initialization cycle, and the full strict proof still stops at eager
`RegExp` in #8.

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test "$p:acorn_locutil_test" "$p:source_ownership_unit_test" \
  "$p:standalone_parser_dump_test" "$p:eslint_test" \
  --test_output=all --remote_build_event_upload=all
```

The [recorded replay](https://app.buildbuddy.io/invocation/7a03b1ae-304b-4bc6-9996-949ed34f516d)
writes `acorn-locutil.json` and `acorn-locutil-spec.json`. If strict emission
succeeds in a future replay, the test checks the actual files from disk
before repeating the public location and namespace checks.
