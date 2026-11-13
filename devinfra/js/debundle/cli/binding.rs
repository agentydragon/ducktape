//! Mutating + listing operations on the spec's per-module member
//! entries: `bindings list`, `bindings rename`, `bindings assign`,
//! `bindings unassign`.
//!
//! The shared invariants:
//!
//! * `<sym>` resolves against the spec: `selector.binding.name`, a source-match
//!   template-local identifier, or an explicit readable `name:`. Graph query
//!   aliases are separate. Ambiguous spec identifiers produce a list of matches.
//! * Mutating commands validate-by-default (atomic post-batch state)
//!   and refuse on collision / atom-split rejection.
//! * Edits use the canonical module schema; changed YAML is reserialized.
//!
//! The per-command contract lives in the clap doc-comments
//! (`cli/bindings_commands.rs`); cross-command semantics (batch atomicity, rejection
//! diagnostics) in `docs/cli.md`.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use graph_provenance::VerifiedGraphSourceContext;
use selector_outcome::SelectorKind;
use serde::Deserialize;

use crate::candidate_assessment::EffectAssessmentUnavailable;
use pipeline::{AnalysisArtifactInput, TransformRequest, assess_candidate_effect_relocation};
use spec::{
    BindingAnnotation, LogicalModule, Member, MemberSelectorSpec, ModulePath, SourceMatchBinding,
    SourceMatchBindingDetail, SourceMatchClaim, is_residual_namespace_path,
};
use spec_modules::{collect_module_files, module_path_from_file, read_module_file};
use yaml_edit::write_yaml_atomic;

use crate::edit_gate::{Gate, post_edit_spec_from_docs};
use crate::outcome::{GateOutcome, MutationOutcome};

/// The source of a binding's current spec identity.
///
/// A direct selector identifier and a source-match capture are both names used
/// by the spec; neither spelling by itself says whether the name is volatile.
/// Structural members have no direct binding identifier.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BindingIdentity {
    DirectBindingIdentifier { name: String },
    SourceMatchCapture { local: String },
    StructuralMember { selector: SelectorKind },
}

impl BindingIdentity {
    fn selector_identifier(&self) -> Option<&str> {
        match self {
            Self::DirectBindingIdentifier { name } => Some(name),
            Self::SourceMatchCapture { local } => Some(local),
            Self::StructuralMember { .. } => None,
        }
    }
}

impl fmt::Display for BindingIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DirectBindingIdentifier { name } => {
                write!(f, "direct binding identifier {name:?}")
            }
            Self::SourceMatchCapture { local } => write!(f, "source-match capture {local:?}"),
            Self::StructuralMember { selector } => {
                write!(f, "structural member ({})", selector.origin(None))
            }
        }
    }
}

/// A spec identity and its optional explicitly configured readable name.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct BindingLabel {
    pub identity: BindingIdentity,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub readable_name: Option<String>,
}

impl BindingLabel {
    pub(crate) fn new(identity: BindingIdentity, readable_name: Option<String>) -> Self {
        Self {
            identity,
            readable_name,
        }
    }

    pub(crate) fn effective_name(&self) -> &str {
        self.readable_name
            .as_deref()
            .or_else(|| self.identity.selector_identifier())
            .expect("a listed binding has an identity or readable name")
    }

    pub(crate) fn is_renamed(&self) -> bool {
        self.readable_name.is_some()
    }

    /// Match a direct selector identifier, a source-match capture, or an
    /// explicit readable name. Structural members match only by readable name.
    fn matches(&self, query: &str) -> bool {
        self.identity.selector_identifier() == Some(query)
            || self.readable_name.as_deref() == Some(query)
    }
}

