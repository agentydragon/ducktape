# Selector Bugs And Gaps

Selector issues found while porting a downstream debundle spec. Examples are
intentionally generic and anonymized. Every entry carries a **Status** naming the
date it was last reproduced; an entry is deleted when its fix lands with a test.

## Unknown Hole Keywords Match As Identifiers

Status: open (reproduced 2026-09-24). The selector capability check is a stub:
`source_match/holes.rs` `unsupported_selector_hole_name` returns `None`, so an
unknown hole keyword is a plain identifier on every path, including
`match-selector` (which parses through
`parse_selector_module_with_capability_check`). A selector written for a newer
hole vocabulary and run by an older pinned debundler reports `no_match` or
`ambiguous` instead of `invalid`.

Desired behavior: a selector naming a hole keyword the binary does not support
is `invalid` with "unsupported selector hole", on every command.

## Duplicate-Claim Outcomes Lack Declaration Detail

Status: open (checked 2026-09-24 against `selector_outcome.rs`). The
`duplicate_claim` outcome carries only `binding` (the minified spelling) and
`claimed_by`. With short minified names reused across chunks, it is hard to
tell which declaration was claimed twice.

Desired behavior: include the declaration kind and source location of the
claimed binding in the outcome.
