# Literal Initializer Selector

## Problem

Some generated bundles contain stable literal initializer values even when the
binding names around them churn. Today, a spec author must write a full
`source_match` body such as:

```yaml
members:
  - name: ApiMode
    selector:
      source_match:
        identifiers: alpha_all
        match: 'const apiMode = "preview";'
```

That is precise, but it makes the selector look like an AST-shaped context
match when the author really means "select the unique binding initialized to
this literal value." The extra source text also nudges authors to copy nearby
generated syntax they do not actually care about.

## Proposed Shape

Add a member selector form that selects a binding by a unique literal
initializer in the chunk:

```yaml
members:
  - name: ApiMode
    selector:
      literal_initializer:
        value: "preview"
```

Possible extension points:

- `kind: string | number | boolean | null`, if untagged YAML values are too
  ambiguous.
- `declaration: const | let | var`, if authors need to avoid same-value
  literals in a different declaration kind.
- `binding_name_hint: apiMode`, for diagnostics only, not as a matching key.

## Semantics

The selector should scan top-level variable declarators and match declarators
whose initializer is exactly the requested literal. It should reject unless the
literal initializer resolves to exactly one binding in the chunk.

Initial scope should stay intentionally small:

- Support top-level `var` / `let` / `const` declarators.
- Support simple literal initializers only.
- Do not match object properties, call arguments, class fields, or nested
  declarations.
- Do not match expressions that evaluate to the literal after folding.

## Diagnostics

Zero-match and ambiguous-match errors should include the requested literal and
the closest relevant candidates. Ambiguity should list body/declarator
locations and binding names so the author can refine the selector, for example
by falling back to `source_match` or by adding a future `declaration:` filter.

## Validation Plan

Use generic e2e fixtures:

- Positive: `const apiMode = "preview";` is selected and emitted under the
  requested export name.
- Ambiguous: two top-level declarators initialized to `"preview"` reject with
  both candidate binding names.
- Zero-match: `const apiMode = "stable";` rejects with the requested
  `"preview"` literal in the message.
- Non-goal guard: nested `function setup() { const apiMode = "preview"; }`
  does not match.
