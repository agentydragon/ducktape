//! CLI verb `debundle modules merge`: splice source YAML modules into a
//! target YAML module and delete the sources. The companion
//! `debundle modules delete --force` verb removes a non-empty module.
//!
//! Both verbs run the unified realizability gate (see
//! [`crate::edit_gate::gate_post_edit_partition`]) against the
//! **post-edit** partition before touching the filesystem. The gate
//! reconstructs the chunk's `OwnerGraph` from `owner_graph.json`
//! (via `OwnerGraph::from_report`), builds the post-edit `Partition`
//! by mapping each surviving spec module's bindings to a fresh
//! `ModuleId`, and runs BOTH `validate_factorization` (cross-module
//! cycles) AND atom-split detection (every `AtomicUnit`'s members
//! must co-locate). Either rejection prints a diagnostic to stderr
//! and exits non-zero without writing any file. `--no-verify` skips
//! the gate; `--dry-run` runs the gate but doesn't write.
//!
//! The splice deserializes the target and each source into the typed
//! [`spec::LogicalModule`] schema, concatenates their `members`,
//! `source_matches`, `annotations`, and `anonymous_statements`, composes the `comment` /
//! `note` blocks, and reserializes. `deny_unknown_fields` makes that
//! round-trip lossless, so the operation never navigates a raw
//! `serde_yaml::Value` tree.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use clap::Args as ClapArgs;
use peel::OutputFormat;
use spec::LogicalModule;
use yaml_edit::apply_yaml_edit;

use crate::edit_gate::{GraphEditArgs, PostEditSpec, post_delete_spec, post_edit_spec_from_docs};
use crate::outcome::{GateOutcome, MutationOutcome, emit_gate_rejection_json, print_outcome_json};

#[derive(Debug, ClapArgs)]
pub struct MergeArgs {
    #[command(flatten)]
    pub edit: GraphEditArgs,

    /// Target module path (relative to --modules) to merge into.
    /// Created if it doesn't exist yet.
    #[arg(long = "target")]
    pub target: PathBuf,

    /// Source module paths (relative to --modules) to merge in. The
    /// `.yaml` suffix is optional: `ai/models/pricing` resolves to
    /// `ai/models/pricing.yaml` even when `ai/models/pricing/` is also
    /// a directory.
    #[arg(required = true)]
    pub sources: Vec<PathBuf>,
}

/// Target and source files shared by merge preview, apply, and CLI reporting.
#[derive(Debug, Clone)]
pub struct MergeSummary {
    /// Absolute path of the rewritten target.
    pub target: PathBuf,
    /// Absolute paths of source files that were merged in and deleted.
    pub merged_sources: Vec<PathBuf>,
}

/// `modules merge` outcome: the shared [`MutationOutcome`] core
/// (target in `files_written`, deleted sources in `files_deleted`)
/// plus the merge target path.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MergeOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub target: String,
}

/// `modules delete` outcome: just the shared core (deleted paths in
/// `files_deleted`).
#[derive(Debug, Clone, serde::Serialize)]
pub struct DeleteOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
}

/// Why `modules merge` cannot compose its documents; the merge refuses before writing anything,
/// in every mode. `target` is the merge target and `source` the source whose claims clash with
/// the target's or an earlier source's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MergeConflict {
    /// The source claims a `members[].selector.binding` name that is already claimed.
    DuplicateSourceBinding {
        name: String,
        target: PathBuf,
        source: PathBuf,
    },
    /// The source claims a readable member name that is already claimed.
    DuplicateReadableName {
        name: String,
        target: PathBuf,
        source: PathBuf,
    },
    /// The source annotates a readable name differently from the target's annotation.
    ConflictingAnnotation {
        name: String,
        target: PathBuf,
        source: PathBuf,
    },
}

impl fmt::Display for MergeConflict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateSourceBinding {
                name,
                target,
                source,
            }
            | Self::DuplicateReadableName {
                name,
                target,
                source,
            } => write!(
                f,
                "duplicate member name \"{name}\" in {} and {}",
                target.display(),
                source.display()
            ),
            Self::ConflictingAnnotation {
                name,
                target,
                source,
            } => write!(
                f,
                "conflicting annotation for \"{name}\" in {} and {}",
                target.display(),
                source.display()
            ),
        }
    }
}

