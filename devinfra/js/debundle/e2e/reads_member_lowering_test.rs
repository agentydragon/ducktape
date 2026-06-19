//! End-to-end coverage for `reads_member` member selectors through the **full
//! lowering pipeline** (P4 step 2, X2 wiring). A `reads_member` member pins its
//! target by the member it reads (`obj.X`) — "the function that reads
//! `.uniqueId`", "the helper that reads `.id` off the codegen context" — instead
//! of by the target's own minified name. This is the stable identity of the ~72
//! TS codegen helpers, currently name-pinned.
//!
//! Unlike `selector_solve_test` (which exercises the kernel directly on a
//! synthetic owner graph), these tests drive the real `debundle` binary: the
//! spec carries `reads_member` selectors, the member-read facts are derived from
//! the chunk's AST and joined to the owner graph, and we assert the resolved
//! binding lands in the right module and the emitted tree runs under Node.
//!
//! The object-constrained tests also pin the **object-anchor ordering decision**
//! (shared with `cross_ref`): the object's minified binding comes from the
//! already-resolved members of the chunk, because the owner graph's `export_name`
//! is not populated at member-resolution time.

use debundle_e2e_support::*;

/// The canonical codegen-helper shape: a helper `function ls(){ return
/// gen.nextUniqueId(); }` pinned as "the function that reads `.nextUniqueId`".
/// Resolves through the full pipeline to `ls`, with no minified name written in
/// the spec for the target.
#[test]
fn reads_member_resolves_helper_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const gen = { counter: 0, nextUniqueId() { return ++this.counter; } };
function ls() { return gen.nextUniqueId(); }
console.log(ls());
export { gen, ls };
"#,
        vec![
            logical_module("registry", &[Member::renamed("idGenerator", "gen")]),
            logical_module(
                "helpers",
                &[Member::reads_member(
                    "generateUniqueId",
                    "nextUniqueId",
                    None,
                    Some("function_declaration"),
                )],
            ),
        ],
    ));

    // The helper resolved to the minified binding `ls` (the `source bindings`
    // provenance comment proves it) and lives in the `helpers` module under its
    // readable export name — pinned purely by the member it reads, never by `ls`.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/helpers.js",
        &["generateUniqueId"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/helpers.js",
        &["source bindings: ls", "function generateUniqueId"],
        &[],
    );
    assert_entry_output(&fixture, "1\n");
}

/// **Object-constrained resolution + ordering decision, pinned.** Two helpers
/// each read `.id`, but off different top-level objects: `ctxId` reads
/// `ctx.id` (the codegen context), `nodeId` reads `node.id`. The bare member is
/// ambiguous; the `object: @CodegenContext` constraint — resolved to the minified
/// binding `ctx` via the *already-resolved* anchor member, never a name shortcut
/// — picks out exactly the context helper. A regression that ignored the object
/// constraint (or resolved the object by its readable name treated as a minified
/// binding) fails this test.
#[test]
fn reads_member_object_constraint_disambiguates_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const ctx = { id: "ctx-1" };
const node = { id: "node-1" };
function ctxId() { return ctx.id; }
function nodeId() { return node.id; }
console.log(ctxId(), nodeId());
export { ctx, node, ctxId, nodeId };
"#,
        vec![
            logical_module(
                "context",
                // The object anchor is selected structurally — its readable name
                // `CodegenContext` maps to the source binding `ctx`, never written
                // in the spec as a target pin.
                &[Member::source_alpha(
                    "CodegenContext",
                    r#"const $a = { id: "ctx-1" };"#,
                )],
            ),
            logical_module(
                "helpers",
                &[Member::reads_member(
                    "readContextId",
                    "id",
                    Some("CodegenContext"),
                    Some("function_declaration"),
                )],
            ),
        ],
    ));

    // The context object was selected structurally: its source binding is `ctx`.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/context.js",
        &["source bindings: ctx"],
        &[],
    );
    // The helper resolved to `ctxId` — the function reading `.id` off `@ctx` — not
    // `nodeId`, which reads `.id` off a different object.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/helpers.js",
        &["readContextId"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/helpers.js",
        &["source bindings: ctxId", "function readContextId"],
        &["source bindings: nodeId"],
    );
    assert_entry_output(&fixture, "ctx-1 node-1\n");
}

/// Fail-closed: a bare `reads_member` whose member is read by **two** declaring
/// owners is ambiguous, so it errors rather than guessing one.
#[test]
fn reads_member_ambiguous_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a = { shared: 1 };
function readA() { return a.shared; }
function readToo() { return a.shared; }
console.log(readA(), readToo());
export { a, readA, readToo };
"#,
            vec![logical_module(
                "helpers",
                &[Member::reads_member("readShared", "shared", None, None)],
            )],
        ),
        &["reads_member", "shared"],
    );
}

/// Fail-closed: a `reads_member` whose member no declaring owner reads errors
/// rather than silently dropping the claim.
#[test]
fn reads_member_zero_match_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a = { present: 1 };
function readPresent() { return a.present; }
console.log(readPresent());
export { a, readPresent };
"#,
            vec![logical_module(
                "helpers",
                &[Member::reads_member(
                    "readMissing",
                    "absentMember",
                    None,
                    None,
                )],
            )],
        ),
        &["reads_member", "absentMember"],
    );
}

/// Fail-closed: a `reads_member` whose `object:` anchor names no resolved member
/// errors rather than guessing.
#[test]
fn reads_member_unknown_object_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const ctx = { id: 1 };
function readId() { return ctx.id; }
console.log(readId());
export { ctx, readId };
"#,
            vec![logical_module(
                "helpers",
                &[Member::reads_member(
                    "readContextId",
                    "id",
                    Some("noSuchObject"),
                    None,
                )],
            )],
        ),
        &["noSuchObject", "reads_member"],
    );
}
