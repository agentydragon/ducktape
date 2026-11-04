//! Source/projection-only entry point shared by the dedicated analyzer and CLI.
use super::{AnalysisSpec, AnalyzeProgramInputs};
use anyhow::{Context, Result};
use artifact::load_js_chunks;
use clap::Args as ClapArgs;
use graph_provenance::{InputCapture, ProvenanceRoots};
use prepare_chunks::prepare_js_chunks;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use vendor::{VendorPlanOptions, build_vendor_resolution_plan};

pub fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let Some(separator) = value.find('=') else {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    };
    if separator == 0 || separator == value.len() - 1 {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
}

pub fn collect_package_roots(
    pairs: impl IntoIterator<Item = (String, PathBuf)>,
) -> Result<std::collections::BTreeMap<String, PathBuf>> {
    let mut roots = std::collections::BTreeMap::new();
    for (name, path) in pairs {
        anyhow::ensure!(
            roots.insert(name.clone(), path).is_none(),
            "duplicate package root mapping for {name}"
        );
    }
    Ok(roots)
}

/// Command-line arguments for `debundle analyze`.
#[derive(ClapArgs, Debug, Clone)]
pub struct AnalyzeArgs {
    /// Analysis projection written by `debundle spec project-analysis`; the
    /// only spec input.
    #[arg(long)]
    pub projection: PathBuf,
    /// Root the projection's source paths are relative to.
    #[arg(long = "tree-source-root", env = "DEBUNDLE_TREE_SOURCE_ROOT")]
    pub tree_source_root: PathBuf,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
    /// Where to write the analysis artifact.
    #[arg(long)]
    pub out: PathBuf,
}

/// `debundle analyze`: compute the cross-module pass and every analysed
/// chunk's facts from a projection and the sources, and write them as an
/// analysis artifact for `debundle run --analysis`.
pub fn analyze_cli(args: AnalyzeArgs, analysis_engine_sha256: &str) -> Result<()> {
    let projection_bytes = fs::read(&args.projection)
        .with_context(|| format!("read analysis projection {}", args.projection.display()))?;
    let spec = AnalysisSpec::decode(&projection_bytes)?;
    anyhow::ensure!(
        spec.encode()? == projection_bytes,
        "{} is not a canonical analysis projection",
        args.projection.display()
    );
    let package_roots: HashMap<String, PathBuf> = collect_package_roots(args.package_roots)?
        .into_iter()
        .collect();
    let source_root = graph_provenance::absolute(&args.tree_source_root)?;
    let mut packages = BTreeMap::new();
    for (name, root) in &package_roots {
        packages.insert(name.clone(), graph_provenance::absolute(root)?);
    }
    if let Some(root) = &args.packages_root {
        packages.insert(
            "__packages_root__".to_string(),
            graph_provenance::absolute(root)?,
        );
    }
    let projection_dir = graph_provenance::absolute(
        args.projection
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new(".")),
    )?;
    let capture = InputCapture::start(
        ProvenanceRoots {
            spec: projection_dir,
            source: source_root.clone(),
            packages,
        },
        None,
        std::env::args().collect(),
    )?;
    capture.set_source_root(&source_root)?;
    let inputs = spec.inputs_under(&source_root);
    let artifact = load_js_chunks(&inputs.input_root, &inputs.js_list_path)?;
    let embedded_loaders: BTreeMap<String, spec::BunLoader> = match &inputs.embedded_loaders_path {
        Some(path) => serde_json::from_str(&graph_provenance::read_source_to_string(path)?)
            .with_context(|| format!("parse embedded loaders {}", path.display()))?,
        None => BTreeMap::new(),
    };
    let vendor = spec.vendor_under(&source_root);
    let materialized: BTreeSet<String> = spec.analysed_chunks().iter().cloned().collect();
    let source = prepare_js_chunks(&materialized, &vendor, artifact)?;
    let vendor_plan = build_vendor_resolution_plan(
        source.program.artifact(),
        source.program.indexes(),
        &vendor,
        &VendorPlanOptions {
            package_roots: &package_roots,
            packages_root: &args.packages_root,
            output_manifest_path: None,
            // Plans where bundled-swap wrappers would be written; analysis
            // writes none and reads no path from them.
            output_wrapper_dir: Some(Path::new("vendors/generated")),
        },
    )?;
    let provenance = capture.finish_inputs()?;
    let bytes = super::analyze_program(
        source.program.into_artifact(),
        AnalyzeProgramInputs {
            spec: &spec,
            embedded_loaders: &embedded_loaders,
            vendor_plan: &vendor_plan,
            graph_provenance: &provenance,
            analysis_engine_sha256,
        },
    )?;
    fs::write(&args.out, bytes)
        .with_context(|| format!("write analysis artifact {}", args.out.display()))
}
