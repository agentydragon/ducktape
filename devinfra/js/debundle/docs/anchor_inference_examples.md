# Anchor inference examples

These synthetic end-to-end examples in
`e2e/selector_anchor_inference_desiderata_test.rs` capture selectors that keep
matching when minified names and unrelated source structure change:

| Example             | Anchor or hole                   | Rebuild edit                                    |
| ------------------- | -------------------------------- | ----------------------------------------------- |
| Role versus tooltip | `"data-role": "document-editor"` | Tooltip text changes                            |
| Array member        | `description: "Review drafts"`   | Other elements are inserted before and after it |
| Enum member         | The `Share` label                | Numeric values change                           |
| Registration use    | `register("primary", selected)`  | Declarations and calls move                     |
| Deep value          | `AST_CONTAINS_STRING`            | Wrapper call path changes                       |

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
from `b` after renaming. The minimizer can select a direct call use site by its
literal marker and the argument reference, with `ARGS` holes around both. This
lets the call and declaration move independently. A dedicated relational
selector remains useful for use sites that cannot be expressed by this shape.

## A deep value without wrapper pins

```js
const a = memo(function () {
  return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open details")))))));
});
const b = memo(function () {
  return wrap1(wrap2(wrap3(wrap4(wrap5(wrap6(wrap7("Open summary")))))));
});
```

The leaf string distinguishes the declarations. For a deep literal the
minimizer can emit `const Selected = AST_CONTAINS_STRING("Open details");`
instead of retaining every wrapper call. This predicate tests for an exact
string literal **AST node** below the initializer; it does not search source
text or perform substring matching. The normal matcher still proves the
selector resolves to one binding before synthesis offers it.
