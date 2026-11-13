//! Diagnostics stay on stderr at each executable boundary, including replay
//! and commands whose stdout is a machine-readable document.
use debundle_e2e_support::saved_analysis::{assert_ok, command, command_with, path, staged};
use debundle_e2e_support::{
    FixtureOpts, Member, MemberPurity, logical_module, projector_path, write_validate_fixture_spec,
};

fn hinted() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "function leaf() { return 7; }\nconsole.log(leaf());\nexport { leaf };\n",
        vec![logical_module(
            "functions",
            &[Member::new("leaf").with_purity(MemberPurity::Pure)],
        )],
    )
    .with_admission_overrides(&["a1_eval"])
}

fn assert_hint_warnings(stderr: &str) {
    assert_eq!(
        stderr
            .matches("`purity: pure` hint on binding `leaf`")
            .count(),
        1,
        "{stderr}"
    );
    assert_eq!(
        stderr
            .matches("admission override `a1_eval` is redundant")
            .count(),
        1,
        "{stderr}"
    );
    assert!(stderr.contains("chunk static/app:"), "{stderr}");
}

#[test]
fn standalone_analyzer_cli_and_replay_preserve_warnings_once() {
    let (saved, _fixture) = staged(hinted());
    let projection = command_with(
        &projector_path(),
        &[
            "--spec",
            path(&saved.spec),
            "--out",
            path(&saved.projection),
        ],
    );
    assert_ok(&projection, "standalone projector");
    // Projection resolves source selectors; it does not compute hint facts.
    assert!(
        !projection.stderr.contains("`purity: pure`"),
        "{}",
        projection.stderr
    );
    let standalone = saved.analyze();
    assert_ok(&standalone, "standalone analyzer");
    assert_hint_warnings(&standalone.stderr);
    assert!(standalone.stdout.is_empty(), "{}", standalone.stdout);
    let cli = command(&[
        "analyze",
        "--projection",
        path(&saved.projection),
        "--tree-source-root",
        path(saved.root()),
        "--out",
        path(&saved.artifact),
    ]);
    assert_ok(&cli, "CLI analyzer");
    assert_hint_warnings(&cli.stderr);
    assert!(cli.stdout.is_empty(), "{}", cli.stdout);
    for run in [
        saved.run(&[]),
        saved.run(&["--analysis", path(&saved.artifact)]),
    ] {
        assert_ok(&run.result, "direct or saved-analysis run");
        assert_hint_warnings(&run.result.stderr);
    }
}

#[test]
fn spec_validate_json_keeps_diagnostics_off_stdout() {
    let fixture = write_validate_fixture_spec(hinted());
    let result = command(&[
        "spec",
        "validate",
        "--spec",
        path(&fixture.spec_path),
        "--report-only",
        "--format",
        "json",
    ]);
    assert_ok(&result, "spec validate");
    assert_hint_warnings(&result.stderr);
    let report: serde_json::Value =
        serde_json::from_str(&result.stdout).expect("one JSON document");
    assert!(report.is_object());
}

#[test]
fn warnings_survive_a_later_analysis_rejection() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "function leaf() { return 7; }\nawait Promise.resolve();\nexport { leaf };\n",
        vec![logical_module(
            "functions",
            &[Member::new("leaf").with_purity(MemberPurity::Pure)],
        )],
    ));
    let result = command(&["run", "--spec", path(&fixture.spec_path)]);
    assert!(!result.status.success());
    assert_eq!(
        result
            .stderr
            .matches("`purity: pure` hint on binding `leaf`")
            .count(),
        1,
        "{}",
        result.stderr
    );
    assert!(
        result.stderr.contains("top-level `await`"),
        "{}",
        result.stderr
    );
}

