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
//! * Operations are YAML-shape preserving via `serde_yaml::Value`.
//!
//! The per-command contract lives in the clap doc-comments
//! (`cli/mod.rs`); cross-command semantics (batch atomicity, rejection
//! diagnostics) in `docs/cli.md`.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use serde::Deserialize;
use serde_yaml::{Mapping, Value};

use spec::ModulePath;
use spec_modules::{collect_module_files, is_residual_module_path, module_path_from_file};

use crate::edit_gate::{Gate, post_edit_spec_from_docs};
use crate::outcome::{GateOutcome, MutationOutcome};
use crate::yaml_edit::{read_yaml, write_yaml_if_semantic_changed, yaml_semantically_changed};

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
    /// Member-like index for callers that still only operate on
    /// `members[]`. For canonical `source_matches[]` bindings this is the
    /// binding index inside the source-match claim; new callers should branch
    /// on [`BindingLocation`] instead.
    pub member_index: usize,
    pub name: BindingName,
    pub has_comment: bool,
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

/// Read every module YAML under `modules_root` once; return the
/// loaded docs by module-path. The `assign` path uses this to compute
/// the post-batch state in memory before writing anything back.
pub fn load_module_docs(modules_root: &Path) -> Result<BTreeMap<String, (PathBuf, Value)>> {
    let mut docs = BTreeMap::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        docs.insert(module_path, (file, doc));
    }
    Ok(docs)
}

fn binding_matches_in_doc(file: &Path, module_path: &str, doc: &Value) -> Vec<BindingMatch> {
    let mut out = Vec::new();

    if let Some(seq) = doc
        .as_mapping()
        .and_then(|m| m.get(yk("members")))
        .and_then(Value::as_sequence)
    {
        for (idx, member) in seq.iter().enumerate() {
            let Some(map) = member.as_mapping() else {
                continue;
            };
            let minified = member_minified_name(map);
            let readable_name = map
                .get(yk("name"))
                .and_then(Value::as_str)
                .map(str::to_string);
            if minified.is_none() && readable_name.is_none() {
                continue;
            }
            out.push(BindingMatch {
                file: file.to_path_buf(),
                module_path: module_path.to_string(),
                location: BindingLocation::Member { member_index: idx },
                member_index: idx,
                name: BindingName::new(minified.unwrap_or_default(), readable_name),
                has_comment: map.get(yk("comment")).is_some(),
            });
        }
    }

    if let Some(claims) = doc
        .as_mapping()
        .and_then(|m| m.get(yk("source_matches")))
        .and_then(Value::as_sequence)
    {
        for (claim_index, claim) in claims.iter().enumerate() {
            let Some(bindings) = claim
                .as_mapping()
                .and_then(|m| m.get(yk("bindings")))
                .and_then(Value::as_sequence)
            else {
                continue;
            };
            for (binding_index, binding) in bindings.iter().enumerate() {
                let Some(name) = source_match_binding_name(binding) else {
                    continue;
                };
                let effective = binding_effective_name(&name).to_string();
                out.push(BindingMatch {
                    file: file.to_path_buf(),
                    module_path: module_path.to_string(),
                    location: BindingLocation::SourceMatch {
                        claim_index,
                        binding_index,
                    },
                    member_index: binding_index,
                    name,
                    has_comment: annotation_has_comment(doc, &effective),
                });
            }
        }
    }

    out
}

fn binding_effective_name(name: &BindingName) -> &str {
    name.readable().unwrap_or_else(|| name.minified())
}

fn source_match_binding_name(binding: &Value) -> Option<BindingName> {
    match binding {
        Value::String(local) if !local.is_empty() => Some(BindingName::new(local.clone(), None)),
        Value::Mapping(map) => {
            let local = map
                .get(yk("local"))
                .and_then(Value::as_str)
                .filter(|local| !local.is_empty())?
                .to_string();
            let readable = map
                .get(yk("name"))
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .map(str::to_string);
            Some(BindingName::new(local, readable))
        }
        _ => None,
    }
}