impl std::error::Error for MergeConflict {}

impl MergeSummary {
    /// Render the one-line stdout summary.
    pub fn summary_line(&self) -> String {
        format!(
            "merged {} source(s) into {}",
            self.merged_sources.len(),
            self.target.display()
        )
    }
}

/// Top-level `debundle modules delete` argument shape.
#[derive(Debug, ClapArgs)]
pub struct DeleteArgs {
    #[command(flatten)]
    pub edit: GraphEditArgs,

    /// Module paths (relative to --modules) to delete. The `.yaml`
    /// suffix is optional. All paths are validated up front; if any
    /// check fails, nothing is deleted.
    #[arg(required = true)]
    pub paths: Vec<PathBuf>,

    /// Delete a module that still has members or anonymous statements.
    /// Default refuses non-empty deletions; pass `--force` to override.
    #[arg(long)]
    pub force: bool,
}

/// Summary returned by [`delete_modules`].
#[derive(Debug, Clone)]
pub struct DeleteSummary {
    /// Absolute paths of the YAML files that were deleted (or, in
    /// `dry-run` mode, would have been deleted).
    pub deleted: Vec<PathBuf>,
    /// Whether the call was a dry-run (no files were actually
    /// touched). When `true`, `deleted` lists the would-be paths.
    pub dry_run: bool,
}

impl DeleteSummary {
    /// Render the one-line stdout summary.
    pub fn summary_line(&self) -> String {
        if self.dry_run {
            format!("dry-run: would delete {} file(s)", self.deleted.len())
        } else {
            format!("deleted {} file(s)", self.deleted.len())
        }
    }
}

/// Public entry point for the merge verb. Used by the top-level
/// `debundle modules merge` command.
///
/// Validation contract (per docs/cli.md § "Validate-by-default"):
///
/// * Default: run the realizability gate against the post-merge
///   partition. Accept and splice if `Verdict::Realizable`; reject
///   and exit non-zero with the same `render_cycle_summary`
///   diagnostic the pipeline prints if `Verdict::Unrealizable`.
/// * `--dry-run`: run the gate but do not modify any file.
/// * `--no-verify`: skip the gate; apply the merge regardless.
pub fn run_merge(merge: MergeArgs) -> Result<()> {
    if merge.edit.no_verify {
        eprintln!(
            "warning: --no-verify skips the realizability gate; the merge YAML splice will \
             not be re-checked for cross-module cycles."
        );
    }
    let gate = merge.edit.gate()?;
    let sources: Vec<&Path> = merge.sources.iter().map(PathBuf::as_path).collect();
    let plan = plan_merge(&merge.edit.modules_root, &merge.target, &sources)?;
    if let Err(err) = gate.check(&merge.edit.modules_root, || {
        plan.post_spec(&merge.edit.modules_root)
    }) {
        emit_gate_rejection_json("merge", merge.edit.format, &err);
        return Err(err);
    }
    let (summary, action) = if merge.edit.dry_run {
        (plan.summary, "dry-run")
    } else {
        (plan.apply()?, "applied")
    };
    let merge_outcome = MergeOutcome {
        outcome: MutationOutcome {
            verb: "merge",
            action,
            gate: gate.outcome(),
            files_written: vec![summary.target.display().to_string()],
            files_deleted: summary
                .merged_sources
                .iter()
                .map(|p| p.display().to_string())
                .collect(),
        },
        target: summary.target.display().to_string(),
    };
    match OutputFormat::resolve(merge.edit.format) {
        OutputFormat::Text => {
            if merge.edit.dry_run {
                println!(
                    "dry-run: would merge {} source(s) into {}",
                    summary.merged_sources.len(),
                    summary.target.display()
                );
            } else {
                println!("{}", summary.summary_line());
            }
        }
        format => print_outcome_json(&merge_outcome, format)?,
    }
    Ok(())
}

