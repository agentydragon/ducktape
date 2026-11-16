//! Search completeness against independent raw-assignment enumeration on real
//! source/spec inputs, with shrinking of source shape and permitted move scope.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use proptest::prelude::*;
use proptest::test_runner::FileFailurePersistence;
use std::collections::BTreeMap;
use std::fs;

fn config() -> ProptestConfig {
    let mut config = ProptestConfig::default();
    if std::env::var_os("PROPTEST_CASES").is_none() {
        config.cases = 32;
    }
    if let Ok(directory) = std::env::var("TEST_UNDECLARED_OUTPUTS_DIR") {
        // Proptest's path API requires a static string. Keep one path for this
        // test process; Bazel uploads the shrunk seed beside retained fixtures.
        let path =
            Box::leak(format!("{directory}/complete-search.proptest-regressions").into_boxed_str());
        config.failure_persistence = Some(Box::new(FileFailurePersistence::Direct(path)));
    }
    config
}

proptest! {
    #![proptest_config(config())]

    #[test]
    fn complete_finds_a_peel_iff_an_independently_materialized_assignment_exists(
        seed in 0u8..5, step in 1u8..4, lazy_second in any::<bool>(), lazy_third in any::<bool>(),
        third_reads_second in any::<bool>(), allowed_mask in 1u8..8, target_mask in 1u8..8,
    ) {
        let second = if lazy_second { "function second() { return first + STEP; }" } else { "const second = first + STEP;" }.replace("STEP", &step.to_string());
        let second_value = if lazy_second { "second()" } else { "second" };
        let operand = if third_reads_second { second_value } else { "first" };
        // Distinct literals keep each declaration structurally identifiable,
        // so this property also exercises successful selector realization.
        // Ambiguous binding/anonymous synthesis has separate no-write cases.
        let third = if lazy_third { format!("function third() {{ return {operand} + 100; }}") } else { format!("const third = {operand} + 100;") };
        let third_value = if lazy_third { "third()" } else { "third" };
        let source = format!("const anchor = 99;\nconst first = {seed};\n{second}\n{third}\nconsole.log(first, {second_value}, {third_value});\n");
        let expected = format!("{} {} {}\n", seed, seed + step, if third_reads_second { seed + step + 100 } else { seed + 100 });
        let fixture = GraphFixture::new(&source, &[("middle.yaml", "members: [{selector: {binding: {name: anchor}}}]\n")]);
        let baseline = read_tree(&fixture.modules);
        let bindings = ["first", "second", "third"];
        let selected: Vec<_> = (0..3).filter(|index| allowed_mask & (1 << index) != 0).collect();
        let ids: Vec<_> = selected.iter().map(|index| fixture.source_unit_for_binding(bindings[*index])).collect();
        let required = ids.last().unwrap();
        let target_names = ["a_before", "middle", "z_after"];
        let targets: Vec<_> = (0..3).filter(|index| target_mask & (1 << index) != 0).map(|index| target_names[index]).collect();
        let mut reference_exists = false;
        // Raw statement choices, without calling atoms, closures, the checker,
        // or any code from the search implementation.
        for model in 0..(targets.len() + 1).pow(selected.len() as u32) {
            let mut cursor = model;
            let mut digits = Vec::new();
            for _ in &selected {
                digits.push(cursor % (targets.len() + 1));
                cursor /= targets.len() + 1;
            }
            if digits.last() == Some(&0) { continue; }
            let mut docs = BTreeMap::<&str, Vec<&str>>::from([("middle", vec!["anchor"])]);
            for (index, digit) in selected.iter().zip(&digits) {
                if *digit != 0 { docs.entry(targets[digit - 1]).or_default().push(bindings[*index]); }
            }
            for (module, bindings) in docs {
                let mut text = "members:\n".to_string();
                for binding in bindings { text.push_str(&format!("  - selector: {{binding: {{name: {binding}}}}}\n")); }
                fs::write(fixture.modules.join(format!("{module}.yaml")), text).unwrap();
            }
            reference_exists |= fixture.current_spec_runs(&expected);
            for path in read_tree(&fixture.modules).keys() { fs::remove_file(fixture.modules.join(path)).unwrap(); }
            for (path, bytes) in &baseline { fs::write(fixture.modules.join(path), bytes).unwrap(); }
        }
        let only_path = fixture.root().join("only.json");
        fs::write(&only_path, serde_json::to_vec(&ids).unwrap()).unwrap();
        let mut args = vec!["modules", "propose", "--only-units", only_path.to_str().unwrap(), "--require", required, "--format", "json"];
        for target in &targets { args.extend(["--into", target]); }
        let output = fixture.command(&args);
        prop_assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        let report = parse_stdout_json(&output);
        prop_assert_eq!(&report["status"], if reference_exists { "found" } else { "exhausted_no_peel" }, "source={}, permitted={:?}, targets={:?}, report={}", source, ids, targets, report);
        prop_assert_eq!(read_tree(&fixture.modules), baseline.clone());
        if reference_exists {
            let plan = fixture.root().join("plan.json");
            fs::write(&plan, serde_json::to_vec(&report["plan"]).unwrap()).unwrap();
            fixture.assert_success(&["modules", "check", "--plan", plan.to_str().unwrap()]);
            fixture.assert_success(&["modules", "apply", "--plan", plan.to_str().unwrap(), "--dry-run"]);
            prop_assert_eq!(read_tree(&fixture.modules), baseline);
            fixture.assert_success(&["modules", "apply", "--plan", plan.to_str().unwrap()]);
            fixture.assert_runs(&expected);
        } else {
            prop_assert!(report["plan"].is_null());
        }
    }
}
