use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use clap::Parser;
use runfiles::{Runfiles, rlocation};
use serde::{Deserialize, Serialize};

use artifact::ArtifactIndexes;
use artifact::load_js_chunks;
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use logical_modules::{MaterializeLogicalModulesOptions, materialize_logical_modules};
use prepare_chunks::prepare_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use spec::{MaterializeLogicalModulesConfig, SwapVendorChunksConfig, TransformSpec, VendorLevel};
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree};
use vendor::{
    SwapVendorOptions, apply_vendor_annotations, rename_vendor_exports, swap_vendor_chunks,
};
use write_tree::write_js_tree;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
    pub force: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Command-line arguments for the debundle transform pipeline.
///
/// Use [`TransformArgs::resolve`] to obtain a [`TransformCli`] with paths
/// resolved against Bazel runfiles when running as a `bazel run` target.
#[derive(Parser, Debug)]
#[command(
    name = "debundle",
    version,
    about = "Run the debundle transform pipeline from a flat or tree-shaped spec.",
    long_about = "Runs the transform pipeline described by a flat transform spec or tree-shaped authoring spec. Pipeline stages \
                  dispatch directly to registered functions; this target does not invoke \
                  Bazel from inside the pipeline. Specs are parsed as YAML."
)]
pub struct TransformArgs {
    /// Path to a flat transform spec YAML.
    #[arg(long)]
    pub spec: Option<PathBuf>,
    /// Path to a tree-shaped authoring config YAML.
    #[arg(long = "tree-config")]
    pub tree_config: Option<PathBuf>,
    /// Root directory containing tree-shaped logical module YAML files.
    #[arg(long = "tree-modules")]
    pub tree_modules: Option<PathBuf>,
    /// Path to tree-shaped vendor marks YAML.
    #[arg(long = "tree-vendor-marks")]
    pub tree_vendor_marks: Option<PathBuf>,
    /// Root for source-relative paths embedded in the tree-shaped config YAML.
    #[arg(long = "tree-source-root")]
    pub tree_source_root: Option<PathBuf>,
    /// Output root used when compiling tree-shaped authoring sources.
    #[arg(long = "out-root")]
    pub out_root: Option<PathBuf>,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
    /// Replace existing output directories for stages that write output trees.
    #[arg(long)]
    pub force: bool,
}

impl TransformArgs {
    /// Resolve all path arguments against Bazel runfiles (when present) and
    /// collapse `--package-root` pairs into a `HashMap`.
    pub fn resolve(self) -> Result<TransformCli> {
        let runfiles = Runfiles::create().ok();
        let spec_source = resolve_spec_source(&self, runfiles.as_ref())?;
        Ok(TransformCli {
            spec_source,
            package_roots: self
                .package_roots
                .into_iter()
                .map(|(name, dir)| (name, resolve_runfiles_path(dir, runfiles.as_ref())))
                .collect(),
            packages_root: self
                .packages_root
                .map(|dir| resolve_runfiles_path(dir, runfiles.as_ref())),
            force: self.force,
        })
    }
}

fn resolve_spec_source(
    args: &TransformArgs,
    runfiles: Option<&Runfiles>,
) -> Result<TransformSpecSource> {
    match (&args.spec, &args.tree_config) {
        (Some(_), Some(_)) => {
            bail!("pass either --spec or --tree-config, not both");
        }
        (Some(path), None) => Ok(TransformSpecSource::Flat {
            path: resolve_runfiles_path(path.clone(), runfiles),
        }),
        (None, Some(config_path)) => {
            let modules_root = required_tree_arg("--tree-modules", &args.tree_modules)?;
            let vendor_marks_path =
                required_tree_arg("--tree-vendor-marks", &args.tree_vendor_marks)?;
            let out_root = required_tree_arg("--out-root", &args.out_root)?;
            Ok(TransformSpecSource::Tree(CompileSpecTreeOptions {
                config_path: resolve_runfiles_path(config_path.clone(), runfiles),
                modules_root: resolve_runfiles_path(modules_root, runfiles),
                vendor_marks_path: resolve_runfiles_path(vendor_marks_path, runfiles),
                source_root: args
                    .tree_source_root
                    .clone()
                    .map(|path| resolve_runfiles_path(path, runfiles)),
                out_root: resolve_runfiles_path(out_root, runfiles),
                force: args.force,
            }))
        }
        (None, None) => {
            bail!("pass either --spec or --tree-config");
        }
    }
}

