//! End-to-end coverage for `intrinsic_alias` member selectors through the **full
//! lowering pipeline** — the follow-on companion of `makes_decorate_call`.
//!
//! ## The decorate-trio's two intrinsic-alias companions (the headline case)
//!
//! esbuild emits a byte-identical `__decorate` helper copy per module, reading its
//! `Object.defineProperty` / `Object.getOwnPropertyDescriptor` companions off the
//! global `Object`:
//!
//! ```js
//! var g0 = Object.getOwnPropertyDescriptor;
//! var p0 = Object.defineProperty;
//! var d0 = (decorators, target, key, kind) => { ... uses g0, p0 ... };
//! ```
//!
//! `makes_decorate_call` retires the helper `d0` (pinned by the class it decorates),
//! but the two companions `g0` / `p0` have **no anchor in their own body** — N
//! byte-identical `var X = Object.<method>` copies across modules, and the anchor is
//! the global `Object`, not a spec member. The one re-minify-invariant edge each
//! carries is that it is read **only inside** its trio's `__decorate` helper body.
//! So `intrinsic_alias` pins each by `referenced_by: @<helper>` (the now-stable
//! `@Name` `makes_decorate_call` produced) plus the intrinsic `property` — never by
//! the minified `g0` / `p0`.
//!
//! These tests drive the real `debundle` binary: the spec carries `intrinsic_alias`
//! selectors riding a `makes_decorate_call`-pinned helper, the alias facts are
//! derived from the chunk AST and joined to each alias's declaring owner, and we
//! assert the resolved binding lands in the right module under its readable name and
//! the emitted tree runs under Node with decoration semantics intact. The
//! fail-closed cases (zero / ambiguous / shadowed `Object`) are exercised as
//! rejections.

use debundle_e2e_support::*;

/// A minimal synthetic stand-in for the esbuild decorate-trio: a
/// `getOwnPropertyDescriptor` companion `g`, a `defineProperty` companion `p`, and
/// the decorate helper `d` that **reads both** and is applied to one class via the
/// decorate-call shape `d([tag], C.prototype, "m", 1)`. The realistic minified
/// `__decorate` loop body is not reproduced — the selectors ride only its structure:
/// the helper references `g`/`p` (the `referenced_by` edge each companion is pinned
/// by) and makes the decorate call (the `makes_decorate_call` anchor). Parameterized
/// by a suffix so two independent trios can coexist in one chunk with distinct
/// minified names. The method returns `base`; the decorator appends `mark`
/// (observable under Node as `base + mark`).
fn trio(suffix: &str, class: &str, method: &str, base: &str, mark: &str) -> String {
    format!(
        r#"var g{suffix} = Object.getOwnPropertyDescriptor;
var p{suffix} = Object.defineProperty;
var d{suffix} = (decorators, target, key) => {{
  const desc = g{suffix}(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  p{suffix}(target, key, desc);
}};
const tag{suffix} = (target, key, desc) => {{
  const original = desc.value;
  desc.value = function () {{ return original.call(this) + "{mark}"; }};
}};
class {class} {{
  {method}() {{ return "{base}"; }}
}}
d{suffix}([tag{suffix}], {class}.prototype, "{method}", 1);
"#,
    )
}

/// **The decorate-companion, end to end.** A minified `__decorate` helper (`d0`)
/// reads its `Object.getOwnPropertyDescriptor` (`g0`) and `Object.defineProperty`
/// (`p0`) companions off the global `Object`. The helper is pinned by
/// `makes_decorate_call` (the class it decorates); each companion has no anchor of
/// its own and is pinned by `intrinsic_alias` — the `Object.<property>` alias the
/// `@decorateHelper` reads — never by the minified `g0` / `p0`. Decoration semantics
/// survive lowering (observable under Node).
#[test]
fn intrinsic_alias_pins_companions_by_referencing_helper() {
    let source = format!(
        "{}console.log(new C().greet());\nexport {{ C }};\n",
        trio("0", "C", "greet", "hi", "!")
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
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
                "decorate_runtime",
                &[
                    // The helper is pinned by the class it decorates — the keystone
                    // `makes_decorate_call` primitive, never by the minified `d0`.
                    Member::makes_decorate_call(
                        "decorateClassMember",
                        "DecoratedClass",
                        None,
                        None,
                    ),
                    // Each companion is pinned by the helper that references it +
                    // the intrinsic property — never by the minified `p0` / `g0`.
                    Member::intrinsic_alias("defineProp", "defineProperty", "decorateClassMember"),
                    Member::intrinsic_alias(
                        "getOwnPropDesc",
                        "getOwnPropertyDescriptor",
                        "decorateClassMember",
                    ),
                ],
            ),
        ],
    ));

    // The helper + both companions resolved to their minified bindings (`d0`, `p0`,
    // `g0`) and live in the `decorate_runtime` module under their readable export
    // names — the companions pinned purely by `intrinsic_alias`, never by name.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/decorate_runtime.js",
        &[
            "source bindings: d0, g0, p0",
            "decorateClassMember",
            "defineProp",
            "getOwnPropDesc",
        ],
        &[],
    );
    // The decorator ran at module-init and appended "!" to the method value.
    assert_entry_output(&fixture, "hi!\n");
}