/// The complete prospective edit. Preview and the gate inspect this exact
/// document; applying the plan does not reconstruct the merge from disk.
struct MergePlan {
    summary: MergeSummary,
    document: LogicalModule,
}

impl MergePlan {
    fn post_spec(&self, modules_root: &Path) -> Result<PostEditSpec> {
        let mut replaced = self.summary.merged_sources.clone();
        replaced.push(self.summary.target.clone());
        let mut post_spec = post_delete_spec(modules_root, &replaced)?;
        // Use the same document-to-claims projection as binding edits, rather
        // than independently concatenating the original files' claims.
        let docs = BTreeMap::from([(
            String::new(),
            (self.summary.target.clone(), self.document.clone()),
        )]);
        post_spec
            .modules
            .extend(post_edit_spec_from_docs(&docs, &BTreeSet::new())?.modules);
        post_spec
            .modules
            .sort_by(|left, right| left.path.cmp(&right.path));
        Ok(post_spec)
    }

    fn apply(self) -> Result<MergeSummary> {
        let target = &self.summary.target;
        let doc = serde_yaml::to_value(&self.document)
            .with_context(|| format!("re-encoding merged {}", target.display()))?;
        // Retain write-before-delete and the shared atomic per-file writer.
        // This does not promise a crash-atomic multi-file transaction.
        apply_yaml_edit(target, &doc, false)?;
        for src in &self.summary.merged_sources {
            fs::remove_file(src)
                .with_context(|| format!("deleting merged source {}", src.display()))?;
        }
        Ok(self.summary)
    }
}

fn plan_merge(modules_root: &Path, target: &Path, sources: &[&Path]) -> Result<MergePlan> {
    let target_abs = resolve_module_file(modules_root, target);
    let source_abs: Vec<PathBuf> = sources
        .iter()
        .map(|p| resolve_module_file(modules_root, p))
        .collect();

    let mut target_module = read_module_or_default(&target_abs)?;
    let mut existing_names = claim_names(&target_module, &target_abs)?;
    let mut merged_source_labels: Vec<String> = Vec::new();
    let mut merged_comments: Vec<String> = Vec::new();

    for src in &source_abs {
        let src_module = read_module(src)?;
        let src_names = claim_names(&src_module, src)?;
        for name in src_names.selector_bindings {
            if !existing_names.selector_bindings.insert(name.clone()) {
                return Err(MergeConflict::DuplicateSourceBinding {
                    name,
                    target: target_abs,
                    source: src.clone(),
                }
                .into());
            }
        }
        for name in src_names.readable_names {
            if !existing_names.readable_names.insert(name.clone()) {
                return Err(MergeConflict::DuplicateReadableName {
                    name,
                    target: target_abs,
                    source: src.clone(),
                }
                .into());
            }
        }
        let label = display_relative(modules_root, src);
        // Source module comments concatenate into the target's
        // module-level `comment:` with a `--- from <source>:` divider
        // (README.md § "Comments").
        if let Some(comment) = src_module
            .comment
            .as_deref()
            .map(str::trim_end)
            .filter(|comment| !comment.trim().is_empty())
        {
            merged_comments.push(format!("--- from {label}:\n{comment}"));
        }
        // `members:`, `source_matches:`, and `anonymous_statements:` are all
        // claims; dropping any of them with the deleted source would silently
        // unclaim their owners on the next `debundle run`. `annotations:` are
        // keyed by readable binding name, so conflicting duplicate metadata
        // must be rejected rather than overwritten.
        target_module.members.extend(src_module.members);
        target_module
            .source_matches
            .extend(src_module.source_matches);
        for (name, annotation) in src_module.annotations {
            if let Some(existing) = target_module.annotations.get(&name)
                && existing != &annotation
            {
                return Err(MergeConflict::ConflictingAnnotation {
                    name,
                    target: target_abs,
                    source: src.clone(),
                }
                .into());
            }
            target_module.annotations.insert(name, annotation);
        }
        target_module
            .anonymous_statements
            .extend(src_module.anonymous_statements);
        merged_source_labels.push(label);
    }

    if !merged_comments.is_empty() {
        target_module.comment = Some(compose_block(
            target_module.comment.as_deref(),
            merged_comments,
        ));
    }

    // Provenance lands in the module-level `note:` field, not a `#` YAML
    // comment: the rewriters (`bindings assign`, `synthesize --apply`,
    // `modules merge`) re-emit the YAML and drop every `#` comment, so a `#`
    // provenance line would be silently lost on the next automated edit
    // (README.md § "Comments"). `note:` is non-emitting and round-trips.
    if !merged_source_labels.is_empty() {
        let provenance = format!("merged from: {}", merged_source_labels.join(", "));
        target_module.note = Some(compose_block(
            target_module.note.as_deref(),
            std::iter::once(provenance),
        ));
    }

    Ok(MergePlan {
        summary: MergeSummary {
            target: target_abs,
            merged_sources: source_abs,
        },
        document: target_module,
    })
}

