//! Mutating + listing operations on the spec's per-module member
//! entries: `bindings list`, `bindings rename`, `bindings assign`,
//! `bindings unassign`.
//!
//! The shared invariants:
//!
//! * `<sym>` accepts either the minified `selector.binding.name` form
//!   or the readable `name:` form. If both forms could match different
//!   members, the operation refuses with a structured list.
//! * Mutating commands validate-by-default (atomic post-batch state)
//!   and refuse on collision / atom-split rejection.
//! * Edits use the canonical module schema; changed YAML is reserialized.
//!
//! The per-command contract lives in the clap doc-comments
//! (`cli/bindings_commands.rs`); cross-command semantics (batch atomicity, rejection
//! diagnostics) in `docs/cli.md`.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use serde::Deserialize;

use spec::{
    BindingAnnotation, LogicalModule, Member, ModulePath, SourceMatchBinding,
    SourceMatchBindingDetail, is_residual_module_path,
};
use spec_modules::{collect_module_files, module_path_from_file};
use yaml_edit::{read_yaml, write_yaml_atomic};

use crate::edit_gate::{Gate, post_edit_spec_from_docs};
use crate::outcome::{GateOutcome, MutationOutcome};

/// A chunk-top binding's public identity: the minified hygiene name
/// (`selector.binding.name`, e.g. `_ab`) plus an optional readable
/// `name:` the spec assigns (e.g. `parseUserId`).
///
/// Serializes internally-tagged so CLI JSON consumers can branch on
/// `.kind` (`"minified"` | `"readable"`) and always read `.minified`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BindingName {
    /// No readable name yet — still the minified hygiene identity.
    Minified { minified: String },
    /// Renamed: carries both the minified anchor and the readable name.
    Readable { minified: String, name: String },
}

impl BindingName {
    pub fn new(minified: String, readable: Option<String>) -> Self {
        match readable {
            Some(name) => Self::Readable { minified, name },
            None => Self::Minified { minified },
        }
    }

    /// The minified hygiene name (always present).
    pub fn minified(&self) -> &str {
        match self {
            Self::Minified { minified } | Self::Readable { minified, .. } => minified,
        }
    }

    /// The readable name, if one was assigned.
    pub fn readable(&self) -> Option<&str> {
        match self {
            Self::Minified { .. } => None,
            Self::Readable { name, .. } => Some(name),
        }
    }

    pub fn is_renamed(&self) -> bool {
        matches!(self, Self::Readable { .. })
    }

    /// True when `query` matches either the minified or readable
    /// spelling — the CLI's `<sym>` lookup rule.
    pub fn matches(&self, query: &str) -> bool {
        self.minified() == query || self.readable() == Some(query)
    }
}

/// A located member inside a module file. Returned by [`find_matches`]
/// and is the unit `assign` / `rename` inspect or mutate.
#[derive(Debug, Clone)]
pub struct BindingMatch {
    pub file: PathBuf,
    pub module_path: String,
    pub location: BindingLocation,
    pub name: BindingName,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum BindingLocation {
    Member {
        member_index: usize,
    },
    SourceMatch {
        claim_index: usize,
        binding_index: usize,
    },
}

impl BindingLocation {
    pub fn describe(&self) -> String {
        match self {
            Self::Member { member_index } => format!("members[{member_index}]"),
            Self::SourceMatch {
                claim_index,
                binding_index,
            } => format!("source_matches[{claim_index}].bindings[{binding_index}]"),
        }
    }
}

type ModuleDocs = BTreeMap<String, (PathBuf, LogicalModule)>;

/// Load one planning snapshot for resolution, collision checks, and edits.
/// Batch persistence compares against this same pre-edit snapshot.
pub fn load_module_docs(modules_root: &Path) -> Result<ModuleDocs> {
    let mut docs = BTreeMap::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_module_doc(&file)?;
        docs.insert(module_path, (file, doc));
    }
    Ok(docs)
}

/// Normalize empty YAML and parse the same schema the pipeline consumes.
pub(crate) fn read_module_doc(file: &Path) -> Result<LogicalModule> {
    serde_yaml::from_value(read_yaml(file)?)
        .with_context(|| format!("parsing module {}", file.display()))
}

/// Compare typed semantics so omitted defaults do not rewrite unrelated files.
fn apply_module_edit(
    file: &Path,
    original: Option<&LogicalModule>,
    doc: &LogicalModule,
    dry_run: bool,
) -> Result<bool> {
    let value = serde_yaml::to_value(doc)?;
    if original.map(serde_yaml::to_value).transpose()?.as_ref() == Some(&value) {
        return Ok(false);
    }
    if !dry_run {
        write_yaml_atomic(file, &value)?;
    }
    Ok(true)
}

