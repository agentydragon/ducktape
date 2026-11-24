//! Export authored source-unit claims for consumers that provide their own checker.
use super::{TransformRequest, load_transform_spec_source};
use anyhow::{Context, Result, ensure};
use artifact::load_js_chunks;
use frontend_elaboration::{ClaimSelector, resolve_ownership};
use lowering::ownership_subset::OwnershipSubset;
use prepare_chunks::prepare_js_chunks;
use serde::Serialize;
use spec::ModulePath;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

#[derive(Serialize)]
struct ResolvedPlacement<'a> {
    chunk_id: &'a str,
    source_sha256: String,
    import_order: Vec<ModulePath>,
    units: Vec<ResolvedUnit>,
}

#[derive(Serialize)]
struct ResolvedUnit {
    ordinal: usize,
    owner: Option<ModulePath>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    members: BTreeMap<String, String>,
}

pub fn resolve_placement(
    cli: &TransformRequest,
    chunk: &str,
    subset: Option<&Path>,
    out: &Path,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let chunk_ids = spec.materialize_chunk_ids();
    ensure!(
        chunk_ids.contains(chunk),
        "unknown materialized chunk {chunk:?}"
    );
    let loaded = load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    // Hash the actual bytes supplied to SWC. SourceMap can normalize a UTF-8 BOM.
    let id = loaded
        .chunk_table
        .get(chunk)
        .with_context(|| format!("missing input chunk {chunk}"))?;
    let source = loaded.chunks.get(id.0).context("missing chunk input")?;
    let source_sha256 = graph_provenance::sha256(
        source
            .get_file(&source.entry_file)
            .and_then(|f| f.source())
            .context("missing chunk source")?
            .as_bytes(),
    );
    let prepared = prepare_js_chunks(&chunk_ids, &BTreeSet::new(), &BTreeMap::new(), loaded)?;
    let mut resolved = resolve_ownership(prepared.program.artifact(), &spec, diagnostics)?;
    let canonical = resolved
        .remove(chunk)
        .context("chunk lacks canonical ownership")?;

    let subset = subset.map(OwnershipSubset::read).transpose()?;
    if let Some(view) = &subset {
        ensure!(
            view.placements.is_empty(),
            "resolve-placement selects canonical owners; fresh placements are not supported"
        );
        view.validate(
            &spec.logical_modules,
            &chunk_ids.into_iter().collect::<Vec<_>>(),
        )?;
    }
    let keep = subset.map(|view| {
        view.modules_for_chunk(chunk)
            .into_iter()
            .map(|m| (m.module.clone(), m))
            .collect::<BTreeMap<_, _>>()
    });
    let mut units: Vec<_> = (0..canonical.unit_count)
        .map(|ordinal| ResolvedUnit {
            ordinal,
            owner: None,
            members: BTreeMap::new(),
        })
        .collect();
    let mut owners = BTreeSet::new();
    for claim in canonical.claims {
        if let Some(keep) = &keep {
            let Some(module) = keep.get(&claim.module) else {
                continue;
            };
            let selected = match &claim.selector {
                ClaimSelector::Member { readable, .. } => module
                    .bindings
                    .as_ref()
                    .is_none_or(|names| names.contains(readable)),
                ClaimSelector::Anonymous(i) => module
                    .anonymous_statements
                    .as_ref()
                    .is_none_or(|indices| indices.contains(i)),
            };
            if !selected {
                continue;
            }
        }
        let row = &mut units[claim.ordinal.0];
        owners.insert(claim.module.clone());
        row.owner = Some(claim.module);
        if let ClaimSelector::Member { binding, readable } = claim.selector {
            ensure!(
                row.members.insert(binding, readable).is_none(),
                "duplicate resolved member on unit {}",
                row.ordinal
            );
        }
    }
    let mut import_order = Vec::new();
    let mut seen = BTreeSet::new();
    for path in spec.import_order.get(chunk).into_iter().flatten() {
        ensure!(
            seen.insert(path),
            "duplicate authored import priority {chunk}::{path}"
        );
        if owners.remove(path) {
            import_order.push(path.clone());
        }
    }
    ensure!(
        owners.is_empty(),
        "retained owners absent from authored import priority in {chunk}: {owners:?}"
    );
    let result = ResolvedPlacement {
        chunk_id: chunk,
        source_sha256,
        import_order,
        units,
    };
    std::fs::write(out, serde_json::to_vec(&result)?)
        .with_context(|| format!("write resolved placement {}", out.display()))
}
