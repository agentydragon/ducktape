//! Soundness: `var` declarations hoist out of blocks (`try`/`catch`, `if`,
//! loops) to the enclosing function or module scope, so
//! `collect_declared_names` (facts/mod.rs) must see them, not only direct
//! top-level `Stmt::Decl` items. For a statement like
//!
//! ```js
//! try { var impl = detect(); } catch (e) { var impl = fallback(); }
//! ```
//!
//! the statement owns `impl`: readers of `impl` get a dependency graph edge and
//! import wiring (otherwise the emitted reader module references a name
//! that doesn't exist in its scope — runtime `ReferenceError`), and `try {
//! var Math = shim; } catch {}` counts for the A8 shadowed-globals
//! computation (the purity whitelist must not treat `Math.floor` as the
//! pristine global).

use debundle_e2e_support::*;

/// Reader split away from a block-hoisted `var` whose declaring
/// statement is anonymously claimed into another module. The `try`
/// statement owns `impl`: the reader's eager edge orders `mod_impl` first
/// and the binding-adoption pass exports/imports it across the split.
#[test]
fn reader_of_block_hoisted_var_in_claimed_statement_is_wired() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }
const reader = impl + "-read";
console.log(reader);
export { reader };
"#,
        vec![
            logical_module_with_anon(
                "mod_impl",
                &[],
                &[r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }"#],
            ),
            logical_module("mod_reader", &[Member::new("reader")]),
        ],
    ));
    assert_entry_output(&fixture, "primary-read\n");
}

/// Green companion: co-locating the reader with the declaring `try`
/// statement is accepted and preserves behavior — the new ownership
/// edge is same-module and imposes no cross-module constraint.
#[test]
fn block_hoisted_var_colocated_with_reader_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }
const reader = impl + "-read";
console.log(reader);
export { reader };
"#,
        vec![logical_module_with_anon(
            "mod_a",
            &[Member::new("reader")],
            &[r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }"#],
        )],
    ));
    assert_entry_output(&fixture, "primary-read\n");
}

/// A nested `var` statement that remains in the entry is already mapped by
/// its source-unit owner; it does not need a separate whole-statement claim.
#[test]
fn unclaimed_nested_var_stays_in_entry_with_lazy_reader() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"try { var impl = "primary"; } catch (e) { var impl = "fallback"; }
function reader() { return impl; }
console.log(reader());
export { reader };
"#,
        vec![logical_module("mod_reader", &[Member::new("reader")])],
    ));
    assert_entry_output(&fixture, "primary\n");
}

/// Nested-var routing is explicit: claiming only one binding cannot silently
/// pull its unassigned sibling into the same module.
#[test]
fn partially_claimed_nested_var_statement_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            "try { var left = 10; var right = 20; } catch (e) {}\n",
            vec![logical_module("left_module", &[Member::new("left")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["NestedVarMissingOwner", "right"],
    );
}

/// Existing residual ownership counts as an observed destination and cannot
/// be combined with a conflicting explicit binding owner.
#[test]
fn nested_var_explicit_and_residual_owners_must_agree() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            "try { var left = 10; var right = 20; } catch (e) {}\n",
            vec![logical_module("left_module", &[Member::new("left")])],
        )
        .with_unassigned_mode(unassigned_mode_catchall_file(None)),
        &["NestedVarDestinationSplit", "left_module", "residual"],
    );
}

/// The explicit whole-statement request participates in the same split
/// decision as binding owners.
#[test]
fn nested_var_whole_statement_request_must_match_binding_owner() {
    let statement = "try { var left = 10; var right = 20; } catch (e) {}";
    expect_rejection_containing_all(
        FixtureOpts::new(
            "try { var left = 10; var right = 20; } catch (e) {}\n",
            vec![
                logical_module(
                    "binding_module",
                    &[Member::new("left"), Member::new("right")],
                ),
                logical_module_with_anon("statement_module", &[], &[statement]),
            ],
        ),
        &[
            "NestedVarDestinationSplit",
            "binding_module",
            "statement_module",
        ],
    );
}

/// A coherent explicit request is accepted, and the enclosing control-flow
/// statement runs in the module that owns its hoisted binding.
#[test]
fn nested_var_whole_statement_request_agrees_with_binding_owner() {
    let statement = "try { var impl = \"primary\"; } catch (e) { var impl = \"fallback\"; }";
    let fixture = run_fixture(FixtureOpts::new(
        "try { var impl = \"primary\"; } catch (e) { var impl = \"fallback\"; }\n\
console.log(impl);\n",
        vec![logical_module_with_anon(
            "impl_module",
            &[Member::new("impl")],
            &[statement],
        )],
    ));
    assert_entry_output(&fixture, "primary\n");
}

/// With no explicit owner, catchall assignment routes the complete statement
/// as a residual unit and preserves its runtime behavior.
#[test]
fn unclaimed_nested_var_statement_runs_from_residual() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "try { var impl = \"primary\"; } catch (e) { var impl = \"fallback\"; }\n\
console.log(impl);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_catchall_file(None)),
    );
    assert_entry_output(&fixture, "primary\n");
}

/// Multiple control-flow statements may share one redeclared module-scoped
/// binding; every statement must follow that same binding owner.
#[test]
fn repeated_nested_var_statements_follow_shared_binding_owner() {
    let fixture = run_fixture(FixtureOpts::new(
        "if (true) { var shared = \"first\"; }\n\
if (true) { var shared = \"second\"; }\n\
console.log(shared);\nexport { shared };\n",
        vec![logical_module("shared_module", &[Member::new("shared")])],
    ));
    assert_entry_output(&fixture, "second\n");
}

/// A8 shadowing: `try { var Math = shim; } catch {}` makes every
/// unqualified `Math.<prop>` in the chunk a read of the shim, not the
/// global. `Math` is chunk-declared, so `const out = Math.PI` is not
/// whitelisted-pure and the read gets an eager dependency edge into entry,
/// which evaluates last — unrealizable, rejected.
#[test]
fn block_hoisted_var_shadowing_whitelisted_global_is_rejected() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"try { var Math = { PI: "shimmed" }; } catch (e) {}
const out = Math.PI;
console.log(out);
export { out };
"#,
            vec![logical_module("mod_out", &[Member::new("out")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mod_out"],
    );
}
