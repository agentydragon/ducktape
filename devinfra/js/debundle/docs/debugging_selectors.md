# Debugging a selector against known source

Use this workflow when you have located the intended source but the selector
fails to match. It compares one authored selector with an explicitly chosen
source range. It does not decide whether that selector can own the range in the
whole spec.

## Pick the source range

List numbered top-level statements near a known minified binding:

```sh
debundle inspect-source --source-file chunk.js --around-binding a --format text
```

Copy the statement numbers into an inclusive range:

```sh
debundle inspect-source --source-file chunk.js --statements 41..42 --format text
```

The labels are zero-based top-level statement indices in the parsed file. A
multi-declarator declaration is one statement. These are not owner IDs from a
prepared pipeline graph. Inspect the same source file you will pass to the
explanation command; indices can change when the source changes. The original
locations and pretty-printed statements let you check the selected boundaries.

## Supply the selector

For a selector you are still writing:

```sh
debundle spec match-selector --source-file chunk.js \
  --match 'const lower = 7; const upper = 8;' \
  --target-binding upper --statements 41..42 --explain --format text
```

Inline patterns use the same alpha-equivalent identifier policy as ordinary
`match-selector`. Add `--anonymous` for anonymous-statement sequence semantics,
including top-level `STMT_LIST` holes. Member selectors use contiguous windows
and their normal target-binding/declarator semantics.

For a selector already in a module file:

```sh
debundle spec match-selector --source-file chunk.js \
  --selector 'modules/limits.yaml#/source_matches/0' \
  --statements 41..42 --explain --format text
```

The address is a file path followed by `#` and a JSON Pointer to a
`source_matches` or `anonymous_statements` entry. File paths are relative to
the current directory. Entry indices are zero-based. The command reads the
selected entry's mode and target bindings; a source-match group produces one
local explanation per claimed binding. Use `--target-binding` to select one
of the group's selector-local names. Anonymous entries retain exact `match`
versus alpha-equivalent `source_match` semantics.

A flat spec can be addressed without compiling it:

```sh
debundle spec match-selector --source-file chunk.js --spec transform.yaml \
  --selector '#/logical_modules/app/limits/source_matches/0' \
  --statements 41..42 --explain --format text
```

JSON Pointer escapes `/` as `~1` and `~` as `~0` within a chunk or module key.
For tree specs, address the module YAML file directly; this command does not
search module roots. `--match` and `--selector` are mutually exclusive.

## Interpret the result

Check the printed selector and source range first. The explanation uses the
same structural matcher and preserves identifier mappings across the selected
statements. Literal/shape/binding failures identify the involved statements;
complex alternative placements may produce a coarser explanation. A reported
branch failure is an observed comparison on an explored alignment, not a claim
that this comparison alone excludes every alignment. A diagnostic
work limit or unsupported shape is inconclusive, not proof of a mismatch.

A local match means the shape matches here. It does **not** establish uniqueness
elsewhere in the chunk, enforce `all_different`, detect competing claims,
resolve references to other spec exports, or validate extraction order. Free
template identifiers are listed explicitly as external constraints not checked
by this command. Their local alpha mappings are not a proof of global identity.
Loading an entry from a spec does not load other selectors into a solve.
A claimed target that is itself a free reference requires locating a declaration
outside the pattern; this returns `unsupported`. Use an inline `--anonymous`
pattern to inspect just the use-site shape.

After fixing the local shape, use ordinary `spec match-selector` to probe a
candidate across the source, and `spec validate` for references and joint
constraints. Use `run` for the full extraction/realizability gate. Do not weaken
a selector simply to silence a local mismatch without checking that it selects
the intended source in the full spec.

`--format json` returns the same selected source and structured explanations
for programmatic inspection. The command writes no spec or generated files.
