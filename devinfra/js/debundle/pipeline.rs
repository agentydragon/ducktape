use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

use artifact::{JsPipelineArtifact, compute_js_asts, load_js_chunks};
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use logical_modules::{MaterializeLogicalModulesOptions, materialize_logical_modules};
use normalize::normalize_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use vendor::{
    SwapVendorOptions, apply_vendor_annotations, rename_vendor_exports, swap_vendor_chunks,
};
use write_tree::write_js_tree;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_path: PathBuf,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParsedTransformCli {
    Help,
    Run(TransformCli),
}

#[derive(Debug, Clone, Serialize)]
pub struct TransformRunSummary {
    pub duration_ms: f64,
    pub spec_path: String,
    pub steps: Vec<TransformStepSummary>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TransformStepSummary {
    pub id: String,
    pub operation: String,
    pub duration_ms: Option<f64>,
    pub manifest_kind: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct TransformSpec {
    kind: Option<String>,
    pipeline: Option<Vec<TransformStage>>,
    operations: Option<Vec<serde_json::Value>>,
}

#[derive(Debug, Clone, Deserialize)]
struct TransformStage {
    id: Option<String>,
    operation: Option<String>,
    disabled: Option<bool>,
    implementation: Option<serde_json::Value>,
    args: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LoadJsChunksArgs {
    input_root: PathBuf,
    js_list_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NormalizeJsChunksArgs {
    jobs: Option<usize>,
    entry_file: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct EmitBrowserHarnessArgs {
    asset_summary_path: PathBuf,
    force: Option<bool>,
    out_dir: PathBuf,
    script_source: Option<String>,
    snapshot_root: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SwapVendorChunksArgs {
    output_manifest_path: Option<PathBuf>,
    output_wrapper_dir: Option<PathBuf>,
    write: Option<bool>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MaterializeLogicalModulesArgs {
    boundary_analysis_dir: Option<PathBuf>,
    chunk_ids: Vec<String>,
    file: Option<String>,
    prune_other_chunks: Option<bool>,
    force: Option<bool>,
    report_out_dir: Option<PathBuf>,
    report_summary_path: Option<PathBuf>,
    selected_owner_ids_by_chunk_path: Option<PathBuf>,
    selected_owner_ids_by_chunk: Option<serde_json::Value>,
    target_dir: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WriteJsTreeArgs {
    force: Option<bool>,
    out_dir: PathBuf,
}

#[derive(Default)]
struct TransformState {
    artifact: JsPipelineArtifact,
}

pub fn transform_cli_help() -> &'static str {
    "Usage:\n  debundle --spec <spec.jsonc> [--package-root <pkg>=<dir>]... [--packages-root <dir>]\n\nRuns the transform pipeline described by the spec. Pipeline stages\ndispatch directly to registered functions; this target does not invoke Bazel\nfrom inside the pipeline. Specs are parsed as JSON with comments.\n"
}

pub fn parse_transform_cli_args(argv: &[String]) -> Result<ParsedTransformCli> {
    let mut package_roots = HashMap::new();
    let mut packages_root = None;
    let mut spec_path = None;
    let mut index = 0usize;
    while index < argv.len() {
        match argv[index].as_str() {
            "--spec" => {
                index += 1;
                spec_path = Some(require_arg_value(argv, index, "--spec")?);
            }
            "--package-root" => {
                index += 1;
                let value = require_arg_value(argv, index, "--package-root")?;
                let (package_name, package_root) =
                    parse_package_root_arg(&value, "--package-root")?;
                package_roots.insert(package_name, package_root);
            }
            "--packages-root" => {
                index += 1;
                packages_root = Some(PathBuf::from(require_arg_value(
                    argv,
                    index,
                    "--packages-root",
                )?));
            }
            "--help" | "-h" => return Ok(ParsedTransformCli::Help),
            arg => bail!("Unknown argument: {arg}"),
        }
        index += 1;
    }
    let Some(spec_path) = spec_path else {
        bail!("--spec is required");
    };
    Ok(ParsedTransformCli::Run(TransformCli {
        spec_path: PathBuf::from(spec_path),
        package_roots,
        packages_root,
    }))
}

pub fn render_transform_summary(summary: &TransformRunSummary) -> String {
    let mut out = format!(
        "Ran {} transform steps from {} in {}\n",
        summary.steps.len(),
        summary.spec_path,
        format_duration(summary.duration_ms)
    );
    for step in &summary.steps {
        out.push_str(&format!(
            "- {}: {} ({}){}\n",
            step.id,
            step.operation,
            format_duration(step.duration_ms.unwrap_or(0.0)),
            step.manifest_kind
                .as_ref()
                .map(|kind| format!(" [{kind}]"))
                .unwrap_or_default()
        ));
    }
    out
}

pub fn run_transform_cli(cli: &TransformCli) -> Result<TransformRunSummary> {
    let spec = load_transform_spec(&cli.spec_path)?;
    validate_transform_spec(&spec)?;
    let pipeline = spec.pipeline.unwrap_or_default();
    let operations = spec.operations.unwrap_or_default();
    let started = Instant::now();
    let mut state = TransformState::default();
    let mut steps = Vec::new();

    for stage in pipeline {
        let id = stage.id.unwrap_or_default();
        let operation = stage.operation.unwrap_or_default();
        if stage.disabled.unwrap_or(false) {
            steps.push(TransformStepSummary {
                id,
                operation,
                duration_ms: Some(0.0),
                manifest_kind: None,
            });
            continue;
        }
        let stage_started = Instant::now();
        let manifest_kind =
            run_transform_stage(&mut state, &operation, stage.args, &operations, cli)?;
        steps.push(TransformStepSummary {
            id,
            operation,
            duration_ms: Some(elapsed_ms(stage_started)),
            manifest_kind,
        });
    }

    Ok(TransformRunSummary {
        duration_ms: elapsed_ms(started),
        spec_path: cli.spec_path.display().to_string(),
        steps,
    })
}

fn run_transform_stage(
    state: &mut TransformState,
    operation: &str,
    args: Option<serde_json::Value>,
    operations: &[serde_json::Value],
    cli: &TransformCli,
) -> Result<Option<String>> {
    match operation {
        "load_js_chunks" => {
            let args: LoadJsChunksArgs =
                serde_json::from_value(args.context("load_js_chunks requires args")?)?;
            let (artifact, manifest) = load_js_chunks(&args.input_root, &args.js_list_path)?;
            state.artifact = artifact;
            Ok(Some(manifest.kind.to_string()))
        }
        "compute_js_asts" => {
            let manifest = compute_js_asts(&mut state.artifact, true)?;
            Ok(Some(manifest.kind.to_string()))
        }
        "normalize_js_chunks" => {
            let args = args
                .map(serde_json::from_value::<NormalizeJsChunksArgs>)
                .transpose()?
                .unwrap_or(NormalizeJsChunksArgs {
                    jobs: None,
                    entry_file: None,
                });
            if args.entry_file.is_some() {
                bail!(
                    "normalizeJsChunks no longer accepts entryFile; normalized chunks always use entry.js"
                );
            }
            let _ = args.jobs;
            let artifact = std::mem::take(&mut state.artifact);
            let (artifact, _manifest) = normalize_js_chunks(artifact)?;
            state.artifact = artifact;
            Ok(None)
        }
        "rewrite_chunk_entry_specifiers" => {
            let manifest = rewrite_chunk_entry_specifiers(&mut state.artifact)?;
            Ok(Some(manifest.kind.to_string()))
        }
        "apply_vendor_annotations" => {
            let manifest = apply_vendor_annotations(&mut state.artifact, operations)?;
            Ok(Some(manifest.kind.to_string()))
        }
        "rename_vendor_exports" => {
            let manifest = rename_vendor_exports(&mut state.artifact, operations)?;
            Ok(Some(manifest.kind.to_string()))
        }
        "swap_vendor_chunks" => {
            let args = args
                .map(serde_json::from_value::<SwapVendorChunksArgs>)
                .transpose()?
                .unwrap_or(SwapVendorChunksArgs {
                    output_manifest_path: None,
                    output_wrapper_dir: None,
                    write: None,
                });
            let manifest = swap_vendor_chunks(
                &mut state.artifact,
                operations,
                SwapVendorOptions {
                    package_roots: &cli.package_roots,
                    packages_root: &cli.packages_root,
                    output_manifest_path: args.output_manifest_path,
                    output_wrapper_dir: args.output_wrapper_dir,
                    write: args.write.unwrap_or(true),
                },
            )?;
            Ok(Some(manifest.kind.to_string()))
        }
        "materialize_logical_modules" => {
            let args: MaterializeLogicalModulesArgs =
                serde_json::from_value(args.context("materialize_logical_modules requires args")?)?;
            let manifest = materialize_logical_modules(
                &mut state.artifact,
                operations,
                MaterializeLogicalModulesOptions {
                    boundary_analysis_dir: args.boundary_analysis_dir,
                    chunk_ids: args.chunk_ids,
                    file: args.file,
                    prune_other_chunks: args.prune_other_chunks.unwrap_or(true),
                    force: args.force.unwrap_or(false),
                    report_out_dir: args.report_out_dir,
                    report_summary_path: args.report_summary_path,
                    selected_owner_ids_by_chunk_path: args.selected_owner_ids_by_chunk_path,
                    selected_owner_ids_by_chunk: args.selected_owner_ids_by_chunk,
                    target_dir: args.target_dir.unwrap_or_else(|| "modules".to_string()),
                },
            )?;
            Ok(Some(manifest.kind.to_string()))
        }
        "write_js_tree" => {
            let args: WriteJsTreeArgs =
                serde_json::from_value(args.context("write_js_tree requires args")?)?;
            let manifest =
                write_js_tree(&state.artifact, &args.out_dir, args.force.unwrap_or(false))?;
            Ok(Some(manifest.kind.to_string()))
        }
        "emit_browser_harness" => {
            let args: EmitBrowserHarnessArgs =
                serde_json::from_value(args.context("emit_browser_harness requires args")?)?;
            emit_browser_harness(
                &state.artifact,
                &EmitBrowserHarnessOptions {
                    asset_summary_path: args.asset_summary_path,
                    force: args.force.unwrap_or(false),
                    out_dir: args.out_dir,
                    script_source: args.script_source.unwrap_or_else(|| "split".to_string()),
                    snapshot_root: args.snapshot_root,
                },
            )?;
            Ok(None)
        }
        _ => bail!("No registered stage handler for operation {operation}"),
    }
}

fn load_transform_spec(spec_path: &Path) -> Result<TransformSpec> {
    let raw = fs::read_to_string(spec_path)
        .with_context(|| format!("reading {}", spec_path.display()))?;
    let stripped = strip_jsonc_comments(&raw);
    serde_json::from_str(&stripped)
        .with_context(|| format!("Failed to parse {} as JSONC", spec_path.display()))
}

fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
    let kind = spec.kind.as_deref().unwrap_or("<missing>");
    if kind != "js.ast_transform_spec" {
        bail!("Unsupported transform spec kind: {kind}");
    }
    let pipeline = spec
        .pipeline
        .as_ref()
        .context("Transform spec must contain a pipeline array")?;
    let mut seen_stage_ids = HashSet::new();
    for stage in pipeline {
        let id = stage
            .id
            .as_deref()
            .context("Pipeline stage is missing id")?;
        let operation = stage
            .operation
            .as_deref()
            .with_context(|| format!("Pipeline stage {id} is missing operation"))?;
        if id == operation {
            bail!("Pipeline stage {id} must differ from operation {operation}");
        }
        if !seen_stage_ids.insert(id.to_string()) {
            bail!("Duplicate pipeline stage id: {id}");
        }
        if stage.implementation.is_some() {
            bail!(
                "Pipeline stage {id} uses legacy implementation wiring; stages now dispatch by operation only"
            );
        }
    }
    Ok(())
}

fn require_arg_value(argv: &[String], index: usize, flag: &str) -> Result<String> {
    argv.get(index)
        .cloned()
        .with_context(|| format!("{flag} requires a value"))
}

fn parse_package_root_arg(value: &str, flag: &str) -> Result<(String, PathBuf)> {
    let Some(separator) = value.find('=') else {
        bail!("{flag} must be in <package>=<dir> form, got {value}");
    };
    if separator == 0 || separator == value.len() - 1 {
        bail!("{flag} must be in <package>=<dir> form, got {value}");
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
}

fn elapsed_ms(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1000.0
}

fn format_duration(duration_ms: f64) -> String {
    if duration_ms >= 1000.0 {
        format!("{:.3}s", duration_ms / 1000.0)
    } else {
        format!("{duration_ms:.3}ms")
    }
}

fn strip_jsonc_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    let mut in_string = false;
    let mut escaping = false;
    while let Some(ch) = chars.next() {
        if in_string {
            out.push(ch);
            if escaping {
                escaping = false;
                continue;
            }
            match ch {
                '\\' => escaping = true,
                '"' => in_string = false,
                _ => {}
            }
            continue;
        }
        match ch {
            '"' => {
                in_string = true;
                out.push(ch);
            }
            '/' if matches!(chars.peek(), Some('/')) => {
                chars.next();
                for next in chars.by_ref() {
                    if next == '\n' {
                        out.push('\n');
                        break;
                    }
                }
            }
            '/' if matches!(chars.peek(), Some('*')) => {
                chars.next();
                let mut prev = '\0';
                for next in chars.by_ref() {
                    if next == '\n' {
                        out.push('\n');
                    }
                    if prev == '*' && next == '/' {
                        break;
                    }
                    prev = next;
                }
            }
            _ => out.push(ch),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::parse_js_list;

    #[test]
    fn parse_js_list_rejects_duplicates() {
        let err = parse_js_list("a.js\na.js\n").expect_err("expected duplicate rejection");
        assert!(err.to_string().contains("duplicate"));
    }

    #[test]
    fn parse_js_list_ignores_comments_and_blank_lines() {
        let parsed = parse_js_list("\n# comment\nfoo.js\nbar.js\n").expect("parse list");
        assert_eq!(parsed, vec!["foo.js", "bar.js"]);
    }

    #[test]
    fn parse_transform_cli_args_matches_js_surface() {
        let parsed = parse_transform_cli_args(&[
            "--spec".to_string(),
            "spec.jsonc".to_string(),
            "--package-root".to_string(),
            "pkg=/tmp/pkg".to_string(),
            "--packages-root".to_string(),
            "/tmp/packages".to_string(),
        ])
        .expect("parse cli");
        let ParsedTransformCli::Run(cli) = parsed else {
            panic!("expected run cli");
        };
        assert_eq!(cli.spec_path, PathBuf::from("spec.jsonc"));
        assert_eq!(
            cli.package_roots.get("pkg"),
            Some(&PathBuf::from("/tmp/pkg"))
        );
        assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
    }

    #[test]
    fn run_transform_cli_writes_spec_pipeline_outputs() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let root = temp.path();
        let snapshot = root.join("snapshot");
        let extracted = root.join("extracted");
        let out = root.join("out");
        fs::create_dir_all(snapshot.join("static"))?;
        fs::create_dir_all(&extracted)?;
        fs::write(
            snapshot.join("index.html"),
            r#"<!doctype html>
<html>
  <head>
    <link rel="modulepreload" href="./static/chunk-DuckMock.js">
  </head>
  <body>
    <script type="module" src="./static/index-DuckMock.js"></script>
  </body>
</html>
"#,
        )?;
        fs::write(
            snapshot.join("static/index-DuckMock.js"),
            "import { y } from './chunk-DuckMock.js';\nglobalThis.__value = y;\n",
        )?;
        fs::write(
            snapshot.join("static/chunk-DuckMock.js"),
            "export const y = 2;\n",
        )?;
        fs::write(
            extracted.join("js-files.txt"),
            "static/index-DuckMock.js\nstatic/chunk-DuckMock.js\n",
        )?;
        fs::write(
            extracted.join("asset-summary.json"),
            serde_json::to_string(&serde_json::json!({
                "entryPoints": {
                    "html": "index.html",
                },
            }))?,
        )?;
        let spec_path = root.join("transform-spec.jsonc");
        fs::write(
            &spec_path,
            serde_json::to_string_pretty(&serde_json::json!({
                "kind": "js.ast_transform_spec",
                "pipeline": [
                    {
                        "id": "load",
                        "operation": "load_js_chunks",
                        "args": {
                            "inputRoot": snapshot,
                            "jsListPath": extracted.join("js-files.txt"),
                        },
                    },
                    {
                        "id": "parse",
                        "operation": "compute_js_asts",
                    },
                    {
                        "id": "normalize",
                        "operation": "normalize_js_chunks",
                    },
                    {
                        "id": "rewrite",
                        "operation": "rewrite_chunk_entry_specifiers",
                    },
                    {
                        "id": "emit",
                        "operation": "emit_browser_harness",
                        "args": {
                            "assetSummaryPath": extracted.join("asset-summary.json"),
                            "force": true,
                            "outDir": out,
                            "scriptSource": "split",
                            "snapshotRoot": snapshot,
                        },
                    },
                ],
            }))?,
        )?;

        let summary = run_transform_cli(&TransformCli {
            spec_path,
            package_roots: HashMap::new(),
            packages_root: None,
        })?;

        assert_eq!(summary.steps.len(), 5);
        assert!(out.join("bootstrap.js").exists());
        assert!(out.join("manifest.json").exists());
        let entry = fs::read_to_string(out.join("static/index-DuckMock/entry.js"))?;
        assert!(entry.contains("../chunk-DuckMock/entry.js"));
        Ok(())
    }
}
