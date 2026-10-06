//! Soundness: `var` declarations hoist out of blocks (`try`/`catch`, `if`,
//! loops) to the enclosing function or module scope, so
//! `collect_declared_names` (facts/mod.rs) must see them, not only direct
//! top-level `Stmt::Decl` items. For a statement like
//!
//! ```js
//! try { var impl = detect(); } catch (e) { var impl = fallback(); }
//! ```
//!
//! the statement owns `impl`: readers of `impl` get an owner-graph edge and
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

/// A8 shadowing: `try { var Math = shim; } catch {}` makes every
/// unqualified `Math.<prop>` in the chunk a read of the shim, not the
/// global. `Math` is chunk-declared, so `const out = Math.PI` is not
/// whitelisted-pure and the read gets an eager owner edge into entry,
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
