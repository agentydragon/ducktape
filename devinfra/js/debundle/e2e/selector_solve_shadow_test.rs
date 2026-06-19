//! Bootstrap shadow gate for the relational-selector solver.
//!
//! The in-process Datalog solver (`selector_solve`) must reproduce the
//! production binding-name resolver (`peel::resolve_binding_owners` — the
//! shared helper behind `describe` / `show-source` / `cluster` /
//! `scc --binding`) on the *minified-name surface*. The solver's
//! `name_owner` relation is the Datalog re-expression of "the owner(s)
//! declaring this minified binding", which is exactly the by-minified
//! branch of `resolve_binding_owners`.
//!
//! Proving the two agree over an `owner_graph.json` the real pipeline
//! emits is the equivalence check the bootstrap rests on (see
//! `plans/selector_constraint_model.md`): it guards against EDB/parse
//! drift between the lean solver EDB and the canonical report schema,
//! and pins the bootstrap precondition (minified names are categorical)
//! on output the binary actually produces — not a hand-built fixture.

use std::collections::BTreeSet;
use std::fs;

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use peel::resolve_binding_owners;
use selector_solve::solve_str;

/// Owner ids (`owner:N`) the production resolver returns for `name` **by
/// its minified binding** — the by-minified branch only, which is the
/// surface the bootstrap solver reproduces (`resolve_binding_owners`
/// also appends owners matched by readable export name, which the
/// minified-only solver deliberately ignores).
fn production_minified_owners(graph: &OwnerGraphReport, name: &str) -> BTreeSet<String> {
    resolve_binding_owners(graph, name)
        .into_iter()
        .filter(|node| node.declared_bindings.iter().any(|b| b.binding == name))
        .map(|node| node.id.clone())
        .collect()
}

#[test]
fn solver_name_pin_agrees_with_production_resolver_on_real_graph() {
    // A multi-binding chunk: a hoisted function, a const initialized from
    // a call to it (an eager_use edge), and a class — three distinct
    // owners with distinct minified names. `alpha` is re-exported under a
    // readable name (`Alpha`) so the emitted graph carries an export_name
    // that differs from the binding, exercising that the solver keys on
    // the minified binding and not the readable export.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function alpha() { return 1; }
const beta = alpha();
class Gamma {}
console.log(beta);
export { alpha, beta, Gamma };
"#,
        vec![logical_module(
            "shapes",
            &[
                Member::renamed("Alpha", "alpha"),
                Member::new("beta"),
                Member::new("Gamma"),
            ],
        )],
    ));
    assert_entry_output(&fixture, "1\n");

    let text = fs::read_to_string(fixture.report_root.join("static/app/owner_graph.json"))
        .expect("emitted owner_graph.json");
    let graph: OwnerGraphReport = serde_json::from_str(&text).expect("parse owner graph report");
    let resolution = solve_str(&text).expect("solve owner graph");

    // Precondition the bootstrap rests on: every minified binding the
    // graph declares resolves to exactly one owner.
    let shadow = resolution.shadow_check();
    assert!(
        shadow.ok(),
        "minified names must be categorical on real output; ambiguous: {:?}",
        shadow.ambiguous,
    );

    let declared: BTreeSet<&str> = graph
        .nodes
        .iter()
        .flat_map(|node| node.declared_bindings.iter().map(|b| b.binding.as_str()))
        .collect();
    assert!(
        declared.is_superset(&BTreeSet::from(["alpha", "beta", "Gamma"])),
        "fixture should declare alpha/beta/Gamma; got {declared:?}",
    );

    // Equivalence: for every minified binding the graph declares, the
    // Datalog solver and the production resolver name the same owner set.
    for name in &declared {
        let solver_owners: BTreeSet<String> = resolution
            .name_to_owners
            .get(*name)
            .into_iter()
            .flatten()
            .map(|owner| format!("owner:{owner}"))
            .collect();
        assert_eq!(
            solver_owners,
            production_minified_owners(&graph, name),
            "solver and production resolver disagree on minified binding {name:?}",
        );
    }

    // Scope guard: the solver's index domain is exactly the declared
    // minified bindings — no readable export name (e.g. `Alpha`) leaks in
    // as a spurious key. The readable surface is future relational work.
    for key in resolution.name_to_owners.keys() {
        assert!(
            declared.contains(key.as_str()),
            "solver indexed {key:?}, which is not a declared minified binding",
        );
    }
}
