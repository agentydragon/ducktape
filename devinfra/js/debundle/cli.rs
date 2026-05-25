use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use peel::{PeelArgs, run_peel};
use pipeline::{TransformArgs, run_transform_cli};
use query::{QueryCommand, binding, cluster, scc};

#[derive(Debug, Parser)]
#[command(
    name = "debundle",
    version,
    about = "Debundle JavaScript bundles and inspect peelable module work.",
    long_about = "Runs the debundle transform pipeline and exposes JSON queries / spec edits over the generated owner graph and spec modules. \n\nThe `peel` subcommand group is preserved for backwards-compatible planning workflows. The orthogonal top-level subcommands (`binding`, `scc`, `cluster`) are the canonical surface for everyday spec authoring."
)]
pub struct DebundleArgs {
    #[command(subcommand)]
    command: DebundleCommand,
}

#[derive(Debug, Subcommand)]
enum DebundleCommand {
    /// Run the debundle transform pipeline from a flat or tree-shaped spec.
    Run(TransformArgs),
    /// Inspect peel-planning evidence from an owner graph and spec modules tree.
    Peel(PeelArgs),
    /// Inspect or edit one binding (spec record, source, module assignment).
    #[command(subcommand)]
    Binding(binding::BindingCommand),
    /// Inspect strongly-connected components in the module-quotient graph.
    Scc(scc::SccArgs),
    /// Inspect 1-hop neighbors of a module or binding in the quotient graph.
    Cluster(cluster::ClusterArgs),
}

pub fn run_debundle_cli(args: DebundleArgs) -> Result<()> {
    match args.command {
        DebundleCommand::Run(args) => {
            let cli = args.resolve()?;
            run_transform_cli(&cli)?;
            Ok(())
        }
        DebundleCommand::Peel(args) => run_peel(args).context("running peel query"),
        DebundleCommand::Binding(args) => query::run_query(QueryCommand::Binding(args)),
        DebundleCommand::Scc(args) => query::run_query(QueryCommand::Scc(args)),
        DebundleCommand::Cluster(args) => query::run_query(QueryCommand::Cluster(args)),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use clap::Parser;
    use pipeline::{TransformArgs, TransformSpecSource};

    use super::DebundleArgs;

    fn parsed_run_args(argv: &[&str]) -> TransformArgs {
        let parsed = DebundleArgs::try_parse_from(argv).expect("parse cli");
        match parsed.command {
            super::DebundleCommand::Run(args) => args,
            super::DebundleCommand::Peel(_)
            | super::DebundleCommand::Binding(_)
            | super::DebundleCommand::Scc(_)
            | super::DebundleCommand::Cluster(_) => panic!("expected run command"),
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
                "--package-root",
                "pkg=/tmp/pkg",
                "--packages-root",
                "/tmp/packages",
            ]);
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

    #[test]
    fn parse_peel_units_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "peel",
            "units",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Peel(_)));
    }

    #[test]
    fn parse_binding_describe_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "binding",
            "describe",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
            "XOe",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Binding(_)));
    }

    #[test]
    fn parse_scc_command_with_filters() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "scc",
            "--graph",
            "owner_graph.json",
            "--singletons-only",
            "--residual-only",
            "--ndjson",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Scc(_)));
    }

    #[test]
    fn parse_cluster_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "cluster",
            "--graph",
            "owner_graph.json",
            "--module",
            "runtime/plugins",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Cluster(_)));
    }
}
