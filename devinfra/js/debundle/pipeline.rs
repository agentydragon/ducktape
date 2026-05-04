use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result, bail};
use clap::Parser;
use runfiles::{Runfiles, rlocation};
use serde::Serialize;

use artifact::{JsPipelineArtifact, compute_js_asts, load_js_chunks};
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use logical_modules::{MaterializeLogicalModulesOptions, materialize_logical_modules};
use normalize::normalize_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use spec::{MaterializeLogicalModulesConfig, SwapVendorChunksConfig, TransformSpec, VendorLevel};
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
    pub operation: String,
    pub duration_ms: Option<f64>,
    pub manifest_kind: Option<String>,
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
            "- {} ({}){}\n",
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
    let started = Instant::now();
    let mut state = TransformState::default();
    let (artifact, _load_manifest) =
        load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    state.artifact = artifact;
    compute_js_asts(&mut state.artifact, true)?;
    let (artifact, _normalize_manifest) = normalize_js_chunks(std::mem::take(&mut state.artifact))?;
    state.artifact = artifact;
    let mut steps = Vec::new();

    if spec.rewrite_chunk_entry_specifiers {
        run_step(&mut steps, "rewrite_chunk_entry_specifiers", || {
            rewrite_chunk_entry_specifiers(&mut state.artifact).map(|m| Some(m.kind.to_string()))
        })?;
    }

    // Vendor stages: each is internally filtered by `level`, so it's
    // safe to always invoke them when `vendor` carries any entries.
    // `apply` runs unconditionally; `rename` and `swap` short-circuit
    // to no-ops when no entry has the right level.
    if !spec.vendor.is_empty() {
        run_step(&mut steps, "apply_vendor_annotations", || {
            apply_vendor_annotations(&state.artifact, &spec.vendor)
                .map(|m| Some(m.kind.to_string()))
        })?;
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::BoundaryRename | VendorLevel::Swap(_)))
        {
            run_step(&mut steps, "rename_vendor_exports", || {
                rename_vendor_exports(&mut state.artifact, &spec.vendor)
                    .map(|m| Some(m.kind.to_string()))
            })?;
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
            run_step(&mut steps, "swap_vendor_chunks", || {
                swap_vendor_chunks(
                    &mut state.artifact,
                    &spec.vendor,
                    SwapVendorOptions {
                        package_roots: &cli.package_roots,
                        packages_root: &cli.packages_root,
                        output_manifest_path,
                        output_wrapper_dir,
                        write,
                    },
                )
                .map(|m| Some(m.kind.to_string()))
            })?;
        }
    }

    let materialise_chunk_ids: Vec<String> = spec
        .logical_modules
        .keys()
        .chain(spec.residual_modules.keys())
        .chain(spec.chunk_renames.keys())
        .cloned()
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    if !materialise_chunk_ids.is_empty() {
        let MaterializeLogicalModulesConfig {
            file,
            prune_other_chunks,
            force,
            report_out_dir,
            report_summary_path,
            target_dir,
        } = spec.materialize_logical_modules.clone();
        run_step(&mut steps, "materialize_logical_modules", || {
            materialize_logical_modules(
                &mut state.artifact,
                &spec.logical_modules,
                &spec.residual_modules,
                &spec.chunk_renames,
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
            .map(|m| Some(m.kind.to_string()))
        })?;
    }

    if let Some(cfg) = &spec.write_js_tree {
        let out_dir = cfg.out_dir.clone();
        let force = cfg.force;
        run_step(&mut steps, "write_js_tree", || {
            write_js_tree(&state.artifact, &out_dir, force).map(|m| Some(m.kind.to_string()))
        })?;
    }

    if let Some(cfg) = &spec.emit_browser_harness {
        let opts = EmitBrowserHarnessOptions {
            asset_summary_path: cfg.asset_summary_path.clone(),
            force: cfg.force,
            out_dir: cfg.out_dir.clone(),
            snapshot_root: cfg.snapshot_root.clone(),
        };
        run_step(&mut steps, "emit_browser_harness", || {
            emit_browser_harness(&state.artifact, &opts)?;
            Ok(None)
        })?;
    }

    Ok(TransformRunSummary {
        duration_ms: elapsed_ms(started),
        spec_path: cli.spec_path.display().to_string(),
        steps,
    })
}

fn run_step(
    steps: &mut Vec<TransformStepSummary>,
    name: &'static str,
    body: impl FnOnce() -> Result<Option<String>>,
) -> Result<()> {
    let started = Instant::now();
    let manifest_kind = body()?;
    steps.push(TransformStepSummary {
        operation: name.to_string(),
        duration_ms: Some(elapsed_ms(started)),
        manifest_kind,
    });
    Ok(())
}

fn load_transform_spec(spec_path: &Path) -> Result<TransformSpec> {
    let raw = fs::read(spec_path).with_context(|| format!("reading {}", spec_path.display()))?;
    serde_json::from_reader(json_comments::StripComments::new(raw.as_slice()))
        .with_context(|| format!("Failed to parse {} as JSONC", spec_path.display()))
}

fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
    if spec.kind != "js.ast_transform_spec" {
        bail!("Unsupported transform spec kind: {}", spec.kind);
    }
    if spec.inputs.input_root.as_os_str().is_empty() {
        bail!("Transform spec inputs.inputRoot must not be empty");
    }
    if spec.inputs.js_list_path.as_os_str().is_empty() {
        bail!("Transform spec inputs.jsListPath must not be empty");
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
                "rewriteChunkEntrySpecifiers": true,
                "emitBrowserHarness": {
                    "assetSummaryPath": extracted.join("asset-summary.json"),
                    "force": true,
                    "outDir": out,
                    "snapshotRoot": snapshot,
                },
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

        // The harness tree must be self-contained: every path the manifest
        // records resolves to a file inside `out_dir`, with no leakage to
        // the original `extracted/` or `snapshots/` input trees. Consumers
        // (live proxy, downstream tools) may receive the manifest through
        // runfiles where the original input trees aren't co-located.
        let manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(out.join("manifest.json"))?)?;
        for field in [
            "sourceHtml",
            "assetSummaryPath",
            "chunksManifestPath",
            "runtimeRoot",
            "outDir",
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
        Ok(())
    }
}