/// Public entry point for `debundle modules delete`.
///
/// Validation contract (per docs/cli.md § "Validate-by-default"):
///
/// * Default: refuse the deletion of a module that still has members
///   or anonymous statements. For empty deletions, no gate run is
///   needed — the partition doesn't change.
/// * `--force`: override the non-empty check. For non-empty
///   deletions the realizability gate runs against the post-delete
///   partition (every binding previously owned by a deleted module
///   falls back to residual); an `Unrealizable` verdict rejects the
///   deletion.
/// * `--dry-run`: run the gate but do not delete any file.
/// * `--no-verify`: skip the gate; delete unconditionally.
///
/// All paths are resolved relative to `args.edit.modules_root` unless
/// absolute. Paths that do not exist on disk are reported as an
/// error before any deletion is attempted; the operation is
/// best-effort atomic (collect-then-remove) but cannot roll back a
/// partial removal if the filesystem fails midway.
pub fn run_delete(args: DeleteArgs) -> Result<()> {
    if args.edit.no_verify {
        eprintln!(
            "warning: --no-verify skips the realizability gate; the deletion will not be \
             re-checked for cross-module cycles."
        );
    }

    let paths_abs: Vec<PathBuf> = args
        .paths
        .iter()
        .map(|p| resolve_module_file(&args.edit.modules_root, p))
        .collect();

    // Verify every path exists up-front so we never get stuck in a
    // partial-removal state on a typo.
    for p in &paths_abs {
        if !p.exists() {
            bail!("module path does not exist: {}", p.display());
        }
    }

    // Classify each module: empty (no claims, annotations, or anonymous
    // statements) vs non-empty. Required for the `--force` check and the
    // empty-fast-path gate.
    let mut non_empty: Vec<(PathBuf, usize, bool)> = Vec::new();
    for p in &paths_abs {
        let module = read_module(p)?;
        let claim_count =
            module.members.len() + module.source_matches.len() + module.annotations.len();
        let has_anon = !module.anonymous_statements.is_empty();
        if !module.is_structurally_empty() {
            non_empty.push((p.clone(), claim_count, has_anon));
        }
    }

    if !non_empty.is_empty() && !args.force {
        // Render a single-line refusal naming the first offender so
        // the user can see why; the additional non-empty paths fall
        // through `--force` once the user opts in.
        let (path, claims, has_anon) = &non_empty[0];
        let anon_msg = if *has_anon {
            " (plus anonymous_statements)"
        } else {
            ""
        };
        bail!(
            "module {} has {} claim(s){}; pass --force to delete anyway",
            path.display(),
            claims,
            anon_msg,
        );
    }

    // Realizability gate. The all-empty fast path is a structural
    // no-op (an empty module owns no bindings and contributes no
    // anonymous statements, so removing it leaves the partition
    // unchanged). For non-empty `--force` deletions we run the full
    // gate against the post-delete partition.
    let gate_outcome = if non_empty.is_empty() {
        GateOutcome::NotRequired
    } else {
        let gate = args.edit.gate()?;
        if let Err(err) = gate.check(&args.edit.modules_root, || {
            post_delete_spec(&args.edit.modules_root, &paths_abs)
        }) {
            emit_gate_rejection_json("delete", args.edit.format, &err);
            return Err(err);
        }
        gate.outcome()
    };

    let summary = delete_modules(&paths_abs, args.edit.dry_run)?;
    let delete_outcome = DeleteOutcome {
        outcome: MutationOutcome {
            verb: "delete",
            action: if summary.dry_run {
                "dry-run"
            } else {
                "applied"
            },
            gate: gate_outcome,
            files_written: Vec::new(),
            files_deleted: summary
                .deleted
                .iter()
                .map(|p| p.display().to_string())
                .collect(),
        },
    };
    match OutputFormat::resolve(args.edit.format) {
        OutputFormat::Text => println!("{}", summary.summary_line()),
        format => print_outcome_json(&delete_outcome, format)?,
    }
    Ok(())
}

