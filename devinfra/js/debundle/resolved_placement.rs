//! Export authored source-unit claims for consumers that provide their own checker.
use anyhow::{Context, Result, ensure};
use artifact::load_js_chunks;
use clap::Args as ClapArgs;
use frontend_elaboration::{CanonicalOwnership, ClaimSelector, resolve_ownership};
use ownership_subset::OwnershipSubset;
use prepare_chunks::prepare_js_chunks;
use serde::{Deserialize, Serialize};
use spec::annotations::SourceMatchClaim;
use spec::{ModulePath, TransformSpec};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use transform_inputs::{SpecSourceArgs, TransformSpecSource, load_transform_spec_source};

#[derive(Serialize)]
struct ResolvedPlacement {
    chunk_id: String,
    source_sha256: String,
    import_order: Vec<ModulePath>,
    units: Vec<ResolvedUnit>,
}

#[derive(Serialize)]
struct ResolvedPlacements<'a> {
    placements: &'a [ResolvedPlacement],
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

/// Add source-backed ownership claims to a module in a batch export.
#[derive(Clone, Debug)]
pub struct ChunkProposalFragment {
    chunk: String,
    fragment: ProposalFragment,
}

impl FromStr for ChunkProposalFragment {
    type Err = anyhow::Error;

    fn from_str(arg: &str) -> Result<Self> {
        let (chunk_owner, path) = arg.split_once('=').context("expected CHUNK::OWNER=YAML")?;
        let (chunk, owner) = chunk_owner
            .split_once("::")
            .context("expected CHUNK::OWNER=YAML")?;
        ensure!(
            !chunk.is_empty(),
            "missing chunk in proposal fragment {arg:?}"
        );
        let fragment = format!("{owner}={path}").parse()?;
        Ok(Self {
            chunk: chunk.to_owned(),
            fragment,
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
fn assemble(spec: &mut TransformSpec, chunk: &str, fragments: &[&ProposalFragment]) -> Result<()> {
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
    for fragment in fragments {
        let owner = &fragment.owner;
        let path = &fragment.path;
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

enum PlacementShape {
    Single,
    Batch,
}

#[derive(Debug, ClapArgs)]
struct PlacementOutputs {
    /// Retain only these canonical modules, member names and anonymous indices.
    #[arg(long)]
    ownership_subset: Option<PathBuf>,
    /// Also write the unfiltered view of this solve.
    #[arg(long, requires = "ownership_subset")]
    canonical_out: Option<PathBuf>,
    /// Destination JSON file.
    #[arg(long)]
    out: PathBuf,
}

impl PlacementOutputs {
    fn resolve_and_write(
        &self,
        source: &TransformSpecSource,
        chunks: &[String],
        fragments: &[(&str, &ProposalFragment)],
        shape: PlacementShape,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<()> {
        let subset = self.ownership_subset.as_deref();
        let canonical_out = self.canonical_out.as_deref();
        ensure!(
            canonical_out.is_none() || subset.is_some(),
            "--canonical-out requires --ownership-subset"
        );
        ensure!(
            canonical_out != Some(self.out.as_path()),
            "canonical and selected outputs must be distinct"
        );
        let (selected, canonical) = resolve_placements_inner(
            source,
            chunks,
            subset,
            fragments,
            canonical_out.is_some(),
            diagnostics,
        )?;
        // Proposal inputs may live anywhere. Never truncate an authored file via
        // an output path (including symlinks or hard links) in these modes.
        let fresh = canonical_out.is_some() || !fragments.is_empty();
        let write = |path: &Path, placements: &[ResolvedPlacement]| match shape {
            PlacementShape::Single => write_json(path, &placements[0], fresh),
            PlacementShape::Batch => write_json(path, &ResolvedPlacements { placements }, fresh),
        };
        if let Some(path) = canonical_out {
            write(
                path,
                &canonical.context("missing canonical placement output")?,
            )?;
        }
        write(&self.out, &selected)
    }
}

fn resolve_placements_inner(
    source: &TransformSpecSource,
    chunks: &[String],
    subset: Option<&Path>,
    fragments: &[(&str, &ProposalFragment)],
    include_canonical: bool,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<(Vec<ResolvedPlacement>, Option<Vec<ResolvedPlacement>>)> {
    ensure!(!chunks.is_empty(), "at least one --chunk is required");
    let mut unique_chunks = BTreeSet::new();
    for chunk in chunks {
        ensure!(
            unique_chunks.insert(chunk.clone()),
            "duplicate requested chunk {chunk:?}"
        );
    }

    let mut spec = load_transform_spec_source(source)?;
    let chunk_ids = spec.materialize_chunk_ids();
    let materialized = chunk_ids.iter().cloned().collect::<BTreeSet<_>>();
    for chunk in chunks {
        ensure!(
            materialized.contains(chunk),
            "unknown materialized chunk {chunk:?}"
        );
    }
    for (chunk, _) in fragments {
        ensure!(
            unique_chunks.contains(*chunk),
            "proposal fragment targets unrequested chunk {chunk:?}"
        );
    }
    for chunk in chunks {
        let chunk_fragments = fragments
            .iter()
            .filter(|(fragment_chunk, _)| *fragment_chunk == chunk.as_str())
            .map(|(_, fragment)| *fragment)
            .collect::<Vec<_>>();
        assemble(&mut spec, chunk, &chunk_fragments)?;
    }

    let loaded = load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    // Hash the actual bytes supplied to SWC. SourceMap can normalize a UTF-8 BOM.
    let mut source_hashes = BTreeMap::new();
    for chunk in chunks {
        let id = loaded
            .chunk_table
            .get(chunk)
            .with_context(|| format!("missing input chunk {chunk}"))?;
        let source = loaded.chunks.get(id.0).context("missing chunk input")?;
        let hash = graph_provenance::sha256(
            source
                .get_file(&source.entry_file)
                .and_then(|f| f.source())
                .context("missing chunk source")?
                .as_bytes(),
        );
        source_hashes.insert(chunk.clone(), hash);
    }
    let source = prepare_js_chunks(&chunk_ids, &BTreeSet::new(), &BTreeMap::new(), loaded)?;
    let mut resolved = resolve_ownership(source.artifact(), &spec, diagnostics)?;

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

    let mut selected = Vec::with_capacity(chunks.len());
    let mut canonical = include_canonical.then(|| Vec::with_capacity(chunks.len()));
    for chunk in chunks {
        let ownership = resolved
            .remove(chunk)
            .with_context(|| format!("chunk {chunk} lacks canonical ownership"))?;
        let source_sha256 = source_hashes
            .remove(chunk)
            .context("missing chunk source hash")?;
        let priority = spec
            .import_order
            .get(chunk)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let selection =
            placement_view(&ownership, chunk, &source_sha256, priority, subset.as_ref())?;
        if let Some(canonical) = &mut canonical {
            canonical.push(placement_view(
                &ownership,
                chunk,
                &source_sha256,
                priority,
                None,
            )?);
        }
        selected.push(selection);
    }
    Ok((selected, canonical))
}

fn write_json<T: Serialize>(path: &Path, value: &T, fresh: bool) -> Result<()> {
    let file = if fresh {
        std::fs::File::create_new(path)
    } else {
        std::fs::File::create(path)
    }
    .with_context(|| format!("open resolved placement output {}", path.display()))?;
    use std::io::Write;
    // Buffer the whole document, rather than issuing a syscall per JSON token.
    (&file)
        .write_all(&serde_json::to_vec(value)?)
        .with_context(|| format!("write resolved placement {}", path.display()))
}

fn placement_view(
    canonical: &CanonicalOwnership,
    chunk: &str,
    source_sha256: &str,
    priority: &[ModulePath],
    subset: Option<&OwnershipSubset>,
) -> Result<ResolvedPlacement> {
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
        chunk_id: chunk.to_owned(),
        source_sha256: source_sha256.to_owned(),
        import_order,
        units,
    })
}

/// Export one chunk's resolved ownership without invoking a split backend.
#[derive(Debug, ClapArgs)]
pub struct ResolvePlacementArgs {
    #[command(flatten)]
    context: SpecSourceArgs,
    /// Materialized chunk ID from the canonical spec.
    #[arg(long)]
    chunk: String,
    /// Append a source_matches-only fragment to OWNER in this chunk; repeat for multiple files.
    #[arg(long, value_name = "OWNER=YAML")]
    proposal_fragment: Vec<ProposalFragment>,
    #[command(flatten)]
    output: PlacementOutputs,
}

impl ResolvePlacementArgs {
    pub fn run(self, stderr: &mut (dyn std::io::Write + Send)) -> Result<()> {
        command_diagnostics::run(stderr, |diagnostics| {
            let fragments = self
                .proposal_fragment
                .iter()
                .map(|fragment| (self.chunk.as_str(), fragment))
                .collect::<Vec<_>>();
            self.output.resolve_and_write(
                &self.context.resolve()?,
                std::slice::from_ref(&self.chunk),
                &fragments,
                PlacementShape::Single,
                diagnostics,
            )
        })
    }
}

/// Export several chunks' resolved ownership with one shared source preparation and selector solve.
#[derive(Debug, ClapArgs)]
pub struct ResolvePlacementsArgs {
    #[command(flatten)]
    context: SpecSourceArgs,
    /// Materialized chunk IDs from the canonical spec; repeat to export a batch.
    #[arg(long, required = true, value_name = "CHUNK")]
    chunk: Vec<String>,
    /// Append a source_matches-only fragment to CHUNK::OWNER in this batch; repeat for multiple files.
    #[arg(long, value_name = "CHUNK::OWNER=YAML")]
    proposal_fragment: Vec<ChunkProposalFragment>,
    #[command(flatten)]
    output: PlacementOutputs,
}

impl ResolvePlacementsArgs {
    pub fn run(self, stderr: &mut (dyn std::io::Write + Send)) -> Result<()> {
        command_diagnostics::run(stderr, |diagnostics| {
            let fragments = self
                .proposal_fragment
                .iter()
                .map(|fragment| (fragment.chunk.as_str(), &fragment.fragment))
                .collect::<Vec<_>>();
            self.output.resolve_and_write(
                &self.context.resolve()?,
                &self.chunk,
                &fragments,
                PlacementShape::Batch,
                diagnostics,
            )
        })
    }
}