/// **Two-copy disambiguation by `referenced_by`.** Two byte-identical decorate trios
/// (`a` and `b`), each with its own `defineProperty` companion read only by its own
/// helper. The class anchor pins each helper; `referenced_by` then pins each
/// companion to the helper that reads it, so `pa` lands in the `a` module and `pb` in
/// the `b` module — the two companions must not cross despite identical
/// `property: defineProperty`.
#[test]
fn intrinsic_alias_disambiguates_two_companions_by_referencing_helper() {
    let source = format!(
        "{}{}console.log(new Alpha().alphaLabel(), new Beta().betaLabel());\nexport {{ Alpha, Beta }};\n",
        trio("a", "Alpha", "alphaLabel", "a", "A"),
        trio("b", "Beta", "betaLabel", "b", "B"),
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
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
                    Member::makes_decorate_call("alphaDecorator", "Alpha", None, None),
                    // `pa` is read only by `da` (the Alpha helper), so referencing it
                    // by `@alphaDecorator` picks `pa` over the byte-identical `pb`.
                    Member::intrinsic_alias("alphaDefineProp", "defineProperty", "alphaDecorator"),
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
                    Member::intrinsic_alias("betaDefineProp", "defineProperty", "betaDecorator"),
                ],
            ),
        ],
    ));

    // Each companion resolved to its own minified binding via the helper that reads
    // it — `pa` to the Alpha module, `pb` to the Beta module — never crossing.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alpha.js",
        &["var alphaDefineProp = ", "alphaDefineProp"],
        &["betaDefineProp"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/beta.js",
        &["var betaDefineProp = ", "betaDefineProp"],
        &["alphaDefineProp"],
    );
    assert_entry_output(&fixture, "aA bB\n");
}

/// **Generic helper `name:` shared across modules resolves per module.** Two
/// byte-identical decorate trios (`a` and `b`) in two modules, each helper pinned by
/// `makes_decorate_call` and given the **same** readable `name:` `applyDecorators` —
/// the realistic case, since esbuild's `__decorate` helpers carry no distinguishing
/// source and ~11 modules' helpers share one readable name. Each companion's
/// `referenced_by: @applyDecorators` must resolve against **its own module's** helper
/// (esbuild co-locates each helper with its companions), so `pa` lands in the `a`
/// module and `pb` in the `b` module.
///
/// Regression gate for the chunk-global `referenced_by` collapse: building the anchor
/// map over every module mapped the shared export name `applyDecorators` to two
/// distinct bindings (`da`, `db`), collapsed it to ambiguous, and bailed before
/// `intrinsic_alias` resolution ran — even though each helper is unambiguous in its
/// own module. The map must be scoped to the companion's module.
#[test]
fn intrinsic_alias_referenced_by_generic_helper_name_resolves_per_module() {
    let source = format!(
        "{}{}console.log(new Alpha().alphaLabel(), new Beta().betaLabel());\nexport {{ Alpha, Beta }};\n",
        trio("a", "Alpha", "alphaLabel", "a", "A"),
        trio("b", "Beta", "betaLabel", "b", "B"),
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![
            logical_module(
                "alpha",
                &[
                    Member::source_alpha(
                        "Alpha",
                        r#"class Alpha {
  alphaLabel() {
    STMT_LIST;
  }
}"#,
                    ),
                    // Both helpers carry the SAME readable name `applyDecorators` —
                    // a chunk-global anchor map would collapse it to ambiguous.
                    Member::makes_decorate_call("applyDecorators", "Alpha", None, None),
                    Member::intrinsic_alias("alphaDefineProp", "defineProperty", "applyDecorators"),
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
                    Member::makes_decorate_call("applyDecorators", "Beta", None, None),
                    Member::intrinsic_alias("betaDefineProp", "defineProperty", "applyDecorators"),
                ],
            ),
        ],
    ));

    // Each companion resolved against its own module's `applyDecorators` helper: `pa`
    // to the Alpha module, `pb` to the Beta module — distinct bindings, never crossing
    // despite the shared helper name and identical `property: defineProperty`.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alpha.js",
        &["var alphaDefineProp = ", "alphaDefineProp"],
        &["betaDefineProp"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/beta.js",
        &["var betaDefineProp = ", "betaDefineProp"],
        &["alphaDefineProp"],
    );
    assert_entry_output(&fixture, "aA bB\n");
}

