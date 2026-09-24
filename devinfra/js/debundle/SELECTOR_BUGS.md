# Selector Bugs And Gaps

Selector issues found while porting a downstream debundle spec. Examples are
intentionally generic and anonymized. Every entry carries a **Status** naming the
date it was last reproduced; an entry is deleted when its fix lands with a test.

## Unknown Hole Keywords Match As Identifiers

Status: open (reproduced 2026-09-24). `source_match/holes.rs`
`unsupported_selector_hole_name` is a stub returning `None`, so a hole keyword
the binary does not know is a plain free identifier on every path, including
`match-selector`. A selector written for a newer hole vocabulary and run by an
older pinned debundler reports `no_match` or `ambiguous` instead of `invalid`.

The fix is a design decision first: an old binary cannot recognise a keyword it
has never seen, only a name of a reserved shape. <SPEC.md> § Matching rule 1
needs a reservation rule for hole-shaped names that every future hole keyword
falls inside and that real chunk identifiers do not (an all-caps rule would
catch globals such as `JSON` and `URL`). A reserved name the binary does not
implement is then `invalid` with "unsupported selector hole", on every command.

## Duplicate-Claim Outcomes Lack Declaration Detail

Status: open (checked 2026-09-24 against `selector_outcome.rs`). The
`duplicate_claim` outcome carries only `binding` (the minified spelling) and
`claimed_by`. With short minified names reused across chunks, it is hard to
tell which declaration was claimed twice.

Desired behavior: include the declaration kind and source location of the
claimed binding in the outcome.
