//! `debundle binding move ...` — atomic batched binding moves.
//!
//! `move` is the batched verb that subsumes `assign` (move to a
//! module) and, with the sentinel destination `-`/`residual`,
//! `unassign` (move into residual). A batch is parsed into a
//! `Vec<MoveOp>` and validated atomically against the same
//! `validate_spec_edits` helper `assign`/`unassign` use, so the
//! gate semantics are identical: every op must resolve to a known
//! binding, no two ops may target the same binding, and the union
//! of moves must not, when applied hypothetically to the in-memory
//! spec, create a realizability cycle, an atomic-unit split, or a
//! duplicate claim.
//!
//! Only if validation passes do we write any spec edits. If it
//! fails, no file is touched and the diagnostic is printed on
//! stderr. `--force` (or `--no-validate`) bypasses validation;
//! `--dry-run` runs validation but skips writes.
//!
//! Per-op result lines stream to stdout (`ok    X -> foo`) so
//! batches stay scriptable; `--ndjson` swaps in JSON-per-line for
//! programmatic consumers.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::Args;
use serde::Serialize;

use spec::{BindingSelector, Member, MemberSelector};
use spec_modules::read_module_file;

use super::binding::{BindingHome, HomeSource, find_home};
use super::io::OutputFormat;
use super::validation::{ProposedEdit, ValidationReport, validate_spec_edits};

/// Sentinel destination that unassigns a binding (sends it back to
/// residual). Aliases: `-`, `residual`, `<residual>`.
const RESIDUAL_DESTINATIONS: &[&str] = &["-", "residual", "<residual>"];

#[derive(Debug, Clone, Args)]
pub struct MoveArgs {
    /// Path to `owner_graph.json` (debundler analysis output). Required
    /// for batch validation; omit only when paired with `--force` /
    /// `--no-validate` so the batch lands without the realizability
    /// gate.
    #[arg(long = "graph")]
    pub owner_graph_path: Option<PathBuf>,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules")]
    pub modules_root: PathBuf,

    /// One `name=destination` operation. Repeat to compose a batch.
    /// Use `name=-` (or `name=residual`) to unassign.
    #[arg(long = "op", action = clap::ArgAction::Append)]
    pub ops: Vec<String>,

    /// Path to a batch file (one `name=destination` per line; blank
    /// lines and `#`-comment lines are ignored). Mixed with `--op`
    /// and positional pairs by concatenation, in declaration order
    /// (file ops first, then `--op`, then positional).
    #[arg(long = "batch")]
    pub batch_file: Option<PathBuf>,

    /// Print planned writes and validation results but do not modify
    /// any files. Validation still runs.
    #[arg(long = "dry-run", default_value_t = false)]
    pub dry_run: bool,

    /// Apply the batch even if realizability validation rejects it.
    /// Duplicate-destination and unresolved-binding checks still run
    /// (those are local, not realizability concerns).
    #[arg(long = "force", default_value_t = false)]
    pub force: bool,

    /// Alias for `--force` with clearer intent. Skips validation.
    #[arg(long = "no-validate", default_value_t = false)]
    pub no_validate: bool,

    /// Emit one JSON record per move to stdout instead of human-
    /// readable `ok` lines, followed by a trailing JSON summary
    /// record.
    #[command(flatten)]
    pub format: OutputFormat,

