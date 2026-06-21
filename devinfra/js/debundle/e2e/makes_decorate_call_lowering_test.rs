//! End-to-end coverage for `makes_decorate_call` member selectors through the
//! **full lowering pipeline** — the inverse-direction sibling of `passed_to_call`.
//! It pins a target by the target binding being the **callee** of an
//! esbuild/TypeScript `__decorate`-style decorator application on a pinned class
//! (`__decorate([d], C.prototype, "m")`), rather than by the target being passed to
//! a call, by its own body / use sites, or by its own minified name.
//!
//! ## The byte-identical decorate-helper copy (the headline case)
//!
//! esbuild emits a byte-identical `__decorate` helper copy per module, reading its
//! `Object.defineProperty` / `Object.getOwnPropertyDescriptor` companions off the
//! global `Object`. The helper definitions therefore have **no anchor in their own
//! body** — no `source_match` can pin them, and they are otherwise stuck as fragile
//! `binding.name` pins (the esbuild TS decorate-helper debt). The one
//! re-minify-invariant edge each helper carries is the decorator application it
//! *makes*: the decorated class is a separately-pinned entity reachable through
//! `resolves_to`, and the decorated-member string literal is a source identifier.
//!
//! These tests drive the real `debundle` binary: the spec carries
//! `makes_decorate_call` selectors, the decorate-call facts are derived from the
//! chunk AST and joined to each helper callee's declaring owner, and we assert the
//! resolved binding lands in the right module under its readable name and the
//! emitted tree runs under Node with decoration semantics intact.

use debundle_e2e_support::*;

/// **The decorate-helper copy, end to end.** A minified `__decorate` helper (here
/// `d0`) reads the `Object.getOwnPropertyDescriptor` / `Object.defineProperty`
/// intrinsic aliases off the global `Object` and is applied to `C.prototype` — the
/// exact esbuild trio shape. The helper has no anchor of its own;
/// `makes_decorate_call` pins it by the class it decorates (`@DecoratedClass`),
/// never by the minified `d0`. Decoration semantics survive lowering (the decorator
/// mutates the prototype, observable under Node).
#[test]
fn makes_decorate_call_pins_helper_by_decorated_class() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var g0 = Object.getOwnPropertyDescriptor;
var p0 = Object.defineProperty;
var d0 = (decorators, target, key, kind) => {
  for (var desc = kind > 1 ? void 0 : kind ? g0(target, key) : target, i = decorators.length - 1, decorator; i >= 0; i--)
    (decorator = decorators[i]) && (desc = (kind ? decorator(target, key, desc) : decorator(desc)) || desc);
  return (kind && desc && p0(target, key, desc), desc);
};
const tag = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + "!"; };
  return desc;
};
class C {
  greet() { return "hi"; }
}
d0([tag], C.prototype, "greet", 1);
console.log(new C().greet());
export { C };
"#,
        vec![
            logical_module(
                "model",
                &[Member::source_alpha(
                    "DecoratedClass",
                    r#"class C {
  greet() {
    STMT_LIST;
  }
}"#,
                )],
            ),
            logical_module(
                "decorate_helper",
                &[Member::makes_decorate_call(
                    "decorateClassMember",
                    "DecoratedClass",
                    None,
                    None,
                )],
            ),
        ],
    ));

    // The helper resolved to the minified `d0` (the `source bindings` provenance
    // comment proves it) and lives in the `decorate_helper` module under its
    // readable export name — pinned purely by decorating `@DecoratedClass`, never by
    // the minified `d0`.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/decorate_helper.js",
        &["source bindings: d0", "decorateClassMember"],
        &[],
    );
    // The decorator ran at module-init and appended "!" to the method value.
    assert_entry_output(&fixture, "hi!\n");
}