/// Delete the given absolute paths (or, in `dry_run` mode, simply
/// return what would be deleted).
///
/// The caller is responsible for resolving relative paths and for
/// the empty/non-empty + gate decision; this function is the
/// filesystem half of `run_delete`.
pub fn delete_modules(paths: &[PathBuf], dry_run: bool) -> Result<DeleteSummary> {
    if dry_run {
        return Ok(DeleteSummary {
            deleted: paths.to_vec(),
            dry_run: true,
        });
    }
    let mut deleted: Vec<PathBuf> = Vec::new();
    for p in paths {
        fs::remove_file(p).with_context(|| format!("deleting {}", p.display()))?;
        deleted.push(p.clone());
    }
    Ok(DeleteSummary {
        deleted,
        dry_run: false,
    })
}

fn resolve_module_file(root: &Path, path: &Path) -> PathBuf {
    let resolved = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    if resolved.extension().is_some() {
        return resolved;
    }
    let yaml = resolved.with_extension("yaml");
    if yaml.exists() || !resolved.exists() || resolved.is_dir() {
        yaml
    } else {
        resolved
    }
}

/// Read a module YAML file into the typed [`LogicalModule`]. `deny_unknown_fields`
/// makes this reject malformed modules up front — the same schema the pipeline loads.
fn read_module(path: &Path) -> Result<LogicalModule> {
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_yaml::from_str(&text).with_context(|| format!("parsing {}", path.display()))
}

#[derive(Default)]
struct ModuleClaimNames {
    selector_bindings: BTreeSet<String>,
    readable_names: BTreeSet<String>,
}

/// Like [`read_module`] but a missing file is the empty module — the merge
/// apply path creates the target from the merged source claims.
fn read_module_or_default(path: &Path) -> Result<LogicalModule> {
    if path.exists() {
        read_module(path)
    } else {
        Ok(LogicalModule::default())
    }
}

fn display_relative(root: &Path, abs: &Path) -> String {
    abs.strip_prefix(root)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| abs.to_string_lossy().into_owned())
}

/// Authored names that `modules merge` can compare without resolving selectors.
///
/// `selector.binding.name` still participates separately so duplicate concrete
/// source-binding claims are rejected even when two members have distinct
/// readable names. Readable names cover `members[].name` (defaulting to the
/// binding name) and canonical `source_matches[].bindings[].name` (defaulting to
/// the selector-local binding).
fn claim_names(module: &LogicalModule, path: &Path) -> Result<ModuleClaimNames> {
    let mut names = ModuleClaimNames::default();
    for (idx, member) in module.members.iter().enumerate() {
        if let Some(binding) = member.selector.binding.as_ref()
            && !names.selector_bindings.insert(binding.name.clone())
        {
            return Err(anyhow!(
                "duplicate member name \"{}\" within {} (entry {})",
                binding.name,
                path.display(),
                idx
            ));
        }
        if let Some(readable_name) = member.name.as_deref().or_else(|| {
            member
                .selector
                .binding
                .as_ref()
                .map(|binding| binding.name.as_str())
        }) && !names.readable_names.insert(readable_name.to_string())
        {
            return Err(anyhow!(
                "duplicate member name \"{}\" within {} (entry {})",
                readable_name,
                path.display(),
                idx
            ));
        }
    }
    for (claim_idx, claim) in module.source_matches.iter().enumerate() {
        for (binding_idx, binding) in claim.bindings.iter().enumerate() {
            let readable_name = binding.name();
            if !names.readable_names.insert(readable_name.to_string()) {
                return Err(anyhow!(
                    "duplicate member name \"{}\" within {} (source_matches[{}].bindings[{}])",
                    readable_name,
                    path.display(),
                    claim_idx,
                    binding_idx
                ));
            }
        }
    }
    Ok(names)
}