    /// Positional `name=destination` pairs. Same syntax as `--op`;
    /// useful for terse single-line invocations.
    ///
    /// Backward-compatibility shim: a two-element positional
    /// invocation `<name> <module>` (no `=` in either token) is
    /// also accepted and folded into a single op so the old
    /// `assign` shape still works through `move`.
    #[arg(value_name = "NAME=DEST")]
    pub positional: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveOp {
    pub name: String,
    /// `None` => unassign (move to residual / no module).
    pub destination: Option<String>,
}

impl MoveOp {
    fn to_edit(&self) -> ProposedEdit {
        match &self.destination {
            Some(module) => ProposedEdit::Assign {
                binding: self.name.clone(),
                module: module.clone(),
            },
            None => ProposedEdit::Unassign {
                binding: self.name.clone(),
            },
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct MoveOpResult {
    pub binding: String,
    pub destination: Option<String>,
    pub previous_home: Option<BindingHome>,
    pub new_home: Option<BindingHome>,
    pub created_destination_file: bool,
    pub source_eq_destination: bool,
    pub dry_run: bool,
    pub written: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct MoveBatchSummary {
    pub applied: usize,
    pub no_op: usize,
    pub dry_run: bool,
    pub forced: bool,
    pub validation_skipped: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub validation: Option<ValidationReport>,
}

/// Public entry point for `debundle binding move`.
pub fn run(args: MoveArgs) -> Result<()> {
    let ops = parse_ops(&args)?;
    if ops.is_empty() {
        // Exit code 2 per design: scripts should distinguish
        // "nothing to do" from "validation failed".
        let _ = writeln!(io::stderr(), "error: no operations specified");
        std::process::exit(2);
    }

    // Duplicate-destination check (same binding moved twice in one
    // batch is rejected regardless of `--force`, to keep the
    // "atomic" contract honest — there is no defensible final state
    // when two ops disagree).
    check_duplicate_ops(&ops)?;

    let plan = plan_batch(&ops, &args)?;
    let validation_skipped = args.force || args.no_validate;
    let validation = if validation_skipped {
        None
    } else if let Some(graph_path) = &args.owner_graph_path {
        let report = load_graph(graph_path)?;
        let edits: Vec<ProposedEdit> = ops.iter().map(MoveOp::to_edit).collect();
        let result = validate_spec_edits(&report, &args.modules_root, &edits)?;
        if !result.is_ok() {
            // Per design: write diagnostic to stderr, write nothing,
            // exit nonzero, mention `--force` escape hatch. Render
            // each edit's diagnostic so the user sees which op
            // contributed to the conflict.
            let diag = render_batch_diagnostic(&result, &ops);
            let _ = writeln!(io::stderr(), "{}", diag.trim_end());
            let _ = writeln!(io::stderr(), "  Batch rejected. No spec edits written.");
            let _ = writeln!(io::stderr(), "  Re-run with --force to commit anyway.");
            std::process::exit(1);
        }
        Some(result)
    } else {
        bail!(
            "validation requires --graph <owner_graph.json>. Pass --no-validate (or --force) to skip the gate."
        );
    };

    let results = apply_plan(&plan, &args)?;
    emit_results(&results, &args, validation_skipped, args.force, validation)?;
    Ok(())
}

fn render_batch_diagnostic(result: &ValidationReport, ops: &[MoveOp]) -> String {
    // The single-edit renderer takes one `ProposedEdit` for the
    // "touches_edit" hint. For batches, we synthesize a placeholder
    // edit (first op) so the renderer still labels the cycle modules
    // as "[target after batch]" via touches_edit. Multi-edit
    // attribution lives in `validation::ValidationReport.cycles`
    // already (each cycle's `touches_edit` is now true if *any* edit
    // targets a member).
    let primary = ops
        .first()
        .map(MoveOp::to_edit)
        .unwrap_or(ProposedEdit::Unassign {
            binding: "<batch>".to_string(),
        });
    result.render_diagnostic(&primary, None)
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

fn parse_ops(args: &MoveArgs) -> Result<Vec<MoveOp>> {
    let mut ops = Vec::new();
    if let Some(path) = &args.batch_file {
        ops.extend(read_batch_file(path)?);
    }
    for raw in &args.ops {
        ops.push(parse_op_token(raw)?);
    }
    ops.extend(parse_positionals(&args.positional)?);
    Ok(ops)
}

fn read_batch_file(path: &Path) -> Result<Vec<MoveOp>> {
    let body = fs::read_to_string(path)
        .with_context(|| format!("reading batch file {}", path.display()))?;
    let mut out = Vec::new();
    for (idx, raw_line) in body.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let op = parse_op_token(line)
            .with_context(|| format!("parsing batch file {} line {}", path.display(), idx + 1))?;
        out.push(op);
    }
    Ok(out)
}

fn parse_positionals(tokens: &[String]) -> Result<Vec<MoveOp>> {
    // Backward-compat shim: `debundle binding move X foo` (two bare
    // tokens, no `=` in either) is folded into a single op so the
    // old `assign` shape still works through `move`.
    if tokens.len() == 2 && !tokens[0].contains('=') && !tokens[1].contains('=') {
        return Ok(vec![MoveOp {
            name: tokens[0].clone(),
            destination: normalise_destination(&tokens[1]),
        }]);
    }
    tokens.iter().map(|t| parse_op_token(t)).collect()
}

fn parse_op_token(raw: &str) -> Result<MoveOp> {
    let (name, dest) = raw
        .split_once('=')
        .with_context(|| format!("expected `name=destination`, got {raw:?}"))?;
    let name = name.trim();
    let dest = dest.trim();
    if name.is_empty() {
        bail!("empty binding name in op {raw:?}");
    }
    if dest.is_empty() {
        bail!("empty destination in op {raw:?}");
    }
    Ok(MoveOp {
        name: name.to_string(),
        destination: normalise_destination(dest),
    })
}

fn normalise_destination(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if RESIDUAL_DESTINATIONS.contains(&trimmed) {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn check_duplicate_ops(ops: &[MoveOp]) -> Result<()> {
    let mut seen = BTreeMap::<String, &MoveOp>::new();
    for op in ops {
        if let Some(prev) = seen.insert(op.name.clone(), op) {
            bail!(
                "duplicate operation in batch: binding {:?} appears twice ({:?} vs {:?})",
                op.name,
                prev.destination,
                op.destination,
            );
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Planning
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct PlannedBatch {
    items: Vec<PlannedItem>,
}

#[derive(Debug, Clone)]
struct PlannedItem {
    op: MoveOp,
    previous_home: Option<BindingHome>,
    new_home: Option<BindingHome>,
    destination_file: Option<PathBuf>,
    destination_existed: bool,
    source_eq_destination: bool,
}

fn plan_batch(ops: &[MoveOp], args: &MoveArgs) -> Result<PlannedBatch> {
    let mut items = Vec::with_capacity(ops.len());
    for op in ops {
        let previous_home = find_home(&args.modules_root, &op.name)?;
        let source_eq_destination = match (&previous_home, &op.destination) {
            (Some(home), Some(dest)) => home.module_path == *dest,
            (None, None) => true,
            _ => false,
        };
        let (new_home, destination_file, destination_existed) = match &op.destination {
            Some(dest) => {
                if dest.starts_with('/') || dest.contains("..") {
                    bail!(
                        "destination module must be a relative path under modules root: {dest:?}"
                    );
                }
                let file = args.modules_root.join(format!("{dest}.yaml"));
                let existed = file.exists();
                let home = BindingHome {
                    source: HomeSource::Module,
                    module_path: dest.clone(),
                    file: file.display().to_string(),
                    renamed_to: previous_home.as_ref().and_then(|h| h.renamed_to.clone()),
                };
                (Some(home), Some(file), existed)
            }
            None => (None, None, false),
        };
        items.push(PlannedItem {
            op: op.clone(),
            previous_home,
            new_home,
            destination_file,
            destination_existed,
            source_eq_destination,
        });
    }
    Ok(PlannedBatch { items })
}

// ---------------------------------------------------------------------------
// Application
// ---------------------------------------------------------------------------

fn apply_plan(plan: &PlannedBatch, args: &MoveArgs) -> Result<Vec<MoveOpResult>> {
    let mut results = Vec::with_capacity(plan.items.len());
    for item in &plan.items {
        let mut written = false;
        if !args.dry_run && !item.source_eq_destination {
            // Remove from previous home (if any).
            if let Some(home) = &item.previous_home {
                remove_binding_from_file(Path::new(&home.file), &item.op.name, home.source)?;
            }
            // Append to destination (if not an unassign).
            if let (Some(dest_file), Some(_new_home)) = (&item.destination_file, &item.new_home) {
                let member = Member {
                    name: item
                        .previous_home
                        .as_ref()
                        .and_then(|h| h.renamed_to.clone()),
                    selector: MemberSelector {
                        binding: BindingSelector {
                            name: item.op.name.clone(),
                            kind: None,
                        },
                    },
                    purity: spec::MemberPurity::Default,
                    effect: spec::MemberEffect::Default,
                    pure_members: Vec::new(),
                };
                append_member_to_module(dest_file, &member)?;
            }
            written = true;
        }
        results.push(MoveOpResult {
            binding: item.op.name.clone(),
            destination: item.op.destination.clone(),
            previous_home: item.previous_home.clone(),
            new_home: item.new_home.clone(),
            created_destination_file: !item.destination_existed && item.destination_file.is_some(),
            source_eq_destination: item.source_eq_destination,
            dry_run: args.dry_run,
            written,
        });
    }
    Ok(results)
}

fn emit_results(
    results: &[MoveOpResult],
    args: &MoveArgs,
    validation_skipped: bool,
    forced: bool,
    validation: Option<ValidationReport>,
) -> Result<()> {
    let mut applied = 0usize;
    let mut no_op = 0usize;
    if args.format.ndjson {
        for r in results {
            println!("{}", serde_json::to_string(r)?);
            if r.source_eq_destination {
                no_op += 1;
            } else {
                applied += 1;
            }
        }
    } else {
        for r in results {
            let dest_label = r.destination.as_deref().unwrap_or("<residual>");
            if r.source_eq_destination {
                println!("noop  {} -> {dest_label}  (already there)", r.binding);
                no_op += 1;
            } else if args.dry_run {
                println!("plan  {} -> {dest_label}", r.binding);
                applied += 1;
            } else {
                println!("ok    {} -> {dest_label}", r.binding);
                applied += 1;
            }
        }
    }
    let summary = MoveBatchSummary {
        applied,
        no_op,
        dry_run: args.dry_run,
        forced,
        validation_skipped,
        validation,
    };
    if args.format.ndjson {
        println!("{}", serde_json::to_string(&summary)?);
    } else {
        let suffix = if args.dry_run {
            " (dry-run; no files written)"
        } else {
            ""
        };
        let no_op_suffix = if no_op > 0 {
            format!(" ({no_op} no-op)")
        } else {
            String::new()
        };
        println!("{applied} ops applied{no_op_suffix}{suffix}.");
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Spec write helpers (duplicated from binding.rs while we let the
// validation-agent's refactor settle; once a shared
// `write_member_to_module` helper lands, both modules collapse onto
// it).
// ---------------------------------------------------------------------------

fn append_member_to_module(file: &Path, member: &Member) -> Result<()> {
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let existing = if file.exists() {
        Some(read_module_file(file)?)
    } else {
        None
    };
    let mut members = existing
        .as_ref()
        .map(|m| m.members.clone())
        .unwrap_or_default();
    // Avoid silently duplicating a member if the same binding lands
    // in the same destination twice across runs.
    members.retain(|m| m.selector.binding.name != member.selector.binding.name);
    members.push(member.clone());
    let anonymous = existing
        .as_ref()
        .map(|m| m.anonymous_statements.clone())
        .unwrap_or_default();
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([
        (
            serde_yaml::Value::String("members".into()),
            serde_yaml::to_value(&members)?,
        ),
        (
            serde_yaml::Value::String("anonymous_statements".into()),
            serde_yaml::to_value(&anonymous)?,
        ),
    ]))?;
    let cleaned = if anonymous.is_empty() {
        document.replace("anonymous_statements: []\n", "")
    } else {
        document
    };
    fs::write(file, cleaned).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn remove_binding_from_file(file: &Path, name: &str, source: HomeSource) -> Result<()> {
    match source {
        HomeSource::Module => remove_binding_from_module_file(file, name),
        HomeSource::BindingPatch => remove_binding_from_patches_file(file, name),
    }
}

fn remove_binding_from_module_file(file: &Path, name: &str) -> Result<()> {
    let module = read_module_file(file)?;
    let before = module.members.len();
    let members: Vec<Member> = module
        .members
        .into_iter()
        .filter(|m| m.selector.binding.name != name)
        .collect();
    if members.len() == before {
        return Ok(());
    }
    if members.is_empty() && module.anonymous_statements.is_empty() {
        fs::remove_file(file).with_context(|| format!("removing empty {}", file.display()))?;
        return Ok(());
    }
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([
        (
            serde_yaml::Value::String("members".into()),
            serde_yaml::to_value(&members)?,
        ),
        (
            serde_yaml::Value::String("anonymous_statements".into()),
            serde_yaml::to_value(&module.anonymous_statements)?,
        ),
    ]))?;
    let cleaned = if module.anonymous_statements.is_empty() {
        document.replace("anonymous_statements: []\n", "")
    } else {
        document
    };
    fs::write(file, cleaned).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn remove_binding_from_patches_file(file: &Path, name: &str) -> Result<()> {
    let patches = spec_modules::read_binding_patches_file(file)?;
    let members: Vec<Member> = patches
        .members
        .into_iter()
        .filter(|m| m.selector.binding.name != name)
        .collect();
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([(
        serde_yaml::Value::String("members".into()),
        serde_yaml::to_value(&members)?,
    )]))?;
    fs::write(file, document).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn load_graph(path: &Path) -> Result<analysis::OwnerGraphReport> {
    serde_json::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_op_token_splits_on_first_equals() {
        let op = parse_op_token("XOe=runtime/plugins").unwrap();
        assert_eq!(op.name, "XOe");
        assert_eq!(op.destination.as_deref(), Some("runtime/plugins"));
    }

    #[test]
    fn parse_op_token_recognises_residual_aliases() {
        for alias in ["-", "residual", "<residual>"] {
            let op = parse_op_token(&format!("XOe={alias}")).unwrap();
            assert_eq!(op.destination, None, "alias {alias} should unassign");
        }
    }

    #[test]
    fn parse_op_token_rejects_missing_equals() {
        assert!(parse_op_token("XOe").is_err());
    }

    #[test]
    fn parse_positionals_accepts_two_token_backcompat_shape() {
        let ops = parse_positionals(&["XOe".into(), "runtime/plugins".into()]).unwrap();
        assert_eq!(ops.len(), 1);
        assert_eq!(ops[0].name, "XOe");
        assert_eq!(ops[0].destination.as_deref(), Some("runtime/plugins"));
    }

    #[test]
    fn check_duplicate_ops_rejects_same_binding_twice() {
        let ops = vec![
            MoveOp {
                name: "X".into(),
                destination: Some("a".into()),
            },
            MoveOp {
                name: "X".into(),
                destination: Some("b".into()),
            },
        ];
        let err = check_duplicate_ops(&ops).unwrap_err();
        assert!(err.to_string().contains("duplicate"));
    }

    #[test]
    fn to_edit_maps_some_destination_to_assign() {
        let op = MoveOp {
            name: "X".into(),
            destination: Some("foo".into()),
        };
        match op.to_edit() {
            ProposedEdit::Assign { binding, module } => {
                assert_eq!(binding, "X");
                assert_eq!(module, "foo");
            }
            _ => panic!("expected Assign"),
        }
    }

    #[test]
    fn to_edit_maps_none_destination_to_unassign() {
        let op = MoveOp {
            name: "X".into(),
            destination: None,
        };
        assert!(matches!(op.to_edit(), ProposedEdit::Unassign { .. }));
    }
}
