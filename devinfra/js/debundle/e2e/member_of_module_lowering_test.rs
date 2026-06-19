//! End-to-end coverage for `member_of_module` member selectors through the
//! **full lowering pipeline** (P4 step 3, X3 wiring). This is the first
//! **use-site** selector: it pins a target by *how it is consumed* — "the entity
//! consumed as `mod.X`", where `mod` is a chunk-top imported binding and `X` an
//! export off it — rather than by the target's own body (X2 `reads_member`) or
//! its own minified name (the name pin). Both labels (the import source `module`
//! and the export `member`) are re-minify-invariant, so the whole edge survives a
//! bundle rebuild.
//!
//! Unlike `selector_solve_test` (which exercises the kernel on a synthetic owner
//! graph), these tests drive the real `debundle` binary: the spec carries
//! `member_of_module` selectors, the use-site facts are derived from the chunk
//! AST joined to the import table, and we assert the resolved binding lands in the
//! right module and the emitted tree runs under Node.
//!
//! ## The empty-class / superclass cluster (the case X3 unlocks)
//!
//! The headline shape is two **empty** subclasses with no internal anchor of
//! their own (`class A extends ns.Base {}`), distinguished *only* by the module
//! member each consumes in its `extends` clause — exactly the `CardsViewAccessor`
//! debt the cross-ref MVP could not reach (debug/2026_06_19_p4_debt_worklist.md
//! Step 3). The use-site member access in the `extends` expression is what the
//! `member_of_module` EDB rides.
//!
//! ## Scope (the faithful boundary, per the abort bar)
//!
//! `member_of_module` pins the **declaring owner whose own subtree consumes
//! `mod.X`** (its `extends` clause, a decorator on it, a body call). It does
//! **not** reach a target distinguished only by an *external* statement that
//! consumes it (`registry.register(Target)`): that owner declares nothing, so the
//! `declares` conjunct correctly excludes it — pinning by a call *argument* needs
//! a `resolves_to`-of-argument edge, a separate later primitive. This test
//! exercises the in-subtree use-site, which is the general primitive built here.

use debundle_e2e_support::*;

const ACCESSORS_CHUNK: &str = r#"export class CardsBase { kind() { return "cards"; } }
export class TreeBase { kind() { return "tree"; } }
"#;

/// **The empty-class / superclass disambiguation, end to end.** Two empty
/// subclasses extend different members of an imported namespace; neither has an
/// internal anchor. `member_of_module` pins each by the `mod.X` it consumes in
/// its `extends` clause — `CardsView` resolves via consuming `accessors.CardsBase`
/// — never by the minified class name. A regression that ignored the use-site
/// edge (or matched the wrong subclass) fails this test.
#[test]
fn member_of_module_disambiguates_empty_subclasses_through_full_pipeline() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import * as accessors from "./accessors.js";
class CardsView extends accessors.CardsBase {}
class TreeView extends accessors.TreeBase {}
console.log(new CardsView().kind(), new TreeView().kind());
export { CardsView, TreeView };
"#,
            vec![logical_module(
                "accessors",
                &[Member::member_of_module(
                    "CardsViewAccessor",
                    "./accessors.js",
                    "CardsBase",
                    Some("class_declaration"),
                )],
            )],
        )
        .with_extra_files(&[("static/app/accessors.js", ACCESSORS_CHUNK)]),
    );

    // The empty subclass resolved to the minified binding `CardsView` (the
    // `source bindings` provenance comment proves it) and lives in the `accessors`
    // module under its readable export name — pinned purely by the module member
    // it consumes, never by the class name. `TreeView` (which consumes a different
    // module member) was correctly left out.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/accessors.js",
        &["CardsViewAccessor"],
        &["TreeView", "CardsView"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/accessors.js",
        &["source bindings: CardsView", "class CardsViewAccessor"],
        &["source bindings: TreeView"],
    );
    assert_entry_output(&fixture, "cards tree\n");
}

/// A delegator with no internal anchor: a helper whose only identity is that it
/// consumes `gen.next` off an imported module. Resolves through the full pipeline
/// to the helper binding, pinned by the (module, member) use-site pair.
#[test]
fn member_of_module_resolves_delegator_through_full_pipeline() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import * as gen from "./gen.js";
function nextId() { return gen.next(); }
console.log(nextId());
export { nextId };
"#,
            vec![logical_module(
                "ids",
                &[Member::member_of_module(
                    "generateId",
                    "./gen.js",
                    "next",
                    Some("function_declaration"),
                )],
            )],
        )
        .with_extra_files(&[(
            "static/app/gen.js",
            "export function next() { return 7; }\n",
        )]),
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/ids.js",
        &["generateId"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/ids.js",
        &["source bindings: nextId", "function generateId"],
        &[],
    );
    assert_entry_output(&fixture, "7\n");
}

/// Fail-closed: a `member_of_module` whose (module, member) pair is consumed by
/// **two** declaring owners is ambiguous, so it errors rather than guessing one.
#[test]
fn member_of_module_ambiguous_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"import * as gen from "./gen.js";
function a() { return gen.next(); }
function b() { return gen.next(); }
console.log(a(), b());
export { a, b };
"#,
            vec![logical_module(
                "ids",
                &[Member::member_of_module("genId", "./gen.js", "next", None)],
            )],
        )
        .with_extra_files(&[(
            "static/app/gen.js",
            "export function next() { return 1; }\n",
        )]),
        &["member_of_module", "next"],
    );
}

/// Fail-closed: a `member_of_module` whose module member no declaring owner
/// consumes errors rather than silently dropping the claim.
#[test]
fn member_of_module_zero_match_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"import * as gen from "./gen.js";
function a() { return gen.present(); }
console.log(a());
export { a };
"#,
            vec![logical_module(
                "ids",
                &[Member::member_of_module("genId", "./gen.js", "absentMember", None)],
            )],
        )
        .with_extra_files(&[("static/app/gen.js", "export function present() { return 1; }\nexport function absentMember() { return 2; }\n")]),
        &["member_of_module", "absentMember"],
    );
}

/// Fail-closed: a member access on a **non-imported** local is not a
/// module-member use, so a `member_of_module` selector naming that object's
/// (would-be) module finds nothing and errors. The import join — not any member
/// access — is what makes a row, so consuming `local.next` off a chunk-top
/// declaration (not an import) yields no use-site edge.
#[test]
fn member_of_module_non_imported_object_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const gen = { next() { return 3; } };
function a() { return gen.next(); }
console.log(a());
export { gen, a };
"#,
            vec![logical_module(
                "ids",
                &[Member::member_of_module("genId", "./gen.js", "next", None)],
            )],
        ),
        &["member_of_module", "next"],
    );
}
