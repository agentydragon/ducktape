//! Exact source-match consolidation is a checked authoring edit, not a source rewrite.

use debundle_e2e_support::{
    GraphFixture, TreeFixture, assert_module_exports, assert_module_source, run_tree_fixture,
};
use serde_json::json;
use serde_yaml::Value;
use std::fs;

const SOURCE: &str = "const a = 1, b = 2, c = 3; console.log(a + b + c);";
const PATTERN: &str = "const left = 1, middle = 2, right = 3;";

fn claim(local: &str, name: &str, note: &str) -> String {
    format!(
        "source_matches:\n  - match: '{PATTERN}'\n    bindings: [{{local: {local}, name: {name}}}]\n    note: {note}\nannotations:\n  {name}: {{comment: '{name} comment', note: '{name} note'}}\n"
    )
}

fn assert_grouped(doc: &Value) {
    let claims = doc["source_matches"].as_sequence().unwrap();
    assert_eq!(claims.len(), 2);
    assert_eq!(claims[0]["match"], PATTERN);
    assert_eq!(claims[0]["note"], "shared selector note");
    let bindings = claims[0]["bindings"].as_sequence().unwrap();
    assert_eq!(bindings.len(), 2);
    assert_eq!(bindings[0]["local"], "left");
    assert_eq!(bindings[0]["name"], "Alpha");
    assert_eq!(bindings[1]["local"], "middle");
    assert_eq!(bindings[1]["name"], "Beta");
    assert_eq!(claims[1]["note"], "different selector note");
    assert_eq!(claims[1]["bindings"][0]["name"], "Gamma");
    for name in ["Alpha", "Beta", "Gamma"] {
        assert_eq!(
            doc["annotations"][name]["comment"],
            format!("{name} comment")
        );
        assert_eq!(doc["annotations"][name]["note"], format!("{name} note"));
    }
}

#[test]
fn merge_and_refine_coalesce_exact_templates_without_losing_aliases_or_metadata() {
    for verb in ["merge", "refine"] {
        let target = format!(
            "comment: Owner comment\nnote: Owner note\n{}",
            claim("left", "Alpha", "shared selector note")
        );
        let beta = claim("middle", "Beta", "shared selector note");
        let gamma = claim("right", "Gamma", "different selector note");
        let fixture = GraphFixture::new(
            SOURCE,
            &[
                ("owner.yaml", &target),
                ("beta.yaml", &beta),
                ("gamma.yaml", &gamma),
            ],
        );
        let batch = fixture.modules.join("reviewed.json");
        let graph = fixture.dependency_graph();
        let assignments: Vec<_> = graph
            .nodes
            .iter()
            .filter(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| matches!(binding.binding.as_ref(), "b" | "c"))
            })
            .map(|node| json!({"unit_id": node.id, "module": "owner"}))
            .collect();
        fs::write(&batch, serde_json::to_vec(&assignments).unwrap()).unwrap();
        let args = if verb == "merge" {
            vec!["modules", "merge", "--target", "owner", "beta", "gamma"]
        } else {
            vec!["modules", "refine", "--batch", batch.to_str().unwrap()]
        };
        let before = fs::read(fixture.modules.join("owner.yaml")).unwrap();
        let mut preview = args.clone();
        preview.push("--dry-run");
        fixture.assert_success(&preview);
        assert_eq!(
            fs::read(fixture.modules.join("owner.yaml")).unwrap(),
            before
        );
        fixture.assert_success(&args);
        let doc: Value =
            serde_yaml::from_slice(&fs::read(fixture.modules.join("owner.yaml")).unwrap()).unwrap();
        assert_grouped(&doc);
        assert_eq!(doc["comment"], "Owner comment");
        assert!(doc["note"].as_str().unwrap().starts_with("Owner note"));
        fixture.assert_runs("6\n");
    }
}

#[test]
fn unchanged_owner_refine_normalizes_only_requested_modules_and_preserves_emitted_bytes() {
    let alpha = claim("left", "Alpha", "shared selector note");
    let beta = claim("middle", "Beta", "shared selector note");
    let mut first: Value = serde_yaml::from_str(&alpha).unwrap();
    let second: Value = serde_yaml::from_str(&beta).unwrap();
    first["source_matches"]
        .as_sequence_mut()
        .unwrap()
        .extend(second["source_matches"].as_sequence().unwrap().clone());
    first["annotations"]
        .as_mapping_mut()
        .unwrap()
        .extend(second["annotations"].as_mapping().unwrap().clone());
    let yaml = serde_yaml::to_string(&first).unwrap();
    let fixture = GraphFixture::new(SOURCE, &[("owner.yaml", &yaml)]);
    let batch = fixture.modules.join("reviewed.json");
    let unit = fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding.as_ref() == "a")
        })
        .unwrap()
        .id;
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{"unit_id": unit, "module": "owner"}])).unwrap(),
    )
    .unwrap();
    let args = ["modules", "refine", "--batch", batch.to_str().unwrap()];
    let mut preview = args.to_vec();
    preview.push("--dry-run");
    assert_eq!(fixture.json(&preview)["action"], "dry-run");
    assert_eq!(
        fs::read_to_string(fixture.modules.join("owner.yaml")).unwrap(),
        yaml
    );
    let result = fixture.json(&args);
    assert_eq!(result["action"], "applied");
    assert_eq!(result["source_units_reassigned"], 0);
    let normalized = fs::read_to_string(fixture.modules.join("owner.yaml")).unwrap();
    let doc: Value = serde_yaml::from_str(&normalized).unwrap();
    assert_eq!(doc["source_matches"].as_sequence().unwrap().len(), 1);
    let original = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[("main", "main")],
            modules: &[("main/owner.yaml", &yaml)],
        },
        &[],
    );
    let grouped = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[("main", "main")],
            modules: &[("main/owner.yaml", &normalized)],
        },
        &[],
    );
    assert!(
        original.result.status.success(),
        "{}",
        original.result.stderr
    );
    assert!(grouped.result.status.success(), "{}", grouped.result.stderr);
    for path in ["app/main/entry.js", "app/main/owner.js"] {
        assert_eq!(
            fs::read(original.out_root.join(path)).unwrap(),
            fs::read(grouped.out_root.join(path)).unwrap(),
            "{path}"
        );
    }
    assert_module_exports(
        &grouped.out_root.join("app"),
        "main/owner.js",
        &["Alpha", "Beta"],
        &[],
    );
    assert_module_source(
        &grouped.out_root.join("app"),
        "main/owner.js",
        &["Alpha comment", "Beta comment"],
        &["Alpha note", "Beta note"],
    );
    fixture.assert_runs("6\n");
    // The same reviewed assignment becomes a true no-op after normalization.
    assert_eq!(fixture.json(&args)["action"], "unchanged");
}
