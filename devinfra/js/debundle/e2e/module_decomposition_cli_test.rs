//! Read-only module decomposition from gated same-module seed fragments.

use debundle_e2e_support::GraphFixture;
use serde_json::Value;
use std::fs;

#[test]
fn proposes_a_realizable_refinement_without_changing_the_spec() {
    let fixture = GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconsole.log(last);\n",
        &[
            (
                "bad.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}]\n",
            ),
            (
                "middle.yaml",
                "members: [{selector: {binding: {name: middle}}}]\n",
            ),
        ],
    );
    let unclaimed = fixture.modules.join("bad/part-1.yaml");
    fs::create_dir_all(unclaimed.parent().unwrap()).unwrap();
    fs::write(&unclaimed, "members: []\n").unwrap();
    let before = ["bad.yaml", "middle.yaml", "bad/part-1.yaml"]
        .into_iter()
        .map(|path| (path, std::fs::read(fixture.modules.join(path)).unwrap()))
        .collect::<Vec<_>>();

    let output = fixture.command(&[
        "modules",
        "decompose",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["coverage_complete"], true, "{report}");

    let bad = report["modules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["module_id"] == "bad")
        .expect("the invalid module has a suggested refinement");
    let fragments = bad["fragments"].as_array().unwrap();
    assert_eq!(fragments.len(), 2, "{report}");
    let suggested_paths = fragments
        .iter()
        .map(|fragment| fragment["suggested_module_path"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(suggested_paths, vec!["bad/part-2", "bad/part-3"]);
    let bindings = fragments
        .iter()
        .flat_map(|fragment| fragment["binding_ids"].as_array().unwrap())
        .map(Value::as_str)
        .collect::<Vec<_>>();
    assert_eq!(bindings, vec![Some("first"), Some("last")]);
    assert_eq!(
        before,
        ["bad.yaml", "middle.yaml", "bad/part-1.yaml"]
            .into_iter()
            .map(|path| (path, std::fs::read(fixture.modules.join(path)).unwrap()))
            .collect::<Vec<_>>(),
        "decomposition is a read-only review report"
    );

    fs::remove_file(fixture.modules.join("bad.yaml")).unwrap();
    fs::remove_file(unclaimed).unwrap();
    for fragment in fragments {
        let module_path = fragment["suggested_module_path"].as_str().unwrap();
        let members = fragment["binding_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|binding| {
                format!(
                    "{{selector: {{binding: {{name: {}}}}}}}",
                    serde_json::to_string(binding.as_str().unwrap()).unwrap()
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        let path = fixture.modules.join(format!("{module_path}.yaml"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("members: [{members}]\n")).unwrap();
    }
    fixture.assert_runs("3\n");
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "3\n", "");
}

#[test]
fn reports_unfixed_cross_module_cycles_instead_of_calling_them_safe() {
    let fixture = GraphFixture::rejected(
        "const alpha = beta;\nconst beta = alpha;\nconsole.log(alpha, beta);\n",
        &[
            (
                "a.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
        ],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["candidate_is_realizable"], false, "{report}");
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert!(!report["cycles"].as_array().unwrap().is_empty(), "{report}");
}
