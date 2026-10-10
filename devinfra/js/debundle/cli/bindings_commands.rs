//! `bindings` command arguments, dispatch, and presentation. Spec edits live
//! in `binding`; this module adapts the CLI to that shared editing API.
use crate::binding::{
    AssignOutcome, BindingsListFilters, Move, UnassignOutcome, parse_batch_json, parse_move_triple,
    rename_binding, run_bindings_assign, run_bindings_list, run_bindings_unassign,
};
use crate::comment::{BindingCommentArgs, run_binding_comment_cmd};
use crate::edit_gate::GraphEditArgs;
use crate::emit_report;
use crate::outcome::{emit_gate_rejection_json, print_outcome_json};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use peel::OutputFormat;
use std::path::PathBuf;

/// Top-level `debundle bindings ...` argument node.
#[derive(Debug, ClapArgs)]
pub(super) struct BindingsNs {
    #[command(subcommand)]
    command: BindingsNsCommand,
}

#[derive(Debug, Subcommand)]
enum BindingsNsCommand {
    /// Read, set, edit, or clear a binding's `comment:` field.
    ///
    /// Positional `"text"` replaces the comment; `--edit` opens
    /// `$EDITOR` (fallback `$VISUAL`, then `vi`) on the current text;
    /// `--clear` removes the field; with none of the three, prints the
    /// current comment. An unset comment reads as `null` in JSON (an
    /// empty line in text), distinct from an explicit `comment: ""`.
    /// Member comments emit as JS comment blocks above the binding's
    /// owner statement. Comments don't affect factorization, so there
    /// is no `--no-verify`; `--dry-run` previews without writing.
    Comment(BindingCommentArgs),
    /// List every binding in the spec with home module + filters.
    ///
    /// Per-row: minified name, home module, readable name if any, and
    /// `orphan` (sole member of its module) / `unrenamed` flags.
    List(BindingsListNsArgs),
    /// Rename a binding's readable `name:` without moving it.
    ///
    /// Validation is name-collision detection: no two bindings in the
    /// chunk get the same readable name. A convenience over
    /// `bindings assign` for the rename-without-move case. It also
    /// works on `source_matches[].bindings[]` entries.
    Rename(BindingsRenameArgs),
    /// Move one or more bindings into named logical modules atomically.
    ///
    /// Each positional `<sym>:<module>[:<readable>]` moves a binding
    /// and optionally renames it in the same step; `--batch` reads
    /// moves as JSON. Validation runs on the *whole batch's* post-state
    /// and includes the realizability + atom-split gate (the same
    /// predicate `modules merge` / `modules delete --force` use), so a
    /// multi-move refactor whose intermediate states would be invalid
    /// can land in one shot; wanting refuse-intermediate-invalid
    /// semantics means invoking once per move. Destination modules are
    /// auto-created (paths canonicalized/lowercased); batch source
    /// modules drained of all ownership claims are deleted unless they carry a
    /// module-level `comment:`, `source_matches:`, `annotations:`, or
    /// `anonymous_statements:`. A `source_matches[].bindings[]` move
    /// carries the source template and its claim note; moving only part
    /// of a claim splits that claim across modules. The post-batch gate
    /// rejects any split of a source statement's atomic owner.
    /// Contract details: docs/cli.md § "Batch atomicity".
    Assign(BindingsAssignArgs),
    /// Remove one or more bindings from their current modules
    /// atomically; they fall through to residual.
    ///
    /// Residual is the default for an owner no spec module claims.
    /// Same post-batch validation, gate, and drained-source-module
    /// sweep as `bindings assign`. Splitting an atom by unassigning
    /// only some of its members is rejected; unassigning a whole atom
    /// together is accepted. Dry-run and apply share an exit code on
    /// the same input. `source_matches[].bindings[]` entries cannot yet
    /// be unassigned with this command.
    Unassign(BindingsUnassignArgs),
}

#[derive(Debug, ClapArgs)]
struct BindingsListNsArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Restrict to bindings whose home module equals this path.
    #[arg(long = "in")]
    pub in_module: Option<String>,
    /// Restrict to bindings still using their minified name.
    #[arg(long)]
    pub unrenamed: bool,
    /// Restrict to bindings that are the only member of their module.
    #[arg(long)]
    pub orphan: bool,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, ClapArgs)]
struct BindingsRenameArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Current minified or readable name of the binding (must not contain `:`).
    pub original: String,
    /// New readable name (must not contain `:`).
    pub readable: String,
    /// Validate but do not modify any file.
    #[arg(long)]
    pub dry_run: bool,
    /// Skip name-collision validation. Don't use casually.
    #[arg(long)]
    pub no_verify: bool,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, ClapArgs)]
struct BindingsUnassignArgs {
    #[command(flatten)]
    pub edit: GraphEditArgs,
    /// Binding symbols (minified or readable) to remove from their
    /// current modules. `source_matches[].bindings[]` entries are not
    /// supported yet.
    #[arg(required = true)]
    pub syms: Vec<String>,
}