/// **Member-literal disambiguation.** Two byte-identical `__decorate` helper copies
/// each decorate a *different* class. The class anchor alone pins each; an explicit
/// `member:` further narrows to the decorator application on one decorated member,
/// proving the member literal rides through the full pipeline. The two helper
/// copies must not interfere.
#[test]
fn makes_decorate_call_disambiguates_two_helper_copies_by_class() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var ga = Object.getOwnPropertyDescriptor;
var pa = Object.defineProperty;
var da = (decorators, target, key, kind) => {
  for (var desc = kind > 1 ? void 0 : kind ? ga(target, key) : target, i = decorators.length - 1, decorator; i >= 0; i--)
    (decorator = decorators[i]) && (desc = (kind ? decorator(target, key, desc) : decorator(desc)) || desc);
  return (kind && desc && pa(target, key, desc), desc);
};
var gb = Object.getOwnPropertyDescriptor;
var pb = Object.defineProperty;
var db = (decorators, target, key, kind) => {
  for (var desc = kind > 1 ? void 0 : kind ? gb(target, key) : target, i = decorators.length - 1, decorator; i >= 0; i--)
    (decorator = decorators[i]) && (desc = (kind ? decorator(target, key, desc) : decorator(desc)) || desc);
  return (kind && desc && pb(target, key, desc), desc);
};
const mark = (suffix) => (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + suffix; };
  return desc;
};
class Alpha {
  alphaLabel() { return "a"; }
}
class Beta {
  betaLabel() { return "b"; }
}
da([mark("A")], Alpha.prototype, "alphaLabel", 1);
db([mark("B")], Beta.prototype, "betaLabel", 1);
console.log(new Alpha().alphaLabel(), new Beta().betaLabel());
export { Alpha, Beta };
"#,
        vec![
            logical_module(
                "alpha",
                &[
                    // Distinct method name `alphaLabel` is an invariant label, so the
                    // source_match pins this class (not the same-shaped Beta).
                    Member::source_alpha(
                        "Alpha",
                        r#"class Alpha {
  alphaLabel() {
    STMT_LIST;
  }
}"#,
                    ),
                    // Pinned by class + the decorated member literal "alphaLabel".
                    Member::makes_decorate_call(
                        "alphaDecorator",
                        "Alpha",
                        Some("alphaLabel"),
                        None,
                    ),
                ],
            ),
            logical_module(
                "beta",
                &[
                    Member::source_alpha(
                        "Beta",
                        r#"class Beta {
  betaLabel() {
    STMT_LIST;
  }
}"#,
                    ),
                    Member::makes_decorate_call("betaDecorator", "Beta", None, None),
                ],
            ),
        ],
    ));

    // Each helper copy resolved to its own minified binding via the class it
    // decorates — `da` to the Alpha module, `db` to the Beta module — never crossing.
    // The provenance comment lists the module's source bindings together
    // (`Alpha, da`), so assert on the readable export name plus the helper binding
    // token, and that the *other* copy's binding never leaks in.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alpha.js",
        &["var alphaDecorator = ", "export { Alpha, alphaDecorator }"],
        &["betaDecorator", "class Beta"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/beta.js",
        &["var betaDecorator = ", "export { Beta, betaDecorator }"],
        &["alphaDecorator", "class Alpha"],
    );
    assert_entry_output(&fixture, "aA bB\n");
}

/// **Kind narrowing through the pipeline.** The decorate helper is a
/// `variable_declarator`; the `kind: variable_declarator` constraint a real selector
/// carries rides through resolution. (A single helper here, but the constraint must
/// not spuriously exclude the genuine `var` helper.)
#[test]
fn makes_decorate_call_constrains_by_kind_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var gx = Object.getOwnPropertyDescriptor;
var px = Object.defineProperty;
var dx = (decorators, target, key, kind) => {
  for (var desc = kind > 1 ? void 0 : kind ? gx(target, key) : target, i = decorators.length - 1, decorator; i >= 0; i--)
    (decorator = decorators[i]) && (desc = (kind ? decorator(target, key, desc) : decorator(desc)) || desc);
  return (kind && desc && px(target, key, desc), desc);
};
const stamp = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + "*"; };
  return desc;
};
class Widget {
  name() { return "w"; }
}
dx([stamp], Widget.prototype, "name", 1);
console.log(new Widget().name());
export { Widget };
"#,
        vec![
            logical_module(
                "widget",
                &[Member::source_alpha(
                    "Widget",
                    r#"class Widget {
  name() {
    STMT_LIST;
  }
}"#,
                )],
            ),
            logical_module(
                "widget_decorate",
                &[Member::makes_decorate_call(
                    "widgetDecorator",
                    "Widget",
                    None,
                    Some("variable_declarator"),
                )],
            ),
        ],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widget_decorate.js",
        &["source bindings: dx", "widgetDecorator"],
        &[],
    );
    assert_entry_output(&fixture, "w*\n");
}
