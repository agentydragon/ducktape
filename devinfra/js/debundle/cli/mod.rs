pub mod binding;
pub mod comment;
pub mod edit_gate;
pub mod gate;
pub mod module;
pub mod outcome;
pub mod scc_cluster;
pub mod validate;

mod bindings_commands;
mod inspection_commands;
mod modules_commands;
mod spec_commands;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use peel::{GraphSummaryArgs, OutputFormat, PatchPlanArgs, UnitsArgs, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli};

use crate::bindings_commands::BindingsNs;
use crate::gate::{GateArgs, run_gate_cli};
use crate::inspection_commands::{DescribeArgs, InspectSourceArgs, ShowSourceArgs};
use crate::modules_commands::ModulesNs;
use crate::scc_cluster::{ClusterArgs, SccArgs, run_cluster, run_scc};
use crate::spec_commands::SpecNs;

/// Read and JSON-parse an `owner_graph.json` into an [`OwnerGraphReport`],
/// the load every `cli` verb that takes `--graph` shares.
pub(crate) fn load_owner_graph_report(
    path: &std::path::Path,
) -> Result<analysis::OwnerGraphReport> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("parsing owner graph {}", path.display()))
}

#[derive(Debug, Parser)]
#[command(
    name = "debundle",
    version,
    about = "Debundle JavaScript bundles and inspect peelable module work.",
    long_about = "Runs the debundle transform pipeline and exposes JSON peel-planning queries over generated owner graphs and spec modules."
)]
pub struct DebundleArgs {
    #[command(subcommand)]
    command: DebundleCommand,
}

#[derive(Debug, Subcommand)]
enum DebundleCommand {
    /// Run the debundle transform pipeline from a flat or tree-shaped spec.
    ///
    /// Parse + facts + owner_graph + atomic_units + realizability gate +
    /// lower + emit. The gate is part of the pipeline contract: an
    /// unrealizable spec is rejected (there is no `run --no-verify` — fix
    /// the spec), and the rejection evidence (`cycles.json` /
    /// `atomic_unit_conflicts.json`) is written under
    /// `reports/tree/<chunk>/` next to `owner_graph.json`, where
    /// `debundle gate list` / `gate describe` read it.
    Run(TransformArgs),
    /// Per-binding spec verbs (list, assign, unassign, rename, comment).
    Bindings(BindingsNs),
    /// Module-level spec verbs (list, merge, delete, propose, comment).
    Modules(ModulesNs),
    /// List structural atoms (owner-level SCCs of the constraining-edge
    /// graph; per docs/design.md § "Two classes of atom").
    Atoms(UnitsArgs),
    /// Report spec coverage against atoms: which atoms are claimed,
    /// which fall through to residual.
    Coverage(PatchPlanArgs),
    /// High-level graph counts (owners, edges, atoms,
    /// residual-eligible bindings, …).
    #[command(name = "graph-summary")]
    GraphSummary(GraphSummaryArgs),
    /// Dereference any identifier (binding, module path, proposal,
    /// atom, owner, diagnostic) with full graph + spec context.
    Describe(DescribeArgs),
    /// Print the source text for any identifier.
    ///
    /// Same `<id>` dispatch as `debundle describe`; a module path,
    /// unambiguous module filename, or module id prints the
    /// concatenated source of every owner statement in the module, in
    /// declaration order.
    #[command(name = "show-source")]
    ShowSource(ShowSourceArgs),
    /// Pretty-print parsed top-level source items with source spans and indices.
    #[command(name = "inspect-source")]
    InspectSource(InspectSourceArgs),
    /// List SCCs in the module-quotient graph.
    Scc(SccArgs),
    /// List the module-quotient neighbors of a binding's owner.
    Cluster(ClusterArgs),
    /// Spec-wide queries (e.g. `spec stats`).
    Spec(SpecNs),
    /// Query the realizability gate's rejected SCCs (list / describe / cut).
    Gate(GateArgs),
}

pub fn run_debundle_cli(args: DebundleArgs) -> Result<()> {
    match args.command {
        DebundleCommand::Run(args) => {
            let dry_run = args.dry_run;
            let keep_going = !args.fail_fast;
            let cli = args.resolve()?;
            run_transform_cli(
                &cli,
                TransformRunOptions {
                    dry_run,
                    keep_going,
                    report_dir_override: None,
                    list_template_identifiers: false,
                },
            )?;
            if dry_run {
                println!("dry-run: transform pipeline checks passed; no outputs written");
            }
            Ok(())
        }
        DebundleCommand::Bindings(args) => bindings_commands::run(args),
        DebundleCommand::Modules(args) => modules_commands::run(args),
        DebundleCommand::Atoms(args) => inspection_commands::run_atoms(args),
        DebundleCommand::Coverage(args) => inspection_commands::run_coverage(args),
        DebundleCommand::GraphSummary(args) => inspection_commands::run_graph_summary(args),
        DebundleCommand::Describe(args) => inspection_commands::run_describe(args),
        DebundleCommand::ShowSource(args) => inspection_commands::run_show_source(args),
        DebundleCommand::InspectSource(args) => inspection_commands::run_inspect_source(args),
        DebundleCommand::Scc(args) => run_scc(args),
        DebundleCommand::Cluster(args) => run_cluster(args),
        DebundleCommand::Spec(args) => spec_commands::run(args),
        // Don't wrap with a generic context — `gate` subcommands
        // already carry enough context in their bail messages (e.g.
        // "no blocking SCC with id ..."), and an outer wrap would
        // hide them since `main.rs` prints only the top context.
        DebundleCommand::Gate(args) => run_gate_cli(args),
    }
}