fn binding_matches_in_doc(
    file: &Path,
    module_path: &str,
    doc: &LogicalModule,
) -> Vec<BindingMatch> {
    let mut out = Vec::new();
    let mut add = |location, name| {
        out.push(BindingMatch {
            file: file.to_path_buf(),
            module_path: module_path.to_string(),
            location,
            name,
        })
    };
    for (member_index, member) in doc.members.iter().enumerate() {
        let minified = member_minified_name(member);
        if minified.is_some() || member.name.is_some() {
            add(
                BindingLocation::Member { member_index },
                BindingName::new(minified.unwrap_or_default(), member.name.clone()),
            );
        }
    }
    for (claim_index, claim) in doc.source_matches.iter().enumerate() {
        for (binding_index, binding) in claim.bindings.iter().enumerate() {
            if !binding.local().is_empty() {
                add(
                    BindingLocation::SourceMatch {
                        claim_index,
                        binding_index,
                    },
                    BindingName::new(binding.local().to_string(), source_match_readable(binding)),
                );
            }
        }
    }
    out
}

fn binding_effective_name(name: &BindingName) -> &str {
    name.readable().unwrap_or_else(|| name.minified())
}

fn source_match_readable(binding: &SourceMatchBinding) -> Option<String> {
    match binding {
        SourceMatchBinding::Local(_) => None,
        SourceMatchBinding::Detailed(detail) => detail.name.clone().filter(|name| !name.is_empty()),
    }
}

/// Find all members matching either the minified binding name or readable `name:`.
pub fn find_matches(modules_root: &Path, sym: &str) -> Result<Vec<BindingMatch>> {
    Ok(matches_in_docs(&load_module_docs(modules_root)?, sym))
}

fn matches_in_docs(docs: &ModuleDocs, sym: &str) -> Vec<BindingMatch> {
    docs.iter()
        .flat_map(|(module, (file, doc))| binding_matches_in_doc(file, module, doc))
        .filter(|binding| binding.name.matches(sym))
        .collect()
}

/// Resolve a single unambiguous match for `sym`, or bail with the
/// canonical structured-list error message.
pub fn resolve_unambiguous(modules_root: &Path, sym: &str) -> Result<BindingMatch> {
    resolve_in_docs(modules_root, &load_module_docs(modules_root)?, sym)
}

fn resolve_in_docs(modules_root: &Path, docs: &ModuleDocs, sym: &str) -> Result<BindingMatch> {
    let matches = matches_in_docs(docs, sym);
    match matches.len() {
        0 => bail!(
            "no binding named \"{sym}\" found under {}",
            modules_root.display()
        ),
        1 => Ok(matches.into_iter().next().unwrap()),
        _ => {
            let locations: Vec<String> = matches
                .iter()
                .map(|m| {
                    format!(
                        "  {}#{} (binding={}, name={})",
                        m.file.display(),
                        m.location.describe(),
                        m.name.minified(),
                        m.name.readable().unwrap_or("-")
                    )
                })
                .collect();
            bail!(
                "ambiguous binding identifier \"{sym}\": {} matches:\n{}",
                matches.len(),
                locations.join("\n")
            );
        }
    }
}

// ---------------------------------------------------------------------
// `bindings list`
// ---------------------------------------------------------------------

#[derive(Debug, Clone, serde::Serialize)]
pub struct BindingsListReport {
    pub bindings: Vec<BindingEntry>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct BindingEntry {
    /// The binding's identity (minified, plus readable name if set).
    /// Flattened into the entry, so JSON carries `kind`/`minified`/
    /// (`name`) alongside `module`/`orphan`. The renamed/unrenamed
    /// distinction is `name.kind`; there is no separate `unrenamed`
    /// bool (it was a redundant restatement of `kind == "minified"`).
    #[serde(flatten)]
    pub name: BindingName,
    pub module: String,
    /// `true` when this binding is the only member of its module.
    pub orphan: bool,
}

#[derive(Debug, Clone, Default)]
pub struct BindingsListFilters {
    pub in_module: Option<String>,
    pub unrenamed: bool,
    pub orphan: bool,
}

pub fn run_bindings_list(
    modules_root: &Path,
    filters: &BindingsListFilters,
) -> Result<BindingsListReport> {
    let mut entries: Vec<BindingEntry> = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_module_doc(&file)?;
        let bindings = binding_matches_in_doc(&file, &module_path, &doc);
        let orphan = bindings.len() == 1;
        entries.extend(bindings.into_iter().map(|binding| BindingEntry {
            name: binding.name,
            module: module_path.clone(),
            orphan,
        }));
    }
    entries.retain(|e| {
        (filters.in_module.as_deref().is_none_or(|m| e.module == m))
            && (!filters.unrenamed || !e.name.is_renamed())
            && (!filters.orphan || e.orphan)
    });
    entries.sort_by(|a, b| {
        (a.module.as_str(), a.name.minified()).cmp(&(b.module.as_str(), b.name.minified()))
    });
    Ok(BindingsListReport { bindings: entries })
}

// ---------------------------------------------------------------------
// `bindings rename`
// ---------------------------------------------------------------------

/// Outcome of a rename. Shares the [`MutationOutcome`] core with the
/// other four mutating verbs; the touched file (when any) is
/// `outcome.files_written`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct RenameOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    /// Minified binding name of the renamed member.
    pub binding: String,
    pub old_readable: Option<String>,
    pub new_readable: String,
}

