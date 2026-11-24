//! Export authored source-unit claims for consumers that provide their own checker.
use super::{TransformRequest, load_transform_spec_source};
use anyhow::{Context, Result, ensure};
use artifact::load_js_chunks;
use frontend_elaboration::{CanonicalOwnership, ClaimSelector, resolve_ownership};
use lowering::ownership_subset::OwnershipSubset;
use prepare_chunks::prepare_js_chunks;
use serde::{Deserialize, Serialize};
use spec::{ModulePath, SourceMatchClaim, TransformSpec};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::str::FromStr;

#[derive(Serialize)]
struct ResolvedPlacement<'a> {
    chunk_id: &'a str,
    source_sha256: &'a str,
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

/// Add source-backed ownership claims to one logical module in the exported chunk.
#[derive(Clone, Debug)]
pub struct ProposalFragment {
    owner: ModulePath,
    path: PathBuf,
}

impl FromStr for ProposalFragment {
    type Err = anyhow::Error;

    fn from_str(arg: &str) -> Result<Self> {
        let (raw, path) = arg.split_once('=').context("expected OWNER=YAML")?;
        let owner = ModulePath::parse(raw, "")?;
        ensure!(
            raw == owner.as_str(),
            "expected canonical proposal owner, got {raw:?}"
        );
        ensure!(!path.is_empty(), "missing fragment path for {owner}");
        Ok(Self {
            owner,
            path: path.into(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectorFragment {
    source_matches: Vec<SourceMatchClaim>,
}

/// Keep the tree's spelling (e.g. `scene/Shape`) and every existing field.
/// New owners follow the shared tree's priority, in fragment argument order.
fn assemble(spec: &mut TransformSpec, chunk: &str, fragments: &[ProposalFragment]) -> Result<()> {
    if fragments.is_empty() {
        return Ok(());
    }
    let modules = spec.logical_modules.entry(chunk.to_owned()).or_default();
    let order = spec.import_order.entry(chunk.to_owned()).or_default();
    let mut paths = BTreeMap::new();
    for raw in modules.keys() {
        let owner = ModulePath::parse(raw, "")?;
        ensure!(
            paths.insert(owner.clone(), raw.clone()).is_none(),
            "duplicate canonical owner {chunk}::{owner}"
        );
    }
    for ProposalFragment { owner, path } in fragments {
        let contents =
            std::fs::read(path).with_context(|| format!("read fragment {}", path.display()))?;
        let fragment: SelectorFragment = serde_yaml::from_slice(&contents).with_context(|| {
            format!(
                "parse selector fragment {} for {chunk}::{owner}",
                path.display()
            )
        })?;
        ensure!(
            !fragment.source_matches.is_empty(),
            "empty selector fragment {}",
            path.display()
        );
        let raw = paths.entry(owner.clone()).or_insert_with(|| {
            if !order.contains(owner) {
                order.push(owner.clone());
            }
            owner.to_string()
        });
        modules
            .entry(raw.clone())
            .or_default()
            .source_matches
            .extend(fragment.source_matches);
    }
    Ok(())
}

pub fn resolve_placement(
    cli: &TransformRequest,
    chunk: &str,
    subset: Option<&Path>,
    out: &Path,
    fragments: &[ProposalFragment],
    canonical_out: Option<&Path>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    ensure!(
        canonical_out.is_none() || subset.is_some(),
        "--canonical-out requires --ownership-subset"
    );
    ensure!(
        canonical_out != Some(out),
        "canonical and selected outputs must be distinct"
    );
    let mut spec = load_transform_spec_source(&cli.spec_source)?;
    let chunk_ids = spec.materialize_chunk_ids();
    ensure!(
        chunk_ids.contains(chunk),
        "unknown materialized chunk {chunk:?}"
    );
    assemble(&mut spec, chunk, fragments)?;
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
    let priority = spec
        .import_order
        .get(chunk)
        .map(Vec::as_slice)
        .unwrap_or_default();
    let view = |subset| placement_view(&canonical, chunk, &source_sha256, priority, subset);
    let selected = view(subset.as_ref())?;
    // Proposal inputs may live anywhere. Never truncate an authored file via
    // an output path (including symlinks or hard links) in these modes.
    let fresh = canonical_out.is_some() || !fragments.is_empty();
    if let Some(path) = canonical_out {
        let full = view(None)?;
        write_placement(path, &full, fresh)?;
    }
    write_placement(out, &selected, fresh)
}

fn write_placement(path: &Path, placement: &ResolvedPlacement<'_>, fresh: bool) -> Result<()> {
    let file = if fresh {
        std::fs::File::create_new(path)
    } else {
        std::fs::File::create(path)
    }
    .with_context(|| format!("open resolved placement output {}", path.display()))?;
    use std::io::Write;
    // Buffer the whole document, rather than issuing a syscall per JSON token.
    (&file)
        .write_all(&serde_json::to_vec(placement)?)
        .with_context(|| format!("write resolved placement {}", path.display()))
}

fn placement_view<'a>(
    canonical: &CanonicalOwnership,
    chunk: &'a str,
    source_sha256: &'a str,
    priority: &[ModulePath],
    subset: Option<&OwnershipSubset>,
) -> Result<ResolvedPlacement<'a>> {
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
    for claim in &canonical.claims {
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
        row.owner = Some(claim.module.clone());
        if let ClaimSelector::Member { binding, readable } = &claim.selector {
            ensure!(
                row.members
                    .insert(binding.clone(), readable.clone())
                    .is_none(),
                "duplicate resolved member on unit {}",
                row.ordinal
            );
        }
    }
    let mut import_order = Vec::new();
    let mut seen = BTreeSet::new();
    for path in priority {
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
    Ok(ResolvedPlacement {
        chunk_id: chunk,
        source_sha256,
        import_order,
        units,
    })
}
