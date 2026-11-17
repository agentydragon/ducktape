//! Exact array spreads retain runtime behavior when materialization reaches its cap.

use debundle_e2e_support::*;

const SMALL_SOURCE: &str = r#"const seed = ["a", "b"];
const expanded = [...seed, ...seed];
console.log(expanded.join(","));
export { expanded };
"#;

fn modules(branch_statement: Option<&str>) -> Vec<LogicalModuleEntry> {
    let members = [Member::new("seed"), Member::new("expanded")];
    vec![match branch_statement {
        Some(branch) => logical_module_with_anon("app/arrays", &members, &[branch]),
        None => logical_module("app/arrays", &members),
    }]
}

fn repeated_spread_source(doublings: usize) -> (String, String) {
    let mut branch = String::from("if (globalThis.expand) {\n");
    for _ in 0..doublings {
        branch.push_str("  expanded = [...expanded, ...expanded];\n");
    }
    branch.push_str("}\n");
    let source = format!(
        "const seed = [\"safe\"];\nlet expanded = seed;\n{branch}console.log(expanded.length, expanded[0]);\nexport {{ expanded }};\n"
    );
    (source, branch.trim_end().to_string())
}

fn callback_spread_source(doublings: usize) -> (String, String) {
    let mut branch = String::from("if (globalThis.expand) {\n");
    for _ in 0..doublings {
        branch.push_str("  expanded = [...expanded, ...expanded];\n");
    }
    branch.push_str("}\n");
    let source = format!(
        "const seed = [() => later];\nlet expanded = seed;\n{branch}\
         try {{ expanded[0](); }} catch (error) {{ console.log(error.name); }}\n\
         const later = 'ready';\nexport {{ expanded }};\n"
    );
    (source, branch.trim_end().to_string())
}

#[test]
fn small_exact_spreads_preserve_node_behavior() {
    let fixture = run_fixture(FixtureOpts::new(SMALL_SOURCE, modules(None)));
    assert_preserved_entry_output(&fixture, "a,b,a,b\n");
}

#[test]
fn unknown_branch_with_repeated_doubling_stays_bounded() {
    // Analysis explores the unknown branch. Node leaves the absent global false,
    // so the generated bundle never executes its exponential runtime path.
    let (source, branch) = repeated_spread_source(24);
    let fixture = run_fixture(FixtureOpts::new(&source, modules(Some(&branch))));
    assert_preserved_entry_output(&fixture, "1 safe\n");
}

#[test]
fn source_spread_overflow_stays_runtime_correct() {
    // Large source literals stay proportional to the input. The spread proof
    // refuses before cloning their slots and the emitted bundle still runs.
    const SOURCE_SLOTS: usize = 4097;
    let items = (0..SOURCE_SLOTS)
        .map(|index| index.to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let source = format!(
        "const expanded = [...[{items}]];\nconsole.log(expanded.length, expanded[4096]);\n"
    );
    let fixture =
        run_fixture(FixtureOpts::new(&source, Vec::new()).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "4097 4096\n");
}

#[test]
fn combined_target_overflow_with_small_spreads_stays_runtime_correct() {
    // Every source has one slot; only the projected destination length crosses
    // the policy cap. The emitted bundle still performs all runtime spreads.
    const SMALL_SPREADS: usize = 4097;
    let operands = vec!["...seed"; SMALL_SPREADS].join(", ");
    let source = format!(
        "const seed = [7];\nconst expanded = [{operands}];\n\
         console.log(expanded.length, expanded[4096]);\n"
    );
    let fixture =
        run_fixture(FixtureOpts::new(&source, Vec::new()).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "4097 7\n");
}

#[test]
fn oversized_spread_fallback_retains_callback_tdz_capability() {
    // Analysis explores the unknown branch and must retain the callback when
    // it loses exact slots. Node keeps the branch false and observes the real
    // TDZ ReferenceError from the callback in the original seed array.
    const CALLBACK_INVOKE: &str =
        "try { expanded[0](); } catch (error) { console.log(error.name); }";
    let (source, branch) = callback_spread_source(24);
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "app/arrays",
            &binding_members(["seed", "expanded", "later"]),
            &[&branch, CALLBACK_INVOKE],
        )],
    ));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");

    // The retained callback reads `later` at initialization time. Splitting
    // that binding must remain rejected as an eager-use atomic conflict.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "app/arrays",
                    &binding_members(["seed", "expanded"]),
                    &[&branch, CALLBACK_INVOKE],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["app/arrays", "values/later"],
        &["eager_use"],
    );
}
