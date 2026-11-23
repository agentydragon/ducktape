//! Opt-in CLI metrics use one invocation session and leave emitted behavior intact.

use debundle_e2e_support::run_rejection_fixture_with_args;
use debundle_e2e_support::saved_analysis::{path, rejected_split};
use debundle_e2e_support::{
    FixtureOpts, Member, analyzer_path, assert_entry_output, logical_module, process,
    projector_path, run_fixture, run_fixture_with_args, write_validate_fixture_spec,
};
use serde_json::Value;
use std::fs;
use std::process::{Command, Output};

fn fixture_opts() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "function answer() { return 42; }\nconsole.log(answer());\n",
        vec![logical_module("values", &[Member::new("answer")])],
    )
}

fn metric_names(rows: &[Value]) -> Vec<String> {
    rows.iter()
        .flat_map(|row| row["resourceMetrics"].as_array().into_iter().flatten())
        .flat_map(|resource| resource["scopeMetrics"].as_array().into_iter().flatten())
        .flat_map(|scope| scope["metrics"].as_array().into_iter().flatten())
        .filter_map(|metric| metric["name"].as_str().map(str::to_owned))
        .collect()
}

fn read_metrics(path: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(path)
        .expect("metrics file written")
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).expect("metrics line is JSON"))
        .collect()
}

fn run_standalone_with_metrics(
    binary: &std::path::Path,
    args: &[&str],
    spec_path: &std::path::Path,
    service_name: &str,
    resource_attributes: &str,
) -> Output {
    let mut command = Command::new(binary);
    command
        .args(args)
        .env("OTEL_SERVICE_NAME", service_name)
        .env("OTEL_RESOURCE_ATTRIBUTES", resource_attributes)
        .env("OTEL_METRIC_EXPORT_INTERVAL", "1000");
    debundle_e2e_support::debundle_fixture_output(&mut command, spec_path)
        .expect("spawn standalone analysis tool")
}

fn resource_attribute<'a>(rows: &'a [Value], key: &str) -> Option<&'a str> {
    rows.iter()
        .flat_map(|row| row["resourceMetrics"].as_array().into_iter().flatten())
        .flat_map(|resource| {
            resource["resource"]["attributes"]
                .as_array()
                .into_iter()
                .flatten()
        })
        .find(|attribute| attribute["key"] == key)
        .and_then(|attribute| attribute["value"]["stringValue"].as_str())
}

#[test]
fn standalone_project_analysis_and_analyze_export_metrics_with_standard_resource_env() {
    let fixture = write_validate_fixture_spec(fixture_opts());
    let root = fixture.spec_path.parent().expect("fixture root");
    let projection = root.join("metrics.projection");
    let analysis = root.join("metrics.analysis");
    let metrics_dir = tempfile::tempdir().expect("metrics output directory");
    let projection_metrics = metrics_dir.path().join("project-analysis.jsonl");
    let analysis_metrics = metrics_dir.path().join("analyze.jsonl");
    let projection_metrics_arg = projection_metrics.to_string_lossy().into_owned();
    let analysis_metrics_arg = analysis_metrics.to_string_lossy().into_owned();
    let service_name = "debundle-standalone-metrics-test";
    let resource_attributes = "deployment.environment.name=metrics-test,team=debundle";

    let projection_result = run_standalone_with_metrics(
        &projector_path(),
        &[
            "--spec",
            path(&fixture.spec_path),
            "--out",
            path(&projection),
            "--metrics-file",
            &projection_metrics_arg,
        ],
        &fixture.spec_path,
        service_name,
        resource_attributes,
    );
    process::assert_success(&projection_result);
    assert!(
        projection.is_file(),
        "project-analysis wrote its projection"
    );
    assert!(
        projection_metrics.is_file(),
        "project-analysis created the requested metrics stream"
    );
    let projection_rows = read_metrics(&projection_metrics);
    assert!(
        !projection_rows.is_empty(),
        "project-analysis exported metrics"
    );
    assert!(
        metric_names(&projection_rows)
            .iter()
            .any(|name| name == "debundle.frontend.chunks_prepared"),
        "project-analysis omitted chunk metrics: {:?}",
        metric_names(&projection_rows)
    );
    assert_eq!(
        resource_attribute(&projection_rows, "service.name"),
        Some(service_name)
    );
    assert_eq!(
        resource_attribute(&projection_rows, "deployment.environment.name"),
        Some("metrics-test")
    );
    assert_eq!(
        resource_attribute(&projection_rows, "team"),
        Some("debundle")
    );

    let analysis_result = run_standalone_with_metrics(
        &analyzer_path(),
        &[
            "--projection",
            path(&projection),
            "--tree-source-root",
            path(root),
            "--out",
            path(&analysis),
            "--metrics-file",
            &analysis_metrics_arg,
        ],
        &fixture.spec_path,
        service_name,
        resource_attributes,
    );
    process::assert_success(&analysis_result);
    assert!(analysis.is_file(), "analyze wrote its artifact");
    let analysis_rows = read_metrics(&analysis_metrics);
    assert!(!analysis_rows.is_empty(), "analyze exported metric records");
    assert!(
        metric_names(&analysis_rows)
            .iter()
            .any(|name| name == "debundle.frontend.statement_views"),
        "analyze omitted statement-view metrics: {:?}",
        metric_names(&analysis_rows)
    );
    assert_eq!(
        resource_attribute(&analysis_rows, "service.name"),
        Some(service_name)
    );
    assert_eq!(
        resource_attribute(&analysis_rows, "deployment.environment.name"),
        Some("metrics-test")
    );
    assert_eq!(resource_attribute(&analysis_rows, "team"), Some("debundle"));
}

