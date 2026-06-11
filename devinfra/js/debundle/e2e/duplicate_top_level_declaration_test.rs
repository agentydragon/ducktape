//! Strict owner mapping: two distinct top-level statements declaring
//! the same binding (`var dup = 1; var dup = 2;` — legal JS) used to
//! be modeled last-insert-wins in `build_owner_graph_with`'s
//! `binding_owner` table, so every edge into the earlier declaration's
//! owner was silently dropped and the earlier statement could be
//! ordered after its readers. The analyzer now rejects such chunks
//! outright (soundness over completeness — see AGENTS.md).

use debundle_e2e_support::*;

#[test]
fn duplicate_top_level_var_declaration_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"var dup = "first";
var dup = "second";
console.log(dup);
export { dup };
"#,
            vec![logical_module("mod_a", &[Member::new("dup")])],
        ),
        &["duplicate top-level declaration", "dup"],
    );
}

#[test]
fn duplicate_function_declaration_is_rejected() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"function pick() { return "first"; }
function pick() { return "second"; }
console.log(pick());
export { pick };
"#,
            vec![logical_module("mod_a", &[Member::new("pick")])],
        ),
        &["duplicate top-level declaration", "pick"],
    );
}

/// Distinct bindings that merely share a name across scopes (a
/// top-level `dup` and a function-local `dup`) are not duplicates:
/// the owner table is keyed by hygienic identity, not name.
#[test]
fn same_name_in_inner_scope_is_not_a_duplicate() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var dup = "outer";
function shadow() { var dup = "inner"; return dup; }
console.log(dup, shadow());
export { dup, shadow };
"#,
        vec![logical_module(
            "mod_a",
            &[Member::new("dup"), Member::new("shadow")],
        )],
    ));
    assert_entry_output(&fixture, "outer inner\n");
}
