# Selector Bugs And Gaps

Selector issues found while porting a downstream debundle spec. Examples are
intentionally generic and anonymized. Every entry carries a **Status** naming the
date it was last reproduced; an entry is deleted when its fix lands with a test.

## Stable Identifiers Are Only Local To One Match

Status: open (reproduced 2026-09-23 through `debundle run`). A free identifier
naming another spec entity is an alpha wildcard, not a failed match. With a
`Widget` class selector plus `const defaultWidget = new Widget(ANYTHING);`, a
chunk constructing two classes is rejected as ambiguous, and a chunk
constructing only the other class resolves `defaultWidget` to that instance.

The same applies to any readable name another selector captures:

```js
function readContext(node, limit = contextLimit) {
  return renderNode(node).slice(0, limit);
}

async function produceResult(node, services) {
  const context = readContext(node, contextLimit);
  if (context.length < minContextChars) return [];
  return services.run(context);
}
```

With `readContext`, `contextLimit` and `minContextChars` captured by other
selectors, a separate selector for `produceResult` treats those names as
wildcards rather than as the bindings those selectors took.

Workarounds: put adjacent declarations in one binding group; replace the
cross-selector references with `EXPR_*` holes; or fall back to a name pin.

The semantics and the fix are template references, <plans/selector_engine.md>.

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