/// Rename a single binding's readable `name:` without moving it.
///
/// `original` accepts the minified or current readable form. `new`
/// must not collide with any other binding's readable name in the
/// chunk (unless `no_verify`).
pub fn rename_binding(
    modules_root: &Path,
    original: &str,
    new: &str,
    dry_run: bool,
    no_verify: bool,
) -> Result<RenameOutcome> {
    if original.contains(':') || new.contains(':') {
        bail!(
            "neither <original> nor <readable> may contain `:`; use --batch JSON for edge \
             cases"
        );
    }
    let mut docs = load_module_docs(modules_root)?;
    let hit = resolve_in_docs(modules_root, &docs, original)?;
    if !no_verify {
        let clashes = find_readable_collisions(&docs, new, &hit.file, &hit.location);
        if !clashes.is_empty() {
            bail!(
                "name collision: \"{new}\" already used by:\n{}",
                clashes.join("\n")
            );
        }
    }
    let (_, doc) = docs
        .get_mut(&hit.module_path)
        .expect("resolved module is loaded");
    let original_doc = doc.clone();
    let old_readable = hit.name.readable().map(str::to_string);
    let old_effective = old_readable
        .clone()
        .unwrap_or_else(|| hit.name.minified().to_string());
    let annotation = doc.annotations.remove(&old_effective);
    set_readable_name(doc, &hit.location, new);
    insert_annotation(doc, new, annotation)?;
    let changed = apply_module_edit(&hit.file, Some(&original_doc), doc, dry_run)?;
    let action = if !changed {
        "unchanged"
    } else if dry_run {
        "dry-run"
    } else {
        "applied"
    };
    Ok(RenameOutcome {
        outcome: MutationOutcome {
            verb: "rename",
            action,
            gate: if no_verify {
                GateOutcome::Skipped
            } else {
                GateOutcome::NamesOnly
            },
            files_written: if changed {
                vec![hit.file.display().to_string()]
            } else {
                Vec::new()
            },
            files_deleted: Vec::new(),
        },
        binding: hit.name.minified().to_string(),
        old_readable,
        new_readable: new.to_string(),
    })
}

fn find_readable_collisions(
    docs: &ModuleDocs,
    new_readable: &str,
    self_file: &Path,
    self_location: &BindingLocation,
) -> Vec<String> {
    let mut clashes = Vec::new();
    for (module_path, (file, doc)) in docs {
        for binding in binding_matches_in_doc(file, module_path, doc) {
            if file == self_file && &binding.location == self_location {
                continue;
            }
            if binding_effective_name(&binding.name) == new_readable {
                clashes.push(format!(
                    "  {}#{} (binding={}, module={})",
                    file.display(),
                    binding.location.describe(),
                    binding.name.minified(),
                    module_path
                ));
            }
        }
    }
    clashes
}

// ---------------------------------------------------------------------
// `bindings assign`
// ---------------------------------------------------------------------

