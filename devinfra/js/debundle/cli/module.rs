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

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use clap::Args as ClapArgs;
use peel::OutputFormat;
use spec::LogicalModule;

use crate::edit_gate::{Gate, post_delete_spec, post_merge_spec};
use crate::outcome::{GateOutcome, MutationOutcome, emit_gate_rejection_json, print_outcome_json};
use crate::yaml_edit::write_yaml_body_if_semantic_changed;

#[derive(Debug, ClapArgs)]
pub struct MergeArgs {
    /// Root directory containing the per-module YAML tree.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

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

    /// Validate but do not modify any file.
    #[arg(long)]
    pub dry_run: bool,

    /// Skip the realizability gate. Don't use casually — bypassing
    /// it can let an unrealizable spec ship.
    #[arg(long)]
    pub no_verify: bool,

    /// `owner_graph.json` for the chunk being merged. Required for
    /// the realizability gate; ignored when `--no-verify` is set.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: Option<PathBuf>,

    /// Root used to resolve relative `source_location.source_path`
    /// values when the gate checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Summary returned by [`merge_modules`].
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
    /// Root directory containing the per-module YAML tree.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Module paths (relative to --modules) to delete. The `.yaml`
    /// suffix is optional. All paths are validated up front; if any
    /// check fails, nothing is deleted.
    #[arg(required = true)]
    pub paths: Vec<PathBuf>,

    /// Validate but do not delete any file.
    #[arg(long)]
    pub dry_run: bool,

    /// Skip the realizability gate. Don't use casually — bypassing
    /// it on a non-empty deletion can let an unrealizable spec ship.
    #[arg(long)]
    pub no_verify: bool,

    /// Delete a module that still has members or anonymous statements.
    /// Default refuses non-empty deletions; pass `--force` to override.
    #[arg(long)]
    pub force: bool,

    /// `owner_graph.json` for the chunk being edited. Required for
    /// the realizability gate on non-empty `--force` deletions;
    /// ignored when `--no-verify` is set or when every target module
    /// is structurally empty (no-op gate).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: Option<PathBuf>,

    /// Root used to resolve relative `source_location.source_path`
    /// values when the gate checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
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
    if merge.no_verify {
        eprintln!(
            "warning: --no-verify skips the realizability gate; the merge YAML splice will \
             not be re-checked for cross-module cycles."
        );
    }
    let gate = Gate::from_cli(
        merge.no_verify,
        merge.owner_graph_path.as_deref(),
        merge.source_root.as_deref(),
    )?;
    if let Err(err) = gate.check(&merge.modules_root, || {
        let target_abs = resolve_module_file(&merge.modules_root, &merge.target);
        let source_abs: Vec<PathBuf> = merge
            .sources
            .iter()
            .map(|p| resolve_module_file(&merge.modules_root, p))
            .collect();
        post_merge_spec(&merge.modules_root, &target_abs, &source_abs)
    }) {
        emit_gate_rejection_json("merge", merge.format, &err);
        return Err(err);
    }

