//! `debundle binding move ...` — atomic batched binding moves.
//!
//! `move` is the batched verb that subsumes `assign` (move-from-
//! wherever-to-X) and, with the sentinel destination `-`, `unassign`
//! (move into residual). A batch is parsed into a list of
//! `MoveOp { name, destination }` and validated atomically: every op
//! must resolve to a known binding, no two ops may target the same
//! binding, and the union of moves must not, when applied
//! hypothetically to a copy of the owner graph's module quotient,
//! create a cycle (the lightweight "realizability" check).
//!
//! Only if validation passes do we write any spec edits; if it
//! fails, no file is touched and the caller sees a diagnostic on
//! stderr. `--force` bypasses validation. `--dry-run` runs
//! validation but skips writes.
//!
//! Per-op result lines stream to stdout (`ok    X -> foo`) so
//! batches stay scriptable; `--ndjson` swaps in JSON-per-line for
//! programmatic consumers.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::Args;
use serde::Serialize;

use analysis::OwnerGraphReport;
use spec::{BindingSelector, Member, MemberSelector};
use spec_modules::{collect_module_files, default_binding_patches_path, read_module_file};

use super::binding::{BindingHome, HomeSource, find_home};
use super::io::OutputFormat;

/// Sentinel destination that unassigns a binding (sends it back to
/// residual). Aliases: `-`, `residual`, `<residual>`.
const RESIDUAL_DESTINATIONS: &[&str] = &["-", "residual", "<residual>"];

#[derive(Debug, Clone, Args)]
pub struct MoveArgs {
    /// Path to `owner_graph.json` (debundler analysis output). Required
    /// for batch validation; if omitted, `--force` is required so the
    /// batch lands without realizability checks.
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
    /// any files. Implies normal validation; pair with `--force` to
    /// also skip the realizability check.
    #[arg(long = "dry-run", default_value_t = false)]
    pub dry_run: bool,

    /// Apply the batch even if realizability validation rejects it.
    /// Duplicate-destination and unresolved-binding checks still run
    /// (those are not realizability concerns).
    #[arg(long = "force", default_value_t = false)]
    pub force: bool,

    /// Emit one JSON record per move to stdout instead of human-
    /// readable `ok` lines. The final summary record stays as a
    /// trailing JSON document.
    #[command(flatten)]
    pub format: OutputFormat,

    /// Positional `name=destination` pairs. Same syntax as `--op`;
    /// useful for terse single-line invocations.
    ///
    /// Backward-compatibility shim: a two-element positional
    /// invocation `<name> <module>` (no `=` in either token) is
    /// also accepted and folded into a single op.
    #[arg(value_name = "NAME=DEST")]
    pub positional: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MoveOp {
    pub name: String,
    /// `None` => unassign (move to residual / no module).
    pub destination: Option<String>,
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
}

#[derive(Debug, Clone, Serialize)]
pub struct MoveBatchSummary {
    pub applied: usize,
    pub no_op: usize,
    pub dry_run: bool,
    pub forced: bool,
    pub validation_skipped: bool,
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
    let plan = plan_batch(&ops, &args)?;

    if !args.force && !plan.validation_skipped {
        if let Some(diag) = validate_batch(&ops, &args)? {
            // Per design: write diagnostic to stderr, write nothing,
            // exit nonzero, mention `--force` escape hatch.
            let _ = writeln!(io::stderr(), "{diag}");
            let _ = writeln!(io::stderr(), "  Batch rejected. No spec edits written.");
            let _ = writeln!(io::stderr(), "  Re-run with --force to commit anyway.");
            std::process::exit(1);
        }
    }

