//! End-to-end coverage for `@Name` cross-reference member selectors through the
//! **full lowering pipeline** (P4 step 1, X1 wiring). A `cross_ref` member pins
//! its target by a relational edge to a separately-identified anchor member —
//! "the function that references `@Anchor`", "the var-decl that aliases
//! `@Anchor`" — instead of by the target's own minified name. Unlike
//! `selector_solve_cross_reference_test` (which exercises the kernel directly on
//! an emitted `owner_graph.json`), these tests drive the real `debundle` binary:
//! the spec carries `cross_ref` selectors, and we assert the resolved binding
//! lands in the right module and the emitted tree runs under Node.
//!
//! These also pin the **anchor-ordering decision** (see `materialize::cross_ref`):
//! the anchor's binding comes from the already-resolved members of the chunk
//! (anchor-first), because the owner graph's `export_name` is not populated at
//! member-resolution time. `cross_ref_anchor_ordering_uses_resolved_member_binding`
//! is the discriminating case — the anchor is itself pinned by a non-name
//! selector, so a name-based shortcut could not have found it.

use debundle_e2e_support::*;

/// The canonical metaNode delegator shape: a shapeless `function UBt(x){ return
/// EBt(x) }` pinned as "the function that references `@isTranscriptionProvider`",
/// where `isTranscriptionProvider` is the readable name of the anchor member
/// `EBt`. Resolves through the full pipeline to `UBt`, with no minified name
/// written in the spec for the target.
#[test]
fn cross_ref_references_resolves_delegator_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function EBt(x) { return x + 1; }
function UBt(x) { return EBt(x); }
console.log(UBt(41));
export { EBt, UBt };
"#,
        vec![
            logical_module(
                "anchors",
                &[Member::renamed("isTranscriptionProvider", "EBt")],
            ),
            logical_module(
                "delegators",
                &[Member::cross_ref_references(
                    "isMeetingTranscriptionProvider",
                    "isTranscriptionProvider",
                    Some("function_declaration"),
                )],
            ),
        ],
    ));

    // The delegator resolved to the minified binding UBt (the `source bindings`
    // provenance comment proves it) and lives in the `delegators` module under its
    // readable export name — pinned purely by what it references, never by `UBt`.
    // Naturalization renames the body binding to its readable export name and the
    // imported anchor to its readable name.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/delegators.js",
        &["isMeetingTranscriptionProvider"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/delegators.js",
        &[
            "source bindings: UBt",
            "import { isTranscriptionProvider }",
            "function isMeetingTranscriptionProvider",
            "return isTranscriptionProvider(",
        ],
        &[],
    );
    // The anchor itself resolved by its readable name into a different module.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/anchors.js",
        &["isTranscriptionProvider"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/anchors.js",
        &["source bindings: EBt"],
        &[],
    );
    assert_entry_output(&fixture, "42\n");
}

/// The re-export shape: `const HI = Acc` pinned as "the var-decl that aliases
/// `@NodeAttributeAccessor`". Resolves to `HI` through the full pipeline.
#[test]
fn cross_ref_aliases_resolves_reexport_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Acc { tag() { return "acc"; } }
const HI = Acc;
console.log(new HI().tag());
export { Acc, HI };
"#,
        vec![
            logical_module(
                "anchors",
                &[Member::renamed("NodeAttributeAccessor", "Acc")],
            ),
            logical_module(
                "aliases",
                &[Member::cross_ref_aliases(
                    "CardsViewAccessor",
                    "NodeAttributeAccessor",
                )],
            ),
        ],
    ));

    // The alias resolved to the minified binding HI (proven by the provenance
    // comment), exported under its readable name and importing the anchor class
    // (renamed `NodeAttributeAccessor`) it aliases.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/aliases.js",
        &["CardsViewAccessor"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/aliases.js",
        &["source bindings: HI", "NodeAttributeAccessor"],
        &[],
    );
    assert_entry_output(&fixture, "acc\n");
}

