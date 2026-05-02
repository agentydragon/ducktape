use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result, bail};
use clap::Parser;
use runfiles::{Runfiles, rlocation};
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

/// Command-line arguments for the debundle transform pipeline.
///
/// Use [`TransformArgs::resolve`] to obtain a [`TransformCli`] with paths
/// resolved against Bazel runfiles when running as a `bazel run` target.
#[derive(Parser, Debug)]
#[command(
    name = "debundle",
    version,
    about = "Run the debundle transform pipeline described by --spec.",
    long_about = "Runs the transform pipeline described by the spec. Pipeline stages \
                  dispatch directly to registered functions; this target does not invoke \
                  Bazel from inside the pipeline. Specs are parsed as JSON with comments."
)]
pub struct TransformArgs {
    /// Path to the transform spec (JSON with comments).
    #[arg(long)]
    pub spec: PathBuf,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
}

impl TransformArgs {
    /// Resolve all path arguments against Bazel runfiles (when present) and
    /// collapse `--package-root` pairs into a `HashMap`.
    pub fn resolve(self) -> TransformCli {
        let runfiles = Runfiles::create().ok();
        TransformCli {
            spec_path: resolve_runfiles_path(self.spec, runfiles.as_ref()),
            package_roots: self
                .package_roots
                .into_iter()
                .map(|(name, dir)| (name, resolve_runfiles_path(dir, runfiles.as_ref())))
                .collect(),
            packages_root: self
                .packages_root
                .map(|dir| resolve_runfiles_path(dir, runfiles.as_ref())),
        }
    }
}

fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let Some(separator) = value.find('=') else {
        return Err(format!(
            "--package-root must be in <package>=<dir> form, got {value}"
        ));
    };
    if separator == 0 || separator == value.len() - 1 {
        return Err(format!(
            "--package-root must be in <package>=<dir> form, got {value}"
        ));
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
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
    inputs: Option<LoadJsChunksArgs>,
    pipeline: Option<Vec<TransformStage>>,
    operations: Option<Vec<serde_json::Value>>,
}

#[derive(Debug, Clone, Deserialize)]
struct TransformStage {
    id: Option<String>,
    operation: Option<String>,
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
struct EmitBrowserHarnessArgs {
    asset_summary_path: PathBuf,
    force: Option<bool>,
    out_dir: PathBuf,
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
    chunk_ids: Vec<String>,
    file: Option<String>,
    prune_other_chunks: Option<bool>,
    force: Option<bool>,
    report_out_dir: Option<PathBuf>,
    report_summary_path: Option<PathBuf>,
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

/// Resolve a path through Bazel runfiles when present, otherwise pass through.
///
/// Lets the binary work as a standalone CLI (filesystem paths) and as a
/// Bazel-run target (runfiles-relative paths produced by `$(rlocationpath ...)`)
/// without a launcher wrapper. A path is treated as runfiles-relative only
/// when it actually resolves to a file inside the runfiles tree; otherwise
/// it's left for the caller's filesystem semantics.
fn resolve_runfiles_path(path: PathBuf, runfiles: Option<&Runfiles>) -> PathBuf {
    if path.is_absolute() {
        return path;
    }
    let Some(runfiles) = runfiles else {
        return path;
    };
    let Some(s) = path.to_str() else {
        return path;
    };
    rlocation!(runfiles, s)
        .filter(|resolved| resolved.exists())
        .unwrap_or(path)
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
    let inputs = spec
        .inputs
        .expect("validate_transform_spec ensures inputs is present");
    let pipeline = spec.pipeline.unwrap_or_default();
    let operations = spec.operations.unwrap_or_default();
    let started = Instant::now();
    let mut state = TransformState::default();
    let (artifact, _load_manifest) = load_js_chunks(&inputs.input_root, &inputs.js_list_path)?;
    state.artifact = artifact;
    compute_js_asts(&mut state.artifact, true)?;
    let (artifact, _normalize_manifest) = normalize_js_chunks(std::mem::take(&mut state.artifact))?;
    state.artifact = artifact;
    let mut steps = Vec::new();

    for stage in pipeline {
        let id = stage.id.unwrap_or_default();
        let operation = stage.operation.unwrap_or_default();
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
                    chunk_ids: args.chunk_ids,
                    file: args.file,
                    prune_other_chunks: args.prune_other_chunks.unwrap_or(true),
                    force: args.force.unwrap_or(false),
                    report_out_dir: args.report_out_dir,
                    report_summary_path: args.report_summary_path,
                    target_dir: args.target_dir.unwrap_or_default(),
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
    let inputs = spec
        .inputs
        .as_ref()
        .context("Transform spec must contain an inputs object with inputRoot and jsListPath")?;
    if inputs.input_root.as_os_str().is_empty() {
        bail!("Transform spec inputs.inputRoot must not be empty");
    }
    if inputs.js_list_path.as_os_str().is_empty() {
        bail!("Transform spec inputs.jsListPath must not be empty");
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
    }
    Ok(())
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
        let args = TransformArgs::try_parse_from([
            "debundle",
            "--spec",
            "spec.jsonc",
            "--package-root",
            "pkg=/tmp/pkg",
            "--packages-root",
            "/tmp/packages",
        ])
        .expect("parse cli");
        let cli = args.resolve();
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
                "inputs": {
                    "inputRoot": snapshot,
                    "jsListPath": extracted.join("js-files.txt"),
                },
                "pipeline": [
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

        assert_eq!(summary.steps.len(), 2);
        assert!(out.join("bootstrap.js").exists());
        assert!(out.join("manifest.json").exists());
        let entry = fs::read_to_string(out.join("static/index-DuckMock/entry.js"))?;
        assert!(entry.contains("../chunk-DuckMock/entry.js"));
        Ok(())
    }
}