fn required_tree_arg(name: &str, value: &Option<PathBuf>) -> Result<PathBuf> {
    value
        .clone()
        .with_context(|| format!("{name} is required with --tree-config"))
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
    pub duration: Duration,
    pub spec_path: String,
    pub preparation_steps: Vec<TransformStepSummary>,
    pub steps: Vec<TransformStepSummary>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TransformStepSummary {
    pub stage: PipelineStage,
    pub duration: Duration,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PipelineStage {
    LoadTransformSpec,
    ValidateTransformSpec,
    LoadJsChunks,
    PrepareJsChunks,
    BuildArtifactIndexes,
    RewriteChunkEntrySpecifiers,
    ApplyVendorAnnotations,
    RenameVendorExports,
    SwapVendorChunks,
    MaterializeLogicalModules,
    WriteJsTree,
    EmitBrowserHarness,
}

impl PipelineStage {
    fn report_name(self) -> String {
        match serde_json::to_value(self).expect("PipelineStage serializes as a string") {
            serde_json::Value::String(name) => name,
            _ => unreachable!("PipelineStage unit enum should serialize as a string"),
        }
    }
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
        humantime::format_duration(summary.duration)
    );
    for step in &summary.preparation_steps {
        out.push_str(&format!(
            "- {} ({})\n",
            step.stage.report_name(),
            humantime::format_duration(step.duration),
        ));
    }
    for step in &summary.steps {
        out.push_str(&format!(
            "- {} ({})\n",
            step.stage.report_name(),
            humantime::format_duration(step.duration),
        ));
    }
    out
}

pub fn run_transform_cli(cli: &TransformCli) -> Result<TransformRunSummary> {
    let started = Instant::now();
    let mut preparation_steps = Vec::new();
    let spec = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::LoadTransformSpec,
        || load_transform_spec_source(&cli.spec_source),
    )?;
    run_step(
        &mut preparation_steps,
        PipelineStage::ValidateTransformSpec,
        || validate_transform_spec(&spec),
    )?;
    let (artifact, _load_manifest) =
        run_step_with_result(&mut preparation_steps, PipelineStage::LoadJsChunks, || {
            load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)
        })?;
    let materialise_chunk_ids: Vec<String> = spec
        .logical_modules
        .keys()
        .chain(spec.residual_modules.keys())
        .chain(spec.chunk_renames.keys())
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let prepare_result = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::PrepareJsChunks,
        || prepare_js_chunks(&spec, artifact),
    )?;
    let artifact_indexes = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::BuildArtifactIndexes,
        || ArtifactIndexes::build(&prepare_result.artifact),
    )?;
    let mut steps = Vec::new();

    let rewrite_result = run_step_with_result(
        &mut steps,
        PipelineStage::RewriteChunkEntrySpecifiers,
        || rewrite_chunk_entry_specifiers(prepare_result.artifact, &artifact_indexes),
    )?;
    let mut artifact = rewrite_result.artifact;

    // Vendor stages: each is internally filtered by `level`, so it's
    // safe to always invoke them when `vendor` carries any entries.
    // `apply` runs unconditionally; `rename` and `swap` short-circuit
    // to no-ops when no entry has the right level.
    if !spec.vendor.is_empty() {
        run_step(&mut steps, PipelineStage::ApplyVendorAnnotations, || {
            apply_vendor_annotations(&artifact, &spec.vendor).map(|_| ())
        })?;
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::BoundaryRename | VendorLevel::Swap(_)))
        {
            let rename_result =
                run_step_with_result(&mut steps, PipelineStage::RenameVendorExports, || {
                    rename_vendor_exports(artifact, &spec.vendor, &artifact_indexes)
                })?;
            artifact = rename_result.artifact;
        }
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::Swap(_)))
        {
            let SwapVendorChunksConfig {
                output_manifest_path,
                output_wrapper_dir,
                write,
            } = spec.swap_vendor_chunks.clone();
            let swap_result =
                run_step_with_result(&mut steps, PipelineStage::SwapVendorChunks, || {
                    swap_vendor_chunks(
                        artifact,
                        &spec.vendor,
                        &artifact_indexes,
                        SwapVendorOptions {
                            package_roots: &cli.package_roots,
                            packages_root: &cli.packages_root,
                            output_manifest_path,
                            output_wrapper_dir,
                            write,
                        },
                    )
                })?;
            artifact = swap_result.artifact;
        }
    }

    if !materialise_chunk_ids.is_empty() {
        let MaterializeLogicalModulesConfig {
            file,
            prune_other_chunks,
            force,
            report_out_dir,
            report_summary_path,
            target_dir,
        } = spec.materialize_logical_modules.clone();
        let force = force || cli.force;
        let materialize_result =
            run_step_with_result(&mut steps, PipelineStage::MaterializeLogicalModules, || {
                materialize_logical_modules(
                    artifact,
                    &spec.logical_modules,
                    &spec.residual_modules,
                    &spec.chunk_renames,
                    &spec.unassigned_mode,
                    MaterializeLogicalModulesOptions {
                        chunk_ids: materialise_chunk_ids,
                        file,
                        prune_other_chunks,
                        force,
                        report_out_dir,
                        report_summary_path,
                        target_dir,
                    },
                )
            })?;
        artifact = materialize_result.artifact;
    }

    if let Some(cfg) = &spec.write_js_tree {
        let out_dir = cfg.out_dir.clone();
        let force = cfg.force || cli.force;
        run_step(&mut steps, PipelineStage::WriteJsTree, || {
            write_js_tree(&artifact, &out_dir, force).map(|_| ())
        })?;
    }

    if let Some(cfg) = &spec.emit_browser_harness {
        let force = cfg.force || cli.force;
        let opts = EmitBrowserHarnessOptions {
            asset_summary_path: cfg.asset_summary_path.clone(),
            force,
            out_dir: cfg.out_dir.clone(),
            snapshot_root: cfg.snapshot_root.clone(),
        };
        run_step(&mut steps, PipelineStage::EmitBrowserHarness, || {
            emit_browser_harness(&artifact, &opts)?;
            Ok(())
        })?;
    }

    Ok(TransformRunSummary {
        duration: started.elapsed(),
        spec_path: spec_source_description(&cli.spec_source),
        preparation_steps,
        steps,
    })
}