    let sources: Vec<&Path> = merge.sources.iter().map(PathBuf::as_path).collect();
    let (summary, action) = if merge.dry_run {
        // Dry-run shape preview: load each file to confirm shape
        // before reporting the action. The full validate+write
        // pass would be the same minus the final `fs::write` /
        // `fs::remove_file`.
        (
            preview_merge(&merge.modules_root, &merge.target, &sources)?,
            "dry-run",
        )
    } else {
        (
            merge_modules(&merge.modules_root, &merge.target, &sources)?,
            "applied",
        )
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
    match OutputFormat::resolve(merge.format) {
        OutputFormat::Text => {
            if merge.dry_run {
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

/// Like `merge_modules` but without writing/deleting. Returns the
/// summary that would be produced. Used by `--dry-run`.
fn preview_merge(modules_root: &Path, target: &Path, sources: &[&Path]) -> Result<MergeSummary> {
    let target_abs = resolve_module_file(modules_root, target);
    let source_abs: Vec<PathBuf> = sources
        .iter()
        .map(|p| resolve_module_file(modules_root, p))
        .collect();
    // Confirm every source + the target deserialize as the typed module
    // schema. This catches malformed files before any write would happen
    // in a non-dry-run. A missing target is valid: the apply path will
    // create it from the merged source claims.
    read_module_or_default(&target_abs)?;
    for src in &source_abs {
        read_module(src)?;
    }
    Ok(MergeSummary {
        target: target_abs,
        merged_sources: source_abs,
    })
}

/// Merge `sources` into `target` under `modules_root`, then delete the
/// source files.
///
/// `target` and each entry in `sources` are interpreted relative to
/// `modules_root` unless already absolute.
///
/// Returns an error if any source declares a member/source-match readable name
/// or a `selector.binding.name` that collides with the target or another
/// source.
pub fn merge_modules(
    modules_root: &Path,
    target: &Path,
    sources: &[&Path],
) -> Result<MergeSummary> {
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
                bail!(
                    "duplicate member name \"{}\" in {} and {}",
                    name,
                    target_abs.display(),
                    src.display()
                );
            }
        }
        for name in src_names.readable_names {
            if !existing_names.readable_names.insert(name.clone()) {
                bail!(
                    "duplicate member name \"{}\" in {} and {}",
                    name,
                    target_abs.display(),
                    src.display()
                );
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
                bail!(
                    "conflicting annotation for \"{}\" in {} and {}",
                    name,
                    target_abs.display(),
                    src.display()
                );
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

    let body = serde_yaml::to_string(&target_module)
        .with_context(|| format!("serializing merged {}", target_abs.display()))?;
    let doc = serde_yaml::to_value(&target_module)
        .with_context(|| format!("re-encoding merged {}", target_abs.display()))?;
    if let Some(parent) = target_abs.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating parent directory {}", parent.display()))?;
    }
    write_yaml_body_if_semantic_changed(&target_abs, &doc, body)?;

    for src in &source_abs {
        fs::remove_file(src)
            .with_context(|| format!("deleting merged source {}", src.display()))?;
    }

    Ok(MergeSummary {
        target: target_abs,
        merged_sources: source_abs,
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
/// All paths are resolved relative to `args.modules_root` unless
/// absolute. Paths that do not exist on disk are reported as an
/// error before any deletion is attempted; the operation is
/// best-effort atomic (collect-then-remove) but cannot roll back a
/// partial removal if the filesystem fails midway.
pub fn run_delete(args: DeleteArgs) -> Result<()> {
    if args.no_verify {
        eprintln!(
            "warning: --no-verify skips the realizability gate; the deletion will not be \
             re-checked for cross-module cycles."
        );
    }

    let paths_abs: Vec<PathBuf> = args
        .paths
        .iter()
        .map(|p| resolve_module_file(&args.modules_root, p))
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
    let mut all_empty = true;
    for p in &paths_abs {
        let module = read_module(p)?;
        let claim_count =
            module.members.len() + module.source_matches.len() + module.annotations.len();
        let has_anon = !module.anonymous_statements.is_empty();
        if claim_count > 0 || has_anon {
            all_empty = false;
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
    let gate_outcome = if all_empty {
        GateOutcome::NotRequired
    } else {
        let gate = Gate::from_cli(
            args.no_verify,
            args.owner_graph_path.as_deref(),
            args.source_root.as_deref(),
        )?;
        if let Err(err) = gate.check(&args.modules_root, || {
            post_delete_spec(&args.modules_root, &paths_abs)
        }) {
            emit_gate_rejection_json("delete", args.format, &err);
            return Err(err);
        }
        gate.outcome()
    };

    let summary = delete_modules(&paths_abs, args.dry_run)?;
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
    match OutputFormat::resolve(args.format) {
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
        if let Some(binding) = member.selector.binding.as_ref() {
            if !names.selector_bindings.insert(binding.name.clone()) {
                return Err(anyhow!(
                    "duplicate member name \"{}\" within {} (entry {})",
                    binding.name,
                    path.display(),
                    idx
                ));
            }
            let readable_name = member.name.as_deref().unwrap_or(&binding.name);
            if !names.readable_names.insert(readable_name.to_string()) {
                return Err(anyhow!(
                    "duplicate member name \"{}\" within {} (entry {})",
                    readable_name,
                    path.display(),
                    idx
                ));
            }
        } else if let Some(readable_name) = member.name.as_deref()
            && !names.readable_names.insert(readable_name.to_string())
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
    use serde_yaml::Value;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    /// Minified `selector.binding.name` of each member in the serialized
    /// merge output, in order. Asserts the merge carried + ordered members.
    fn member_names(doc: &Value) -> Vec<String> {
        doc["members"]
            .as_sequence()
            .unwrap()
            .iter()
            .map(|m| {
                m["selector"]["binding"]["name"]
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect()
    }

    #[test]
    fn merge_appends_members_and_deletes_sources() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "target.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "src1.yaml",
            "members:\n  - selector: { binding: { name: b } }\n",
        );
        write(
            root,
            "src2.yaml",
            "members:\n  - selector: { binding: { name: c } }\n",
        );

        let summary = merge_modules(
            root,
            Path::new("target.yaml"),
            &[Path::new("src1.yaml"), Path::new("src2.yaml")],
        )
        .unwrap();

        assert_eq!(summary.merged_sources.len(), 2);
        assert!(summary.summary_line().contains("merged 2 source(s) into"));
        assert!(!root.join("src1.yaml").exists());
        assert!(!root.join("src2.yaml").exists());

        let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
        assert!(!merged.contains("# merged from"), "merged={merged}");
        let doc: Value = serde_yaml::from_str(&merged).unwrap();
        assert_eq!(
            doc["note"].as_str(),
            Some("merged from: src1.yaml, src2.yaml"),
            "merged={merged}",
        );
        assert_eq!(member_names(&doc), vec!["a", "b", "c"]);
    }

    #[test]
    fn merge_records_provenance_note_and_preserves_members() {
        // Even a member-empty source records `merged from:` provenance
        // in the target's module-level `note:` (durable, non-emitting).
        // The merge necessarily reserializes the target since `note:`
        // is now real structure; the member content must round-trip.
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "target.yaml",
            "members: [ { selector: { binding: { name: a } } } ]\n",
        );
        write(root, "empty.yaml", "members: []\n");

        let summary =
            merge_modules(root, Path::new("target.yaml"), &[Path::new("empty.yaml")]).unwrap();

        assert_eq!(summary.merged_sources, vec![root.join("empty.yaml")]);
        assert!(!root.join("empty.yaml").exists());
        let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
        let doc: Value = serde_yaml::from_str(&merged).unwrap();
        assert_eq!(
            doc["note"].as_str(),
            Some("merged from: empty.yaml"),
            "merged={merged}",
        );
        assert_eq!(member_names(&doc), vec!["a"]);
        // No `#` provenance comment is emitted anymore.
        assert!(!merged.contains("# merged from"), "merged={merged}");
    }

    #[test]
    fn merge_resolves_extensionless_module_paths_to_yaml_files() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "ai/models/pricing.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "ai/models/pricing/lookup.yaml",
            "members:\n  - selector: { binding: { name: b } }\n",
        );

        let summary = merge_modules(
            root,
            Path::new("ai/models/pricing"),
            &[Path::new("ai/models/pricing/lookup")],
        )
        .unwrap();

        assert_eq!(summary.target, root.join("ai/models/pricing.yaml"));
        assert_eq!(
            summary.merged_sources,
            vec![root.join("ai/models/pricing/lookup.yaml")]
        );
        assert!(!root.join("ai/models/pricing/lookup.yaml").exists());
        let merged = fs::read_to_string(root.join("ai/models/pricing.yaml")).unwrap();
        let doc: Value = serde_yaml::from_str(&merged).unwrap();
        assert_eq!(
            doc["note"].as_str(),
            Some("merged from: ai/models/pricing/lookup.yaml"),
            "merged={merged}",
        );
    }

    #[test]
    fn merge_creates_missing_target_from_sources() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "src/one.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "src/two.yaml",
            "members:\n  - selector: { binding: { name: b } }\n",
        );

        let summary = merge_modules(
            root,
            Path::new("new/nested/target"),
            &[Path::new("src/one"), Path::new("src/two.yaml")],
        )
        .unwrap();

        assert_eq!(summary.target, root.join("new/nested/target.yaml"));
        assert!(root.join("new/nested/target.yaml").exists());
        assert!(!root.join("src/one.yaml").exists());
        assert!(!root.join("src/two.yaml").exists());
        let merged = fs::read_to_string(root.join("new/nested/target.yaml")).unwrap();
        let doc: Value = serde_yaml::from_str(&merged).unwrap();
        assert_eq!(
            doc["note"].as_str(),
            Some("merged from: src/one.yaml, src/two.yaml"),
            "merged={merged}",
        );
        assert_eq!(member_names(&doc), vec!["a", "b"]);
    }

    #[test]
    fn duplicate_name_across_files_is_rejected() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "target.yaml",
            "members:\n  - selector: { binding: { name: dup } }\n",
        );
        write(
            root,
            "src.yaml",
            "members:\n  - selector: { binding: { name: dup } }\n",
        );
        let err =
            merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")]).unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains("duplicate member name \"dup\""), "msg={msg}");
        // Source must not be deleted on failure.
        assert!(root.join("src.yaml").exists());
    }

    #[test]
    fn delete_accepts_extensionless_module_path() {
        // CLI_DOGFOOD #3: `modules delete <bare-path>` (no `.yaml`)
        // resolves through the shared `resolve_module_file`, the same
        // way `modules merge` does. Regression guard so the suffix
        // requirement does not creep back in.
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "auto_partition/auto_partition_0004.yaml",
            "members: []\n",
        );
        let args = DeleteArgs {
            modules_root: root.to_path_buf(),
            paths: vec![PathBuf::from("auto_partition/auto_partition_0004")],
            dry_run: true,
            no_verify: false,
            force: false,
            owner_graph_path: None,
            source_root: None,
            format: Some(OutputFormat::Text),
        };
        run_delete(args).expect("bare path resolves to the .yaml file");
        assert!(
            root.join("auto_partition/auto_partition_0004.yaml")
                .exists(),
            "dry-run must not delete",
        );
    }

    #[test]
    fn anonymous_statements_are_spliced_too() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "target.yaml",
            "members: []\nanonymous_statements:\n  - { match: 'sideEffectA();' }\n",
        );
        write(
            root,
            "src.yaml",
            "members: []\nanonymous_statements:\n  - { match: 'sideEffectB();' }\n",
        );
        merge_modules(root, Path::new("target.yaml"), &[Path::new("src.yaml")]).unwrap();
        let merged = fs::read_to_string(root.join("target.yaml")).unwrap();
        let doc: Value = serde_yaml::from_str(&merged).unwrap();
        let matches: Vec<String> = doc["anonymous_statements"]
            .as_sequence()
            .unwrap()
            .iter()
            .map(|s| s["match"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(matches, vec!["sideEffectA();", "sideEffectB();"]);
    }
}