/// A located member inside a module file. Returned by [`resolve_unambiguous`]
/// and is the unit `assign` / `rename` inspect or mutate.
#[derive(Debug, Clone)]
pub struct BindingMatch {
    pub file: PathBuf,
    pub module_path: String,
    pub location: BindingLocation,
    pub label: BindingLabel,
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

pub(crate) type ModuleDocs = BTreeMap<String, (PathBuf, LogicalModule)>;

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

pub(crate) fn binding_matches_in_doc(
    file: &Path,
    module_path: &str,
    doc: &LogicalModule,
) -> Vec<BindingMatch> {
    let mut out = Vec::new();
    let mut add = |location, label| {
        out.push(BindingMatch {
            file: file.to_path_buf(),
            module_path: module_path.to_string(),
            location,
            label,
        })
    };
    for (member_index, member) in doc.members.iter().enumerate() {
        let identity = match member.selector.validate() {
            Ok(MemberSelectorSpec::Binding(binding)) => {
                BindingIdentity::DirectBindingIdentifier { name: binding.name }
            }
            Ok(structural) => BindingIdentity::StructuralMember {
                selector: SelectorKind::from(&structural),
            },
            // An invalid selector with a `binding.name` still resolves by that name.
            Err(_) if let Some(binding) = &member.selector.binding => {
                BindingIdentity::DirectBindingIdentifier {
                    name: binding.name.clone(),
                }
            }
            Err(_) => BindingIdentity::StructuralMember {
                selector: SelectorKind::UnparsedMember,
            },
        };
        let label = BindingLabel::new(identity, member.name.clone());
        if label
            .identity
            .selector_identifier()
            .is_some_and(|name| !name.is_empty())
            || label.readable_name.is_some()
        {
            add(BindingLocation::Member { member_index }, label);
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
                    BindingLabel::new(
                        BindingIdentity::SourceMatchCapture {
                            local: binding.local().to_string(),
                        },
                        source_match_readable(binding),
                    ),
                );
            }
        }
    }
    out
}

pub(crate) fn binding_effective_name(label: &BindingLabel) -> &str {
    label.effective_name()
}

pub(crate) fn source_match_readable(binding: &SourceMatchBinding) -> Option<String> {
    match binding {
        SourceMatchBinding::Local(_) => None,
        SourceMatchBinding::Detailed(detail) => detail.name.clone().filter(|name| !name.is_empty()),
    }
}

pub(crate) fn matches_in_docs(docs: &ModuleDocs, sym: &str) -> Vec<BindingMatch> {
    let mut matches = Vec::new();
    for (module, (file, doc)) in docs {
        matches.extend(binding_matches_in_doc(file, module, doc));
    }
    matches
        .into_iter()
        .filter(|binding| binding.label.matches(sym))
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
        0 => {
            let modules_arg = modules_root.display().to_string();
            let modules_arg = shlex::try_quote(&modules_arg)
                .context("module path cannot be represented in a POSIX shell command")?;
            bail!(
                "no binding named \"{sym}\" found under {}\nList current spec identifiers with:\n  debundle bindings list --modules {modules_arg}",
                modules_root.display()
            );
        }
        1 => Ok(matches.into_iter().next().unwrap()),
        _ => {
            let locations: Vec<String> = matches
                .iter()
                .map(|m| {
                    format!(
                        "  {}#{} (identity={}, readable_name={})",
                        m.file.display(),
                        m.location.describe(),
                        m.label.identity,
                        m.label.readable_name.as_deref().unwrap_or("-")
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
    /// Direct binding identifiers, source-match captures, or structural members,
    /// with an optional explicit readable name.
    #[serde(flatten)]
    pub label: BindingLabel,
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
            label: binding.label,
            module: module_path.clone(),
            orphan,
        }));
    }
    entries.retain(|e| {
        (filters.in_module.as_deref().is_none_or(|m| e.module == m))
            && (!filters.unrenamed || !e.label.is_renamed())
            && (!filters.orphan || e.orphan)
    });
    entries.sort_by(|a, b| {
        (
            a.module.as_str(),
            a.label.identity.selector_identifier().unwrap_or(""),
        )
            .cmp(&(
                b.module.as_str(),
                b.label.identity.selector_identifier().unwrap_or(""),
            ))
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
    /// Typed spec identity of the renamed member.
    pub identity: BindingIdentity,
    pub old_readable: Option<String>,
    pub new_readable: String,
}

/// Rename a single binding's readable `name:` without moving it.
///
/// `original` accepts the current spec identifier or readable name. `new`
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
        bail!("neither <original> nor <readable> may contain `:`");
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
    let old_readable = hit.label.readable_name.clone();
    let old_effective = hit.label.effective_name().to_string();
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
            needs_full_assessment: false,
            files_written: if changed {
                vec![hit.file.display().to_string()]
            } else {
                Vec::new()
            },
            files_deleted: Vec::new(),
        },
        identity: hit.label.identity.clone(),
        old_readable,
        new_readable: new.to_string(),
    })
}

/// A names-only request resolved against the invocation's original spec.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rename {
    pub sym: String,
    pub readable: String,
}

#[derive(Debug, serde::Serialize)]
pub struct RenameResult {
    pub sym: String,
    pub module: String,
    pub identity: BindingIdentity,
    pub old_readable: String,
    pub new_readable: String,
}

#[derive(Debug, serde::Serialize)]
pub struct RenameBatchOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub renames_applied: usize,
    pub renames: Vec<RenameResult>,
}

