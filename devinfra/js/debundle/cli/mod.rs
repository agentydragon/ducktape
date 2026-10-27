pub mod binding;
pub mod comment;
pub mod edit_gate;
pub mod gate;
pub mod invocation;
pub mod module;
mod module_apply;
pub mod outcome;
pub mod scc_cluster;
pub mod validate;

mod bindings_commands;
mod inspection_commands;
mod modules_commands;
mod selector_explanation;
mod selector_input;
mod snapshot;
mod spec_commands;

use anyhow::{Context, Result};
use clap::{Args as ClapArgs, CommandFactory, FromArgMatches, Parser, Subcommand};
use graph_provenance::{GraphMismatch, GraphMismatchReason};
use peel::{AtomicGroupsArgs, GraphSummaryArgs, OutputFormat, PatchPlanArgs, print_report};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli};

use crate::bindings_commands::BindingsNs;
use crate::gate::{GateArgs, InspectionRoots, run_gate_cli};
use crate::inspection_commands::{DescribeArgs, InspectSourceArgs};
use crate::invocation::ResolvedInvocation;
use crate::modules_commands::ModulesNs;
use crate::scc_cluster::{ClusterArgs, SccArgs, run_cluster, run_scc};
use crate::spec_commands::SpecNs;

/// Read and JSON-parse an `dependency_graph.json` into an [`DependencyGraphReport`],
/// the load every `cli` verb that takes `--graph` shares.
pub(crate) fn load_dependency_graph_report(
    path: &std::path::Path,
) -> Result<analysis::DependencyGraphReport> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text)
        .with_context(|| format!("parsing dependency graph {}", path.display()))
}

