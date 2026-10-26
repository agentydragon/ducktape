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

use analysis::{DependencyGraphReport, SourceUnitId, StatementKind};
use anonymous_resolution::resolve_source_claims;
use anyhow::{Context, Result, anyhow, bail};
use serde::Deserialize;

use spec::{
    AnonymousStatement, BindingAnnotation, LogicalModule, Member, MemberSelectorSpec, ModulePath,
    SourceMatchBinding, SourceMatchBindingDetail, SourceMatchClaim, is_residual_module_path,
};
use spec_modules::{collect_module_files, module_path_from_file, read_module_file};
use spec_tree::ModuleTreeScope;
use yaml_edit::write_yaml_atomic;

use crate::edit_gate::{
    Gate, SourceUnitModuleAssignments, gate_post_edit_assignment_with_expected_source_unit_modules,
    post_edit_spec_from_docs, source_claim_sets_from_post_spec, source_unit_module_assignments,
};
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
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
        let doc = read_module_file(&file)?;
        docs.insert(module_path, (file, doc));
    }
    Ok(docs)
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

pub(crate) fn matches_in_docs(docs: &ModuleDocs, sym: &str) -> Vec<BindingMatch> {
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
        let doc = read_module_file(&file)?;
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
    anonymous_statement_source_unit_ids: Vec<String>,
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
             `merge_into` / `anonymous_statement_source_unit_ids` rows with `modules merge` or manual YAML, \
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
    if !proposal.anonymous_statement_source_unit_ids.is_empty() {
        return Err((
            id,
            format!(
                "contains anonymous statements ({}) but `bindings assign` only moves members",
                proposal.anonymous_statement_source_unit_ids.join(", ")
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
///   * Moves are deduplicated on **resolved binding identity** (the
///     binding's source file + location): a batch carrying both the
///     local and readable spelling of one binding collapses to a
///     single move. Two moves for the same binding with contradictory
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
///     ([`crate::edit_gate::gate_post_edit_assignment`]) against the
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
    // Step 1: locate each move's binding and canonicalize its
    // destination. Identity is its resolved member or source-match slot:
    // `<sym>` accepts both local and readable spellings, so a raw-string
    // dedupe would produce two plan entries for one extraction slot.
    let mut by_identity: BTreeMap<(String, BindingLocation), PlannedMove> = BTreeMap::new();
    for m in moves {
        let hit = resolve_in_docs(modules_root, &docs, &m.sym).with_context(|| {
            "bindings assign moves existing YAML claims; for a fresh residual proposal use `debundle modules apply`"
        })?;
        let dest_module = canonical_module_path(&m.module)?;
        let planned = PlannedMove {
            req: Move {
                sym: m.sym,
                module: dest_module,
                readable: m.readable,
            },
            source_module: hit.module_path.clone(),
            source_location: hit.location,
            source_effective_name: binding_effective_name(&hit.name).to_string(),
        };
        match by_identity.entry((hit.module_path, hit.location)) {
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
    // Step 3: extract from the planning snapshot, preserving original indices.
    // A source-match claim may be split by selecting only some bindings; each
    // extracted binding keeps the shape and note of its original claim.
    let mut extracted_members = take_members(
        &mut docs,
        plan.iter().filter_map(|p| match p.source_location {
            BindingLocation::Member { member_index } => {
                Some((p.source_module.clone(), member_index))
            }
            BindingLocation::SourceMatch { .. } => None,
        }),
    );
    let mut extracted_claims = take_source_match_bindings(
        &mut docs,
        plan.iter().filter_map(|p| match p.source_location {
            BindingLocation::SourceMatch {
                claim_index,
                binding_index,
            } => Some((p.source_module.clone(), claim_index, binding_index)),
            BindingLocation::Member { .. } => None,
        }),
    );
    let mut pulled: BTreeMap<(String, BindingLocation), PulledBinding> = BTreeMap::new();
    let mut pulled_annotations: BTreeMap<String, (String, Option<BindingAnnotation>)> =
        BTreeMap::new();
    for p in &plan {
        let mut entry = match p.source_location {
            BindingLocation::Member { member_index } => PulledBinding::Member(Box::new(
                extracted_members
                    .remove(&(p.source_module.clone(), member_index))
                    .expect("resolved member is loaded"),
            )),
            BindingLocation::SourceMatch {
                claim_index,
                binding_index,
            } => PulledBinding::SourceMatch(
                extracted_claims
                    .remove(&(p.source_module.clone(), claim_index, binding_index))
                    .expect("resolved source-match binding is loaded"),
            ),
        };
        let (_, doc) = docs
            .get_mut(&p.source_module)
            .expect("resolved module is loaded");
        let annotation = doc.annotations.remove(&p.source_effective_name);
        if let Some(new_readable) = &p.req.readable {
            entry.set_readable(new_readable);
        }
        let new_effective = entry
            .effective_name()
            .with_context(|| format!("binding {:?} has no effective binding name", p.req.sym))?;
        pulled_annotations.insert(p.req.sym.clone(), (new_effective, annotation));
        pulled.insert((p.source_module.clone(), p.source_location), entry);
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
                .filter(|((module, location), _)| {
                    module != &p.source_module || location != &p.source_location
                })
                .filter(|(_, entry)| entry.effective_name().as_deref() == Some(new_readable))
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
    let mut destination_claims: BTreeMap<(String, String, usize), usize> = BTreeMap::new();
    for p in &plan {
        let dest_path = p.req.module.clone();
        if !docs.contains_key(&dest_path) {
            let dest_file = modules_root.join(format!("{dest_path}.yaml"));
            docs.insert(dest_path.clone(), (dest_file, LogicalModule::default()));
        }
        let entry = pulled
            .remove(&(p.source_module.clone(), p.source_location))
            .expect("pulled binding missing");
        let (export_name, annotation) = pulled_annotations
            .remove(&p.req.sym)
            .expect("pulled annotation missing");
        let (_, doc) = docs.get_mut(&dest_path).expect("dest just created");
        match entry {
            PulledBinding::Member(member) => doc.members.push(*member),
            PulledBinding::SourceMatch(mut claim) => {
                let BindingLocation::SourceMatch { claim_index, .. } = p.source_location else {
                    unreachable!()
                };
                let key = (dest_path.clone(), p.source_module.clone(), claim_index);
                if let Some(index) = destination_claims.get(&key) {
                    doc.source_matches[*index]
                        .bindings
                        .append(&mut claim.bindings);
                } else {
                    let index = doc.source_matches.len();
                    doc.source_matches.push(claim);
                    destination_claims.insert(key, index);
                }
            }
        }
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

/// One reviewed source-unit reassignment. Source units not listed keep their current
/// destination. This command deliberately accepts only source units already claimed
/// by the scoped module tree; it does not synthesize selectors for residuals.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceUnitAssignment {
    pub unit_id: String,
    pub module: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SourceUnitRefineOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub source_units_reassigned: usize,
}

#[derive(Debug, Clone)]
enum SourceUnitClaimSlot {
    Member(usize),
    SourceMatch(usize, usize),
    Anonymous(usize),
}

#[derive(Debug, Clone)]
struct SourceUnitClaimLocation {
    source_unit_id: String,
    /// Exact key used by the in-memory document map and filesystem path.
    physical_module: String,
    /// Canonical logical module identity used by the complete source-unit map.
    logical_module: String,
    slot: SourceUnitClaimSlot,
}

#[derive(Debug, Clone)]
struct PlannedSourceUnitMove {
    source_unit_id: String,
    source_module: String,
    destination: String,
    slot: SourceUnitClaimSlot,
}

/// Move complete existing source-unit claims as one checked edit. Selector locations
/// come from the canonical joint resolver; names, selector text, annotations,
/// and anonymous-statement metadata are preserved from the original YAML.
pub fn run_source_unit_refine(
    modules_root: &Path,
    dependency_graph_path: &Path,
    source_root: &Path,
    tree_config: Option<&Path>,
    assignments: Vec<SourceUnitAssignment>,
    dry_run: bool,
) -> Result<SourceUnitRefineOutcome> {
    if assignments.is_empty() {
        bail!("source-unit assignment batch is empty");
    }

    let dependency_graph = crate::load_dependency_graph_report(dependency_graph_path)?;
    let scope = tree_config
        .map(|path| ModuleTreeScope::from_config(path, &dependency_graph.chunk_id))
        .transpose()?;
    let mut original_docs = load_module_docs(modules_root)?;
    if let Some(scope) = &scope {
        original_docs.retain(|module, _| scope.contains(Path::new(module)));
    }
    let mut docs = original_docs.clone();
    let initial_spec = post_edit_spec_from_docs(&original_docs, &BTreeSet::new())?;
    let claim_sets = source_claim_sets_from_post_spec(&initial_spec);
    let resolved = resolve_source_claims(
        &dependency_graph,
        dependency_graph_path,
        modules_root,
        Some(source_root),
        &claim_sets,
    )?;
    let current_source_unit_modules = source_unit_module_assignments(
        &dependency_graph,
        modules_root,
        &initial_spec,
        &resolved,
        scope.as_ref(),
    )?;
    check_graph_source_unit_destinations(
        &dependency_graph,
        &current_source_unit_modules,
        scope.as_ref(),
    )?;

    let mut source_unit_by_binding = BTreeMap::<String, SourceUnitId>::new();
    let mut source_unit_id_by_report_key = BTreeMap::<String, SourceUnitId>::new();
    for (index, node) in dependency_graph.nodes.iter().enumerate() {
        let source_unit = SourceUnitId(index);
        source_unit_id_by_report_key.insert(node.id.clone(), source_unit);
        for binding in &node.declared_bindings {
            source_unit_by_binding
                .entry(binding.binding.to_string())
                .or_insert(source_unit);
        }
    }

    let locations = source_unit_claim_locations(
        &dependency_graph,
        modules_root,
        &original_docs,
        &initial_spec,
        &resolved,
        &source_unit_by_binding,
        scope.as_ref(),
    )?;
    let mut locations_by_source_unit = BTreeMap::<String, Vec<SourceUnitClaimLocation>>::new();
    for location in locations {
        let expected_module = current_source_unit_modules
            .get(&location.source_unit_id)
            .and_then(Option::as_ref)
            .with_context(|| {
                format!(
                    "source unit {:?} has a module claim location but resolves to residual",
                    location.source_unit_id
                )
            })?;
        anyhow::ensure!(
            expected_module.as_str() == location.logical_module,
            "source unit {:?} selector is stored in {}, but the complete spec resolves it to {}",
            location.source_unit_id,
            location.logical_module,
            expected_module
        );
        locations_by_source_unit
            .entry(location.source_unit_id.clone())
            .or_default()
            .push(location);
    }

    let mut requested_destinations = BTreeMap::<String, String>::new();
    for assignment in assignments {
        anyhow::ensure!(
            source_unit_id_by_report_key.contains_key(&assignment.unit_id),
            "source-unit ID {:?} is absent from dependency_graph.json",
            assignment.unit_id
        );
        anyhow::ensure!(
            !requested_destinations.contains_key(&assignment.unit_id),
            "source-unit assignment batch repeats {:?}",
            assignment.unit_id
        );
        let source_unit = source_unit_id_by_report_key[&assignment.unit_id];
        let node = &dependency_graph.nodes[source_unit.0];
        anyhow::ensure!(
            !matches!(
                node.statement_kind,
                StatementKind::Import | StatementKind::Export
            ),
            "source unit {:?} is import/export scaffolding and cannot be moved by modules refine",
            assignment.unit_id
        );
        let current_module = current_source_unit_modules
            .get(&assignment.unit_id)
            .and_then(Option::as_ref)
            .with_context(|| {
                format!(
                    "source unit {:?} is currently unclaimed/residual; modules refine moves existing claims only",
                    assignment.unit_id
                )
            })?;
        anyhow::ensure!(
            !is_residual_module_path(current_module.as_str()),
            "source unit {:?} is claimed from a residual module and cannot be moved",
            assignment.unit_id
        );
        let destination = if let Some(scope) = &scope {
            scope.canonical_destination(&assignment.module)?
        } else {
            canonical_module_path(&assignment.module)?
        };
        anyhow::ensure!(
            !is_residual_module_path(&destination),
            "destination {:?} is a reserved residual path",
            destination
        );
        requested_destinations.insert(assignment.unit_id, destination);
    }

    let mut expected_source_unit_modules = current_source_unit_modules.clone();
    let mut planned_moves = Vec::new();
    let mut source_modules = BTreeSet::new();
    let mut source_units_reassigned = 0;
    let existing_destinations = physical_modules_by_logical_destination(&docs, scope.as_ref())?;
    for (source_unit_id, destination) in &requested_destinations {
        let source_logical_module = current_source_unit_modules[source_unit_id]
            .as_ref()
            .expect("requested source units were checked as claimed")
            .as_str()
            .to_string();
        if destination == &source_logical_module {
            continue;
        }
        source_units_reassigned += 1;
        expected_source_unit_modules.insert(source_unit_id.clone(), Some(destination.clone()));
        let source_unit_locations = locations_by_source_unit.get(source_unit_id).with_context(|| {
            format!("source unit {source_unit_id:?} has no movable member or anonymous selector in the spec")
        })?;
        let mut physical_sources = BTreeSet::new();
        for location in source_unit_locations {
            anyhow::ensure!(
                location.logical_module == source_logical_module,
                "source unit {source_unit_id:?} has claims in more than one source module"
            );
            physical_sources.insert(location.physical_module.clone());
        }
        anyhow::ensure!(
            physical_sources.len() == 1,
            "source unit {source_unit_id:?} resolves to multiple physical module files with the same logical identity"
        );
        let source_module = physical_sources
            .into_iter()
            .next()
            .expect("movable source unit has at least one location");
        let destination_module = match existing_destinations.get(destination) {
            Some(paths) if paths.len() == 1 => paths[0].clone(),
            Some(paths) => anyhow::bail!(
                "logical destination {destination:?} matches multiple module files: {}",
                paths.join(", ")
            ),
            None => destination.clone(),
        };
        for location in source_unit_locations {
            source_modules.insert(source_module.clone());
            planned_moves.push(PlannedSourceUnitMove {
                source_unit_id: source_unit_id.clone(),
                source_module: source_module.clone(),
                destination: destination_module.clone(),
                slot: location.slot.clone(),
            });
        }
    }

    let moved_members = planned_moves
        .iter()
        .filter_map(|movement| match movement.slot {
            SourceUnitClaimSlot::Member(index) => Some((movement.source_module.clone(), index)),
            _ => None,
        });
    let mut pulled_members = take_members(&mut docs, moved_members);
    let moved_source_match_bindings =
        planned_moves
            .iter()
            .filter_map(|movement| match movement.slot {
                SourceUnitClaimSlot::SourceMatch(claim, binding) => {
                    Some((movement.source_module.clone(), claim, binding))
                }
                _ => None,
            });
    let mut pulled_source_match_bindings =
        take_source_match_bindings(&mut docs, moved_source_match_bindings);
    let moved_anonymous = planned_moves
        .iter()
        .filter_map(|movement| match movement.slot {
            SourceUnitClaimSlot::Anonymous(index) => Some((movement.source_module.clone(), index)),
            _ => None,
        });
    let mut pulled_anonymous = take_anonymous_statements(&mut docs, moved_anonymous);

    let mut destination_claims = BTreeMap::<(String, String, usize), usize>::new();
    let mut moved_names_by_destination = BTreeMap::<String, BTreeSet<String>>::new();
    for movement in &planned_moves {
        match movement.slot {
            SourceUnitClaimSlot::Member(index) => {
                let member = pulled_members
                    .remove(&(movement.source_module.clone(), index))
                    .expect("planned member was extracted");
                let name = member_effective_name(&member).with_context(|| {
                    format!(
                        "source unit {:?} member has no binding name",
                        movement.source_unit_id
                    )
                })?;
                moved_names_by_destination
                    .entry(movement.destination.clone())
                    .or_default()
                    .insert(name.clone());
                let annotation = docs
                    .get_mut(&movement.source_module)
                    .expect("source module remains loaded")
                    .1
                    .annotations
                    .remove(&name);
                let destination =
                    ensure_owner_destination(&mut docs, modules_root, &movement.destination);
                destination.members.push(member);
                insert_annotation(destination, &name, annotation)?;
            }
            SourceUnitClaimSlot::SourceMatch(claim_index, binding_index) => {
                let mut claim = pulled_source_match_bindings
                    .remove(&(movement.source_module.clone(), claim_index, binding_index))
                    .expect("planned source-match binding was extracted");
                anyhow::ensure!(
                    claim.bindings.len() == 1,
                    "source-match split did not isolate one binding"
                );
                let binding = &claim.bindings[0];
                let name =
                    BindingName::new(binding.local().to_string(), source_match_readable(binding));
                let effective_name = binding_effective_name(&name).to_string();
                moved_names_by_destination
                    .entry(movement.destination.clone())
                    .or_default()
                    .insert(effective_name.clone());
                let annotation = docs
                    .get_mut(&movement.source_module)
                    .expect("source module remains loaded")
                    .1
                    .annotations
                    .remove(&effective_name);
                let destination =
                    ensure_owner_destination(&mut docs, modules_root, &movement.destination);
                let key = (
                    movement.destination.clone(),
                    movement.source_module.clone(),
                    claim_index,
                );
                if let Some(index) = destination_claims.get(&key) {
                    destination.source_matches[*index]
                        .bindings
                        .append(&mut claim.bindings);
                } else {
                    let index = destination.source_matches.len();
                    destination.source_matches.push(claim);
                    destination_claims.insert(key, index);
                }
                insert_annotation(destination, &effective_name, annotation)?;
            }
            SourceUnitClaimSlot::Anonymous(index) => {
                let statement = pulled_anonymous
                    .remove(&(movement.source_module.clone(), index))
                    .expect("planned anonymous selector was extracted");
                ensure_owner_destination(&mut docs, modules_root, &movement.destination)
                    .anonymous_statements
                    .push(statement);
            }
        }
    }

    for (destination, moved_names) in &moved_names_by_destination {
        let (_, module) = docs
            .get(destination)
            .with_context(|| format!("destination module {destination:?} is missing"))?;
        let mut counts = BTreeMap::<String, usize>::new();
        for binding in binding_matches_in_doc(
            &modules_root.join(format!("{destination}.yaml")),
            destination,
            module,
        ) {
            *counts
                .entry(binding_effective_name(&binding.name).to_string())
                .or_default() += 1;
        }
        for name in moved_names {
            let count = counts.get(name).copied().unwrap_or_default();
            anyhow::ensure!(
                count == 1,
                "moving a claim named {name:?} into {destination} would create a binding-name collision ({count} entries)"
            );
        }
    }

    let to_delete = drained_source_modules(&docs, &source_modules);
    let post_spec = post_edit_spec_from_docs(&docs, &to_delete)?;
    gate_post_edit_assignment_with_expected_source_unit_modules(
        dependency_graph_path,
        modules_root,
        Some(source_root),
        &post_spec,
        &expected_source_unit_modules,
        scope.as_ref(),
    )?;

    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    let action = if source_units_reassigned == 0 {
        "unchanged"
    } else if dry_run {
        "dry-run"
    } else {
        "applied"
    };
    Ok(SourceUnitRefineOutcome {
        outcome: MutationOutcome {
            verb: "refine",
            action,
            gate: GateOutcome::Passed,
            files_written,
            files_deleted,
        },
        source_units_reassigned,
    })
}

fn source_unit_claim_locations(
    dependency_graph: &DependencyGraphReport,
    modules_root: &Path,
    docs: &ModuleDocs,
    post_spec: &crate::edit_gate::PostEditSpec,
    resolved: &[anonymous_resolution::ResolvedSourceClaims],
    source_unit_by_binding: &BTreeMap<String, SourceUnitId>,
    scope: Option<&ModuleTreeScope>,
) -> Result<Vec<SourceUnitClaimLocation>> {
    anyhow::ensure!(
        post_spec.modules.len() == resolved.len(),
        "resolved selector results do not match the current module list"
    );
    let mut locations = Vec::new();
    for (module, resolved) in post_spec.modules.iter().zip(resolved) {
        let module_path = module_path_from_file(&module.path, modules_root);
        let logical_module = logical_module_path(&module_path, scope)?;
        let (_, doc) = docs.get(&module_path).with_context(|| {
            format!(
                "module {} is absent from the scoped spec",
                module.path.display()
            )
        })?;
        for (member_index, member) in doc.members.iter().enumerate() {
            let binding = match member.selector.selected()? {
                MemberSelectorSpec::Binding(binding) => binding.name,
                _ => {
                    let export_name = member.name.as_deref().with_context(|| {
                        format!("non-binding member in {module_path} requires a readable name")
                    })?;
                    resolved
                        .member_bindings
                        .get(export_name)
                        .cloned()
                        .with_context(|| {
                            format!(
                                "member {export_name:?} in {module_path} has no resolved binding"
                            )
                        })?
                }
            };
            let source_unit = source_unit_by_binding.get(&binding).with_context(|| {
                format!(
                    "member binding {binding:?} in {module_path} is absent from dependency graph"
                )
            })?;
            locations.push(SourceUnitClaimLocation {
                source_unit_id: dependency_graph.nodes[source_unit.0].id.clone(),
                physical_module: module_path.clone(),
                logical_module: logical_module.clone(),
                slot: SourceUnitClaimSlot::Member(member_index),
            });
        }
        for (claim_index, claim) in doc.source_matches.iter().enumerate() {
            for (binding_index, binding) in claim.bindings.iter().enumerate() {
                let resolved_binding = resolved
                    .member_bindings
                    .get(binding.name())
                    .with_context(|| {
                        format!(
                            "source_matches[{claim_index}] binding {:?} in {module_path} did not resolve",
                            binding.local()
                        )
                    })?;
                // `local` is the selector-local name used by the source
                // pattern. Under alpha-equivalent matching, the actual source
                // declaration can have a different name; the canonical
                // resolver's outcome is the authoritative binding identity.
                let source_unit = source_unit_by_binding.get(resolved_binding).with_context(|| {
                    format!(
                        "source-match binding {:?} resolved as {:?} in {module_path} is absent from dependency graph",
                        binding.local(), resolved_binding
                    )
                })?;
                locations.push(SourceUnitClaimLocation {
                    source_unit_id: dependency_graph.nodes[source_unit.0].id.clone(),
                    physical_module: module_path.clone(),
                    logical_module: logical_module.clone(),
                    slot: SourceUnitClaimSlot::SourceMatch(claim_index, binding_index),
                });
            }
        }
        let mut selectors = BTreeSet::new();
        for (statement_index, statement) in doc.anonymous_statements.iter().enumerate() {
            let selector = statement.selector()?;
            anyhow::ensure!(
                selectors.insert(selector.clone()),
                "module {module_path} repeats an anonymous selector, so its metadata cannot be assigned unambiguously"
            );
            let source_unit = resolved
                .anonymous_selector_source_units
                .get(&selector)
                .with_context(|| {
                    format!(
                        "anonymous_statements[{statement_index}] in {module_path} did not resolve to a source unit"
                    )
                })?;
            locations.push(SourceUnitClaimLocation {
                source_unit_id: dependency_graph
                    .nodes
                    .get(source_unit.0)
                    .with_context(|| {
                        format!("anonymous source-unit index {} is absent", source_unit.0)
                    })?
                    .id
                    .clone(),
                physical_module: module_path.clone(),
                logical_module: logical_module.clone(),
                slot: SourceUnitClaimSlot::Anonymous(statement_index),
            });
        }
    }
    Ok(locations)
}

fn logical_module_path(raw_path: &str, scope: Option<&ModuleTreeScope>) -> Result<String> {
    if let Some(scope) = scope {
        scope.canonical_destination(raw_path)
    } else {
        canonical_module_path(raw_path)
    }
}

fn physical_modules_by_logical_destination(
    docs: &ModuleDocs,
    scope: Option<&ModuleTreeScope>,
) -> Result<BTreeMap<String, Vec<String>>> {
    let mut by_logical = BTreeMap::<String, Vec<String>>::new();
    for physical in docs.keys() {
        by_logical
            .entry(logical_module_path(physical, scope)?)
            .or_default()
            .push(physical.clone());
    }
    Ok(by_logical)
}

fn check_graph_source_unit_destinations(
    dependency_graph: &DependencyGraphReport,
    spec_source_unit_modules: &SourceUnitModuleAssignments,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    for node in &dependency_graph.nodes {
        let graph_destination = dependency_graph
            .module(&node.destination)
            .with_context(|| {
                format!("source unit {:?} has an unknown graph destination", node.id)
            })?;
        let expected = if graph_destination.residual {
            None
        } else {
            Some(graph_destination.path.as_str().to_string())
        };
        let actual = spec_source_unit_modules
            .get(&node.id)
            .with_context(|| {
                format!(
                    "source unit {:?} is absent from the current spec mapping",
                    node.id
                )
            })?
            .as_ref()
            .map(|path| {
                let logical = if let Some(scope) = scope {
                    scope.logical_destination(path)?
                } else {
                    path.as_str()
                };
                ModulePath::parse(logical, "")
                    .map(|path| path.as_str().to_string())
                    .map_err(|err| {
                        anyhow!("invalid logical module path in source-unit spec: {err}")
                    })
            })
            .transpose()?;
        anyhow::ensure!(
            actual == expected,
            "dependency graph is stale for {:?}: graph destination {:?}, current spec destination {:?}",
            node.id,
            expected,
            actual
        );
    }
    Ok(())
}

fn take_anonymous_statements(
    docs: &mut ModuleDocs,
    identities: impl IntoIterator<Item = (String, usize)>,
) -> BTreeMap<(String, usize), AnonymousStatement> {
    let mut by_module: BTreeMap<String, BTreeSet<usize>> = BTreeMap::new();
    for (module, index) in identities {
        by_module.entry(module).or_default().insert(index);
    }
    let mut taken = BTreeMap::new();
    for (module, indices) in by_module {
        let (_, doc) = docs.get_mut(&module).expect("resolved module is loaded");
        for (index, statement) in std::mem::take(&mut doc.anonymous_statements)
            .into_iter()
            .enumerate()
        {
            if indices.contains(&index) {
                taken.insert((module.clone(), index), statement);
            } else {
                doc.anonymous_statements.push(statement);
            }
        }
    }
    taken
}

fn ensure_owner_destination<'a>(
    docs: &'a mut ModuleDocs,
    modules_root: &Path,
    destination: &str,
) -> &'a mut LogicalModule {
    &mut docs
        .entry(destination.to_string())
        .or_insert_with(|| {
            (
                modules_root.join(format!("{destination}.yaml")),
                LogicalModule::default(),
            )
        })
        .1
}

#[derive(Debug, Clone)]
struct PlannedMove {
    req: Move,
    source_module: String,
    source_location: BindingLocation,
    source_effective_name: String,
}

enum PulledBinding {
    Member(Box<Member>),
    SourceMatch(SourceMatchClaim),
}

impl PulledBinding {
    fn effective_name(&self) -> Option<String> {
        match self {
            Self::Member(member) => member_effective_name(member),
            Self::SourceMatch(claim) => claim
                .bindings
                .first()
                .map(|binding| binding.name().to_string()),
        }
    }

    fn set_readable(&mut self, readable: &str) {
        match self {
            Self::Member(member) => member.name = Some(readable.to_string()),
            Self::SourceMatch(claim) => {
                let binding = &mut claim.bindings[0];
                *binding = SourceMatchBinding::Detailed(SourceMatchBindingDetail {
                    local: binding.local().to_string(),
                    name: Some(readable.to_string()),
                });
            }
        }
    }
}

/// Canonicalize a destination module path through the same
/// [`ModulePath::parse`] normalization the spec pipeline applies
/// (lowercasing, traversal rejection), so `UI/Widgets` and
/// `ui/widgets` cannot fork into two case-variant files.
pub(crate) fn canonical_module_path(raw: &str) -> Result<String> {
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
pub(crate) fn apply_doc_changes(
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

/// Split selected bindings from source-backed claims using their original
/// indices. The unselected bindings remain in the source claim. A fully moved
/// claim disappears, while each selected binding carries its original template
/// and claim-level note for destination grouping.
fn take_source_match_bindings(
    docs: &mut ModuleDocs,
    identities: impl IntoIterator<Item = (String, usize, usize)>,
) -> BTreeMap<(String, usize, usize), SourceMatchClaim> {
    let mut by_module: BTreeMap<String, BTreeMap<usize, BTreeSet<usize>>> = BTreeMap::new();
    for (module, claim_index, binding_index) in identities {
        by_module
            .entry(module)
            .or_default()
            .entry(claim_index)
            .or_default()
            .insert(binding_index);
    }
    let mut taken = BTreeMap::new();
    for (module, claims) in by_module {
        let (_, doc) = docs.get_mut(&module).expect("resolved module is loaded");
        for (claim_index, mut claim) in std::mem::take(&mut doc.source_matches)
            .into_iter()
            .enumerate()
        {
            if let Some(indices) = claims.get(&claim_index) {
                let mut remaining = Vec::new();
                for (binding_index, binding) in
                    std::mem::take(&mut claim.bindings).into_iter().enumerate()
                {
                    if indices.contains(&binding_index) {
                        let mut selected = claim.clone();
                        selected.bindings.push(binding);
                        taken.insert((module.clone(), claim_index, binding_index), selected);
                    } else {
                        remaining.push(binding);
                    }
                }
                claim.bindings = remaining;
            }
            if !claim.bindings.is_empty() {
                doc.source_matches.push(claim);
            }
        }
    }
    taken
}

fn bail_source_match_unassign<T>(hit: &BindingMatch) -> Result<T> {
    bail!(
        "bindings unassign does not yet support canonical source_matches[] bindings; `{}` resolved to \
         {}#{}. Use bindings assign to move this binding; removing its claim requires \
         source-match unassign support.",
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
            BindingLocation::SourceMatch { .. } => bail_source_match_unassign(&hit)?,
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
    use analysis::{DepKind, SourceUnitReport};
    use peel::propose::propose;
    use report_fixtures::{
        active_source_unit, atomic_edge, atomic_group_for, claims, graph_of, no_claims, owner_edge,
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
                    "anonymous_statement_source_unit_ids": ["unit:7"],
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
        from: SourceUnitReport,
        to: SourceUnitReport,
        active_claims: &BTreeMap<String, ModulePath>,
    ) -> Result<Vec<Move>> {
        let graph = graph_of(
            vec![from.clone(), to.clone()],
            vec![dependency_edge(
                "edge:0",
                &from.id,
                &to.id,
                DepKind::EagerUse,
                true,
            )],
            vec![
                atomic_group_for("atomic:0", &[&from]),
                atomic_group_for("atomic:1", &[&to]),
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
            residual_source_unit("unit:a", 1, &["BindingA"], 10),
            residual_source_unit("unit:b", 2, &["BindingB"], 10),
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
            residual_source_unit("unit:b", 2, &["BindingB"], 5),
            active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x"),
            &claims(&[("BindingA", "ui/x")]),
        )
        .unwrap();
        assert_eq!(sym_module_pairs(&moves), [("BindingB", "ui/x")]);
    }

    #[test]
    fn proposed_batch_with_anonymous_statements_is_rejected() {
        propose_as_batch(
            residual_source_unit("unit:a", 1, &["BindingA"], 10),
            residual_source_unit("unit:anon", 2, &[], 5),
            &no_claims(),
        )
        .unwrap_err();
    }
}
