//! Module trees of a tree-authored spec: several trees may scope to one chunk,
//! and every chunk's selectors resolve as one program.

use debundle_e2e_support::{
    TreeFixture, TreeRun, assert_module_source, read_chunk_selector_outcomes, run_tree_fixture,
};
use serde_json::{Value, json};

const MAIN: &str = "console.log('main');\n";
const ROUTING: &str = r#"function pickProvider(name) {
  return "provider:" + name;
}
function listModels() {
  return ["small", "large"];
}
console.log(pickProvider("a"), listModels().length);
"#;

fn assert_succeeded(run: &TreeRun) {
    assert!(
        run.result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        run.result.status.code(),
        run.result.stdout,
        run.result.stderr,
    );
}

fn assert_failed(run: &TreeRun) {
    assert!(
        !run.result.status.success(),
        "debundler succeeded\nstdout:\n{}\nstderr:\n{}",
        run.result.stdout,
        run.result.stderr,
    );
}

/// Two trees whose roots name the same chunk file, each claiming its own
/// declaration of it.
#[test]
fn two_trees_share_one_chunk() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", MAIN), ("routing", ROUTING)],
            module_roots: &[("cli/routing", "routing"), ("print/routing", "routing")],
            modules: &[
                (
                    "cli/routing/provider.yaml",
                    r#"source_matches:
  - match: |
      function f(name) {
        return "provider:" + name;
      }
    bindings:
      - local: f
        name: pickProvider
"#,
                ),
                (
                    "print/routing/models.yaml",
                    r#"source_matches:
  - match: |
      function f() {
        return ["small", "large"];
      }
    bindings:
      - local: f
        name: listModels
"#,
                ),
            ],
        },
        &[],
    );
    assert_succeeded(&run);
    assert_module_source(
        &run.out_root,
        "app/routing/provider.js",
        &["function pickProvider", "export"],
        &["listModels"],
    );
    assert_module_source(
        &run.out_root,
        "app/routing/models.js",
        &["function listModels", "export"],
        &["pickProvider"],
    );
}

/// Two trees of one chunk whose selectors can only take the same place: one
/// program, so `all_different` holds across the trees.
#[test]
fn trees_sharing_a_chunk_conflict_on_one_place() {
    let selector = r#"source_matches:
  - match: |
      function f(name) {
        return "provider:" + name;
      }
    bindings:
      - local: f
        name: pickProvider
"#;
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", MAIN), ("routing", ROUTING)],
            module_roots: &[("cli/routing", "routing"), ("print/routing", "routing")],
            modules: &[
                ("cli/routing/provider.yaml", selector),
                ("print/routing/provider_copy.yaml", selector),
            ],
        },
        &[],
    );
    assert_failed(&run);
    let outcomes = read_chunk_selector_outcomes(&run.report_root, "routing");
    let conflicts = outcomes
        .iter()
        .map(|record| {
            (
                record["placement"]["logical_module"].clone(),
                record["outcome"].clone(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        conflicts,
        [
            (
                json!("provider"),
                json!({"kind": "conflict", "with": [
                    {"logical_module": "provider_copy", "entity": {"export": "pickProvider"}},
                ]}),
            ),
            (
                json!("provider_copy"),
                json!({"kind": "conflict", "with": [
                    {"logical_module": "provider", "entity": {"export": "pickProvider"}},
                ]}),
            ),
        ],
        "{outcomes:#?}"
    );
}

/// Per chunk: a broad selector unique only by elimination beside a specific
/// one, and in `print` a selector that matches nothing.
fn multi_chunk_fixture() -> Vec<(String, String)> {
    ["cli", "print"]
        .into_iter()
        .flat_map(|chunk| {
            let mut modules = vec![(
                format!("chunks/{chunk}/routes.yaml"),
                format!(
                    r#"source_matches:
  - match: |
      function selected() {{
        return ANYTHING;
      }}
    bindings:
      - local: selected
        name: Broad{chunk}
  - match: |
      function selected() {{
        return "specific-{chunk}";
      }}
    bindings:
      - local: selected
        name: Specific{chunk}
"#
                ),
            )];
            if chunk == "print" {
                modules.push((
                    "chunks/print/missing.yaml".to_string(),
                    r#"source_matches:
  - match: |
      function selected() {
        return "nowhere";
      }
    bindings:
      - local: selected
        name: Missing
"#
                    .to_string(),
                ));
            }
            modules
        })
        .collect()
}

fn run_multi_chunk(extra_args: &[&str]) -> TreeRun {
    let modules = multi_chunk_fixture();
    let modules = modules
        .iter()
        .map(|(path, body)| (path.as_str(), body.as_str()))
        .collect::<Vec<_>>();
    run_tree_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "cli",
                    "function cliBroad() { return 'common-cli'; }\nfunction cliSpecific() { return 'specific-cli'; }\nconsole.log(cliBroad(), cliSpecific());\n",
                ),
                (
                    "print",
                    "function printBroad() { return 'common-print'; }\nfunction printSpecific() { return 'specific-print'; }\nconsole.log(printBroad(), printSpecific());\n",
                ),
            ],
            module_roots: &[("chunks/cli", "cli"), ("chunks/print", "print")],
            modules: &modules,
        },
        extra_args,
    )
}

fn outcome_kinds(outcomes: &[Value]) -> Vec<(String, Value)> {
    outcomes
        .iter()
        .map(|record| {
            (
                record["placement"]["entity"]["export"]
                    .as_str()
                    .unwrap()
                    .to_string(),
                record["outcome"].clone(),
            )
        })
        .collect()
}

/// `Broad<chunk>` resolved to the chunk's first declaration by elimination.
fn eliminated(chunk: &str) -> (String, Value) {
    (
        format!("Broad{chunk}"),
        json!({"kind": "resolved", "owner": 0, "binding": format!("{chunk}Broad"),
        "resolved_by": {"by": "elimination", "claimers": [
            {"logical_module": "routes", "entity": {"export": format!("Specific{chunk}")}},
        ]}}),
    )
}

/// Every chunk reports its own outcomes, and an error in one chunk does not
/// hide another chunk's.
#[test]
fn multi_chunk_outcomes_are_per_chunk() {
    let run = run_multi_chunk(&[]);
    assert_failed(&run);
    assert_eq!(
        outcome_kinds(&read_chunk_selector_outcomes(&run.report_root, "cli")),
        [eliminated("cli")],
    );
    assert_eq!(
        outcome_kinds(&read_chunk_selector_outcomes(&run.report_root, "print")),
        [
            ("Missing".to_string(), json!({"kind": "no_match"})),
            eliminated("print"),
        ],
    );
    assert!(
        run.result.stderr.contains("[no_match] print::missing"),
        "{}",
        run.result.stderr
    );
}

/// `--fail-fast` stops at the first error outcome: here the only one.
#[test]
fn multi_chunk_fail_fast_stops_at_the_error() {
    let run = run_multi_chunk(&["--fail-fast"]);
    assert_failed(&run);
    assert!(
        run.result.stderr.contains("[no_match] print::missing"),
        "{}",
        run.result.stderr
    );
    assert!(
        !run.result.stderr.contains("Selector outcome report"),
        "{}",
        run.result.stderr
    );
}