/// Compose an optional existing text block with appended blocks, one per line,
/// trimming trailing whitespace on the existing block. Used for the merged
/// `comment:` and the `merged from:` `note:` provenance.
fn compose_block(existing: Option<&str>, additions: impl IntoIterator<Item = String>) -> String {
    existing
        .map(|existing| existing.trim_end().to_string())
        .into_iter()
        .chain(additions)
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Writes `target`, `first` and `second` as module files under a fresh root and plans
    /// merging `first` then `second` into `target`.
    fn plan(target: &str, first: &str, second: &str) -> (tempfile::TempDir, Result<MergePlan>) {
        let root = tempfile::tempdir().unwrap();
        for (name, yaml) in [("target", target), ("first", first), ("second", second)] {
            fs::write(root.path().join(format!("{name}.yaml")), yaml).unwrap();
        }
        let plan = plan_merge(
            root.path(),
            Path::new("target"),
            &[Path::new("first"), Path::new("second")],
        );
        (root, plan)
    }

    /// The root and the typed refusal of a merge that must be refused.
    fn refused(target: &str, first: &str, second: &str) -> (tempfile::TempDir, MergeConflict) {
        let (root, plan) = plan(target, first, second);
        let error = plan.err().expect("the clash must be refused");
        (root, error.downcast::<MergeConflict>().unwrap())
    }

    #[test]
    fn a_readable_name_claimed_by_two_sources_is_refused_naming_the_later_one() {
        let (root, conflict) = refused(
            "members: [{selector: {binding: {name: a}}}]",
            "members: [{name: Clash, selector: {binding: {name: b}}}]",
            "members: [{name: Clash, selector: {binding: {name: c}}}]",
        );
        assert_eq!(
            conflict,
            MergeConflict::DuplicateReadableName {
                name: "Clash".to_string(),
                target: root.path().join("target.yaml"),
                source: root.path().join("second.yaml"),
            }
        );
    }

    #[test]
    fn a_source_binding_claimed_twice_is_refused_apart_from_readable_names() {
        let (root, conflict) = refused(
            "members: [{selector: {binding: {name: shared_binding}}}]",
            "members: [{selector: {binding: {name: shared_binding}}}]",
            "members: []",
        );
        assert_eq!(
            conflict,
            MergeConflict::DuplicateSourceBinding {
                name: "shared_binding".to_string(),
                target: root.path().join("target.yaml"),
                source: root.path().join("first.yaml"),
            }
        );
    }

    #[test]
    fn a_member_name_and_a_source_match_binding_name_share_one_namespace() {
        let (root, conflict) = refused(
            "members: [{name: Widget, selector: {binding: {name: a}}}]",
            "source_matches: [{match: 'const b = 2;', bindings: [{local: b, name: Widget}]}]",
            "members: []",
        );
        assert_eq!(
            conflict,
            MergeConflict::DuplicateReadableName {
                name: "Widget".to_string(),
                target: root.path().join("target.yaml"),
                source: root.path().join("first.yaml"),
            }
        );
    }

    #[test]
    fn differing_annotations_clash_and_identical_ones_do_not() {
        let target = "members: [{selector: {binding: {name: annotated}}}]\nannotations: {annotated: {note: first}}";
        let source = |note: &str| {
            format!(
                "members: [{{selector: {{binding: {{name: b}}}}}}]\nannotations: {{annotated: {{note: {note}}}}}"
            )
        };
        let (root, conflict) = refused(target, &source("second"), "members: []");
        assert_eq!(
            conflict,
            MergeConflict::ConflictingAnnotation {
                name: "annotated".to_string(),
                target: root.path().join("target.yaml"),
                source: root.path().join("first.yaml"),
            }
        );
        let (_root, plan) = plan(target, &source("first"), "members: []");
        plan.expect("identical annotations merge");
    }
}