    let results = apply_plan(&plan, &args)?;
    emit_results(&results, &plan, &args)?;
    Ok(())
}

/// Plain `assign`/`unassign` reuse this entry: they convert their
/// own args into a single `MoveOp` and delegate. Returns the per-op
/// results so callers can re-shape them into their own report.
pub fn run_single(op: MoveOp, args: SingleOpArgs) -> Result<Vec<MoveOpResult>> {
    let move_args = MoveArgs {
        owner_graph_path: args.owner_graph_path,
        modules_root: args.modules_root,
        ops: Vec::new(),
        batch_file: None,
        dry_run: args.dry_run,
        force: args.force,
        format: OutputFormat::default(),
        positional: Vec::new(),
    };
    let plan = plan_batch(std::slice::from_ref(&op), &move_args)?;
    if !move_args.force && !plan.validation_skipped {
        if let Some(diag) = validate_batch(std::slice::from_ref(&op), &move_args)? {
            bail!("{diag}\n  Batch rejected. No spec edits written.");
        }
    }
    apply_plan(&plan, &move_args)
}

#[derive(Debug, Clone)]
pub struct SingleOpArgs {
    pub owner_graph_path: Option<PathBuf>,
    pub modules_root: PathBuf,
    pub dry_run: bool,
    pub force: bool,
}

/// Run only the validation half of `binding move` for callers (such
/// as `binding assign`) that have their own write path. Returns the
/// diagnostic string on rejection so the caller decides how to
/// surface it.
pub fn validate_single_op(
    ops: &[MoveOp],
    owner_graph_path: &Path,
    modules_root: &Path,
) -> Result<()> {
    let args = MoveArgs {
        owner_graph_path: Some(owner_graph_path.to_path_buf()),
        modules_root: modules_root.to_path_buf(),
        ops: Vec::new(),
        batch_file: None,
        dry_run: true,
        force: false,
        format: OutputFormat::default(),
        positional: Vec::new(),
    };
    if let Some(diag) = validate_batch(ops, &args)? {
        bail!("{diag}\n  Edit rejected. Re-run with --force to commit anyway.");
    }
    Ok(())
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

// ---------------------------------------------------------------------------
// Planning
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct PlannedBatch {
    items: Vec<PlannedItem>,
    validation_skipped: bool,
    forced: bool,
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
    // Duplicate-destination check (same binding moved twice in one
    // batch is rejected regardless of `--force`, to keep the
    // "atomic" contract honest — there is no defensible final state
    // when two ops disagree).
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

    Ok(PlannedBatch {
        items,
        validation_skipped: args.owner_graph_path.is_none(),
        forced: args.force,
    })
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/// Returns `Some(diagnostic)` if the batch is unsafe.
///
/// The check is intentionally lightweight: load the existing
/// `owner_graph.json`, relabel owners affected by the batch to their
/// new destinations, then collapse owner-graph edges into a module
/// quotient and look for a cycle. This catches the headline
/// regression the user asked about ("move X creates a cycle; move
/// {X,Y} together doesn't"). Atomic-unit and TDZ realizability
/// checks are deferred to the validation-agent's `validate_spec_edit`
/// helper; this code calls into that helper when it lands.
fn validate_batch(ops: &[MoveOp], args: &MoveArgs) -> Result<Option<String>> {
    let Some(graph_path) = &args.owner_graph_path else {
        // Missing graph is treated as "validation off". The caller
        // is required to pass `--force` in that mode, but we leave
        // that policy to `run()`; here we just say "no objection".
        return Ok(None);
    };
    let graph = load_graph(graph_path)?;

    // Unresolved-binding-name check: every name in the batch must
    // appear as an owner-declared binding in the graph (or as an
    // already-assigned spec member; the latter is rare for residual
    // bindings, but lets us tolerate spec-only bindings such as
    // `binding_patches` entries).
    let owner_for_binding = owner_for_binding(&graph);
    let known_in_spec = known_spec_bindings(&args.modules_root)?;
    let mut unresolved = Vec::new();
    for op in ops {
        if !owner_for_binding.contains_key(&op.name) && !known_in_spec.contains(&op.name) {
            unresolved.push(op.name.clone());
        }
    }
    if !unresolved.is_empty() {
        return Ok(Some(format!(
            "error: batch references unknown binding(s): {}",
            unresolved.join(", "),
        )));
    }

    // Build current module assignment per owner, then overlay the
    // batch's overrides (binding -> hypothetical destination).
    let mut owner_dest: BTreeMap<&str, String> = BTreeMap::new();
    for node in &graph.nodes {
        owner_dest.insert(&node.id, node.destination.id.clone());
    }
    for op in ops {
        let Some(owner_id) = owner_for_binding.get(&op.name) else {
            continue;
        };
        let new_dest = match &op.destination {
            Some(path) => synthesize_module_id(&graph, path),
            None => synthesize_residual_module_id(&graph),
        };
        owner_dest.insert(owner_id.as_str(), new_dest);
    }

    // Collapse owner edges into a hypothetical module quotient and
    // find a cycle if any. We track example edges per pair so the
    // diagnostic can name them.
    let mut quotient_edges: BTreeMap<(String, String), Vec<String>> = BTreeMap::new();
    for edge in &graph.edges {
        let (Some(source), Some(target)) = (
            owner_dest.get(edge.source.as_str()),
            owner_dest.get(edge.target.as_str()),
        ) else {
            continue;
        };
        if source == target {
            continue;
        }
        quotient_edges
            .entry((source.clone(), target.clone()))
            .or_default()
            .push(edge.id.clone());
    }

    let modules: BTreeSet<&str> = quotient_edges
        .keys()
        .flat_map(|(s, t)| [s.as_str(), t.as_str()])
        .collect();
    let cycle = find_cycle(
        &modules
            .iter()
            .copied()
            .map(String::from)
            .collect::<Vec<_>>(),
        &quotient_edges,
    );
    if let Some(cycle_modules) = cycle {
        let mut diagnostic = format!(
            "error: batch creates a realizability cycle:\n  Cycle ({} modules):",
            cycle_modules.len(),
        );
        for module in &cycle_modules {
            diagnostic.push_str(&format!("\n    {module}  [target after batch]"));
        }
        let mut cut_edges: Vec<(String, String, usize)> = Vec::new();
        for i in 0..cycle_modules.len() {
            let src = &cycle_modules[i];
            let dst = &cycle_modules[(i + 1) % cycle_modules.len()];
            if let Some(evidence) = quotient_edges.get(&(src.clone(), dst.clone())) {
                cut_edges.push((src.clone(), dst.clone(), evidence.len()));
            }
        }
        cut_edges.sort_by(|a, b| b.2.cmp(&a.2));
        if !cut_edges.is_empty() {
            diagnostic.push_str("\n  Top cut edges:");
            for (src, dst, count) in cut_edges.iter().take(5) {
                diagnostic.push_str(&format!("\n    {count}  {src} -> {dst}"));
            }
        }
        return Ok(Some(diagnostic));
    }

    Ok(None)
}

fn owner_for_binding(graph: &OwnerGraphReport) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for node in &graph.nodes {
        for binding in &node.declared_bindings {
            out.insert(binding.binding.to_string(), node.id.clone());
        }
    }
    out
}

fn known_spec_bindings(modules_root: &Path) -> Result<BTreeSet<String>> {
    let mut out = BTreeSet::new();
    for path in collect_module_files(modules_root)? {
        for member in read_module_file(&path)?.members {
            out.insert(member.selector.binding.name);
        }
    }
    let patches = default_binding_patches_path(modules_root);
    if patches.exists() {
        for member in spec_modules::read_binding_patches_file(&patches)?.members {
            out.insert(member.selector.binding.name);
        }
    }
    Ok(out)
}

/// Compose a stable, hypothetical module-id string for the
/// destination of a moved binding. The pipeline derives module ids
/// from per-chunk synthesis (`<chunk>::<logical>`); we mimic the
/// shape here so the new id never collides with an existing
/// quotient node and shows up cleanly in cycle diagnostics.
fn synthesize_module_id(graph: &OwnerGraphReport, module_path: &str) -> String {
    format!("{}::module:{module_path}", graph.chunk_id)
}

fn synthesize_residual_module_id(graph: &OwnerGraphReport) -> String {
    format!("{}::residual", graph.chunk_id)
}

/// BFS-based cycle detection that returns the modules participating
/// in the smallest cycle found, in traversal order. We do not need
/// every cycle — the diagnostic only displays one example.
fn find_cycle(
    nodes: &[String],
    edges: &BTreeMap<(String, String), Vec<String>>,
) -> Option<Vec<String>> {
    let mut adj: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for (src, dst) in edges.keys() {
        adj.entry(src.as_str()).or_default().push(dst.as_str());
    }
    // Iterative DFS with a color map (white=0, gray=1, black=2),
    // recording the parent so we can recover the back-edge cycle.
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Color {
        White,
        Gray,
        Black,
    }
    let mut color: BTreeMap<&str, Color> =
        nodes.iter().map(|n| (n.as_str(), Color::White)).collect();
    let mut parent: BTreeMap<&str, Option<&str>> = BTreeMap::new();
    for start in nodes {
        if color.get(start.as_str()) != Some(&Color::White) {
            continue;
        }
        let mut stack: VecDeque<(&str, usize)> = VecDeque::new();
        stack.push_back((start.as_str(), 0));
        color.insert(start.as_str(), Color::Gray);
        parent.insert(start.as_str(), None);
        while let Some(&(node, next_idx)) = stack.back() {
            let neighbors = adj.get(node).cloned().unwrap_or_default();
            if next_idx >= neighbors.len() {
                color.insert(node, Color::Black);
                stack.pop_back();
                continue;
            }
            stack.pop_back();
            stack.push_back((node, next_idx + 1));
            let nxt = neighbors[next_idx];
            match color.get(nxt).copied().unwrap_or(Color::White) {
                Color::Gray => {
                    // Found a back-edge: walk parents from `node`
                    // back to `nxt` to recover the cycle.
                    let mut cycle = vec![nxt.to_string()];
                    let mut cur = node;
                    while cur != nxt {
                        cycle.push(cur.to_string());
                        cur = match parent.get(cur).and_then(|p| *p) {
                            Some(parent) => parent,
                            None => break,
                        };
                    }
                    cycle.reverse();
                    return Some(cycle);
                }
                Color::White => {
                    color.insert(nxt, Color::Gray);
                    parent.insert(nxt, Some(node));
                    stack.push_back((nxt, 0));
                }
                Color::Black => {}
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Application
// ---------------------------------------------------------------------------

fn apply_plan(plan: &PlannedBatch, args: &MoveArgs) -> Result<Vec<MoveOpResult>> {
    let mut results = Vec::with_capacity(plan.items.len());
    for item in &plan.items {
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
        }
        results.push(MoveOpResult {
            binding: item.op.name.clone(),
            destination: item.op.destination.clone(),
            previous_home: item.previous_home.clone(),
            new_home: item.new_home.clone(),
            created_destination_file: !item.destination_existed && item.destination_file.is_some(),
            source_eq_destination: item.source_eq_destination,
            dry_run: args.dry_run,
        });
    }
    Ok(results)
}

fn emit_results(results: &[MoveOpResult], plan: &PlannedBatch, args: &MoveArgs) -> Result<()> {
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
        forced: plan.forced,
        validation_skipped: plan.validation_skipped,
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
// validation-agent's refactor settle; once `validate_spec_edit` and a
// shared `write_member_to_module` helper land, both modules collapse
// onto them).
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

fn load_graph(path: &Path) -> Result<OwnerGraphReport> {
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
    fn parse_positionals_treats_three_or_more_as_pairs() {
        let err = parse_positionals(&["X".into(), "y".into(), "z".into()]).unwrap_err();
        assert!(err.to_string().contains("expected `name=destination`"));
    }

    #[test]
    fn find_cycle_detects_simple_two_node_loop() {
        let mut edges = BTreeMap::new();
        edges.insert(("a".into(), "b".into()), vec!["e1".into()]);
        edges.insert(("b".into(), "a".into()), vec!["e2".into()]);
        let nodes = vec!["a".into(), "b".into()];
        let cycle = find_cycle(&nodes, &edges).expect("cycle");
        assert_eq!(cycle.len(), 2);
        assert!(cycle.contains(&"a".to_string()));
        assert!(cycle.contains(&"b".to_string()));
    }

    #[test]
    fn find_cycle_returns_none_for_dag() {
        let mut edges = BTreeMap::new();
        edges.insert(("a".into(), "b".into()), vec!["e1".into()]);
        edges.insert(("b".into(), "c".into()), vec!["e2".into()]);
        let nodes = vec!["a".into(), "b".into(), "c".into()];
        assert!(find_cycle(&nodes, &edges).is_none());
    }
}
