//! Source/projection application service with explicit, resolved inputs.

use super::{AnalysisSpec, AnalyzeProgramInputs};
use anyhow::{Context, Result};
use artifact::load_js_chunks;
use graph_provenance::{InputCapture, ProvenanceRoots};
use prepare_chunks::prepare_js_chunks;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use vendor::{VendorPlanOptions, build_vendor_resolution_plan};

/// Resolved inputs for computing a reusable analysis artifact.
#[derive(Debug, Clone)]
pub struct AnalyzeRequest {
    pub projection: PathBuf,
    pub tree_source_root: PathBuf,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
    pub out: PathBuf,
}

/// `debundle analyze`: compute the cross-module pass and every analysed
/// chunk's facts from a projection and the sources, and write them as an
/// analysis artifact for `debundle run --analysis`.
pub fn analyze_from_projection(
    args: AnalyzeRequest,
    analysis_engine_sha256: &str,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    let projection_bytes = fs::read(&args.projection)
        .with_context(|| format!("read analysis projection {}", args.projection.display()))?;
    let spec = AnalysisSpec::decode(&projection_bytes)?;
    anyhow::ensure!(
        spec.encode()? == projection_bytes,
        "{} is not a canonical analysis projection",
        args.projection.display()
    );
    let package_roots = args.package_roots;
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
    // Match live preparation's provider retention using the canonical source-site
    // identities, without depending on authored selectors or materialization.
    let analysis_providers = spec.source_sites.source_sha256.keys().cloned().collect();
    let source = prepare_js_chunks(&materialized, &analysis_providers, &vendor, artifact)?;
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
            diagnostics,
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