/// Preflight the complete final map before replacing any file. Persistence is
/// atomic per file, rather than a crash-atomic transaction across the tree.
pub fn rename_bindings(
    stderr: &mut dyn std::io::Write,
    modules_root: &Path,
    requests: Vec<Rename>,
    dry_run: bool,
    no_verify: bool,
) -> Result<RenameBatchOutcome> {
    let original_docs = load_module_docs(modules_root)?;
    let mut resolved = BTreeMap::new();
    for request in requests {
        if request.readable.trim().is_empty() {
            bail!("readable name must not be blank");
        }
        let hit = resolve_in_docs(modules_root, &original_docs, &request.sym)?;
        let identity = (hit.module_path.clone(), hit.location);
        if let Some((previous, _)) = resolved.get(&identity) {
            let previous: &Rename = previous;
            if previous.readable != request.readable {
                bail!(
                    "contradictory renames for {}#{}",
                    hit.file.display(),
                    hit.location.describe()
                );
            }
            command_diagnostics::write_diagnostic(
                stderr,
                format_args!(
                    "duplicate rename for {}#{} collapsed",
                    hit.file.display(),
                    hit.location.describe()
                ),
            )?;
        } else {
            resolved.insert(identity, (request, hit));
        }
    }
    // Only requested targets introduce a constraint: unrelated pre-existing
    // duplicate names remain outside this edit's verdict.
    if !no_verify {
        for (identity, (request, _)) in &resolved {
            for (module, (file, doc)) in &original_docs {
                for binding in binding_matches_in_doc(file, module, doc) {
                    let other = (module.clone(), binding.location);
                    if &other == identity {
                        continue;
                    }
                    let final_name = resolved
                        .get(&other)
                        .map(|(rename, _)| rename.readable.as_str())
                        .unwrap_or_else(|| binding_effective_name(&binding.label));
                    if final_name == request.readable {
                        bail!(
                            "name collision: \"{}\" used by {}#{}",
                            request.readable,
                            file.display(),
                            binding.location.describe()
                        );
                    }
                }
            }
        }
    }
    let mut docs = original_docs.clone();
    let mut annotations = BTreeMap::new();
    // Remove every moving key before inserting any target key. Metadata belongs
    // to a binding only when that old effective name has one owner in its module.
    for (identity, (request, hit)) in &resolved {
        let old = binding_effective_name(&hit.label);
        if old == request.readable {
            continue;
        }
        let (file, original) = &original_docs[&hit.module_path];
        if original.annotations.contains_key(old) {
            let owners = binding_matches_in_doc(file, &hit.module_path, original)
                .into_iter()
                .filter(|binding| binding_effective_name(&binding.label) == old)
                .count();
            if owners != 1 {
                bail!(
                    "ambiguous annotation ownership: {} annotations.{old}",
                    file.display()
                );
            }
        }
        let (_, doc) = docs
            .get_mut(&hit.module_path)
            .expect("resolved module is loaded");
        annotations.insert(identity.clone(), doc.annotations.remove(old));
    }
    let mut renames = Vec::new();
    let mut renames_applied = 0;
    for (identity, (request, hit)) in &resolved {
        let old = binding_effective_name(&hit.label);
        if old != request.readable {
            let (_, doc) = docs
                .get_mut(&hit.module_path)
                .expect("resolved module is loaded");
            if doc.annotations.contains_key(&request.readable) {
                bail!(
                    "annotations.{} already exists in {}",
                    request.readable,
                    hit.file.display()
                );
            }
            set_readable_name(doc, &hit.location, &request.readable);
            insert_annotation(
                doc,
                &request.readable,
                annotations.remove(identity).flatten(),
            )?;
            renames_applied += 1;
        }
        renames.push(RenameResult {
            sym: request.sym.clone(),
            module: hit.module_path.clone(),
            identity: hit.label.identity.clone(),
            old_readable: old.to_string(),
            new_readable: request.readable.clone(),
        });
    }
    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &BTreeSet::new(), dry_run)?;
    Ok(RenameBatchOutcome {
        outcome: MutationOutcome {
            verb: "rename",
            action: if resolved.is_empty() {
                "noop"
            } else if files_written.is_empty() {
                "unchanged"
            } else if dry_run {
                "dry-run"
            } else {
                "applied"
            },
            gate: if resolved.is_empty() {
                GateOutcome::NotRequired
            } else if no_verify {
                GateOutcome::Skipped
            } else {
                GateOutcome::NamesOnly
            },
            needs_full_assessment: false,
            files_written,
            files_deleted,
        },
        renames_applied,
        renames,
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
            if binding_effective_name(&binding.label) == new_readable {
                clashes.push(format!(
                    "  {}#{} (identity={}, module={})",
                    file.display(),
                    binding.location.describe(),
                    binding.label.identity,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effect_relocation_assessment: Option<gate::CandidateRelocationAssessmentReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effect_assessment_unavailable: Option<EffectAssessmentUnavailable>,
}

#[derive(Clone, Copy)]
pub struct CandidateEffectAssessmentContext<'a> {
    pub transform: &'a TransformRequest,
    pub analysis: AnalysisArtifactInput<'a>,
    pub graph_source: &'a VerifiedGraphSourceContext,
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
///     ([`Gate::check`]) against the
///     in-memory post-batch spec; cycle or atom-split rejections
///     bail before any file is written. [`Gate::Skip`] (`--no-verify`)
///     skips validation. The CLI dispatcher requires `--graph` unless
///     `--no-verify` is set ([`Gate::from_cli`]).
pub fn run_bindings_assign(
    stderr: &mut dyn std::io::Write,
    modules_root: &Path,
    moves: Vec<Move>,
    dry_run: bool,
    gate: Gate<'_>,
    effect_assessment_context: Option<CandidateEffectAssessmentContext<'_>>,
) -> Result<AssignOutcome> {
    let no_moves = moves.is_empty();
    if no_moves && effect_assessment_context.is_none() {
        return Ok(AssignOutcome {
            outcome: MutationOutcome {
                verb: "assign",
                action: "noop",
                gate: GateOutcome::NotRequired,
                needs_full_assessment: false,
                files_written: Vec::new(),
                files_deleted: Vec::new(),
            },
            moves_applied: 0,
            effect_relocation_assessment: None,
            effect_assessment_unavailable: Some(EffectAssessmentUnavailable::AnalysisNotSupplied),
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
            source_effective_name: binding_effective_name(&hit.label).to_string(),
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
                command_diagnostics::write_diagnostic(
                    stderr,
                    format_args!(
                        "warning: batch contains duplicate moves for one member ({:?} / {:?}); \
                     collapsed",
                        prev.req.sym, planned.req.sym,
                    ),
                )?;
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
    // effective-identity predicate `bindings rename` uses: an entry without
    // an explicit readable name keeps its current spec identity as its public
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
                    let effective_name = binding
                        .label
                        .readable_name
                        .as_deref()
                        .filter(|name| !name.is_empty())
                        .or_else(|| binding.label.identity.selector_identifier())
                        .unwrap_or("");
                    if effective_name == new_readable {
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
    let effect_relocation_assessment = if let Some(context) = effect_assessment_context {
        let candidate_modules = docs
            .iter()
            .filter(|(path, _)| !to_delete.contains(*path))
            .map(|(path, (_, doc))| (path.clone(), doc.clone()))
            .collect();
        Some(command_diagnostics::run(stderr, |diagnostics| {
            assess_candidate_effect_relocation(
                context.transform,
                context.analysis,
                context.graph_source,
                modules_root,
                &candidate_modules,
                diagnostics,
            )
        })?)
    } else {
        None
    };
    if let Some(assessment) = &effect_relocation_assessment {
        pipeline::ensure_candidate_emittable_for_write(assessment, dry_run)?;
    }
    if !no_moves {
        gate.check(stderr, modules_root, || {
            post_edit_spec_from_docs(&docs, &to_delete)
        })?;
    }

    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    Ok(AssignOutcome {
        outcome: MutationOutcome::ownership_edit(
            "assign",
            if no_moves {
                "noop"
            } else if dry_run {
                "dry-run"
            } else {
                "applied"
            },
            if no_moves {
                GateOutcome::NotRequired
            } else {
                gate.outcome()
            },
            effect_relocation_assessment.as_ref(),
            files_written,
            files_deleted,
        ),
        moves_applied: plan.len(),
        effect_assessment_unavailable: effect_relocation_assessment
            .is_none()
            .then_some(EffectAssessmentUnavailable::AnalysisNotSupplied),
        effect_relocation_assessment,
    })
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
/// set, else the direct `selector.binding.name`. `bindings rename` and
/// `bindings assign` share this predicate so both treat the current spec
/// identity as a claimed name.
pub(crate) fn member_effective_name(member: &Member) -> Option<String> {
    member
        .name
        .clone()
        .filter(|name| !name.is_empty())
        .or_else(|| member_selector_binding_name(member))
}

pub(crate) fn insert_annotation(
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

fn member_selector_binding_name(member: &Member) -> Option<String> {
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
pub(crate) fn drained_source_modules(
    docs: &ModuleDocs,
    move_sources: &BTreeSet<String>,
) -> BTreeSet<String> {
    move_sources
        .iter()
        .filter(|mp| {
            if is_residual_namespace_path(mp) {
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
pub(crate) fn take_members(
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
pub(crate) fn take_source_match_bindings(
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
        hit.label.identity,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effect_relocation_assessment: Option<gate::CandidateRelocationAssessmentReport>,
}

/// Remove one or more bindings from their current modules as one validated batch.
/// Source modules drained of members are deleted unless they carry a
/// module-level `comment:` or `note:`, remaining `source_matches:`, `annotations:`,
/// or `anonymous_statements:` — same drain rule as
/// `run_bindings_assign`.
///
/// After unassign, the bindings fall through to residual (the default
/// when an source unit isn't claimed by any spec module's `members:`). The
/// realizability + atom-split gate runs against the post-batch spec
/// the same way `bindings assign` does. The CLI dispatcher enforces
/// the "graph or no-verify" policy ([`Gate::from_cli`]).
///
/// Contract:
///   * Each sym must resolve to exactly one member via
///     [`resolve_unambiguous`]; syms are deduplicated on the resolved
///     member identity (warn on duplicates), so its spec identifier and
///     readable name collapse to one removal.
///   * Only modules that were sources of a removal in THIS batch are
///     swept after draining.
///   * [`Gate::Run`] gates the in-memory post-batch spec; cycle or
///     atom-split rejections bail before any file is written.
pub fn run_bindings_unassign(
    stderr: &mut dyn std::io::Write,
    modules_root: &Path,
    syms: Vec<String>,
    dry_run: bool,
    gate: Gate<'_>,
    effect_assessment_context: Option<CandidateEffectAssessmentContext<'_>>,
) -> Result<UnassignOutcome> {
    let no_symbols = syms.is_empty();
    if no_symbols && effect_assessment_context.is_none() {
        return Ok(UnassignOutcome {
            outcome: MutationOutcome {
                verb: "unassign",
                action: "noop",
                gate: GateOutcome::NotRequired,
                needs_full_assessment: false,
                files_written: Vec::new(),
                files_deleted: Vec::new(),
            },
            unassigned: 0,
            effect_relocation_assessment: None,
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
            command_diagnostics::write_diagnostic(
                stderr,
                format_args!(
                    "warning: duplicate sym in batch ({prev:?} / {s:?} resolve to one member); \
                 ignoring repeat"
                ),
            )?;
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

    // Assess the exact typed post-edit documents before the legacy gate or any
    // writes. The pipeline resolves this candidate through the same canonical
    // selector and emitter-planning path as `run --analysis`.
    let effect_relocation_assessment = if let Some(context) = effect_assessment_context {
        let candidate_modules = docs
            .iter()
            .filter(|(path, _)| !to_delete.contains(*path))
            .map(|(path, (_, doc))| (path.clone(), doc.clone()))
            .collect();
        Some(command_diagnostics::run(stderr, |diagnostics| {
            assess_candidate_effect_relocation(
                context.transform,
                context.analysis,
                context.graph_source,
                modules_root,
                &candidate_modules,
                diagnostics,
            )
        })?)
    } else {
        None
    };

    // Step 5: gate the in-memory post-batch spec (cycles +
    // atom-split) before any file is written. Built from the mutated
    // docs through the run pipeline's claims model, so source_matches[]
    // claims in surviving modules stay claimed.
    if !no_symbols {
        gate.check(stderr, modules_root, || {
            post_edit_spec_from_docs(&docs, &to_delete)
        })?;
    }

    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    Ok(UnassignOutcome {
        outcome: MutationOutcome::ownership_edit(
            "unassign",
            if no_symbols {
                "noop"
            } else if dry_run {
                "dry-run"
            } else {
                "applied"
            },
            if no_symbols {
                GateOutcome::NotRequired
            } else {
                gate.outcome()
            },
            effect_relocation_assessment.as_ref(),
            files_written,
            files_deleted,
        ),
        unassigned: plan.len(),
        effect_relocation_assessment,
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
    use analysis::SourceUnitReport;
    use analysis_model::DepKind;
    use peel::propose::propose;
    use report_fixtures::{
        active_source_unit, atomic_edge, atomic_group_for, claims, dependency_edge, graph_of,
        residual_source_unit,
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
        let input = analysis::DependencyGraphInput::from_report(graph).unwrap();
        let report = propose(&input, active_claims, 10_000).unwrap();
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
            &BTreeMap::new(),
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
            &BTreeMap::new(),
        )
        .unwrap_err();
    }
}
