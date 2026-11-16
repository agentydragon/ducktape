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