/// **Multiple `cross_ref` members in one module, anchored in that same module.**
/// Two alias re-exports (`const A2 = Acc1; const B2 = Acc2;`) are both `cross_ref`
/// `aliases:` members in the **same** logical module, each anchoring on a class
/// (`Acc1` / `Acc2`) that is itself pinned by `source_match` in that same module.
///
/// Regression gate for the same-module multi-`cross_ref` bug: the pre-resolution
/// duplicate-binding gate grouped both still-unresolved cross_ref members under the
/// empty `<unresolved>` binding and rejected the module before the cross-ref pass
/// ran, even though each alias resolves to a distinct binding (`A2`, `B2`). The
/// gate must skip post-Stage-A selector members (whose binding is filled in later)
/// — they are duplicate-checked against their real bindings once resolved.
#[test]
fn cross_ref_aliases_multiple_in_one_module_resolve_to_distinct_bindings() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Acc1 { m1() { return "a1"; } }
class Acc2 { m2() { return "a2"; } }
const A2 = Acc1;
const B2 = Acc2;
console.log(new A2().m1() + new B2().m2());
export { Acc1, Acc2, A2, B2 };
"#,
        vec![logical_module(
            "exports",
            &[
                // Both anchors pinned structurally in this same module.
                Member::source_alpha("FirstAccessor", r#"class $a { m1() { return "a1"; } }"#),
                Member::source_alpha("SecondAccessor", r#"class $a { m2() { return "a2"; } }"#),
                // Two cross_ref alias members anchored on those same-module anchors.
                Member::cross_ref_aliases("FirstAccessorExport", "FirstAccessor"),
                Member::cross_ref_aliases("SecondAccessorExport", "SecondAccessor"),
            ],
        )],
    ));

    // All four resolve into the one module: the two anchors under their readable
    // names, and the two aliases each to their distinct binding (`A2`, `B2`).
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/exports.js",
        &[
            "FirstAccessor",
            "SecondAccessor",
            "FirstAccessorExport",
            "SecondAccessorExport",
        ],
        &[],
    );
    // Each alias resolved to its own distinct binding: the first cross_ref to `A2`
    // (`const A2 = Acc1`), the second to `B2` (`const B2 = Acc2`) — not both to the
    // shared `<unresolved>` sentinel. The combined provenance comment lists both
    // source bindings, and both distinct alias declarations are emitted.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/exports.js",
        &[
            "source bindings: A2",
            "B2",
            "const FirstAccessorExport = FirstAccessor;",
            "const SecondAccessorExport = SecondAccessor;",
        ],
        &[],
    );
    assert_entry_output(&fixture, "a1a2\n");
}

/// **Anchor-ordering decision, pinned.** The anchor member `EBt` is itself pinned
/// by a `source_match` selector (not by name), and renamed to the readable
/// `isTranscriptionProvider`. The `cross_ref` member then anchors on that readable
/// name. This only resolves if the cross-ref pass takes the anchor's binding from
/// the **already-resolved member** (`EBt`, resolved by source_match) rather than
/// from the owner graph's `export_name` (which is not populated at
/// member-resolution time) or from the readable name treated as a minified
/// binding. A regression to either of those shortcuts fails this test.
#[test]
fn cross_ref_anchor_ordering_uses_resolved_member_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function EBt(x) { return x + 1; }
function UBt(x) { return EBt(x); }
console.log(UBt(41));
export { EBt, UBt };
"#,
        vec![
            logical_module(
                "anchors",
                // The anchor is selected structurally — its readable name
                // `isTranscriptionProvider` maps to the source-resolved binding
                // `EBt`, never written in the spec.
                &[Member::source_alpha(
                    "isTranscriptionProvider",
                    "function $a($b) { return $b + 1; }",
                )],
            ),
            logical_module(
                "delegators",
                &[Member::cross_ref_references(
                    "isMeetingTranscriptionProvider",
                    "isTranscriptionProvider",
                    Some("function_declaration"),
                )],
            ),
        ],
    ));

    // The anchor was selected structurally: its source binding is `EBt` (proven by
    // the provenance comment), exported under the readable `isTranscriptionProvider`.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/anchors.js",
        &["isTranscriptionProvider"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/anchors.js",
        &["source bindings: EBt"],
        &[],
    );
    // The delegator resolved to UBt via the source-resolved anchor binding — the
    // anchor-first handle, not a name shortcut.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/delegators.js",
        &["isMeetingTranscriptionProvider"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/delegators.js",
        &["source bindings: UBt", "import { isTranscriptionProvider }"],
        &[],
    );
    assert_entry_output(&fixture, "42\n");
}

/// Fail-closed: a `cross_ref` whose anchor names no resolved member errors rather
/// than guessing.
#[test]
fn cross_ref_unknown_anchor_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function EBt(x) { return x + 1; }
function UBt(x) { return EBt(x); }
console.log(UBt(41));
export { EBt, UBt };
"#,
            vec![logical_module(
                "delegators",
                &[Member::cross_ref_references(
                    "isMeetingTranscriptionProvider",
                    "noSuchAnchor",
                    Some("function_declaration"),
                )],
            )],
        ),
        &["noSuchAnchor", "cross_ref"],
    );
}