#[derive(Debug, ClapArgs)]
struct BindingsAssignArgs {
    #[command(flatten)]
    pub edit: GraphEditArgs,
    /// Positional `<sym>:<module>[:<readable>]` triples. May be empty
    /// when `--batch` is supplied.
    ///
    /// `<sym>` accepts the minified or current readable name; the
    /// optional third field sets the new readable `name:` (omitting it
    /// preserves the current one). Neither `<sym>` nor `<readable>` may
    /// contain `:` — use `--batch` JSON for such edge cases.
    pub triples: Vec<String>,
    /// Read additional moves from a JSON file (or `-` for stdin).
    /// Accepts explicit `{sym, module, readable?}` move arrays, plus
    /// reviewed binding-only `modules propose` rows.
    ///
    /// Moves are deduplicated on resolved member identity (duplicates
    /// collapse with a stderr warning); contradictory destinations or
    /// readable names for one member are rejected. Formats and the
    /// propose-row admission rules: docs/cli.md § "Batch atomicity".
    #[arg(long)]
    pub batch: Option<String>,
}

fn run_bindings_list_cmd(args: BindingsListNsArgs) -> Result<()> {
    let filters = BindingsListFilters {
        in_module: args.in_module,
        unrenamed: args.unrenamed,
        orphan: args.orphan,
    };
    let report = run_bindings_list(&args.modules_root, &filters)?;
    emit_report(
        args.format,
        &report,
        render_bindings_list_text,
        "writing bindings list output",
    )
}

fn render_bindings_list_text(report: &crate::binding::BindingsListReport, out: &mut String) {
    out.push_str(&format!("{} binding(s)\n", report.bindings.len()));
    for entry in &report.bindings {
        let mut flags = Vec::new();
        if entry.orphan {
            flags.push("orphan");
        }
        if !entry.name.is_renamed() {
            flags.push("unrenamed");
        }
        let readable = entry.name.readable().unwrap_or("-");
        out.push_str(&format!(
            "  {}  {}  [{}]  {}\n",
            entry.name.minified(),
            entry.module,
            readable,
            flags.join(",")
        ));
    }
}

fn run_bindings_rename_cmd(args: BindingsRenameArgs) -> Result<()> {
    let out = rename_binding(
        &args.modules_root,
        &args.original,
        &args.readable,
        args.dry_run,
        args.no_verify,
    )?;
    match OutputFormat::resolve(args.format) {
        OutputFormat::Text => {
            let file = out
                .outcome
                .files_written
                .first()
                .map(|f| format!(" ({f})"))
                .unwrap_or_default();
            println!(
                "{}: {} -> {}{file}",
                out.outcome.action,
                out.old_readable.as_deref().unwrap_or(&out.binding),
                out.new_readable,
            );
        }
        format => print_outcome_json(&out, format)?,
    }
    Ok(())
}

fn run_bindings_assign_cmd(args: BindingsAssignArgs) -> Result<()> {
    use std::io::Read;
    let mut moves: Vec<Move> = Vec::new();
    for t in &args.triples {
        moves.push(parse_move_triple(t)?);
    }
    if let Some(path) = &args.batch {
        let text = if path == "-" {
            let mut buf = String::new();
            std::io::stdin().read_to_string(&mut buf)?;
            buf
        } else {
            std::fs::read_to_string(path).with_context(|| format!("reading {path}"))?
        };
        moves.extend(parse_batch_json(&text)?);
    }
    // `GraphEditArgs::gate` is the shared "graph or no-verify" policy
    // every mutating verb uses; no verb can silently skip the
    // realizability gate.
    let gate = args.edit.gate()?;
    let out = match run_bindings_assign(&args.edit.modules_root, moves, args.edit.dry_run, gate) {
        Ok(out) => out,
        Err(err) => {
            emit_gate_rejection_json("assign", args.edit.format, &err);
            return Err(err);
        }
    };
    let format = OutputFormat::resolve(args.edit.format);
    print_assign_outcome(&out, format)
}

fn run_bindings_unassign_cmd(args: BindingsUnassignArgs) -> Result<()> {
    let gate = args.edit.gate()?;
    let out =
        match run_bindings_unassign(&args.edit.modules_root, args.syms, args.edit.dry_run, gate) {
            Ok(out) => out,
            Err(err) => {
                emit_gate_rejection_json("unassign", args.edit.format, &err);
                return Err(err);
            }
        };
    let format = OutputFormat::resolve(args.edit.format);
    print_unassign_outcome(&out, format)
}

fn print_unassign_outcome(out: &UnassignOutcome, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Text => {
            println!(
                "{}: {} unassign(s); {} file(s) written, {} file(s) deleted",
                out.outcome.action,
                out.unassigned,
                out.outcome.files_written.len(),
                out.outcome.files_deleted.len()
            );
            Ok(())
        }
        format => print_outcome_json(out, format),
    }
}

fn print_assign_outcome(out: &AssignOutcome, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Text => {
            println!(
                "{}: {} move(s); {} file(s) written, {} file(s) deleted",
                out.outcome.action,
                out.moves_applied,
                out.outcome.files_written.len(),
                out.outcome.files_deleted.len()
            );
            Ok(())
        }
        format => print_outcome_json(out, format),
    }
}

pub(super) fn run(args: BindingsNs) -> Result<()> {
    match args.command {
        BindingsNsCommand::Comment(c) => run_binding_comment_cmd(c),
        BindingsNsCommand::List(l) => run_bindings_list_cmd(l),
        BindingsNsCommand::Rename(r) => run_bindings_rename_cmd(r),
        BindingsNsCommand::Assign(a) => run_bindings_assign_cmd(a),
        BindingsNsCommand::Unassign(u) => run_bindings_unassign_cmd(u),
    }
}
