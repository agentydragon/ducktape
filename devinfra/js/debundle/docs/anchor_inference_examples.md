# Anchor inference examples

These synthetic cases capture selector choices that matter when minified names
change between builds. The three executable examples are in
`e2e/selector_anchor_inference_desiderata_test.rs`. They are ignored until the
minimizer can keep matching after the indicated source edit:

| Example             | Distinguishing anchor            | Harmless edit that breaks today's selector      |
| ------------------- | -------------------------------- | ----------------------------------------------- |
| Role versus tooltip | `"data-role": "document-editor"` | Tooltip text changes                            |
| Array member        | `description: "Review drafts"`   | Other elements are inserted before and after it |
| Enum member         | The `Share` label                | Numeric values change                           |

Run the current failures with
`bazel test //devinfra/js/debundle/e2e:selector_anchor_inference_desiderata_test --test_arg=--ignored`.
The normal test target compiles the examples without making CI red.

## A relation is needed when declarations are indistinguishable

```js
function a(x) {
  return x.trim();
}
function b(x) {
  return x.trim();
}
register("primary", a);
register("secondary", b);
```

No selector built only from either function declaration can distinguish `a`
from `b` after renaming. The meaningful anchors are the two `register` calls
and their references to the functions. A neighboring statement can identify a
function by current source position, but that relationship can break if the
statements move. Anchor inference should expose this dependency and offer a
relational selector where one is supported. This is a capability example, not
a failing test for a declaration-only AST selector.

## A deep value makes a long path selector

```js
const a = memo(function () {
  return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open details")))))));
});
const b = memo(function () {
  return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open summary")))))));
});
```

The leaf string distinguishes the declarations, but today's AST selector must
retain each enclosing call to reach it. A descendant-style anchor could express
the intent more compactly if it keeps uniqueness and binding ownership checks.
This needs selector-language design before it can have an executable desired
output assertion.