/// **Property narrowing.** One helper reads *both* companions of its trio; the
/// `property` label narrows to the matching one — `defineProperty` to `p0`,
/// `getOwnPropertyDescriptor` to `g0` — even though both are referenced by the same
/// `@decorateHelper`.
#[test]
fn intrinsic_alias_narrows_two_companions_of_one_helper_by_property() {
    let source = format!(
        "{}console.log(new Widget().name());\nexport {{ Widget }};\n",
        trio("0", "Widget", "name", "hi", "!")
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
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
                "widget_runtime",
                &[
                    Member::makes_decorate_call("widgetDecorator", "Widget", None, None),
                    Member::intrinsic_alias("defineProp", "defineProperty", "widgetDecorator"),
                    Member::intrinsic_alias(
                        "getOwnPropDesc",
                        "getOwnPropertyDescriptor",
                        "widgetDecorator",
                    ),
                ],
            ),
        ],
    ));

    // Both companions resolved distinctly — the property label kept `p0` and `g0`
    // apart despite sharing the `@widgetDecorator` referencer.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widget_runtime.js",
        &[
            "source bindings: d0, g0, p0",
            "defineProp",
            "getOwnPropDesc",
        ],
        &[],
    );
    assert_entry_output(&fixture, "hi!\n");
}

/// **Fail-closed: no matching alias.** A property the trio never aliases
/// (`getPrototypeOf`) resolves to nothing — the relation picks out zero owners, so
/// the spec is rejected rather than guessing.
#[test]
fn intrinsic_alias_fails_closed_when_no_alias_matches() {
    let source = format!(
        "{}console.log(new C().greet());\nexport {{ C }};\n",
        trio("0", "C", "greet", "hi", "!")
    );
    expect_rejection(
        FixtureOpts::new(
            &source,
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
                    "decorate_runtime",
                    &[
                        Member::makes_decorate_call(
                            "decorateClassMember",
                            "DecoratedClass",
                            None,
                            None,
                        ),
                        // The trio never aliases `Object.getPrototypeOf` — no match.
                        Member::intrinsic_alias("absent", "getPrototypeOf", "decorateClassMember"),
                    ],
                ),
            ],
        ),
        &["intrinsic_alias", "did not resolve"],
    );
}

/// **Fail-closed: ambiguous referencer.** When one helper reads two *distinct*
/// `defineProperty` aliases (a contrived shape that does not occur in real esbuild
/// output), the relation picks out two owners — ambiguous, so the spec is rejected.
#[test]
fn intrinsic_alias_fails_closed_when_two_aliases_share_property_and_referencer() {
    // The helper `d0` reads BOTH `p0` and `q0`, each a `defineProperty` alias.
    let source = r#"var g0 = Object.getOwnPropertyDescriptor;
var p0 = Object.defineProperty;
var q0 = Object.defineProperty;
var d0 = (decorators, target, key) => {
  const desc = g0(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  p0(target, key, desc);
  q0(target, key, desc);
};
const tag0 = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + "!"; };
};
class C {
  greet() { return "hi"; }
}
d0([tag0], C.prototype, "greet", 1);
console.log(new C().greet());
export { C };
"#;
    expect_rejection(
        FixtureOpts::new(
            source,
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
                    "decorate_runtime",
                    &[
                        Member::makes_decorate_call(
                            "decorateClassMember",
                            "DecoratedClass",
                            None,
                            None,
                        ),
                        // Two distinct `defineProperty` aliases referenced by the one
                        // helper — ambiguous, fail-closed.
                        Member::intrinsic_alias(
                            "ambiguous",
                            "defineProperty",
                            "decorateClassMember",
                        ),
                    ],
                ),
            ],
        ),
        &["intrinsic_alias", "did not resolve"],
    );
}

/// **Fail-closed: shadowed `Object`.** When the chunk shadows the global `Object`
/// (`var Object = ...`), the intrinsic-identity guard refuses to emit any
/// intrinsic-alias fact — the alias might read off the local `Object`, not the
/// global — so the selector resolves to nothing and the spec is rejected.
#[test]
fn intrinsic_alias_fails_closed_when_object_is_shadowed() {
    // A chunk-top `var Object = ...` shadows the global; the companions then read off
    // a local `Object`, so the genuine-intrinsic guard yields no rows. The helper is
    // still pinned by `makes_decorate_call` (that mechanism is unaffected), proving
    // the rejection is the intrinsic-alias guard, not a broken helper anchor.
    let source = r#"var Object = globalThis.Object;
var g0 = Object.getOwnPropertyDescriptor;
var p0 = Object.defineProperty;
var d0 = (decorators, target, key) => {
  const desc = g0(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  p0(target, key, desc);
};
const tag0 = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + "!"; };
};
class C {
  greet() { return "hi"; }
}
d0([tag0], C.prototype, "greet", 1);
console.log(new C().greet());
export { C };
"#;
    expect_rejection(
        FixtureOpts::new(
            source,
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
                    "decorate_runtime",
                    &[
                        Member::makes_decorate_call(
                            "decorateClassMember",
                            "DecoratedClass",
                            None,
                            None,
                        ),
                        Member::intrinsic_alias(
                            "defineProp",
                            "defineProperty",
                            "decorateClassMember",
                        ),
                    ],
                ),
            ],
        ),
        &["intrinsic_alias", "did not resolve"],
    );
}