fn annotation_has_comment(doc: &Value, export_name: &str) -> bool {
    doc.as_mapping()
        .and_then(|m| m.get(yk("annotations")))
        .and_then(Value::as_mapping)
        .and_then(|annotations| annotations.get(yk(export_name)))
        .and_then(Value::as_mapping)
        .is_some_and(|annotation| annotation.get(yk("comment")).is_some())
}

/// Locate every member matching `sym` under `modules_root`. `sym`
/// matches either the minified binding name or the readable `name:`.
pub fn find_matches(modules_root: &Path, sym: &str) -> Result<Vec<BindingMatch>> {
    let mut out = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        for binding in binding_matches_in_doc(&file, &module_path, &doc) {
            let name = binding.name.clone();
            if name.matches(sym) {
                out.push(binding);
            }
        }
    }
    Ok(out)
}

/// Resolve a single unambiguous match for `sym`, or bail with the
/// canonical structured-list error message.
pub fn resolve_unambiguous(modules_root: &Path, sym: &str) -> Result<BindingMatch> {
    let matches = find_matches(modules_root, sym)?;
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
    // First pass: collect counts per module to detect orphans.
    let mut per_module_counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut entries: Vec<BindingEntry> = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        let bindings = binding_matches_in_doc(&file, &module_path, &doc);
        per_module_counts.insert(module_path.clone(), bindings.len());
        for binding in bindings {
            let entry = BindingEntry {
                name: binding.name,
                module: module_path.clone(),
                orphan: false,
            };
            entries.push(entry);
        }
    }
    // Second pass: fill in the orphan flag now we have the counts.
    for entry in entries.iter_mut() {
        if per_module_counts.get(&entry.module).copied().unwrap_or(0) <= 1 {
            entry.orphan = true;
        }
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
    let hit = resolve_unambiguous(modules_root, original)?;
    if !no_verify {
        let clashes = find_readable_collisions(modules_root, new, &hit.file, &hit.location)?;
        if !clashes.is_empty() {
            bail!(
                "name collision: \"{new}\" already used by:\n{}",
                clashes.join("\n")
            );
        }
    }
    let mut doc = read_yaml(&hit.file)?;
    let old_readable = current_readable_name(&doc, &hit.file, &hit.location)?;
    let old_effective = old_readable
        .clone()
        .unwrap_or_else(|| hit.name.minified().to_string());
    let annotation = remove_annotation(&mut doc, &old_effective)?;
    set_readable_name(&mut doc, &hit.file, &hit.location, new)?;
    insert_annotation(&mut doc, new, annotation)?;
    let changed = yaml_semantically_changed(&hit.file, &doc)?;
    let action = if !changed {
        "unchanged"
    } else if dry_run {
        "dry-run"
    } else {
        "applied"
    };
    if changed && !dry_run {
        write_yaml_if_semantic_changed(&hit.file, &doc)?;
    }
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
    modules_root: &Path,
    new_readable: &str,
    self_file: &Path,
    self_location: &BindingLocation,
) -> Result<Vec<String>> {
    let mut clashes = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        for binding in binding_matches_in_doc(&file, &module_path, &doc) {
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
    Ok(clashes)
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

/// Apply a sequence of moves atomically: read every module's YAML
/// once, mutate in-memory, validate (collisions + realizability +
/// atom-split), then write back. Source modules drained of members
/// are deleted unless they carry a module-level `comment:`.
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
///     `comment:`, no remaining `source_matches:`, `annotations:`, or
///     `anonymous_statements:`.
///   * [`Gate::Run`] runs the unified realizability gate
///     ([`crate::edit_gate::gate_post_edit_partition`]) against the
///     in-memory post-batch spec; cycle or atom-split rejections
///     bail before any file is written. [`Gate::NamesOnly`] keeps
///     collision detection; [`Gate::Skip`] (`--no-verify`) skips
///     everything. The CLI dispatcher requires `--graph` unless
///     `--no-verify` is set ([`Gate::from_cli`]).
pub fn run_bindings_assign(
    modules_root: &Path,
    moves: Vec<Move>,
    dry_run: bool,
    gate: Gate<'_>,
) -> Result<AssignOutcome> {
    // Step 1: locate each move's member and canonicalize its
    // destination. Identity is the resolved (source module, member
    // index) slot: `<sym>` accepts both the minified and readable
    // spelling, so a raw-string dedupe would let both spellings of
    // one member produce two plan entries for one slot — the second
    // extraction would then splice a null sentinel into the spec.
    let mut by_identity: BTreeMap<(String, usize), PlannedMove> = BTreeMap::new();
    for m in moves {
        let hit = resolve_unambiguous(modules_root, &m.sym)?;
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
    if plan.is_empty() {
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

    // Step 2: load every module YAML once.
    let mut docs = load_module_docs(modules_root)?;

    // Step 3: pull each member out of its source doc (and rename if
    // requested). `take_member` leaves a `Null` sentinel so indices
    // stay stable across multiple takes from one module; collapse
    // them once every take has run.
    let mut pulled: BTreeMap<String, Value> = BTreeMap::new();
    let mut pulled_annotations: BTreeMap<String, (String, Option<Value>)> = BTreeMap::new();
    for p in &plan {
        let Some((file, doc)) = docs.get_mut(&p.source_module) else {
            bail!("source module {:?} not in tree", p.source_module);
        };
        let mut member = take_member(doc, file, p.source_index)?;
        let old_effective = member_effective_name_value(&member)
            .with_context(|| format!("member {:?} has no effective binding name", p.req.sym))?;
        let annotation = remove_annotation(doc, &old_effective)?;
        if let Some(new_readable) = &p.req.readable {
            if let Some(map) = member.as_mapping_mut() {
                map.insert(yk("name"), Value::String(new_readable.clone()));
            }
        }
        let new_effective = member_effective_name_value(&member)
            .with_context(|| format!("member {:?} has no effective binding name", p.req.sym))?;
        pulled_annotations.insert(p.req.sym.clone(), (new_effective, annotation));
        pulled.insert(p.req.sym.clone(), member);
    }
    for (_, (_, doc)) in docs.iter_mut() {
        collapse_null_members(doc);
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
                let Some(seq) = members_seq(doc) else {
                    continue;
                };
                for (idx, member) in seq.iter().enumerate() {
                    let Some(map) = member.as_mapping() else {
                        continue;
                    };
                    if member_effective_name(map).as_deref() == Some(new_readable) {
                        hits.push(format!("  {} ({}@{})", file.display(), mp, idx));
                    }
                }
            }
            // Also check the pulled bin: another move might carry the
            // same effective name to a different destination.
            let pulled_hits = pulled
                .iter()
                .filter(|(other_sym, _)| *other_sym != &p.req.sym)
                .filter(|(_, member)| {
                    member
                        .as_mapping()
                        .and_then(member_effective_name)
                        .as_deref()
                        == Some(new_readable)
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
            let mut map = Mapping::new();
            map.insert(yk("members"), Value::Sequence(Vec::new()));
            let dest_file = modules_root.join(format!("{dest_path}.yaml"));
            docs.insert(dest_path.clone(), (dest_file, Value::Mapping(map)));
        }
        let member = pulled.remove(&p.req.sym).expect("pulled member missing");
        let (export_name, annotation) = pulled_annotations
            .remove(&p.req.sym)
            .expect("pulled annotation missing");
        let (_, doc) = docs.get_mut(&dest_path).expect("dest just created");
        push_member(doc, member)?;
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

    let (files_written, files_deleted) = apply_doc_changes(&docs, &to_delete, dry_run)?;
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
fn member_effective_name(map: &Mapping) -> Option<String> {
    if let Some(name) = map
        .get(yk("name"))
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
    {
        return Some(name.to_string());
    }
    member_minified_name(map)
}

fn member_effective_name_value(member: &Value) -> Option<String> {
    member.as_mapping().and_then(member_effective_name)
}

fn remove_annotation(doc: &mut Value, export_name: &str) -> Result<Option<Value>> {
    let Some(root) = doc.as_mapping_mut() else {
        return Ok(None);
    };
    let annotations_key = yk("annotations");
    let Some(annotations_value) = root.get_mut(&annotations_key) else {
        return Ok(None);
    };
    let annotations = annotations_value
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("annotations exists but is not a mapping"))?;
    let export_key = yk(export_name);
    let removed = annotations.remove(&export_key);
    let empty = annotations.is_empty();
    if empty {
        root.remove(&annotations_key);
    }
    Ok(removed)
}

fn insert_annotation(doc: &mut Value, export_name: &str, annotation: Option<Value>) -> Result<()> {
    let Some(annotation) = annotation else {
        return Ok(());
    };
    let root = doc
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("module YAML is not a mapping"))?;
    let annotations_key = yk("annotations");
    let entry = root
        .entry(annotations_key)
        .or_insert_with(|| Value::Mapping(Mapping::new()));
    let annotations = entry
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("annotations exists but is not a mapping"))?;
    let export_key = yk(export_name);
    match annotations.get(&export_key) {
        Some(existing) if existing == &annotation => Ok(()),
        Some(_) => bail!("annotations.{export_name} already exists with different metadata"),
        None => {
            annotations.insert(export_key, annotation);
            Ok(())
        }
    }
}

fn member_minified_name(map: &Mapping) -> Option<String> {
    map.get(yk("selector"))
        .and_then(Value::as_mapping)
        .and_then(|s| s.get(yk("binding")))
        .and_then(Value::as_mapping)
        .and_then(|b| b.get(yk("name")))
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .map(str::to_string)
}

/// Move-source modules drained to zero members that are safe to
/// auto-delete. Only modules that were sources of the current batch
/// are considered — a pre-existing empty module shell is not this
/// command's business — and a drained source survives when it still
/// carries a module-level `comment:`, `source_matches:`, `annotations:`,
/// or `anonymous_statements:` (all of which are spec content the sweep must
/// not destroy).
fn drained_source_modules(
    docs: &BTreeMap<String, (PathBuf, Value)>,
    move_sources: &BTreeSet<String>,
) -> BTreeSet<String> {
    move_sources
        .iter()
        .filter(|mp| {
            if is_residual_module_path(mp) {
                return false;
            }
            let Some((_, doc)) = docs.get(*mp) else {
                return false;
            };
            let Some(map) = doc.as_mapping() else {
                return false;
            };
            let members_empty = members_seq(doc).is_none_or(|s| s.is_empty());
            let keeps_content = map.get(yk("comment")).is_some()
                || map
                    .get(yk("annotations"))
                    .and_then(Value::as_mapping)
                    .is_some_and(|m| !m.is_empty())
                || [yk("source_matches"), yk("anonymous_statements")]
                    .iter()
                    .any(|key| {
                        map.get(key)
                            .and_then(Value::as_sequence)
                            .is_some_and(|s| !s.is_empty())
                    });
            members_empty && !keeps_content
        })
        .cloned()
        .collect()
}

/// Persist the post-batch docs: write every surviving changed file
/// FIRST, then delete the drained sources — so an interrupted batch
/// can never lose a member that was not yet spliced into its
/// destination on disk.
fn apply_doc_changes(
    docs: &BTreeMap<String, (PathBuf, Value)>,
    to_delete: &BTreeSet<String>,
    dry_run: bool,
) -> Result<(Vec<String>, Vec<String>)> {
    let mut files_written: Vec<String> = Vec::new();
    let mut files_deleted: Vec<String> = Vec::new();
    for (mp, (file, doc)) in docs {
        if to_delete.contains(mp) {
            continue;
        }
        let changed = yaml_semantically_changed(file, doc)?;
        if changed && !dry_run {
            if let Some(parent) = file.parent() {
                fs::create_dir_all(parent)
                    .with_context(|| format!("creating {}", parent.display()))?;
            }
            write_yaml_if_semantic_changed(file, doc)?;
        }
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

fn take_member(doc: &mut Value, file: &Path, index: usize) -> Result<Value> {
    let seq = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
        .ok_or_else(|| anyhow!("module YAML {} missing members sequence", file.display()))?;
    if index >= seq.len() {
        bail!("member index {index} out of range in {}", file.display());
    }
    // Replace with null so collapse_null_members can compact the
    // sequence after every batch take has run.
    let taken = std::mem::replace(&mut seq[index], Value::Null);
    Ok(taken)
}

fn collapse_null_members(doc: &mut Value) {
    let Some(seq) = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
    else {
        return;
    };
    seq.retain(|v| !v.is_null());
}

fn push_member(doc: &mut Value, member: Value) -> Result<()> {
    let map = doc
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("destination YAML is not a mapping"))?;
    let entry = map
        .entry(yk("members"))
        .or_insert_with(|| Value::Sequence(Vec::new()));
    if entry.is_null() {
        *entry = Value::Sequence(Vec::new());
    }
    entry
        .as_sequence_mut()
        .ok_or_else(|| anyhow!("members is not a sequence"))?
        .push(member);
    Ok(())
}

fn members_seq(doc: &Value) -> Option<&Vec<Value>> {
    doc.as_mapping()
        .and_then(|m| m.get(yk("members")))
        .and_then(Value::as_sequence)
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

/// Remove one or more bindings from their current modules atomically.
/// Source modules drained of members are deleted unless they carry a
/// module-level `comment:`, remaining `source_matches:`, `annotations:`,
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
    // Step 1: resolve each sym and dedupe on member identity (same
    // rule as `run_bindings_assign` — both spellings of one member
    // are one removal).
    let mut plan: BTreeMap<(String, usize), String> = BTreeMap::new();
    for s in syms {
        let hit = resolve_unambiguous(modules_root, &s)?;
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
    if plan.is_empty() {
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

    // Step 2: load every module YAML once.
    let mut docs = load_module_docs(modules_root)?;

    // Step 3: drop each member from its source doc. Same null-sentinel
    // + collapse-after pattern `run_bindings_assign` uses so multiple
    // unassigns from the same module don't shift indices mid-pass.
    for (source_module, source_index) in plan.keys() {
        let Some((file, doc)) = docs.get_mut(source_module) else {
            bail!("source module {source_module:?} not in tree");
        };
        let member = take_member(doc, file, *source_index)?;
        if let Some(export_name) = member_effective_name_value(&member) {
            remove_annotation(doc, &export_name)?;
        }
    }
    for (_, (_, doc)) in docs.iter_mut() {
        collapse_null_members(doc);
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

    let (files_written, files_deleted) = apply_doc_changes(&docs, &to_delete, dry_run)?;
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

// ---------------------------------------------------------------------
// YAML helpers (re-declared here so this submodule remains
// independent of `cli::comment`; both share the same shape but
// neither imports the other).
// ---------------------------------------------------------------------

fn yk(s: &str) -> Value {
    Value::String(s.to_string())
}

fn current_readable_name(
    doc: &Value,
    file: &Path,
    location: &BindingLocation,
) -> Result<Option<String>> {
    match location {
        BindingLocation::Member { member_index } => {
            current_member_readable_name(doc, file, *member_index)
        }
        BindingLocation::SourceMatch {
            claim_index,
            binding_index,
        } => current_source_match_binding_readable_name(doc, file, *claim_index, *binding_index),
    }
}

fn current_member_readable_name(doc: &Value, file: &Path, index: usize) -> Result<Option<String>> {
    let seq = doc
        .as_mapping()
        .and_then(|m| m.get(yk("members")))
        .and_then(Value::as_sequence)
        .ok_or_else(|| anyhow!("module YAML {} missing members sequence", file.display()))?;
    let member = seq
        .get(index)
        .ok_or_else(|| anyhow!("member index {index} out of range in {}", file.display()))?;
    Ok(member
        .as_mapping()
        .and_then(|m| m.get(yk("name")))
        .and_then(Value::as_str)
        .map(str::to_string))
}

fn current_source_match_binding_readable_name(
    doc: &Value,
    file: &Path,
    claim_index: usize,
    binding_index: usize,
) -> Result<Option<String>> {
    let binding = source_match_binding(doc, file, claim_index, binding_index)?;
    match binding {
        Value::String(_) => Ok(None),
        Value::Mapping(map) => Ok(map
            .get(yk("name"))
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .map(str::to_string)),
        _ => bail!(
            "source_matches[{claim_index}].bindings[{binding_index}] is not a string or mapping in {}",
            file.display()
        ),
    }
}

fn set_readable_name(
    doc: &mut Value,
    file: &Path,
    location: &BindingLocation,
    name: &str,
) -> Result<()> {
    match location {
        BindingLocation::Member { member_index } => {
            set_member_readable_name(doc, file, *member_index, name)
        }
        BindingLocation::SourceMatch {
            claim_index,
            binding_index,
        } => set_source_match_binding_readable_name(doc, file, *claim_index, *binding_index, name),
    }
}

fn set_member_readable_name(doc: &mut Value, file: &Path, index: usize, name: &str) -> Result<()> {
    let seq = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
        .ok_or_else(|| anyhow!("module YAML {} missing members sequence", file.display()))?;
    let member = seq
        .get_mut(index)
        .ok_or_else(|| anyhow!("member index {index} out of range in {}", file.display()))?;
    let map = member
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("member entry is not a mapping in {}", file.display()))?;
    map.insert(yk("name"), Value::String(name.to_string()));
    Ok(())
}

fn set_source_match_binding_readable_name(
    doc: &mut Value,
    file: &Path,
    claim_index: usize,
    binding_index: usize,
    name: &str,
) -> Result<()> {
    let binding = source_match_binding_mut(doc, file, claim_index, binding_index)?;
    match binding {
        Value::String(local) => {
            let local = local.clone();
            let mut map = Mapping::new();
            map.insert(yk("local"), Value::String(local));
            map.insert(yk("name"), Value::String(name.to_string()));
            *binding = Value::Mapping(map);
            Ok(())
        }
        Value::Mapping(map) => {
            if map
                .get(yk("local"))
                .and_then(Value::as_str)
                .filter(|local| !local.is_empty())
                .is_none()
            {
                bail!(
                    "source_matches[{claim_index}].bindings[{binding_index}] missing local in {}",
                    file.display()
                );
            }
            map.insert(yk("name"), Value::String(name.to_string()));
            Ok(())
        }
        _ => bail!(
            "source_matches[{claim_index}].bindings[{binding_index}] is not a string or mapping in {}",
            file.display()
        ),
    }
}

fn source_match_binding<'a>(
    doc: &'a Value,
    file: &Path,
    claim_index: usize,
    binding_index: usize,
) -> Result<&'a Value> {
    doc.as_mapping()
        .and_then(|m| m.get(yk("source_matches")))
        .and_then(Value::as_sequence)
        .and_then(|claims| claims.get(claim_index))
        .and_then(Value::as_mapping)
        .and_then(|claim| claim.get(yk("bindings")))
        .and_then(Value::as_sequence)
        .and_then(|bindings| bindings.get(binding_index))
        .ok_or_else(|| {
            anyhow!(
                "source_match binding index {claim_index}.{binding_index} out of range in {}",
                file.display()
            )
        })
}

fn source_match_binding_mut<'a>(
    doc: &'a mut Value,
    file: &Path,
    claim_index: usize,
    binding_index: usize,
) -> Result<&'a mut Value> {
    doc.as_mapping_mut()
        .and_then(|m| m.get_mut(yk("source_matches")))
        .and_then(Value::as_sequence_mut)
        .and_then(|claims| claims.get_mut(claim_index))
        .and_then(Value::as_mapping_mut)
        .and_then(|claim| claim.get_mut(yk("bindings")))
        .and_then(Value::as_sequence_mut)
        .and_then(|bindings| bindings.get_mut(binding_index))
        .ok_or_else(|| {
            anyhow!(
                "source_match binding index {claim_index}.{binding_index} out of range in {}",
                file.display()
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, body).unwrap();
    }

    fn read(root: &Path, rel: &str) -> String {
        fs::read_to_string(root.join(rel)).unwrap()
    }

    #[test]
    fn binding_name_match_and_rename() {
        let minified = BindingName::new("_ab".to_string(), None);
        assert!(minified.matches("_ab"));
        assert!(!minified.matches("parseUserId"));
        assert!(!minified.is_renamed());

        let readable = BindingName::new("_ab".to_string(), Some("parseUserId".to_string()));
        // Both spellings resolve the same binding.
        assert!(readable.matches("_ab"));
        assert!(readable.matches("parseUserId"));
        assert!(readable.is_renamed());
    }

    #[test]
    fn binding_name_serializes_internally_tagged() {
        let readable = BindingName::new("_ab".to_string(), Some("parseUserId".to_string()));
        let json = serde_json::to_value(&readable).unwrap();
        assert_eq!(json["kind"], "readable");
        assert_eq!(json["minified"], "_ab");
        assert_eq!(json["name"], "parseUserId");

        let minified = BindingName::new("_ab".to_string(), None);
        let json = serde_json::to_value(&minified).unwrap();
        assert_eq!(json["kind"], "minified");
        assert_eq!(json["minified"], "_ab");
        assert!(json.get("name").is_none());
    }

    #[test]
    fn parse_move_triple_two_fields() {
        let m = parse_move_triple("XOe:runtime/plugins").unwrap();
        assert_eq!(m.sym, "XOe");
        assert_eq!(m.module, "runtime/plugins");
        assert_eq!(m.readable, None);
    }

    #[test]
    fn parse_move_triple_three_fields() {
        let m = parse_move_triple("XOe:runtime/plugins:PluginSettingsAccessor").unwrap();
        assert_eq!(m.sym, "XOe");
        assert_eq!(m.module, "runtime/plugins");
        assert_eq!(m.readable.as_deref(), Some("PluginSettingsAccessor"));
    }

    #[test]
    fn parse_move_triple_rejects_one_field() {
        assert!(parse_move_triple("XOe").is_err());
    }

    #[test]
    fn parse_batch_json_array_shape() {
        let m = parse_batch_json(
            r#"[{"sym":"a","module":"m"},{"sym":"b","module":"m","readable":"B"}]"#,
        )
        .unwrap();
        assert_eq!(m.len(), 2);
        assert_eq!(m[1].readable.as_deref(), Some("B"));
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
        let err = parse_batch_json(
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
        let msg = format!("{err}");
        assert!(msg.contains("merge_into"), "got {msg}");
        assert!(msg.contains("modules merge"), "got {msg}");
    }

    #[test]
    fn parse_batch_json_rejects_anonymous_statement_proposal() {
        let err = parse_batch_json(
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
        let msg = format!("{err}");
        assert!(msg.contains("anonymous_statement_owner_ids"), "got {msg}");
        assert!(msg.contains("bindings assign"), "got {msg}");
    }

    #[test]
    fn rename_updates_readable_field() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "m.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        let out = rename_binding(root, "XOe", "PluginSettings", false, false).unwrap();
        assert_eq!(out.new_readable, "PluginSettings");
        let body = read(root, "m.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(doc["members"][0]["name"].as_str(), Some("PluginSettings"));
    }

    #[test]
    fn rename_to_existing_readable_name_preserves_formatting() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        let original = "# hand formatted\nmembers: [ { name: PluginSettings, selector: { binding: { name: XOe } } } ]\n";
        write(root, "m.yaml", original);

        let out = rename_binding(root, "XOe", "PluginSettings", false, false).unwrap();

        assert_eq!(out.outcome.action, "unchanged");
        assert_eq!(read(root, "m.yaml"), original);
    }

    #[test]
    fn rename_rejects_collision() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - name: Other\n    selector: { binding: { name: AOe } }\n",
        );
        write(
            root,
            "b.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        let err = rename_binding(root, "XOe", "Other", false, false).unwrap_err();
        assert!(format!("{err}").contains("name collision"), "got {err}");
    }

    #[test]
    fn rename_no_verify_bypasses_collision() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - name: Other\n    selector: { binding: { name: AOe } }\n",
        );
        write(
            root,
            "b.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        let out = rename_binding(root, "XOe", "Other", false, true).unwrap();
        assert_eq!(out.new_readable, "Other");
    }

    #[test]
    fn assign_moves_member_and_deletes_drained_source() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "src.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        write(
            root,
            "dest.yaml",
            "members:\n  - selector: { binding: { name: YOe } }\n",
        );
        let moves = vec![Move {
            sym: "XOe".into(),
            module: "dest".into(),
            readable: None,
        }];
        let out = run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
        assert_eq!(out.moves_applied, 1);
        assert!(!root.join("src.yaml").exists(), "source should be deleted");
        let dest = read(root, "dest.yaml");
        let doc: Value = serde_yaml::from_str(&dest).unwrap();
        let names: Vec<&str> = doc["members"]
            .as_sequence()
            .unwrap()
            .iter()
            .map(|m| m["selector"]["binding"]["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, vec!["YOe", "XOe"]);
    }

    #[test]
    fn assign_keeps_source_with_module_comment() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "src.yaml",
            "comment: keepalive\nmembers:\n  - selector: { binding: { name: XOe } }\n",
        );
        write(root, "dest.yaml", "members: []\n");
        let moves = vec![Move {
            sym: "XOe".into(),
            module: "dest".into(),
            readable: None,
        }];
        run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
        assert!(root.join("src.yaml").exists(), "src kept due to comment");
        let src = read(root, "src.yaml");
        let doc: Value = serde_yaml::from_str(&src).unwrap();
        assert_eq!(doc["comment"].as_str(), Some("keepalive"));
        assert!(doc["members"].as_sequence().unwrap().is_empty());
    }

    #[test]
    fn assign_creates_missing_destination() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "src.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        let moves = vec![Move {
            sym: "XOe".into(),
            module: "runtime/plugins".into(),
            readable: Some("PluginSettings".into()),
        }];
        run_bindings_assign(root, moves, false, Gate::NamesOnly).unwrap();
        assert!(root.join("runtime/plugins.yaml").exists());
        let body = read(root, "runtime/plugins.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(doc["members"][0]["name"].as_str(), Some("PluginSettings"));
    }

    #[test]
    fn assign_dry_run_skips_writes() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "src.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        write(root, "dest.yaml", "members: []\n");
        let moves = vec![Move {
            sym: "XOe".into(),
            module: "dest".into(),
            readable: None,
        }];
        let out = run_bindings_assign(root, moves, true, Gate::NamesOnly).unwrap();
        assert_eq!(out.outcome.action, "dry-run");
        assert!(root.join("src.yaml").exists(), "src not deleted");
        let original = read(root, "src.yaml");
        assert!(original.contains("XOe"), "src unchanged");
    }

    #[test]
    fn bindings_list_returns_every_member() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - selector: { binding: { name: a } }\n  - selector: { binding: { name: b } }\n",
        );
        write(
            root,
            "c.yaml",
            "members:\n  - name: Solo\n    selector: { binding: { name: c } }\n",
        );
        let report = run_bindings_list(root, &BindingsListFilters::default()).unwrap();
        assert_eq!(report.bindings.len(), 3);
        let unrenamed: Vec<&str> = report
            .bindings
            .iter()
            .filter(|e| !e.name.is_renamed())
            .map(|e| e.name.minified())
            .collect();
        assert_eq!(unrenamed, vec!["a", "b"]);
        let orphans: Vec<&str> = report
            .bindings
            .iter()
            .filter(|e| e.orphan)
            .map(|e| e.name.minified())
            .collect();
        assert_eq!(orphans, vec!["c"]);
    }

    #[test]
    fn bindings_list_in_module_filter() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "a.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "b.yaml",
            "members:\n  - selector: { binding: { name: b } }\n",
        );
        let report = run_bindings_list(
            root,
            &BindingsListFilters {
                in_module: Some("a".to_string()),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(report.bindings.len(), 1);
        assert_eq!(report.bindings[0].name.minified(), "a");
    }
}