/// One requested move: optionally with a new readable name.
#[derive(Debug, Clone, Deserialize)]
pub struct Move {
    pub sym: String,
    pub module: String,
    #[serde(default)]
    pub readable: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ProposalBatch {
    proposals: Vec<BatchProposal>,
}

#[derive(Debug, Deserialize)]
struct BatchProposal {
    proposed_module_id: String,
    #[serde(default)]
    binding_ids: Vec<String>,
    #[serde(default)]
    anonymous_statement_owner_ids: Vec<String>,
    #[serde(default)]
    landable_today: bool,
    #[serde(default)]
    extends_module_id: Option<String>,
    #[serde(default)]
    merge_into: Option<Vec<String>>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct AssignOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub moves_applied: usize,
}

/// Parse a positional `<sym>:<module>[:<readable>]` triple.
pub fn parse_move_triple(s: &str) -> Result<Move> {
    let parts: Vec<&str> = s.splitn(3, ':').collect();
    if parts.len() < 2 {
        bail!("expected `<sym>:<module>[:<readable>]`, got {s:?} (one colon at minimum)");
    }
    // splitn(3) leaves any further `:` inside the third field; the
    // documented contract (same as `bindings rename`) is that
    // `<readable>` may not contain `:`.
    if let Some(readable) = parts.get(2)
        && readable.contains(':')
    {
        bail!("<readable> may not contain `:` (got {s:?}); use --batch JSON for edge cases");
    }
    Ok(Move {
        sym: parts[0].to_string(),
        module: parts[1].to_string(),
        readable: parts.get(2).map(|s| s.to_string()),
    })
}

/// Parse `--batch <file>` JSON.
///
/// Accepted shapes:
///   * a top-level array of `{sym, module, readable?}` objects
///   * `modules propose --format json` output, when every selected
///     proposal is a binding-only fresh/extension proposal
///   * a top-level array of those proposal objects, e.g. from a `jq`
///     `.proposals[]` filter
pub fn parse_batch_json(text: &str) -> Result<Vec<Move>> {
    // Try the simple array shape first.
    if let Ok(moves) = serde_json::from_str::<Vec<Move>>(text) {
        return Ok(moves);
    }
    if let Ok(batch) = serde_json::from_str::<ProposalBatch>(text) {
        return proposal_batch_to_moves(batch.proposals);
    }
    if let Ok(proposals) = serde_json::from_str::<Vec<BatchProposal>>(text) {
        return proposal_batch_to_moves(proposals);
    }
    bail!(
        "--batch JSON must be a top-level array of {{sym, module, readable?}} objects, \
         `modules propose --format json` output, or an array of proposal objects"
    );
}

fn proposal_batch_to_moves(proposals: Vec<BatchProposal>) -> Result<Vec<Move>> {
    let mut moves = Vec::new();
    let mut rejected = Vec::new();

    for proposal in proposals {
        match proposal_to_moves(proposal) {
            Ok(mut proposal_moves) => moves.append(&mut proposal_moves),
            Err((id, reason)) => rejected.push(format!("{id}: {reason}")),
        }
    }

    if !rejected.is_empty() {
        bail!(
            "--batch modules-propose JSON contains proposals that `bindings assign` cannot apply \
             directly:\n  - {}\nSelect only landable binding-only fresh/extension proposals; handle \
             `merge_into` / `anonymous_statement_owner_ids` rows with `modules merge` or manual YAML, \
             and grow `blocked_residual_dependency` rows to a landable closure (or co-locate the \
             referenced cells manually) first.",
            rejected.join("\n  - ")
        );
    }
    if moves.is_empty() {
        bail!(
            "--batch modules-propose JSON did not contain any binding moves; select proposals with \
             non-empty `binding_ids`"
        );
    }
    Ok(moves)
}

fn proposal_to_moves(proposal: BatchProposal) -> std::result::Result<Vec<Move>, (String, String)> {
    let id = proposal.proposed_module_id.clone();
    if !proposal.landable_today {
        return Err((id, "`landable_today` is false".to_string()));
    }
    if let Some(merge_into) = &proposal.merge_into {
        return Err((
            id,
            format!(
                "`merge_into` proposals merge existing modules ({}) and are not member moves",
                merge_into.join(", ")
            ),
        ));
    }
    if !proposal.anonymous_statement_owner_ids.is_empty() {
        return Err((
            id,
            format!(
                "contains anonymous statements ({}) but `bindings assign` only moves members",
                proposal.anonymous_statement_owner_ids.join(", ")
            ),
        ));
    }
    if proposal.binding_ids.is_empty() {
        return Err((id, "no `binding_ids` to move".to_string()));
    }

    let module = proposal
        .extends_module_id
        .unwrap_or(proposal.proposed_module_id);
    Ok(proposal
        .binding_ids
        .into_iter()
        .map(|sym| Move {
            sym,
            module: module.clone(),
            readable: None,
        })
        .collect())
}

/// Plan a batch from one module snapshot, validate collisions + realizability +
/// atom-split, then write destinations before deleting drained sources. Each
/// file replacement is atomic; the multi-file write is not a transaction.
/// Remaining semantic content prevents source deletion (see below).
///
/// Contract:
///   * Moves are deduplicated on **resolved member identity** (the
///     member's source file + index): a batch carrying both the
///     minified and readable spelling of one member collapses to a
///     single move. Two moves for the same member with contradictory
///     destinations or readable names are rejected.
///   * Destination module paths are canonicalized via
///     [`spec::ModulePath::parse`] (lowercased), so `UI/Widgets` and
///     `ui/widgets` resolve to the same file.
///   * Destination modules are auto-created.
///   * Only modules that were sources of a move in THIS batch are
///     swept after draining, and only when they have no module-level
///     `comment:` or `note:`, no remaining `source_matches:`, `annotations:`, or
///     `anonymous_statements:`.
///   * [`Gate::Run`] runs the unified realizability gate
///     ([`crate::edit_gate::gate_post_edit_partition`]) against the
///     in-memory post-batch spec; cycle or atom-split rejections
///     bail before any file is written. [`Gate::Skip`] (`--no-verify`)
///     skips validation. The CLI dispatcher requires `--graph` unless
///     `--no-verify` is set ([`Gate::from_cli`]).
pub fn run_bindings_assign(
    modules_root: &Path,
    moves: Vec<Move>,
    dry_run: bool,
    gate: Gate<'_>,
) -> Result<AssignOutcome> {
    if moves.is_empty() {
        return Ok(AssignOutcome {
            outcome: MutationOutcome {
                verb: "assign",
                action: "noop",
                gate: GateOutcome::NotRequired,
                files_written: Vec::new(),
                files_deleted: Vec::new(),
            },
            moves_applied: 0,
        });
    }

    // Preserve the planning snapshot for semantic no-op detection at apply time.
    let original_docs = load_module_docs(modules_root)?;
    let mut docs = original_docs.clone();
    // Step 1: locate each move's member and canonicalize its
    // destination. Identity is the resolved (source module, member
    // index) slot: `<sym>` accepts both the minified and readable
    // spelling, so a raw-string dedupe would let both spellings of
    // one member produce two plan entries for a single extraction slot.
    let mut by_identity: BTreeMap<(String, usize), PlannedMove> = BTreeMap::new();
    for m in moves {
        let hit = resolve_in_docs(modules_root, &docs, &m.sym)?;
        let source_index = match &hit.location {
            BindingLocation::Member { member_index } => *member_index,
            BindingLocation::SourceMatch { .. } => {
                bail_source_match_split("bindings assign", &hit)?
            }
        };
        let dest_module = canonical_module_path(&m.module)?;
        let planned = PlannedMove {
            req: Move {
                sym: m.sym,
                module: dest_module,
                readable: m.readable,
            },
            source_module: hit.module_path.clone(),
            source_index,
        };
        match by_identity.entry((hit.module_path, source_index)) {
            std::collections::btree_map::Entry::Vacant(slot) => {
                slot.insert(planned);
            }
            std::collections::btree_map::Entry::Occupied(mut slot) => {
                let prev = slot.get_mut();
                if prev.req.module != planned.req.module {
                    bail!(
                        "batch contains contradictory destinations for the same member: {:?} \
                         -> {:?} vs {:?} -> {:?}",
                        prev.req.sym,
                        prev.req.module,
                        planned.req.sym,
                        planned.req.module,
                    );
                }
                match (&prev.req.readable, &planned.req.readable) {
                    (Some(a), Some(b)) if a != b => bail!(
                        "batch contains contradictory readable names for the same member: \
                         {:?} -> {:?} vs {:?} -> {:?}",
                        prev.req.sym,
                        a,
                        planned.req.sym,
                        b,
                    ),
                    (None, Some(_)) => prev.req.readable = planned.req.readable,
                    _ => {}
                }
                eprintln!(
                    "warning: batch contains duplicate moves for one member ({:?} / {:?}); \
                     collapsed",
                    prev.req.sym, planned.req.sym,
                );
            }
        }
    }
    let plan: Vec<PlannedMove> = by_identity.into_values().collect();
    // Step 3: extract each source module's selected members in one pass,
    // preserving original indices until every identity has been resolved.
    let mut extracted = take_members(
        &mut docs,
        plan.iter()
            .map(|p| (p.source_module.clone(), p.source_index)),
    );
    let mut pulled: BTreeMap<String, Member> = BTreeMap::new();
    let mut pulled_annotations: BTreeMap<String, (String, Option<BindingAnnotation>)> =
        BTreeMap::new();
    for p in &plan {
        let mut member = extracted
            .remove(&(p.source_module.clone(), p.source_index))
            .expect("resolved member is loaded");
        let old_effective = member_effective_name(&member)
            .with_context(|| format!("member {:?} has no effective binding name", p.req.sym))?;
        let (_, doc) = docs
            .get_mut(&p.source_module)
            .expect("resolved module is loaded");
        let annotation = doc.annotations.remove(&old_effective);
        if let Some(new_readable) = &p.req.readable {
            member.name = Some(new_readable.clone());
        }
        let new_effective = member_effective_name(&member)
            .with_context(|| format!("member {:?} has no effective binding name", p.req.sym))?;
        pulled_annotations.insert(p.req.sym.clone(), (new_effective, annotation));
        pulled.insert(p.req.sym.clone(), member);
    }

    // Step 4: collision detection for renames, sharing the same
    // effective-identity predicate `bindings rename` uses: an
    // unrenamed member's minified binding name is its public
    // identity, so a rename target that matches it is a clash.
    if gate.verify_names() {
        for p in &plan {
            let Some(new_readable) = &p.req.readable else {
                continue;
            };
            // The check is against the post-state docs (the moved
            // members sit in `pulled`, so no self-exclusion needed).
            let mut hits = Vec::new();
            for (mp, (file, doc)) in &docs {
                for binding in binding_matches_in_doc(file, mp, doc) {
                    if binding
                        .name
                        .readable()
                        .filter(|name| !name.is_empty())
                        .unwrap_or_else(|| binding.name.minified())
                        == new_readable
                    {
                        hits.push(format!(
                            "  {} ({}@{})",
                            file.display(),
                            mp,
                            binding.location.describe()
                        ));
                    }
                }
            }
            // Also check the pulled bin: another move might carry the
            // same effective name to a different destination.
            let pulled_hits = pulled
                .iter()
                .filter(|(other_sym, _)| *other_sym != &p.req.sym)
                .filter(|(_, member)| {
                    member_effective_name(member).as_deref() == Some(new_readable)
                })
                .count();
            if !hits.is_empty() || pulled_hits > 0 {
                bail!(
                    "name collision: rename of {:?} -> {:?} collides with existing entries:\n\
                     {} (and {} pending in this batch)",
                    p.req.sym,
                    new_readable,
                    hits.join("\n"),
                    pulled_hits
                );
            }
        }
    }

    // Step 5: splice into destinations (auto-create missing).
    for p in &plan {
        let dest_path = p.req.module.clone();
        if !docs.contains_key(&dest_path) {
            let dest_file = modules_root.join(format!("{dest_path}.yaml"));
            docs.insert(dest_path.clone(), (dest_file, LogicalModule::default()));
        }
        let member = pulled.remove(&p.req.sym).expect("pulled member missing");
        let (export_name, annotation) = pulled_annotations
            .remove(&p.req.sym)
            .expect("pulled annotation missing");
        let (_, doc) = docs.get_mut(&dest_path).expect("dest just created");
        doc.members.push(member);
        insert_annotation(doc, &export_name, annotation)?;
    }

    // Step 6: identify drained move-source modules to sweep.
    let move_sources: BTreeSet<String> = plan.iter().map(|p| p.source_module.clone()).collect();
    let to_delete = drained_source_modules(&docs, &move_sources);

    // Step 7: realizability + atom-split gate against the in-memory
    // post-batch docs — the same `ModuleFile` claims model `debundle
    // run` loads, so canonical source_matches[] claims gate
    // identically. Runs before any file is written.
    gate.check(modules_root, || post_edit_spec_from_docs(&docs, &to_delete))?;

    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    Ok(AssignOutcome {
        outcome: MutationOutcome {
            verb: "assign",
            action: if dry_run { "dry-run" } else { "applied" },
            gate: gate.outcome(),
            files_written,
            files_deleted,
        },
        moves_applied: plan.len(),
    })
}

#[derive(Debug, Clone)]
struct PlannedMove {
    req: Move,
    source_module: String,
    source_index: usize,
}

/// Canonicalize a destination module path through the same
/// [`ModulePath::parse`] normalization the spec pipeline applies
/// (lowercasing, traversal rejection), so `UI/Widgets` and
/// `ui/widgets` cannot fork into two case-variant files.
fn canonical_module_path(raw: &str) -> Result<String> {
    Ok(ModulePath::parse(raw, "")
        .map_err(|err| anyhow!("invalid destination module: {err}"))?
        .as_str()
        .to_string())
}

/// A member's effective public identity: the readable `name:` when
/// set, else the minified `selector.binding.name`. `bindings rename`
/// and `bindings assign` share this predicate so both treat an
/// unrenamed member's minified name as a claimed identity.
fn member_effective_name(member: &Member) -> Option<String> {
    member
        .name
        .clone()
        .filter(|name| !name.is_empty())
        .or_else(|| member_minified_name(member))
}

fn insert_annotation(
    doc: &mut LogicalModule,
    export_name: &str,
    annotation: Option<BindingAnnotation>,
) -> Result<()> {
    let Some(annotation) = annotation else {
        return Ok(());
    };
    match doc.annotations.get(export_name) {
        Some(existing) if existing == &annotation => Ok(()),
        Some(_) => bail!("annotations.{export_name} already exists with different metadata"),
        None => {
            doc.annotations.insert(export_name.to_string(), annotation);
            Ok(())
        }
    }
}

fn member_minified_name(member: &Member) -> Option<String> {
    member
        .selector
        .binding
        .as_ref()
        .map(|binding| binding.name.clone())
        .filter(|name| !name.is_empty())
}

/// Move-source modules drained to zero members that are safe to
/// auto-delete. Only modules that were sources of the current batch
/// are considered — a pre-existing empty module shell is not this
/// command's business — and a drained source survives when it still
/// carries a module-level `comment:` or `note:`, `source_matches:`, `annotations:`,
/// or `anonymous_statements:` (all of which are spec content the sweep must
/// not destroy).
fn drained_source_modules(docs: &ModuleDocs, move_sources: &BTreeSet<String>) -> BTreeSet<String> {
    move_sources
        .iter()
        .filter(|mp| {
            if is_residual_module_path(mp) {
                return false;
            }
            let Some((_, doc)) = docs.get(*mp) else {
                return false;
            };
            doc.is_auto_deletable()
        })
        .cloned()
        .collect()
}

/// Persist the post-batch docs: write every surviving changed file
/// FIRST, then delete the drained sources — so an interrupted batch
/// can never lose a member that was not yet spliced into its
/// destination on disk.
fn apply_doc_changes(
    original_docs: &ModuleDocs,
    docs: &ModuleDocs,
    to_delete: &BTreeSet<String>,
    dry_run: bool,
) -> Result<(Vec<String>, Vec<String>)> {
    let mut files_written: Vec<String> = Vec::new();
    let mut files_deleted: Vec<String> = Vec::new();
    for (mp, (file, doc)) in docs {
        if to_delete.contains(mp) {
            continue;
        }
        let original = original_docs.get(mp).map(|(_, doc)| doc);
        let changed = apply_module_edit(file, original, doc, dry_run)?;
        if changed {
            files_written.push(file.display().to_string());
        }
    }
    for mp in to_delete {
        let (file, _) = &docs[mp];
        if !dry_run && file.exists() {
            fs::remove_file(file).with_context(|| format!("rm {}", file.display()))?;
        }
        files_deleted.push(file.display().to_string());
    }
    Ok((files_written, files_deleted))
}

/// Extract by snapshot identity without null sentinels or repeated Vec removals.
fn take_members(
    docs: &mut ModuleDocs,
    identities: impl IntoIterator<Item = (String, usize)>,
) -> BTreeMap<(String, usize), Member> {
    let mut by_module: BTreeMap<String, BTreeSet<usize>> = BTreeMap::new();
    for (module, index) in identities {
        by_module.entry(module).or_default().insert(index);
    }
    let mut taken = BTreeMap::new();
    for (module, indices) in by_module {
        let (_, doc) = docs.get_mut(&module).expect("resolved module is loaded");
        for (index, member) in std::mem::take(&mut doc.members).into_iter().enumerate() {
            if indices.contains(&index) {
                taken.insert((module.clone(), index), member);
            } else {
                doc.members.push(member);
            }
        }
    }
    taken
}

fn bail_source_match_split<T>(verb: &str, hit: &BindingMatch) -> Result<T> {
    bail!(
        "{verb} does not yet support canonical source_matches[] bindings; `{}` resolved to \
         {}#{}. Rename can edit the binding alias, but moving or unassigning one binding out of a \
         source_match claim needs a dedicated split operation.",
        hit.name.minified(),
        hit.file.display(),
        hit.location.describe()
    )
}

// ---------------------------------------------------------------------
// `bindings unassign`
// ---------------------------------------------------------------------

/// Outcome of an unassign batch. Mirrors [`AssignOutcome`]: the
/// shared [`MutationOutcome`] core plus the verb-specific count.
#[derive(Debug, Clone, serde::Serialize)]
pub struct UnassignOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub unassigned: usize,
}