#[test]
fn run_exports_metrics_jsonl_and_preserves_emitted_output() {
    let metrics_dir = tempfile::tempdir().expect("metrics output directory");
    let metrics_file = metrics_dir.path().join("metrics.jsonl");
    let metrics_arg = metrics_file.to_string_lossy().into_owned();
    let fixture = run_fixture_with_args(fixture_opts(), &["--metrics-file", &metrics_arg]);

    assert_entry_output(&fixture, "42\n");
    let rows = read_metrics(&metrics_file);
    assert!(
        !rows.is_empty(),
        "metrics export contained no measurement rows"
    );
    let names = metric_names(&rows);
    for expected in [
        "debundle.frontend.statement_views",
        "debundle.inference.operations",
    ] {
        assert!(
            names.iter().any(|name| name == expected),
            "missing {expected}: {names:?}"
        );
    }

    let disabled_fixture = run_fixture(fixture_opts());
    assert_entry_output(&disabled_fixture, "42\n");
}

#[test]
fn metrics_do_not_turn_a_rejected_run_into_success() {
    let metrics_dir = tempfile::tempdir().expect("metrics output directory");
    let metrics_file = metrics_dir.path().join("rejected.jsonl");
    let metrics_arg = metrics_file.to_string_lossy().into_owned();
    let rejected =
        run_rejection_fixture_with_args(rejected_split(), &["--metrics-file", &metrics_arg], 1);

    assert!(rejected.report_path("cycles.json").is_file());
    assert!(!rejected.app_root().exists());
    let rows = read_metrics(&metrics_file);
    assert!(
        !rows.is_empty(),
        "the rejected operation still flushes its metrics"
    );
}

fn counter_with_attributes(rows: &[Value], name: &str, attributes: &[(&str, &str)]) -> u64 {
    rows.last().expect("final metrics export")["resourceMetrics"]
        .as_array()
        .expect("resources")
        .iter()
        .flat_map(|resource| resource["scopeMetrics"].as_array().into_iter().flatten())
        .flat_map(|scope| scope["metrics"].as_array().into_iter().flatten())
        .filter(|metric| metric["name"] == name)
        .flat_map(|metric| metric["sum"]["dataPoints"].as_array().into_iter().flatten())
        .filter(|point| {
            attributes.iter().all(|(key, value)| {
                point["attributes"]
                    .as_array()
                    .expect("attributes")
                    .iter()
                    .any(|attribute| {
                        attribute["key"] == *key && attribute["value"]["stringValue"] == *value
                    })
            })
        })
        .map(|point| {
            point["asInt"]
                .as_str()
                .expect("integer counter")
                .parse::<u64>()
                .expect("counter value")
        })
        .sum()
}

#[test]
fn precision_loss_counters_separate_recursive_and_unknown_callee_fallbacks() {
    let metrics_dir = tempfile::tempdir().expect("metrics output directory");
    let metrics_file = metrics_dir.path().join("causes.jsonl");
    let metrics_arg = metrics_file.to_string_lossy().into_owned();
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            "function factory(count) { return count ? factory(count - 1) : 3; }\nconst result = factory(2);\nconsole.log(result + 4);\n",
            vec![],
        ).with_import_order(&["residual"]),
        &["--metrics-file", &metrics_arg],
    );
    assert_entry_output(&fixture, "7\n");
    let rows = read_metrics(&metrics_file);
    for attributes in [
        vec![("reason", "recursive_invocation"), ("reach", "bounded")],
        vec![
            ("reason", "unknown_call_target"),
            ("reach", "global"),
            ("global_cause", "unknown_callee"),
        ],
    ] {
        assert!(
            counter_with_attributes(&rows, "debundle.execution.precision_losses", &attributes) > 0,
            "missing precision-loss cause {attributes:?}: {rows:#?}"
        );
    }
    assert!(
        counter_with_attributes(
            &rows,
            "debundle.source_flow.unknowns",
            &[("cause", "undeclared_binding")]
        ) > 0
    );
}

#[test]
fn counters_distinguish_missing_dynamic_properties_and_unsupported_coercion() {
    let metrics_dir = tempfile::tempdir().expect("metrics output directory");
    let metrics_file = metrics_dir.path().join("property-causes.jsonl");
    let metrics_arg = metrics_file.to_string_lossy().into_owned();
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            "const object = { valueOf() { return 3; } };\nconst missing = object.absent;\nconst key = 'valueOf';\nconst dynamic = object[key];\nconst result = +object;\nconsole.log(result);\n",
            vec![],
        ).with_import_order(&["residual"]),
        &["--metrics-file", &metrics_arg],
    );
    assert_entry_output(&fixture, "3\n");
    let rows = read_metrics(&metrics_file);
    for cause in [
        "missing_property",
        "dynamic_property",
        "unsupported_expression",
    ] {
        assert!(
            counter_with_attributes(&rows, "debundle.source_flow.unknowns", &[("cause", cause)])
                > 0,
            "missing source unknown cause {cause}: {rows:#?}"
        );
    }
    assert!(
        counter_with_attributes(
            &rows,
            "debundle.execution.precision_losses",
            &[
                ("reason", "unsupported_operation"),
                ("operation", "implicit_coercion"),
                ("reach", "global"),
                ("global_cause", "unmodelled_operation"),
            ]
        ) > 0
    );
}