#[test]
fn project_analysis_and_run_render_selector_warnings_once() {
    let (saved, _fixture) = staged(FixtureOpts::new(
        "function first() { return 'first'; }\nfunction second() { return 'other'; }\nconsole.log(first(), second());\n",
        vec![
            logical_module(
                "elimination/either",
                &[Member::source_alpha(
                    "Either",
                    "function f() { return EXPR; }",
                )],
            ),
            logical_module(
                "elimination/other",
                &[Member::source_alpha(
                    "Other",
                    "function g() { return 'other'; }",
                )],
            ),
        ],
    ));
    let project = saved.project(&saved.projection);
    let standalone = command_with(
        &projector_path(),
        &[
            "--spec",
            path(&saved.spec),
            "--out",
            path(&saved.projection),
        ],
    );
    for result in [project, standalone, saved.run(&[]).result] {
        assert_ok(&result, "selector projection or run");
        assert_eq!(
            result.stderr.matches("[warning: resolved]").count(),
            1,
            "{}",
            result.stderr
        );
        assert!(
            result.stderr.contains("elimination/either"),
            "{}",
            result.stderr
        );
        assert!(
            result.stderr.contains("resolved by elimination"),
            "{}",
            result.stderr
        );
    }
}

#[test]
fn standalone_analysis_and_run_preserve_dangling_contract_warnings() {
    let contracts = [
        debundle_e2e_support::ChunkExportPurityBuilder::new("static/provider")
            .with_fluent_exports(&["gone"])
            .with_pure_exports(&["missing"])
            .build(),
    ];
    let (saved, _fixture) = staged(
        FixtureOpts::new(
            "import { value } from './provider.js';\nconst result = value;\nconsole.log(result);\n",
            vec![logical_module("result", &[Member::new("result")])],
        )
        .with_extra_chunks(&[("static/provider", "export const value = 7;\n")])
        .with_chunk_export_purity(&contracts),
    );
    for result in [saved.analyze(), saved.run(&[]).result] {
        assert_ok(&result, "analyzer or run");
        for warning in [
            "dangling fluent_exports assertion static/provider:gone",
            "dangling pure_exports assertion static/provider:missing",
        ] {
            assert_eq!(
                result.stderr.matches(warning).count(),
                1,
                "{}",
                result.stderr
            );
        }
    }
}

#[test]
fn progress_environment_controls_every_phase_without_changing_json() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "import { leaf } from './provider.js'; const result = leaf(); console.log(result); export { result };",
        vec![logical_module("app", &[Member::new("result")])],
    ).with_extra_chunks(&[("static/provider", "export function leaf() { return 7; }")]));
    for value in [
        None,
        Some(""),
        Some("0"),
        Some("FALSE"),
        Some("off"),
        Some("NO"),
        Some("1"),
    ] {
        let mut command = std::process::Command::new(debundle_e2e_support::debundler_path());
        command
            .args([
                "spec",
                "validate",
                "--spec",
                path(&fixture.spec_path),
                "--report-only",
                "--format",
                "json",
            ])
            .env_remove("DUCKTAPE_DEBUNDLE_PROGRESS");
        if let Some(value) = value {
            command.env("DUCKTAPE_DEBUNDLE_PROGRESS", value);
        }
        let result = command.output().unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let report: serde_json::Value =
            serde_json::from_slice(&result.stdout).expect("one JSON document");
        assert!(report.is_object());
        let stderr = String::from_utf8(result.stderr).unwrap();
        let enabled = value == Some("1");
        for phase in [
            "prepare_cross_module_purity",
            "compute_chunk_analysis",
            "materialize_logical_chunk",
        ] {
            assert_eq!(
                stderr.contains(&format!("phase={phase} state=start")),
                enabled,
                "value={value:?}: {stderr}"
            );
        }
        if enabled {
            for phase in ["prepare_cross_module_purity", "compute_chunk_analysis"] {
                assert!(
                    stderr.contains(&format!("phase={phase} state=end")),
                    "{stderr}"
                );
            }
        } else {
            assert!(!stderr.contains("[debundle progress]"), "{stderr}");
        }
    }
}