/// Remove one or more bindings from their current modules as one validated batch.
/// Source modules drained of members are deleted unless they carry a
/// module-level `comment:` or `note:`, remaining `source_matches:`, `annotations:`,
/// or `anonymous_statements:` — same drain rule as
/// `run_bindings_assign`.
///
/// After unassign, the bindings fall through to residual (the default
/// when an owner isn't claimed by any spec module's `members:`). The
/// realizability + atom-split gate runs against the post-batch spec
/// the same way `bindings assign` does. The CLI dispatcher enforces
/// the "graph or no-verify" policy ([`Gate::from_cli`]).
///
/// Contract:
///   * Each sym must resolve to exactly one member via
///     [`resolve_unambiguous`]; syms are deduplicated on the resolved
///     member identity (warn on duplicates), so the minified and
///     readable spelling of one member collapse to one removal.
///   * Only modules that were sources of a removal in THIS batch are
///     swept after draining.
///   * [`Gate::Run`] gates the in-memory post-batch spec; cycle or
///     atom-split rejections bail before any file is written.
pub fn run_bindings_unassign(
    modules_root: &Path,
    syms: Vec<String>,
    dry_run: bool,
    gate: Gate<'_>,
) -> Result<UnassignOutcome> {
    if syms.is_empty() {
        return Ok(UnassignOutcome {
            outcome: MutationOutcome {
                verb: "unassign",
                action: "noop",
                gate: GateOutcome::NotRequired,
                files_written: Vec::new(),
                files_deleted: Vec::new(),
            },
            unassigned: 0,
        });
    }

    // Preserve the planning snapshot for semantic no-op detection at apply time.
    let original_docs = load_module_docs(modules_root)?;
    let mut docs = original_docs.clone();
    // Step 1: resolve each sym and dedupe on member identity (same
    // rule as `run_bindings_assign` — both spellings of one member
    // are one removal).
    let mut plan: BTreeMap<(String, usize), String> = BTreeMap::new();
    for s in syms {
        let hit = resolve_in_docs(modules_root, &docs, &s)?;
        let member_index = match &hit.location {
            BindingLocation::Member { member_index } => *member_index,
            BindingLocation::SourceMatch { .. } => {
                bail_source_match_split("bindings unassign", &hit)?
            }
        };
        if let Some(prev) = plan.insert((hit.module_path, member_index), s.clone()) {
            eprintln!(
                "warning: duplicate sym in batch ({prev:?} / {s:?} resolve to one member); \
                 ignoring repeat"
            );
        }
    }
    // Step 3: remove members and their annotations from the snapshot.
    for ((source_module, _), member) in take_members(&mut docs, plan.keys().cloned()) {
        let (_, doc) = docs
            .get_mut(&source_module)
            .expect("resolved module is loaded");
        if let Some(export_name) = member_effective_name(&member) {
            doc.annotations.remove(&export_name);
        }
    }

    // Step 4: identify drained move-source modules to sweep.
    let move_sources: BTreeSet<String> = plan
        .keys()
        .map(|(source_module, _)| source_module.clone())
        .collect();
    let to_delete = drained_source_modules(&docs, &move_sources);

    // Step 5: gate the in-memory post-batch spec (cycles +
    // atom-split) before any file is written. Built from the mutated
    // docs through the run pipeline's claims model, so source_matches[]
    // claims in surviving modules stay claimed.
    gate.check(modules_root, || post_edit_spec_from_docs(&docs, &to_delete))?;

    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    Ok(UnassignOutcome {
        outcome: MutationOutcome {
            verb: "unassign",
            action: if dry_run { "dry-run" } else { "applied" },
            gate: gate.outcome(),
            files_written,
            files_deleted,
        },
        unassigned: plan.len(),
    })
}

