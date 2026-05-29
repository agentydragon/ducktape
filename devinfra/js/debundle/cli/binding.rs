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
//! See `docs/cli.md` § "Bindings" for the user-facing contract.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use serde::Deserialize;
use serde_yaml::{Mapping, Value};

use spec_modules::{collect_module_files, is_residual_module_path, module_path_from_file};

use crate::edit_gate::{gate_post_edit_partition, post_assign_spec, post_unassign_spec};
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
/// and is the unit `assign` / `rename` mutate.
#[derive(Debug, Clone)]
pub struct BindingMatch {
    pub file: PathBuf,
    pub module_path: String,
    pub member_index: usize,
    pub name: BindingName,
    pub has_comment: bool,
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

/// Locate every member matching `sym` under `modules_root`. `sym`
/// matches either the minified binding name or the readable `name:`.
pub fn find_matches(modules_root: &Path, sym: &str) -> Result<Vec<BindingMatch>> {
    let mut out = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        let Some(members) = doc.as_mapping().and_then(|m| m.get(yk("members"))) else {
            continue;
        };
        let Some(seq) = members.as_sequence() else {
            continue;
        };
        for (idx, member) in seq.iter().enumerate() {
            let Some(map) = member.as_mapping() else {
                continue;
            };
            let minified = map
                .get(yk("selector"))
                .and_then(Value::as_mapping)
                .and_then(|s| s.get(yk("binding")))
                .and_then(Value::as_mapping)
                .and_then(|b| b.get(yk("name")))
                .and_then(Value::as_str)
                .map(str::to_string);
            let readable_name = map
                .get(yk("name"))
                .and_then(Value::as_str)
                .map(str::to_string);
            let has_comment = map.get(yk("comment")).is_some();
            let name = BindingName::new(minified.unwrap_or_default(), readable_name);
            if name.matches(sym) {
                out.push(BindingMatch {
                    file: file.clone(),
                    module_path: module_path.clone(),
                    member_index: idx,
                    name,
                    has_comment,
                });
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
                        "  {} (binding={}, name={})",
                        m.file.display(),
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
        let module = spec_modules::read_module_file(&file)?;
        per_module_counts.insert(module_path.clone(), module.members.len());
        for member in module.members {
            let entry = BindingEntry {
                name: BindingName::new(member.selector.binding.name, member.name.clone()),
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

/// Outcome of a rename. Currently just confirms which file and index
/// were touched; downstream callers may want to use the file path to
/// produce a diff in a future iteration.
#[derive(Debug, Clone)]
pub struct RenameOutcome {
    pub file: PathBuf,
    pub binding_name: String,
    pub old_readable: Option<String>,
    pub new_readable: String,
    pub action: &'static str,
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
        let clashes = find_readable_collisions(modules_root, new, &hit.file, hit.member_index)?;
        if !clashes.is_empty() {
            bail!(
                "name collision: \"{new}\" already used by:\n{}",
                clashes.join("\n")
            );
        }
    }
    let mut doc = read_yaml(&hit.file)?;
    let old_readable = current_readable_name(&doc, hit.member_index)?;
    set_readable_name(&mut doc, hit.member_index, new)?;
    let changed = yaml_semantically_changed(&hit.file, &doc)?;
    let action = if !changed {
        "unchanged"
    } else if dry_run {
        "dry-run"
    } else {
        "renamed"
    };
    if changed && !dry_run {
        write_yaml_if_semantic_changed(&hit.file, &doc)?;
    }
    Ok(RenameOutcome {
        file: hit.file,
        binding_name: hit.name.minified().to_string(),
        old_readable,
        new_readable: new.to_string(),
        action,
    })
}

fn find_readable_collisions(
    modules_root: &Path,
    new_readable: &str,
    self_file: &Path,
    self_index: usize,
) -> Result<Vec<String>> {
    let mut clashes = Vec::new();
    for file in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&file, modules_root);
        let doc = read_yaml(&file)?;
        let Some(seq) = doc
            .as_mapping()
            .and_then(|m| m.get(yk("members")))
            .and_then(Value::as_sequence)
        else {
            continue;
        };
        for (idx, member) in seq.iter().enumerate() {
            if file == self_file && idx == self_index {
                continue;
            }
            let Some(map) = member.as_mapping() else {
                continue;
            };
            let readable = map
                .get(yk("name"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            let binding_name = map
                .get(yk("selector"))
                .and_then(Value::as_mapping)
                .and_then(|s| s.get(yk("binding")))
                .and_then(Value::as_mapping)
                .and_then(|b| b.get(yk("name")))
                .and_then(Value::as_str)
                .unwrap_or_default();
            // Binding-name fallback: a member without an explicit
            // readable name keeps the minified name as its public
            // identity. A rename target that matches that minified
            // name is still a clash.
            let effective = if readable.is_empty() {
                binding_name
            } else {
                readable
            };
            if effective == new_readable {
                clashes.push(format!(
                    "  {} (binding={}, module={})",
                    file.display(),
                    binding_name,
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
    pub moves_applied: usize,
    pub files_written: Vec<String>,
    pub files_deleted: Vec<String>,
    pub action: &'static str,
}

/// Parse a positional `<sym>:<module>[:<readable>]` triple.
pub fn parse_move_triple(s: &str) -> Result<Move> {
    let parts: Vec<&str> = s.splitn(3, ':').collect();
    if parts.len() < 2 {
        bail!("expected `<sym>:<module>[:<readable>]`, got {s:?} (one colon at minimum)");
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
             directly:\n  - {}\nSelect only binding-only fresh/extension proposals, or handle \
             `merge_into` / `anonymous_statement_owner_ids` rows with `modules merge` or manual YAML.",
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
///   * Last-wins for duplicate `sym` in the batch (with a stderr warn).
///   * Destination modules are auto-created.
///   * Source modules drained are deleted iff they have no
///     module-level `comment:` AND no remaining
///     `anonymous_statements:`.
///   * When `owner_graph_path` is `Some` and `no_verify` is `false`,
///     the unified realizability gate
///     ([`crate::edit_gate::gate_post_edit_partition`]) runs against
///     the in-memory post-batch spec; cycle or atom-split rejections
///     bail before any file is written. Pass `None` to skip the gate
///     (collision detection still runs); the CLI dispatcher requires
///     `--graph` unless `--no-verify` is set.
pub fn run_bindings_assign(
    modules_root: &Path,
    moves: Vec<Move>,
    dry_run: bool,
    no_verify: bool,
    owner_graph_path: Option<&Path>,
    source_root: Option<&Path>,
) -> Result<AssignOutcome> {
    // Step 1: dedupe moves (last-wins per sym).
    let mut by_sym: BTreeMap<String, Move> = BTreeMap::new();
    for m in moves {
        if let Some(prev) = by_sym.insert(m.sym.clone(), m.clone()) {
            eprintln!(
                "warning: duplicate sym {:?} in batch; later move ({:?}) wins over earlier \
                 ({:?})",
                m.sym, m.module, prev.module
            );
        }
    }
    let moves: Vec<Move> = by_sym.into_values().collect();
    if moves.is_empty() {
        return Ok(AssignOutcome {
            moves_applied: 0,
            files_written: Vec::new(),
            files_deleted: Vec::new(),
            action: "noop",
        });
    }

    // Step 2: load every module YAML once.
    let mut docs = load_module_docs(modules_root)?;

    // Step 3: locate each sym's current home + the member entry.
    let mut plan: Vec<PlannedMove> = Vec::new();
    for m in &moves {
        let hit = resolve_unambiguous(modules_root, &m.sym)?;
        plan.push(PlannedMove {
            req: m.clone(),
            source_module: hit.module_path.clone(),
            source_index: hit.member_index,
        });
    }

    // Step 4: detect destination duplicate-binding claims.
    //
    // After all moves are applied, each (module, binding_name)
    // pair must be unique. Compute the target set.
    let mut planned_destinations: BTreeMap<(String, String), &PlannedMove> = BTreeMap::new();
    for p in &plan {
        let key = (p.req.module.clone(), p.req.sym.clone());
        if let Some(prev) = planned_destinations.insert(key.clone(), p) {
            bail!(
                "duplicate-claim: two moves both want binding {} in module {}: previous \
                 from {}, latest from {}",
                key.1,
                key.0,
                prev.source_module,
                p.source_module
            );
        }
    }

    // Step 5: pull each member out of its source doc.
    let mut pulled: BTreeMap<String, Value> = BTreeMap::new();
    // First pass: extract each (and rename if requested).
    for p in &plan {
        let Some((_, doc)) = docs.get_mut(&p.source_module) else {
            bail!("source module {:?} not in tree", p.source_module);
        };
        let mut member = take_member(doc, p.source_index)?;
        if let Some(new_readable) = &p.req.readable {
            if let Some(map) = member.as_mapping_mut() {
                map.insert(yk("name"), Value::String(new_readable.clone()));
            }
        }
        pulled.insert(p.req.sym.clone(), member);
    }
    // Second pass: re-key source docs so the member sequence is
    // contiguous (drop the now-removed entries). `take_member` left
    // a sentinel `Null`; collapse them.
    for (_, (_, doc)) in docs.iter_mut() {
        collapse_null_members(doc);
    }

    // Step 6: collision detection for renames (basic).
    if !no_verify {
        for p in &plan {
            let Some(new_readable) = &p.req.readable else {
                continue;
            };
            // The check is against the post-state docs.
            let mut hits = Vec::new();
            for (mp, (file, doc)) in &docs {
                let Some(seq) = members_seq(doc) else {
                    continue;
                };
                for (idx, member) in seq.iter().enumerate() {
                    let Some(map) = member.as_mapping() else {
                        continue;
                    };
                    let readable = map
                        .get(yk("name"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if readable == new_readable {
                        hits.push(format!("  {} ({}@{})", file.display(), mp, idx));
                    }
                }
            }
            // Also check the pulled bin: another move might write the
            // same readable name to a different destination.
            let mut pulled_hits = 0usize;
            for (other_sym, member) in &pulled {
                if other_sym == &p.req.sym {
                    continue;
                }
                if member
                    .as_mapping()
                    .and_then(|m| m.get(yk("name")))
                    .and_then(Value::as_str)
                    == Some(new_readable.as_str())
                {
                    pulled_hits += 1;
                }
            }
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

    // Step 6.5: realizability + atom-split gate against the
    // post-batch spec. Runs the same check `debundle modules merge`
    // / `debundle modules delete --force` use, so all three mutating
    // verbs share one rejection signal (cycles + atom-split). Skip
    // when `--no-verify` is set; bail when `owner_graph_path` is
    // missing — the CLI dispatcher enforces "graph or no-verify".
    if !no_verify {
        if let Some(graph_path) = owner_graph_path {
            let removals: Vec<(PathBuf, String)> = plan
                .iter()
                .map(|p| {
                    (
                        modules_root.join(format!("{}.yaml", p.source_module)),
                        p.req.sym.clone(),
                    )
                })
                .collect();
            let insertions: Vec<(PathBuf, String)> = plan
                .iter()
                .map(|p| {
                    (
                        modules_root.join(format!("{}.yaml", p.req.module)),
                        p.req.sym.clone(),
                    )
                })
                .collect();
            let post_spec = post_assign_spec(modules_root, &removals, &insertions)?;
            gate_post_edit_partition(graph_path, modules_root, source_root, &post_spec)?;
        }
    }

    // Step 7: splice into destinations (auto-create missing).
    for p in &plan {
        // Resolve destination doc (create if absent).
        let dest_path = p.req.module.clone();
        if !docs.contains_key(&dest_path) {
            let mut map = Mapping::new();
            map.insert(yk("members"), Value::Sequence(Vec::new()));
            let dest_file = modules_root.join(format!("{dest_path}.yaml"));
            docs.insert(dest_path.clone(), (dest_file, Value::Mapping(map)));
        }
        let member = pulled.remove(&p.req.sym).expect("pulled member missing");
        let (_, doc) = docs.get_mut(&dest_path).expect("dest just created");
        push_member(doc, member)?;
    }

    // Step 8: identify drained-but-not-deleted source modules
    // (keep modules whose module-level `comment:` is set or that
    // still carry anonymous_statements).
    let mut to_delete: Vec<String> = Vec::new();
    for (mp, (_, doc)) in &docs {
        if is_residual_module_path(mp) {
            continue;
        }
        let members_empty = members_seq(doc).is_none_or(|s| s.is_empty());
        if !members_empty {
            continue;
        }
        let has_comment = doc
            .as_mapping()
            .and_then(|m| m.get(yk("comment")))
            .is_some();
        let has_anon = doc
            .as_mapping()
            .and_then(|m| m.get(yk("anonymous_statements")))
            .and_then(Value::as_sequence)
            .is_some_and(|s| !s.is_empty());
        if !has_comment && !has_anon {
            // Only delete if this module was actually a source of a
            // move (i.e. we drained it just now). New empty modules
            // we just created should not be deleted; but the order
            // of operations means destinations always get >=1 push,
            // so members_empty would be false for them.
            to_delete.push(mp.clone());
        }
    }

    let mut files_written: Vec<String> = Vec::new();
    let mut files_deleted: Vec<String> = Vec::new();
    let action = if dry_run { "dry-run" } else { "applied" };
    for (mp, (file, doc)) in &docs {
        if to_delete.contains(mp) {
            if !dry_run && file.exists() {
                fs::remove_file(file).with_context(|| format!("rm {}", file.display()))?;
            }
            files_deleted.push(file.display().to_string());
            continue;
        }
        let changed = yaml_semantically_changed(file, doc)?;
        if changed && !dry_run {
            if let Some(parent) = file.parent() {
                fs::create_dir_all(parent).ok();
            }
            write_yaml_if_semantic_changed(file, doc)?;
        }
        if changed {
            files_written.push(file.display().to_string());
        }
    }
    Ok(AssignOutcome {
        moves_applied: plan.len(),
        files_written,
        files_deleted,
        action,
    })
}

#[derive(Debug, Clone)]
struct PlannedMove {
    req: Move,
    source_module: String,
    source_index: usize,
}

fn take_member(doc: &mut Value, index: usize) -> Result<Value> {
    let seq = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
        .ok_or_else(|| anyhow!("module YAML missing members sequence"))?;
    if index >= seq.len() {
        bail!("member index {index} out of range");
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

// ---------------------------------------------------------------------
// `bindings unassign`
// ---------------------------------------------------------------------

/// Outcome of an unassign batch. Mirrors [`AssignOutcome`] so the CLI
/// printer can share a renderer if desired; the field set is
/// deliberately the same shape.
#[derive(Debug, Clone, serde::Serialize)]
pub struct UnassignOutcome {
    pub unassigned: usize,
    pub files_written: Vec<String>,
    pub files_deleted: Vec<String>,
    pub action: &'static str,
}

/// Remove one or more bindings from their current modules atomically.
/// Source modules drained of members are deleted unless they carry a
/// module-level `comment:` or remaining `anonymous_statements:` —
/// same drain rule as `run_bindings_assign`.
///
/// After unassign, the bindings fall through to residual (the default
/// when an owner isn't claimed by any spec module's `members:`). The
/// realizability + atom-split gate runs against the post-batch spec
/// the same way `bindings assign` does. The CLI dispatcher enforces
/// the "graph or no-verify" policy.
///
/// Contract:
///   * Dedupe by sym (warn on duplicates; later wins).
///   * Each sym must resolve to exactly one member via
///     [`resolve_unambiguous`].
///   * Source modules drained are deleted iff they have no
///     module-level `comment:` AND no remaining
///     `anonymous_statements:`.
///   * When `owner_graph_path` is `Some` and `no_verify` is `false`,
///     the gate runs against the in-memory post-batch spec; cycle or
///     atom-split rejections bail before any file is written.
pub fn run_bindings_unassign(
    modules_root: &Path,
    syms: Vec<String>,
    dry_run: bool,
    no_verify: bool,
    owner_graph_path: Option<&Path>,
    source_root: Option<&Path>,
) -> Result<UnassignOutcome> {
    // Step 1: dedupe syms (later-wins isn't meaningful here, just
    // dedupe; warn so authors don't ship typos as silent duplicates).
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut syms_unique: Vec<String> = Vec::new();
    for s in syms {
        if !seen.insert(s.clone()) {
            eprintln!("warning: duplicate sym {s:?} in batch; ignoring repeat");
            continue;
        }
        syms_unique.push(s);
    }
    if syms_unique.is_empty() {
        return Ok(UnassignOutcome {
            unassigned: 0,
            files_written: Vec::new(),
            files_deleted: Vec::new(),
            action: "noop",
        });
    }

    // Step 2: load every module YAML once.
    let mut docs = load_module_docs(modules_root)?;

    // Step 3: locate each sym's current home + the member entry.
    let mut plan: Vec<PlannedUnassign> = Vec::new();
    for s in &syms_unique {
        let hit = resolve_unambiguous(modules_root, s)?;
        plan.push(PlannedUnassign {
            sym: s.clone(),
            source_module: hit.module_path.clone(),
            source_index: hit.member_index,
        });
    }

    // Step 4: gate the post-batch spec (cycles + atom-split). Runs
    // before any mutation. CLI dispatcher enforces "graph or
    // no-verify" so reaching here without a graph + with !no_verify
    // would be a programmer error.
    if !no_verify {
        if let Some(graph_path) = owner_graph_path {
            let removals: Vec<(PathBuf, String)> = plan
                .iter()
                .map(|p| {
                    (
                        modules_root.join(format!("{}.yaml", p.source_module)),
                        p.sym.clone(),
                    )
                })
                .collect();
            let post_spec = post_unassign_spec(modules_root, &removals)?;
            gate_post_edit_partition(graph_path, modules_root, source_root, &post_spec)?;
        }
    }

    // Step 5: drop each member from its source doc. Same null-sentinel
    // + collapse-after pattern `run_bindings_assign` uses so multiple
    // unassigns from the same module don't shift indices mid-pass.
    for p in &plan {
        let Some((_, doc)) = docs.get_mut(&p.source_module) else {
            bail!("source module {:?} not in tree", p.source_module);
        };
        let _ = take_member(doc, p.source_index)?;
    }
    for (_, (_, doc)) in docs.iter_mut() {
        collapse_null_members(doc);
    }

    // Step 6: identify drained-but-not-deleted source modules.
    let mut to_delete: Vec<String> = Vec::new();
    for (mp, (_, doc)) in &docs {
        if is_residual_module_path(mp) {
            continue;
        }
        let members_empty = members_seq(doc).is_none_or(|s| s.is_empty());
        if !members_empty {
            continue;
        }
        let has_comment = doc
            .as_mapping()
            .and_then(|m| m.get(yk("comment")))
            .is_some();
        let has_anon = doc
            .as_mapping()
            .and_then(|m| m.get(yk("anonymous_statements")))
            .and_then(Value::as_sequence)
            .is_some_and(|s| !s.is_empty());
        if !has_comment && !has_anon {
            to_delete.push(mp.clone());
        }
    }

    let mut files_written: Vec<String> = Vec::new();
    let mut files_deleted: Vec<String> = Vec::new();
    let action = if dry_run { "dry-run" } else { "applied" };
    for (mp, (file, doc)) in &docs {
        if to_delete.contains(mp) {
            if !dry_run && file.exists() {
                fs::remove_file(file).with_context(|| format!("rm {}", file.display()))?;
            }
            files_deleted.push(file.display().to_string());
            continue;
        }
        let changed = yaml_semantically_changed(file, doc)?;
        if changed && !dry_run {
            if let Some(parent) = file.parent() {
                fs::create_dir_all(parent).ok();
            }
            write_yaml_if_semantic_changed(file, doc)?;
        }
        if changed {
            files_written.push(file.display().to_string());
        }
    }
    Ok(UnassignOutcome {
        unassigned: plan.len(),
        files_written,
        files_deleted,
        action,
    })
}

#[derive(Debug, Clone)]
struct PlannedUnassign {
    sym: String,
    source_module: String,
    source_index: usize,
}

// ---------------------------------------------------------------------
// YAML helpers (re-declared here so this submodule remains
// independent of `cli::comment`; both share the same shape but
// neither imports the other).
// ---------------------------------------------------------------------

fn yk(s: &str) -> Value {
    Value::String(s.to_string())
}

fn current_readable_name(doc: &Value, index: usize) -> Result<Option<String>> {
    let seq = doc
        .as_mapping()
        .and_then(|m| m.get(yk("members")))
        .and_then(Value::as_sequence)
        .ok_or_else(|| anyhow!("module YAML missing members sequence"))?;
    let member = seq
        .get(index)
        .ok_or_else(|| anyhow!("member index {index} out of range"))?;
    Ok(member
        .as_mapping()
        .and_then(|m| m.get(yk("name")))
        .and_then(Value::as_str)
        .map(str::to_string))
}

fn set_readable_name(doc: &mut Value, index: usize, name: &str) -> Result<()> {
    let seq = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
        .ok_or_else(|| anyhow!("module YAML missing members sequence"))?;
    let member = seq
        .get_mut(index)
        .ok_or_else(|| anyhow!("member index {index} out of range"))?;
    let map = member
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("member entry is not a mapping"))?;
    map.insert(yk("name"), Value::String(name.to_string()));
    Ok(())
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

        assert_eq!(out.action, "unchanged");
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
        let out = run_bindings_assign(root, moves, false, false, None, None).unwrap();
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
        run_bindings_assign(root, moves, false, false, None, None).unwrap();
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
        run_bindings_assign(root, moves, false, false, None, None).unwrap();
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
        let out = run_bindings_assign(root, moves, true, false, None, None).unwrap();
        assert_eq!(out.action, "dry-run");
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