fn load_transform_spec_source(source: &TransformSpecSource) -> Result<TransformSpec> {
    match source {
        TransformSpecSource::Flat { path } => load_flat_transform_spec(path),
        TransformSpecSource::Tree(options) => compile_spec_tree(options),
    }
}

fn spec_source_description(source: &TransformSpecSource) -> String {
    match source {
        TransformSpecSource::Flat { path } => path.display().to_string(),
        TransformSpecSource::Tree(options) => options.config_path.display().to_string(),
    }
}

fn run_step(
    steps: &mut Vec<TransformStepSummary>,
    stage: PipelineStage,
    body: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let started = Instant::now();
    body()?;
    steps.push(TransformStepSummary {
        stage,
        duration: started.elapsed(),
    });
    Ok(())
}

fn run_step_with_result<T>(
    steps: &mut Vec<TransformStepSummary>,
    stage: PipelineStage,
    body: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let started = Instant::now();
    let value = body()?;
    steps.push(TransformStepSummary {
        stage,
        duration: started.elapsed(),
    });
    Ok(value)
}

fn load_flat_transform_spec(spec_path: &Path) -> Result<TransformSpec> {
    let raw = fs::read(spec_path).with_context(|| format!("reading {}", spec_path.display()))?;
    serde_yaml::from_slice(&raw)
        .with_context(|| format!("Failed to parse {} as YAML", spec_path.display()))
}

fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
    if spec.inputs.input_root.as_os_str().is_empty() {
        bail!("Transform spec inputs.input_root must not be empty");
    }
    if spec.inputs.js_list_path.as_os_str().is_empty() {
        bail!("Transform spec inputs.js_list_path must not be empty");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::parse_js_list;
    use std::collections::BTreeMap;

    #[derive(Serialize)]
    struct AssetSummaryFixture<'a> {
        #[serde(rename = "entryPoints")]
        entry_points: AssetSummaryEntryPoints<'a>,
    }

    #[derive(Serialize)]
    struct AssetSummaryEntryPoints<'a> {
        html: &'a str,
    }

    #[derive(Serialize)]
    struct PipelineSpecFixture<'a> {
        inputs: PipelineSpecInputs<'a>,
        emit_browser_harness: EmitBrowserHarnessFixture<'a>,
    }

    #[derive(Serialize)]
    struct PipelineSpecInputs<'a> {
        input_root: &'a Path,
        js_list_path: &'a Path,
    }

    #[derive(Serialize)]
    struct EmitBrowserHarnessFixture<'a> {
        asset_summary_path: &'a Path,
        force: bool,
        out_dir: &'a Path,
        snapshot_root: &'a Path,
    }

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
            "spec.yaml",
            "--package-root",
            "pkg=/tmp/pkg",
            "--packages-root",
            "/tmp/packages",
            "--force",
        ])
        .expect("parse cli");
        let cli = args.resolve().expect("resolve cli");
        assert_eq!(
            cli.spec_source,
            TransformSpecSource::Flat {
                path: PathBuf::from("spec.yaml")
            }
        );
        assert_eq!(
            cli.package_roots.get("pkg"),
            Some(&PathBuf::from("/tmp/pkg"))
        );
        assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
        assert!(cli.force);
    }

    #[test]
    fn parse_tree_transform_cli_args() {
        let args = TransformArgs::try_parse_from([
            "debundle",
            "--tree-config",
            "spec_config.yaml",
            "--tree-modules",
            "modules",
            "--tree-vendor-marks",
            "vendor_marks.yaml",
            "--tree-source-root",
            "/workspace",
            "--out-root",
            "out",
        ])
        .expect("parse cli");
        let cli = args.resolve().expect("resolve cli");
        assert_eq!(
            cli.spec_source,
            TransformSpecSource::Tree(CompileSpecTreeOptions {
                config_path: PathBuf::from("spec_config.yaml"),
                modules_root: PathBuf::from("modules"),
                vendor_marks_path: PathBuf::from("vendor_marks.yaml"),
                source_root: Some(PathBuf::from("/workspace")),
                out_root: PathBuf::from("out"),
                force: false,
            })
        );
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
        let js_list_path = extracted.join("js-files.txt");
        let asset_summary_path = extracted.join("asset-summary.json");
        fs::write(
            &asset_summary_path,
            serde_json::to_string(&AssetSummaryFixture {
                entry_points: AssetSummaryEntryPoints { html: "index.html" },
            })?,
        )?;
        let spec_path = root.join("transform-spec.yaml");
        fs::write(
            &spec_path,
            serde_yaml::to_string(&PipelineSpecFixture {
                inputs: PipelineSpecInputs {
                    input_root: &snapshot,
                    js_list_path: &js_list_path,
                },
                emit_browser_harness: EmitBrowserHarnessFixture {
                    asset_summary_path: &asset_summary_path,
                    force: true,
                    out_dir: &out,
                    snapshot_root: &snapshot,
                },
            })?,
        )?;

        let summary = run_transform_cli(&TransformCli {
            spec_source: TransformSpecSource::Flat { path: spec_path },
            package_roots: HashMap::new(),
            packages_root: None,
            force: false,
        })?;

        assert_eq!(summary.steps.len(), 2);
        assert_eq!(summary.preparation_steps.len(), 5);
        let rendered_summary = render_transform_summary(&summary);
        assert!(rendered_summary.contains("- load_transform_spec"));
        assert!(rendered_summary.contains("- prepare_js_chunks"));
        assert!(rendered_summary.contains("- build_artifact_indexes"));
        assert!(rendered_summary.contains("- rewrite_chunk_entry_specifiers"));
        assert!(rendered_summary.contains("- emit_browser_harness"));
        assert!(out.join("bootstrap.js").exists());
        assert!(out.join("manifest.json").exists());
        let entry = fs::read_to_string(out.join("static/index-DuckMock/entry.js"))?;
        assert!(entry.contains("../chunk-DuckMock/entry.js"));
        let chunk_entry = fs::read_to_string(out.join("static/chunk-DuckMock/entry.js"))?;
        let total_bytes = entry.len() + chunk_entry.len();
        let total_lines = entry.lines().count() + chunk_entry.lines().count();

        // The harness tree must be self-contained: every path the manifest
        // records resolves to a file inside `out_dir`, with no leakage to
        // the original `extracted/` or `snapshots/` input trees. Consumers
        // (live proxy, downstream tools) may receive the manifest through
        // runfiles where the original input trees aren't co-located.
        let manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(out.join("manifest.json"))?)?;
        assert_eq!(
            manifest
                .pointer("/output_metrics/total/files")
                .and_then(serde_json::Value::as_u64),
            Some(2),
        );
        assert_eq!(
            manifest
                .pointer("/output_metrics/total/bytes")
                .and_then(serde_json::Value::as_u64),
            Some(total_bytes as u64),
        );
        assert_eq!(
            manifest
                .pointer("/output_metrics/total/lines")
                .and_then(serde_json::Value::as_u64),
            Some(total_lines as u64),
        );
        assert_eq!(
            manifest
                .pointer("/output_metrics/top_level_entry/files")
                .and_then(serde_json::Value::as_u64),
            Some(2),
        );
        assert_eq!(
            manifest
                .pointer("/output_metrics/named_modules/files")
                .and_then(serde_json::Value::as_u64),
            Some(0),
        );
        assert_eq!(
            manifest
                .pointer("/output_metrics/largest_files_by_bytes/0/role")
                .and_then(serde_json::Value::as_str),
            Some("top_level_entry"),
        );
        assert!(
            manifest.get("schema_version").is_none(),
            "harness manifest should not carry a compatibility schema_version"
        );
        assert!(
            manifest.get("parse_plan").is_none(),
            "harness manifest should no longer carry a parse_plan field"
        );
        for field in [
            "source_html",
            "asset_summary_path",
            "chunks_manifest_path",
            "runtime_root",
            "out_dir",
        ] {
            let value = manifest
                .get(field)
                .and_then(serde_json::Value::as_str)
                .unwrap_or_else(|| panic!("manifest is missing {field}"));
            assert!(
                !value.starts_with('/') && !value.starts_with(".."),
                "manifest.{field} = {value:?} escapes the harness tree"
            );
            let resolved = out.join(value);
            assert!(
                resolved.exists(),
                "manifest.{field} = {value:?} resolves to {resolved:?} which does not exist"
            );
        }
        let chunks_manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(out.join("chunks.manifest.json"))?)?;
        assert!(
            chunks_manifest.get("schema_version").is_none(),
            "chunks manifest should not carry a compatibility schema_version"
        );
        assert_eq!(
            chunks_manifest
                .get("chunks")
                .and_then(serde_json::Value::as_array)
                .map(Vec::len),
            Some(2)
        );
        Ok(())
    }

    #[test]
    fn cli_force_overrides_output_stage_force_flags() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let root = temp.path();
        let snapshot = root.join("snapshot");
        let extracted = root.join("extracted");
        let js_out = root.join("js-out");
        let harness_out = root.join("harness-out");
        fs::create_dir_all(snapshot.join("static"))?;
        fs::create_dir_all(&extracted)?;
        fs::create_dir_all(&js_out)?;
        fs::create_dir_all(&harness_out)?;
        fs::write(js_out.join("stale.txt"), "old")?;
        fs::write(harness_out.join("stale.txt"), "old")?;
        fs::write(
            snapshot.join("index.html"),
            r#"<!doctype html>
<script type="module" src="./static/index.js"></script>
"#,
        )?;
        fs::write(
            snapshot.join("static/index.js"),
            "globalThis.__value = 1;\n",
        )?;
        let js_list_path = extracted.join("js-files.txt");
        fs::write(&js_list_path, "static/index.js\n")?;
        let asset_summary_path = extracted.join("asset-summary.json");
        fs::write(
            &asset_summary_path,
            serde_json::to_string(&AssetSummaryFixture {
                entry_points: AssetSummaryEntryPoints { html: "index.html" },
            })?,
        )?;

        let mut spec = empty_transform_spec();
        spec.inputs = spec::LoadJsChunksArgs {
            input_root: snapshot.clone(),
            js_list_path,
        };
        spec.write_js_tree = Some(spec::WriteJsTreeConfig {
            out_dir: js_out.clone(),
            force: false,
        });
        spec.emit_browser_harness = Some(spec::EmitBrowserHarnessConfig {
            asset_summary_path,
            out_dir: harness_out.clone(),
            snapshot_root: snapshot,
            force: false,
        });
        let spec_path = root.join("transform-spec.yaml");
        fs::write(&spec_path, serde_yaml::to_string(&spec)?)?;

        let summary = run_transform_cli(&TransformCli {
            spec_source: TransformSpecSource::Flat { path: spec_path },
            package_roots: HashMap::new(),
            packages_root: None,
            force: true,
        })?;

        assert!(
            summary
                .steps
                .iter()
                .any(|step| matches!(step.stage, PipelineStage::WriteJsTree))
        );
        assert!(
            summary
                .steps
                .iter()
                .any(|step| matches!(step.stage, PipelineStage::EmitBrowserHarness))
        );
        assert!(!js_out.join("stale.txt").exists());
        assert!(!harness_out.join("stale.txt").exists());
        assert!(js_out.join("static/index/entry.js").exists());
        assert!(harness_out.join("bootstrap.js").exists());
        Ok(())
    }

    fn empty_transform_spec() -> TransformSpec {
        TransformSpec {
            inputs: spec::LoadJsChunksArgs {
                input_root: PathBuf::from("."),
                js_list_path: PathBuf::from("js-files.txt"),
            },
            vendor: BTreeMap::new(),
            logical_modules: BTreeMap::new(),
            residual_modules: BTreeMap::new(),
            chunk_renames: BTreeMap::new(),
            unassigned_mode: BTreeMap::new(),
            swap_vendor_chunks: SwapVendorChunksConfig::default(),
            materialize_logical_modules: MaterializeLogicalModulesConfig::default(),
            write_js_tree: None,
            emit_browser_harness: None,
        }
    }
}