/// Locations come from the same immutable snapshot used to resolve the edit.
fn set_readable_name(doc: &mut LogicalModule, location: &BindingLocation, name: &str) {
    match *location {
        BindingLocation::Member { member_index } => {
            doc.members[member_index].name = Some(name.to_string())
        }
        BindingLocation::SourceMatch {
            claim_index,
            binding_index,
        } => {
            let binding = &mut doc.source_matches[claim_index].bindings[binding_index];
            *binding = SourceMatchBinding::Detailed(SourceMatchBindingDetail {
                local: binding.local().to_string(),
                name: Some(name.to_string()),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{DepKind, OwnerGraphNodeReport};
    use peel::propose::propose;
    use report_fixtures::{
        active_owner, atomic_edge, atomic_unit_for, claims, graph_of, no_claims, owner_edge,
        residual_owner,
    };

    #[test]
    fn parse_move_triple_rejects_one_field() {
        assert!(parse_move_triple("XOe").is_err());
    }

    #[test]
    fn parse_batch_json_modules_propose_report_shape() {
        let m = parse_batch_json(
            r#"{
                "proposals": [
                    {
                        "proposed_module_id": "auto_partition_0001",
                        "binding_ids": ["a", "b"],
                        "landable_today": true
                    }
                ],
                "diagnostics": []
            }"#,
        )
        .unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!(m[0].sym, "a");
        assert_eq!(m[0].module, "auto_partition_0001");
        assert_eq!(m[1].sym, "b");
        assert_eq!(m[1].module, "auto_partition_0001");
    }

    #[test]
    fn parse_batch_json_proposal_array_uses_extend_destination() {
        let m = parse_batch_json(
            r#"[
                {
                    "proposed_module_id": "extend:runtime/plugins",
                    "binding_ids": ["a"],
                    "landable_today": true,
                    "extends_module_id": "runtime/plugins"
                }
            ]"#,
        )
        .unwrap();
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].sym, "a");
        assert_eq!(m[0].module, "runtime/plugins");
    }

    #[test]
    fn parse_batch_json_rejects_merge_proposal() {
        parse_batch_json(
            r#"[
                {
                    "proposed_module_id": "merge:domains/system/ids+domains/system/types",
                    "binding_ids": ["a"],
                    "landable_today": true,
                    "merge_into": ["domains/system/ids", "domains/system/types"]
                }
            ]"#,
        )
        .unwrap_err();
    }

    #[test]
    fn parse_batch_json_rejects_anonymous_statement_proposal() {
        parse_batch_json(
            r#"[
                {
                    "proposed_module_id": "auto_partition_0002",
                    "binding_ids": ["a"],
                    "anonymous_statement_owner_ids": ["owner:7"],
                    "landable_today": true
                }
            ]"#,
        )
        .unwrap_err();
    }

    // `modules propose --format json` is documented `--batch` input (docs/cli.md). The row
    // fields are read through `serde(default)`, so a renamed `ModuleProposal` field would parse
    // as absent instead of failing: these pair the real proposer with `parse_batch_json`.
    fn propose_as_batch(
        from: OwnerGraphNodeReport,
        to: OwnerGraphNodeReport,
        active_claims: &BTreeMap<String, ModulePath>,
    ) -> Result<Vec<Move>> {
        let graph = graph_of(
            vec![from.clone(), to.clone()],
            vec![owner_edge(
                "edge:0",
                &from.id,
                &to.id,
                DepKind::EagerUse,
                true,
            )],
            vec![
                atomic_unit_for("atomic:0", &[&from]),
                atomic_unit_for("atomic:1", &[&to]),
            ],
            vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
        );
        let report = propose(&graph, active_claims, 10_000).unwrap();
        parse_batch_json(&serde_json::to_string(&report).unwrap())
    }

    fn sym_module_pairs(moves: &[Move]) -> Vec<(&str, &str)> {
        moves
            .iter()
            .map(|m| (m.sym.as_str(), m.module.as_str()))
            .collect()
    }

    #[test]
    fn proposed_fresh_module_batch_moves_its_bindings_there() {
        let moves = propose_as_batch(
            residual_owner("owner:a", 1, &["BindingA"], 10),
            residual_owner("owner:b", 2, &["BindingB"], 10),
            &no_claims(),
        )
        .unwrap();
        assert_eq!(
            sym_module_pairs(&moves),
            [
                ("BindingA", "auto_partition_0000"),
                ("BindingB", "auto_partition_0000")
            ],
        );
    }

    #[test]
    fn proposed_extension_batch_moves_its_bindings_into_the_extended_module() {
        let moves = propose_as_batch(
            residual_owner("owner:b", 2, &["BindingB"], 5),
            active_owner("owner:a", 1, &["BindingA"], 10, "ui/x"),
            &claims(&[("BindingA", "ui/x")]),
        )
        .unwrap();
        assert_eq!(sym_module_pairs(&moves), [("BindingB", "ui/x")]);
    }

    #[test]
    fn proposed_batch_with_anonymous_statements_is_rejected() {
        propose_as_batch(
            residual_owner("owner:a", 1, &["BindingA"], 10),
            residual_owner("owner:anon", 2, &[], 5),
            &no_claims(),
        )
        .unwrap_err();
    }
}