/// Shared tail of every report subcommand: resolve `format` (tty-aware default),
/// render `report`, and tag IO errors with `context`.
fn emit_report<T, F>(
    format: Option<OutputFormat>,
    report: &T,
    text_render: F,
    context: &'static str,
) -> Result<()>
where
    T: serde::Serialize,
    F: FnOnce(&T, &mut String),
{
    print_report(report, OutputFormat::resolve(format), text_render).context(context)
}

/// One flattened, tagged NDJSON row. Each caller owns its section order and payload schema.
fn print_section<T: serde::Serialize>(section: &str, payload: &T) -> Result<()> {
    #[derive(serde::Serialize)]
    struct Line<'a, T> {
        section: &'a str,
        #[serde(flatten)]
        payload: &'a T,
    }
    println!("{}", serde_json::to_string(&Line { section, payload })?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use clap::Parser;
    use pipeline::{TransformArgs, TransformSpecSource};

    use super::{DebundleArgs, DebundleCommand};

    fn parsed_run_args(argv: &[&str]) -> TransformArgs {
        let parsed = DebundleArgs::try_parse_from(argv).expect("parse cli");
        match parsed.command {
            DebundleCommand::Run(args) => args,
            other => panic!("expected run command, got {other:?}"),
        }
    }

    #[test]
    fn parse_run_args_matches_js_surface() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&[
                "debundle",
                "run",
                "--spec",
                "spec.yaml",
                "--dry-run",
                "--package-root",
                "pkg=/tmp/pkg",
                "--packages-root",
                "/tmp/packages",
            ]);
            assert!(args.dry_run);
            assert!(!args.fail_fast);
            let cli = args.resolve().expect("resolve cli");
            assert_eq!(
                cli.spec_source,
                TransformSpecSource::Flat {
                    path: PathBuf::from("spec.yaml")
                }
            );
            assert_eq!(
                cli.package_roots.get("pkg"),
                Some(&PathBuf::from("/tmp/pkg"))
            );
            assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
        });
    }

    #[test]
    fn parse_run_args_accepts_fail_fast_opt_out() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&["debundle", "run", "--spec", "spec.yaml", "--fail-fast"]);
            assert!(args.fail_fast);
        });
    }

    #[test]
    fn parse_tree_run_args() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&[
                "debundle",
                "run",
                "--tree-config",
                "spec_config.yaml",
                "--tree-modules",
                "modules",
                "--tree-vendor-marks",
                "vendor_marks.yaml",
                "--tree-source-root",
                "/workspace",
                "--out-root",
                "out",
            ]);
            let cli = args.resolve().expect("resolve cli");
            assert_eq!(
                cli.spec_source,
                TransformSpecSource::Tree(spec_tree::CompileSpecTreeOptions {
                    config_path: PathBuf::from("spec_config.yaml"),
                    modules_root: PathBuf::from("modules"),
                    vendor_marks_path: PathBuf::from("vendor_marks.yaml"),
                    source_root: Some(PathBuf::from("/workspace")),
                    out_root: PathBuf::from("out"),
                })
            );
        });
    }

    /// Wiring check: each subcommand path parses to its `DebundleCommand`
    /// variant.
    #[test]
    fn subcommands_parse_to_their_variants() {
        type IsExpected = fn(&DebundleCommand) -> bool;
        let cases: [(&str, IsExpected); 10] = [
            ("atoms --graph g.json --modules m", |command| {
                matches!(command, DebundleCommand::Atoms(_))
            }),
            ("coverage --graph g.json --modules m", |command| {
                matches!(command, DebundleCommand::Coverage(_))
            }),
            ("graph-summary --graph g.json --modules m", |command| {
                matches!(command, DebundleCommand::GraphSummary(_))
            }),
            ("describe XOe --graph g.json --modules m", |command| {
                matches!(command, DebundleCommand::Describe(_))
            }),
            (
                "show-source XOe --graph g.json --modules m --source-root /s",
                |command| matches!(command, DebundleCommand::ShowSource(_)),
            ),
            ("bindings comment --modules m XOe text", |command| {
                matches!(command, DebundleCommand::Bindings(_))
            }),
            ("modules comment --modules m runtime/x --clear", |command| {
                matches!(command, DebundleCommand::Modules(_))
            }),
            ("gate list --graph g.json", |command| {
                matches!(command, DebundleCommand::Gate(_))
            }),
            ("gate describe 0 --graph g.json --binding XOe", |command| {
                matches!(command, DebundleCommand::Gate(_))
            }),
            ("gate cut 3 --graph g.json --cycles c.json", |command| {
                matches!(command, DebundleCommand::Gate(_))
            }),
        ];
        for (args, is_expected) in cases {
            let parsed = DebundleArgs::try_parse_from(
                std::iter::once("debundle").chain(args.split_whitespace()),
            )
            .unwrap_or_else(|error| panic!("{args}: {error}"));
            assert!(
                is_expected(&parsed.command),
                "{args} parsed to {:?}",
                parsed.command
            );
        }
    }
}