/// Shared pre-edit/pre-query check for any operation that joins a saved graph
/// to current authoring files. Historical graph-only views use the loader above.
pub(crate) fn verify_graph_compatibility(
    graph_path: &std::path::Path,
    modules_root: &std::path::Path,
    selector_source_root: Option<&std::path::Path>,
    spec_root: Option<&std::path::Path>,
    graph_source_root: Option<&std::path::Path>,
    graph_package_roots: &std::collections::BTreeMap<String, std::path::PathBuf>,
    tree_config: Option<&std::path::Path>,
) -> Result<()> {
    let graph = graph_provenance::read_graph_header(graph_path)?;
    let provenance = graph.provenance.as_ref().ok_or_else(|| {
        GraphMismatch::new(
            GraphMismatchReason::MissingProvenance {
                graph: graph_path.to_path_buf(),
            },
            vec!["debundle".to_string(), "run".to_string()],
            None,
        )
    })?;
    let current = graph_provenance::CurrentRoots {
        spec: spec_root.map(std::path::Path::to_path_buf),
        source: graph_source_root.map(std::path::Path::to_path_buf),
        packages: graph_package_roots.clone(),
    };
    let (regenerate_command, regenerate_cwd) = provenance.regeneration_recipe(&current)?;
    if let Some(directory) = &provenance.modules_directory {
        let base = current
            .spec
            .as_ref()
            .unwrap_or(&provenance.roots.spec)
            .join(directory);
        let modules = graph_provenance::absolute(modules_root)?;
        let base = graph_provenance::absolute(&base)?;
        if !modules.starts_with(&base) {
            return Err(GraphMismatch::new(
                GraphMismatchReason::ModulesRoot {
                    expected: base,
                    actual: modules,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        let relative = modules.strip_prefix(&base).expect("checked prefix");
        let scopes = provenance
            .module_roots_by_chunk
            .get(&graph.chunk_id)
            .with_context(|| {
                format!(
                    "graph chunk {:?} has no authored module scope",
                    graph.chunk_id
                )
            })?;
        let exact_chunk_scope = scopes.len() == 1 && relative == scopes[0];
        let full_tree_with_config = relative.as_os_str().is_empty() && tree_config.is_some();
        if !exact_chunk_scope && !full_tree_with_config {
            return Err(GraphMismatch::new(
                GraphMismatchReason::ModuleScope {
                    chunk_id: graph.chunk_id.clone(),
                    expected: scopes.iter().map(|scope| base.join(scope)).collect(),
                    actual: modules,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        if let Some(config) = tree_config {
            let expected = provenance
                .config_path
                .as_ref()
                .map(|path| {
                    current
                        .spec
                        .as_ref()
                        .unwrap_or(&provenance.roots.spec)
                        .join(path)
                })
                .context("tree graph lacks config path")?;
            let expected = graph_provenance::absolute(&expected)?;
            let actual = graph_provenance::absolute(config)?;
            if expected != actual {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::ConfigPath { expected, actual },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
        }
    } else {
        return Err(GraphMismatch::new(
            GraphMismatchReason::ModuleScope {
                chunk_id: graph.chunk_id.clone(),
                expected: Vec::new(),
                actual: graph_provenance::absolute(modules_root)?,
            },
            regenerate_command.clone(),
            Some(regenerate_cwd.clone()),
        )
        .into());
    }
    provenance.verify(&current)?;
    let source = provenance
        .chunk_sources
        .iter()
        .find(|source| source.chunk_id == graph.chunk_id)
        .with_context(|| {
            format!(
                "graph {} lacks source mapping for chunk {}",
                graph_path.display(),
                graph.chunk_id
            )
        })?;
    let selector_path = if let Some(root) = selector_source_root {
        graph_provenance::validate_relative_path(&source.source_path)?;
        Some(root.join(&source.source_path))
    } else {
        // Legacy source-backed selectors search fallback roots when no flag is
        // supplied. If that search finds a file, it must be the analyzed one.
        // If it finds none, a graph/spec-only query can still proceed; a later
        // source-backed selector read will fail in the resolver as before.
        anonymous_resolution::resolve_source_file(
            &source.source_path.to_string_lossy(),
            None,
            graph_path,
            modules_root,
        )
        .ok()
    };
    if let Some(path) = selector_path {
        let expected_path =
            provenance.resolve(&source.input_file.root, &source.input_file.path, &current)?;
        let canonical_expected = graph_provenance::absolute(&expected_path)?;
        let canonical_actual = graph_provenance::absolute(&path)?;
        if canonical_actual != canonical_expected {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSourceRoot {
                    chunk_id: graph.chunk_id.clone(),
                    expected: canonical_expected,
                    actual: canonical_actual,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        let actual = match std::fs::read(&path) {
            Ok(bytes) => graph_provenance::sha256(&bytes),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => "missing".to_string(),
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("reading selector source {}", path.display()));
            }
        };
        if actual != source.sha256 {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSource {
                    path,
                    chunk_id: graph.chunk_id,
                    expected: source.sha256.clone(),
                    actual,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
    }
    Ok(())
}

#[derive(Debug, Parser)]
#[command(
    name = "debundle",
    version,
    about = "Debundle JavaScript bundles and inspect peelable module work.",
    long_about = "Runs the debundle transform pipeline and exposes JSON peel-planning queries over generated dependency graphs and spec modules."
)]
pub struct DebundleArgs {
    /// Current authoring root for graph compatibility after relocating a spec.
    /// The value overrides DEBUNDLE_SPEC_ROOT; required only when a saved graph
    /// is combined with a relocated spec tree.
    #[arg(long, global = true, env = "DEBUNDLE_SPEC_ROOT")]
    spec_root: Option<std::path::PathBuf>,
    /// Current root for graph generation inputs after relocating a source tree.
    /// Distinct from the authoring commands' --source-root selector path.
    #[arg(long, global = true, env = "DEBUNDLE_GRAPH_SOURCE_ROOT")]
    graph_source_root: Option<std::path::PathBuf>,
    /// Relocated package root used by graph compatibility; repeat for each package.
    #[arg(long = "graph-package-root", global = true, value_name = "NAME=DIR", value_parser = pipeline::parse_package_root_kv)]
    graph_package_roots: Vec<(String, std::path::PathBuf)>,
    #[command(flatten)]
    invocation: InvocationArgs,
    #[command(subcommand)]
    command: DebundleCommand,
}

#[derive(Debug, Clone, ClapArgs)]
struct InvocationArgs {
    /// Print the resolved command as JSON and exit without running it.
    #[arg(long, global = true)]
    print_invocation: bool,

    /// Write the resolved invocation record before running the command.
    #[arg(long, global = true, value_name = "PATH")]
    invocation_out: Option<std::path::PathBuf>,
}

impl InvocationArgs {
    fn requested(&self) -> bool {
        self.print_invocation || self.invocation_out.is_some()
    }
}

#[derive(Debug, Subcommand)]
enum DebundleCommand {
    /// Run the debundle transform pipeline from a flat or tree-shaped spec.
    ///
    /// Parse + facts + dependency_graph + atomic_groups + realizability gate +
    /// lower + emit. The gate is part of the pipeline contract: an
    /// unrealizable spec is rejected (there is no `run --no-verify` — fix
    /// the spec), and the rejection evidence (`cycles.json` /
    /// `atomic_group_conflicts.json`) is written under
    /// `reports/tree/<chunk>/` next to `dependency_graph.json`, where
    /// `debundle gate list` / `gate describe` read it.
    Run(TransformArgs),
    /// Per-binding spec verbs (list, assign, unassign, rename, comment).
    Bindings(BindingsNs),
    /// Module-level spec verbs (list, merge, delete, propose, apply, comment).
    Modules(ModulesNs),
    /// List atomic groups (source-unit-level SCCs of the constraining-edge
    /// graph; per docs/design.md § "Two classes of atom").
    Atoms(AtomicGroupsArgs),
    /// Report spec coverage against atomic groups: which groups are claimed,
    /// which fall through to residual.
    Coverage(PatchPlanArgs),
    /// High-level graph counts (source_units, edges, atomic groups,
    /// residual-eligible bindings, …).
    #[command(name = "graph-summary")]
    GraphSummary(GraphSummaryArgs),
    /// Dereference any identifier (binding, module path, proposal,
    /// atomic group, source_unit, diagnostic) with full graph + spec context.
    Describe(DescribeArgs),
    /// Inspect source by graph ID, binding, or numbered top-level item.
    #[command(name = "inspect-source")]
    InspectSource(InspectSourceArgs),
    /// List SCCs in the module-quotient graph.
    Scc(SccArgs),
    /// List the module-quotient neighbors of a binding's source_unit.
    Cluster(ClusterArgs),
    /// Spec-wide queries (e.g. `spec stats`).
    Spec(SpecNs),
    /// Query the realizability gate's rejected SCCs (list / describe / cut).
    Gate(GateArgs),
    /// Create and verify isolated input snapshots for delegated work.
    Snapshot(snapshot::SnapshotArgs),
}

/// Parse the current process arguments with Clap metadata available for
/// resolving environment values and defaults, then dispatch the command.
pub fn run_debundle_cli_from_process() -> Result<()> {
    let raw_argv = std::env::args_os().collect::<Vec<_>>();
    let command = DebundleArgs::command();
    let matches = command.clone().get_matches_from(raw_argv.clone());
    let args = DebundleArgs::from_arg_matches(&matches)
        .expect("Clap validated DebundleArgs before constructing ArgMatches");
    let invocation = if args.invocation.requested() {
        Some(ResolvedInvocation::capture(
            &command,
            &matches,
            &raw_argv,
            args.invocation.print_invocation,
            args.invocation.invocation_out.as_deref(),
        )?)
    } else {
        None
    };
    run_debundle_cli(args, invocation)
}

fn run_debundle_cli(args: DebundleArgs, invocation: Option<ResolvedInvocation>) -> Result<()> {
    if let Some(record) = invocation {
        if let Some(path) = &args.invocation.invocation_out {
            let mut bytes = serde_json::to_vec_pretty(&record)?;
            bytes.push(b'\n');
            std::fs::write(path, bytes)
                .with_context(|| format!("writing invocation record to {}", path.display()))?;
        }
        if args.invocation.print_invocation {
            println!("{}", serde_json::to_string_pretty(&record)?);
            return Ok(());
        }
    }
    let graph_package_roots = pipeline::collect_package_roots(args.graph_package_roots)?;
    if let Err(error) = verify_mixed_graph_command(
        &args.command,
        args.spec_root.as_deref(),
        args.graph_source_root.as_deref(),
        &graph_package_roots,
    ) {
        emit_graph_mismatch_json(&args.command, &error);
        return Err(error);
    }
    let writes_with_graph = match &args.command {
        DebundleCommand::Bindings(args) => args.writes_with_graph(),
        DebundleCommand::Modules(args) => args.writes_with_graph(),
        _ => false,
    };
    let result = match args.command {
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
        DebundleCommand::InspectSource(args) => inspection_commands::run_inspect_source(args),
        DebundleCommand::Scc(args) => run_scc(args),
        DebundleCommand::Cluster(args) => run_cluster(args),
        DebundleCommand::Spec(args) => spec_commands::run(args),
        // Don't wrap with a generic context — `gate` subcommands
        // already carry enough context in their bail messages (e.g.
        // "no blocking SCC with id ..."), and an outer wrap would
        // hide them since `main.rs` prints only the top context.
        DebundleCommand::Gate(args) => run_gate_cli(
            args,
            InspectionRoots {
                spec_root: args.spec_root.clone(),
                graph_source_root: args.graph_source_root.clone(),
                graph_package_roots: graph_package_roots.clone(),
            },
        ),
        DebundleCommand::Snapshot(snapshot_args) => snapshot::run(
            snapshot_args,
            graph_provenance::CurrentRoots {
                spec: args.spec_root,
                source: args.graph_source_root,
                packages: graph_package_roots,
            },
        ),
    };
    if result.is_ok() && writes_with_graph {
        eprintln!(
            "saved graph describes the pre-edit inputs; if spec bytes changed, regenerate it before another graph-backed edit"
        );
    }
    result
}

fn emit_graph_mismatch_json(command: &DebundleCommand, error: &anyhow::Error) {
    let Some(mismatch) = error.downcast_ref::<GraphMismatch>() else {
        return;
    };
    let format = match command {
        DebundleCommand::Bindings(args) => args.format(),
        DebundleCommand::Modules(args) => args.format(),
        DebundleCommand::Coverage(args) => args.format,
        DebundleCommand::GraphSummary(args) => args.format,
        DebundleCommand::Describe(args) => args.format,
        DebundleCommand::InspectSource(args) => args.format,
        _ => None,
    };
    let format = OutputFormat::resolve(format);
    if format == OutputFormat::Text {
        return;
    }
    #[derive(serde::Serialize)]
    struct Rejection<'a> {
        kind: &'static str,
        action: &'static str,
        mismatch: &'a GraphMismatch,
    }
    let value = Rejection {
        kind: "graph_compatibility",
        action: "rejected",
        mismatch,
    };
    match format {
        OutputFormat::Json => println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("serialize mismatch")
        ),
        OutputFormat::Ndjson => println!(
            "{}",
            serde_json::to_string(&value).expect("serialize mismatch")
        ),
        OutputFormat::Text => unreachable!(),
    }
}

fn verify_mixed_graph_command(
    command: &DebundleCommand,
    spec_root: Option<&std::path::Path>,
    graph_source_root: Option<&std::path::Path>,
    graph_package_roots: &std::collections::BTreeMap<String, std::path::PathBuf>,
) -> Result<()> {
    match command {
        DebundleCommand::Bindings(args) => {
            args.verify_graph(spec_root, graph_source_root, graph_package_roots)
        }
        DebundleCommand::Modules(args) => {
            args.verify_graph(spec_root, graph_source_root, graph_package_roots)
        }
        DebundleCommand::Coverage(args) => verify_graph_compatibility(
            &args.common.dependency_graph_path,
            &args.common.modules_root,
            args.source_root.as_deref(),
            spec_root,
            graph_source_root,
            graph_package_roots,
            None,
        ),
        DebundleCommand::GraphSummary(args) if args.include_proposals => {
            verify_graph_compatibility(
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                args.source_root.as_deref(),
                spec_root,
                graph_source_root,
                graph_package_roots,
                None,
            )
        }
        DebundleCommand::Describe(args) => verify_graph_compatibility(
            &args.common.dependency_graph_path,
            &args.common.modules_root,
            args.source_root.as_deref(),
            spec_root,
            graph_source_root,
            graph_package_roots,
            None,
        ),
        DebundleCommand::InspectSource(args) if args.id.is_some() => {
            let graph = args.graph.as_deref().context("--id requires --graph")?;
            let modules = args.modules.as_deref().context("--id requires --modules")?;
            verify_graph_compatibility(
                graph,
                modules,
                args.source_root.as_deref(),
                spec_root,
                graph_source_root,
                graph_package_roots,
                None,
            )
        }
        _ => Ok(()),
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
    use pipeline::TransformArgs;

    use super::{DebundleArgs, DebundleCommand};

    fn parsed_run_args(argv: &[&str]) -> TransformArgs {
        let parsed = DebundleArgs::try_parse_from(argv).expect("parse cli");
        match parsed.command {
            DebundleCommand::Run(args) => args,
            other => panic!("expected run command, got {other:?}"),
        }
    }

    #[test]
    fn parse_run_args_packages_root() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&[
                "debundle",
                "run",
                "--spec",
                "spec.yaml",
                "--packages-root",
                "/tmp/packages",
            ]);
            let cli = args.resolve().expect("resolve cli");
            assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
        });
    }
}
