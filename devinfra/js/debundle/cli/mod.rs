pub mod binding;
mod candidate_assessment;
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
mod contract_audit;
mod inspection_commands;
mod integrity_explanation;
mod modules_commands;
mod propose_commutations;
mod selector_explanation;
mod selector_input;
mod snapshot;
mod source_unit_refine;
mod spec_commands;
mod spec_lint;
mod static_imports;

use analyze_command::AnalyzeArgs;
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, CommandFactory, FromArgMatches, Parser, Subcommand};
use command_output::{ReportOutputError, write_report};
use graph_provenance::{CurrentRoots, GraphMismatch, GraphMismatchReason};
use lowering::FullReportKind;
use peel::{AtomicGroupsArgs, GraphSummaryArgs, OutputFormat, PatchPlanArgs};
use pipeline::{
    GATE_REJECTED_EXIT_CODE, GateRejected, RunArtifacts, RunOutput, TransformRunOptions,
    run_transform_with_analysis,
};
use std::io::{self, Write};
use std::process::ExitCode;
use transform_command::TransformArgs;

use crate::bindings_commands::BindingsNs;
use crate::gate::{GateArgs, run_gate_cli};
use crate::inspection_commands::{DescribeArgs, InspectSourceArgs};
use crate::invocation::ResolvedInvocation;
use crate::modules_commands::ModulesNs;
use crate::scc_cluster::{ClusterArgs, SccArgs, run_cluster, run_scc};
use crate::spec_commands::SpecNs;

/// Which current authoring scope a graph-backed command actually interprets.
#[derive(Clone, Copy)]
pub(crate) enum GraphCompatibilityScope<'a> {
    AuthoredModuleTree {
        tree_config: Option<&'a std::path::Path>,
    },
    ExactSourceUnit,
}

/// Shared pre-edit/pre-query check for any operation that joins a saved graph
/// to current authoring files. Historical graph-only views load a `DependencyGraphInput` directly.
pub(crate) fn verify_graph_compatibility(
    graph_path: &std::path::Path,
    modules_root: &std::path::Path,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    tree_config: Option<&std::path::Path>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    verify_graph_compatibility_with_scope(
        graph_path,
        Some(modules_root),
        selector_source_root,
        roots,
        GraphCompatibilityScope::AuthoredModuleTree { tree_config },
    )
}

/// `modules_root` is required by the authored-module-tree scope; the
/// exact-source-unit scope uses it only to reject a fallback source shadow
/// when no selector root is supplied.
pub(crate) fn verify_graph_compatibility_with_scope(
    graph_path: &std::path::Path,
    modules_root: Option<&std::path::Path>,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    scope: GraphCompatibilityScope<'_>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    let graph = graph_provenance::read_graph_header(graph_path)?;
    verify_graph_header_compatibility(
        graph_path,
        &graph,
        modules_root,
        selector_source_root,
        roots,
        scope,
    )
}

fn verify_graph_header_compatibility(
    graph_path: &std::path::Path,
    graph: &graph_provenance::GraphHeader,
    modules_root: Option<&std::path::Path>,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    scope: GraphCompatibilityScope<'_>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    let (require_module_scope, tree_config) = match scope {
        GraphCompatibilityScope::AuthoredModuleTree { tree_config } => (true, tree_config),
        GraphCompatibilityScope::ExactSourceUnit => (false, None),
    };
    let scoped_modules_root = match (require_module_scope, modules_root) {
        (true, None) => anyhow::bail!("a module-scoped graph check requires --modules"),
        (_, modules_root) => modules_root,
    };
    let provenance = graph.provenance.as_ref().ok_or_else(|| {
        GraphMismatch::new(
            GraphMismatchReason::MissingProvenance {
                graph: graph_path.to_path_buf(),
            },
            vec!["debundle".to_string(), "run".to_string()],
            None,
        )
    })?;
    let (regenerate_command, regenerate_cwd) = provenance.regeneration_recipe(roots)?;
    if let (true, None) = (require_module_scope, &provenance.modules_directory) {
        return Err(GraphMismatch::new(
            GraphMismatchReason::ModuleScope {
                chunk_id: graph.chunk_id.clone(),
                expected: Vec::new(),
                actual: graph_provenance::absolute(
                    scoped_modules_root.expect("checked by the scope match above"),
                )?,
            },
            regenerate_command.clone(),
            Some(regenerate_cwd.clone()),
        )
        .into());
    }
    // `inspect-source --id unit:*` resolves a saved node without interpreting
    // current module claims, so it skips only this authored-scope check.
    if require_module_scope {
        let modules_root = scoped_modules_root.expect("checked by the scope match above");
        let directory = provenance
            .modules_directory
            .as_ref()
            .expect("checked above");
        let base = roots
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
                    roots
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
    }
    let source_context = provenance.verify_chunk_source(roots, &graph.chunk_id)?;
    let expected_path = graph_provenance::absolute(source_context.input_file())?;
    let source_path = source_context.source_path().to_string_lossy();
    let selector_candidate = if let Some(root) = selector_source_root {
        let canonical_root = graph_provenance::absolute(root)?;
        let logical_path = source_context.source_path();
        let candidate = if !logical_path.is_absolute()
            && graph_provenance::validate_relative_path(logical_path).is_ok()
        {
            Some(canonical_root.join(logical_path))
        } else {
            None
        };
        let root_contains_input =
            canonical_root.is_dir() && expected_path.starts_with(&canonical_root);
        let candidate_exists = match candidate.as_ref().map(std::fs::symlink_metadata) {
            Some(Ok(_)) => true,
            Some(Err(error)) if error.kind() != std::io::ErrorKind::NotFound => true,
            _ => false,
        };
        let canonical_candidate = if candidate_exists {
            candidate.as_ref().map(|candidate| {
                graph_provenance::absolute(candidate).unwrap_or_else(|_| candidate.clone())
            })
        } else {
            None
        };
        if !root_contains_input
            || canonical_candidate
                .as_ref()
                .is_some_and(|candidate| candidate != &expected_path)
        {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSourceRoot {
                    chunk_id: graph.chunk_id.clone(),
                    expected: expected_path,
                    actual: canonical_candidate.unwrap_or_else(|| {
                        candidate
                            .clone()
                            .unwrap_or_else(|| canonical_root.join(logical_path))
                    }),
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        candidate.filter(|_| candidate_exists)
    } else {
        modules_root.and_then(|modules_root| {
            anonymous_resolution::find_source_file_candidate(
                &source_path,
                None,
                graph_path,
                modules_root,
            )
        })
    };
    if let Some(candidate) = selector_candidate {
        let canonical_candidate = graph_provenance::absolute(&candidate)?;
        if canonical_candidate != expected_path {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSourceRoot {
                    chunk_id: graph.chunk_id.clone(),
                    expected: expected_path,
                    actual: canonical_candidate,
                },
                regenerate_command,
                Some(regenerate_cwd),
            )
            .into());
        }
    }
    Ok(source_context)
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
    #[arg(long = "graph-package-root", global = true, value_name = "NAME=DIR", value_parser = package_args::parse_package_root_kv)]
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

/// Args for `debundle run`.
#[derive(Debug, ClapArgs)]
pub(crate) struct RunArgs {
    #[command(flatten)]
    pub(crate) transform: TransformArgs,
    /// JSON {"modules":[{"chunk":"app","module":"features/view"}]}:
    /// retain only these modules after resolving the complete spec and its
    /// contracts. An empty list retains no named modules. Requires
    /// inline_in_entry, per_atomic_group, or coarse_atomic_groups for every
    /// materialized chunk.
    /// Omitted members keep their source names in entry or generated residual
    /// modules; normal gates and emission still run.
    #[arg(long, conflicts_with = "graphs", value_name = "JSON")]
    pub(crate) ownership_subset: Option<std::path::PathBuf>,
    /// Take the cross-module summaries and per-chunk facts from this analysis
    /// artifact (`debundle analyze`) instead of recomputing them. The run
    /// refuses an artifact computed by another engine, from other sources,
    /// or for another analysis projection, naming what differs.
    #[arg(long)]
    pub(crate) analysis: Option<std::path::PathBuf>,
    /// Serialize complete dependency/program graphs for graph-backed queries.
    #[arg(long)]
    pub(crate) graphs: bool,
    /// Write reports under this output root, with no JS/assets. Uses the same
    /// canonical validators and requires a precomputed analysis artifact.
    #[arg(
        long,
        requires = "analysis",
        conflicts_with = "dry_run",
        value_name = "OUT_ROOT"
    )]
    pub(crate) reports_only: Option<std::path::PathBuf>,
    /// Stream all effect-conflict pairs and cells as per-chunk JSONL beside
    /// reports. An accepted dry-run suppresses reports and refuses this request.
    #[arg(long)]
    pub(crate) effect_conflicts_jsonl: bool,
    /// Export JSONL only for these materialized chunks (repeatable). Analysis
    /// and bounded reports still cover the full configured input.
    #[arg(long = "effect-conflicts-chunk", requires = "effect_conflicts_jsonl", value_parser = artifact::paths::normalize_module_path)]
    pub(crate) effect_conflicts_chunks: Vec<String>,
    /// Emit diagnostic evidence independently of full graphs (repeatable):
    /// translation_validation, export_reentry, residual_atomic_groups, or residual_extractions.
    #[arg(long = "full-report", value_name = "KIND")]
    pub(crate) full_reports: Vec<FullReportKind>,
    /// Select only these full-report sinks; all summaries and checks stay exact.
    #[arg(long = "full-report-chunk", requires = "full_reports", value_parser = artifact::paths::normalize_module_path)]
    pub(crate) full_report_chunks: Vec<String>,
    /// Probe only these residual atoms (repeatable), e.g. atomic:3.
    #[arg(
        long = "residual-extraction-atom",
        requires = "full_reports",
        value_name = "ATOM"
    )]
    pub(crate) residual_extraction_atoms: Vec<String>,
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
    /// `reports/tree/<chunk>/` next to `dependency_graph.json.gz`, where
    /// `debundle gate list` / `gate describe` read it.
    Run(RunArgs),
    /// Compute the analysis (cross-module summaries and every analysed
    /// chunk's facts) from a projection written by `debundle spec
    /// project-analysis`, as an artifact `debundle run --analysis` reads.
    ///
    /// The projection is the only spec input, so the artifact stays valid
    /// across every spec edit that leaves the projection unchanged.
    Analyze(AnalyzeArgs),
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
    /// Explain which native-integrity guards a source unit failed and which
    /// census witnesses leave each required intrinsic slot unknown.
    #[command(name = "explain-integrity")]
    ExplainIntegrity(integrity_explanation::ExplainIntegrityArgs),
    /// Query original chunks' resolved ordinary static ESM import paths.
    #[command(name = "static-imports")]
    StaticImports(static_imports::StaticImportsArgs),
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

fn cli_command() -> clap::Command {
    DebundleArgs::command().mut_subcommand("modules", |modules| {
        modules
            .mut_subcommand("refine", |refine| {
                // These commands did not previously consume the shared transform
                // environment defaults. Keep their opt-in context explicit while
                // leaving those defaults available to the existing run/edit verbs.
                refine
                    .mut_arg("tree_source_root", |arg| arg.env(None::<&'static str>))
                    .mut_arg("out_root", |arg| arg.env(None::<&'static str>))
            })
            .mut_subcommand("apply", |apply| {
                apply
                    .mut_arg("tree_source_root", |arg| arg.env(None::<&'static str>))
                    .mut_arg("out_root", |arg| arg.env(None::<&'static str>))
            })
    })
}

/// Parse the current process arguments with Clap metadata available for
/// resolving environment values and defaults, then dispatch the command.
pub fn run_debundle_cli_from_process(analysis_engine_sha256: &'static str) -> ExitCode {
    let raw_argv = std::env::args_os().collect::<Vec<_>>();
    let command = cli_command();
    let matches = command.clone().get_matches_from(raw_argv.clone());
    if let Err(error) = transform_command::validate_transform_arg_matches(&matches) {
        command
            .clone()
            .error(clap::error::ErrorKind::ArgumentConflict, error.to_string())
            .exit();
    }
    let args = DebundleArgs::from_arg_matches(&matches)
        .expect("Clap validated DebundleArgs before constructing ArgMatches");
    let query_format = top_level_query_format(&args.command);
    let stdout = io::stdout();
    let mut stdout = stdout.lock();
    let mut stderr = io::stderr();
    let result = (|| {
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
        run_debundle_cli(
            &mut stderr,
            args,
            invocation,
            analysis_engine_sha256,
            &mut stdout,
        )
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if report_output_broken_pipe(&error) {
                return ExitCode::SUCCESS;
            }
            if let Some(format) = query_format
                .filter(|format| *format != OutputFormat::Text)
                .filter(|_| !has_report_output_error(&error))
            {
                let output_error = if error.downcast_ref::<GraphMismatch>().is_some() {
                    emit_graph_mismatch_json(&mut stdout, format, &error)
                } else {
                    emit_query_failure(&mut stdout, format, &error)
                };
                if let Err(output_error) = output_error {
                    let error = command_output::retain_operation_error(error, Err(output_error));
                    return report_cli_error(&mut stderr, &error);
                }
            }
            if error.downcast_ref::<GateRejected>().is_some() {
                report_cli_error_with_code(&mut stderr, &error, GATE_REJECTED_EXIT_CODE)
            } else {
                report_cli_error(&mut stderr, &error)
            }
        }
    }
}

fn run_debundle_cli(
    stderr: &mut (dyn std::io::Write + Send),
    args: DebundleArgs,
    invocation: Option<ResolvedInvocation>,
    analysis_engine_sha256: &'static str,
    output: &mut dyn Write,
) -> Result<()> {
    if let Some(record) = invocation {
        if let Some(path) = &args.invocation.invocation_out {
            let mut bytes = serde_json::to_vec_pretty(&record)?;
            bytes.push(b'\n');
            std::fs::write(path, bytes)
                .with_context(|| format!("writing invocation record to {}", path.display()))?;
        }
        if args.invocation.print_invocation {
            write_report(&record, OutputFormat::Json, output, |_, _| Ok(()))
                .context("writing invocation record to stdout")?;
            return Ok(());
        }
    }
    let roots = CurrentRoots {
        spec: args.spec_root,
        source: args.graph_source_root,
        packages: package_args::collect_package_roots(args.graph_package_roots)?,
    };
    let describe_query = match &args.command {
        DebundleCommand::Describe(describe_args) => {
            Some(inspection_commands::validate_describe_query(describe_args)?)
        }
        _ => None,
    };
    let source_context = match verify_mixed_graph_command(&args.command, &roots) {
        Ok(source_context) => source_context,
        Err(error) => {
            if top_level_query_format(&args.command).is_none() {
                let emission =
                    emit_graph_mismatch_json(output, command_output_format(&args.command), &error);
                return Err(command_output::retain_operation_error(error, emission));
            }
            return Err(error);
        }
    };
    let writes_with_graph = match &args.command {
        DebundleCommand::Bindings(args) => args.writes_with_graph(),
        DebundleCommand::Modules(args) => args.writes_with_graph(),
        _ => false,
    };
    let result = match args.command {
        DebundleCommand::Run(RunArgs {
            transform: mut args,
            ownership_subset,
            analysis,
            graphs,
            reports_only,
            effect_conflicts_jsonl,
            effect_conflicts_chunks,
            full_reports,
            full_report_chunks,
            residual_extraction_atoms,
        }) => {
            if let Some(root) = &reports_only {
                if let Some(out_root) = &args.context.auxiliary.out_root {
                    anyhow::ensure!(
                        graph_provenance::absolute(root)? == graph_provenance::absolute(out_root)?,
                        "--reports-only and --out-root must name the same output root"
                    );
                }
                args.context.auxiliary.out_root = Some(root.clone());
            }
            let artifacts = reports_only.map_or(RunArtifacts::Program, |out_root| {
                RunArtifacts::ReportsOnly { out_root }
            });
            let run_output = args.run_output()?;
            let keep_going = !args.fail_fast;
            let cli = args.context.resolve()?;
            command_diagnostics::run(stderr, |diagnostics| {
                run_transform_with_analysis(
                    &cli,
                    TransformRunOptions {
                        ownership_subset: ownership_subset
                            .as_deref()
                            .map(lowering::ownership_subset::OwnershipSubset::read)
                            .transpose()?,
                        output: run_output,
                        artifacts,
                        graph_reports: if graphs {
                            lowering::GraphReportSelection::Full
                        } else {
                            lowering::GraphReportSelection::None
                        },
                        keep_going,
                        report_dir_override: None,
                        list_template_identifiers: false,
                        effect_conflicts_jsonl,
                        effect_conflicts_chunks: effect_conflicts_chunks.into_iter().collect(),
                        full_reports: full_reports.into_iter().collect(),
                        full_report_chunks: full_report_chunks.into_iter().collect(),
                        residual_extraction_atoms: residual_extraction_atoms.into_iter().collect(),
                    },
                    analysis
                        .as_deref()
                        .map(|path| pipeline::AnalysisArtifactInput {
                            path,
                            analysis_engine_sha256,
                        }),
                    diagnostics,
                )
            })?;
            if run_output == RunOutput::DryRun {
                command_output::write_line(
                    output,
                    format_args!("dry-run: transform pipeline checks passed; no outputs written"),
                )?;
            }
            Ok(())
        }
        DebundleCommand::Analyze(args) => args.run(stderr, analysis_engine_sha256),
        DebundleCommand::Bindings(args) => bindings_commands::run(
            stderr,
            args,
            source_context.as_ref(),
            analysis_engine_sha256,
            output,
        ),
        DebundleCommand::Modules(args) => modules_commands::run(
            stderr,
            args,
            source_context.as_ref(),
            analysis_engine_sha256,
            output,
        ),
        DebundleCommand::Atoms(args) => inspection_commands::run_atoms(args, output),
        DebundleCommand::Coverage(args) => {
            inspection_commands::run_coverage(stderr, args, source_context.as_ref(), output)
        }
        DebundleCommand::GraphSummary(args) => {
            inspection_commands::run_graph_summary(args, source_context.as_ref(), output)
        }
        DebundleCommand::Describe(describe_args) => inspection_commands::run_describe(
            stderr,
            describe_args,
            source_context,
            describe_query.expect("Describe query was validated before graph preflight"),
            output,
        ),
        DebundleCommand::InspectSource(args) => {
            inspection_commands::run_inspect_source(stderr, args, source_context.as_ref(), output)
        }
        DebundleCommand::StaticImports(args) => static_imports::run(args, output),
        DebundleCommand::ExplainIntegrity(explain_args) => {
            integrity_explanation::run(stderr, explain_args, roots, output)
        }
        DebundleCommand::Scc(args) => run_scc(args, output),
        DebundleCommand::Cluster(args) => run_cluster(args, output),
        DebundleCommand::Spec(args) => {
            spec_commands::run(stderr, args, source_context.as_ref(), output)
        }
        // Don't wrap with a generic context — `gate` subcommands
        // already carry enough context in their bail messages (e.g.
        // "no blocking SCC with id ..."), and an outer wrap would
        // hide them since `main.rs` prints only the top context.
        DebundleCommand::Gate(gate_args) => run_gate_cli(gate_args, roots, output),
        DebundleCommand::Snapshot(snapshot_args) => snapshot::run(output, snapshot_args, roots),
    };
    let operation_completed = result.is_ok()
        || result.as_ref().is_err_and(|error| {
            has_report_output_error(error)
                && error
                    .downcast_ref::<command_output::FailedOperationOutput>()
                    .is_none()
        });
    if operation_completed && writes_with_graph {
        return command_diagnostics::finish(
            result,
            command_diagnostics::write_diagnostic(
                stderr,
                format_args!(
                    "saved graph describes the pre-edit inputs; if spec bytes changed, regenerate it before another graph-backed edit"
                ),
            ),
        );
    }
    result
}

fn has_report_output_error(error: &anyhow::Error) -> bool {
    first_report_output_error(error).is_some()
}

fn report_output_broken_pipe(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<command_diagnostics::DiagnosticOutputError>()
        .is_none()
        && error
            .downcast_ref::<command_output::FailedOperationOutput>()
            .is_none()
        && first_report_output_error(error).is_some_and(ReportOutputError::is_broken_pipe)
}

fn first_report_output_error(error: &anyhow::Error) -> Option<&ReportOutputError> {
    error
        .chain()
        .find_map(|cause| cause.downcast_ref::<ReportOutputError>())
}

fn report_cli_error(stderr: &mut dyn Write, error: &anyhow::Error) -> ExitCode {
    report_cli_error_with_code(stderr, error, 1)
}

fn report_cli_error_with_code(stderr: &mut dyn Write, error: &anyhow::Error, code: u8) -> ExitCode {
    match command_diagnostics::write_diagnostic(stderr, format_args!("{error:#}")) {
        Ok(()) => ExitCode::from(code),
        Err(_) => ExitCode::from(1),
    }
}

fn command_output_format(command: &DebundleCommand) -> OutputFormat {
    let format = match command {
        DebundleCommand::Bindings(args) => args.format(),
        DebundleCommand::Modules(args) => args.format(),
        DebundleCommand::Coverage(args) => args.format,
        DebundleCommand::GraphSummary(args) => args.format,
        DebundleCommand::Describe(args) => args.format,
        DebundleCommand::InspectSource(args) => args.format,
        DebundleCommand::StaticImports(args) => args.format,
        DebundleCommand::ExplainIntegrity(args) => args.format,
        _ => None,
    };
    OutputFormat::resolve(format)
}

fn emit_graph_mismatch_json(
    output: &mut dyn Write,
    format: OutputFormat,
    error: &anyhow::Error,
) -> Result<()> {
    let Some(mismatch) = error.downcast_ref::<GraphMismatch>() else {
        return Ok(());
    };
    if format == OutputFormat::Text {
        return Ok(());
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
    write_report(&value, format, output, |_, _| Ok(())).context("writing graph mismatch output")
}

#[derive(serde::Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
enum QueryFailureReason<'a> {
    Io,
    InvalidJson,
    SourceUnitNotFound,
    UnsupportedIdentifierPrefix {
        identifier: &'a str,
        prefix: &'a str,
        #[serde(skip_serializing_if = "Option::is_none")]
        suggestion: Option<&'a str>,
        accepted_prefixes: &'static [&'static str],
        examples: &'static [&'static str],
    },
    QueryFailed,
}

#[derive(serde::Serialize)]
struct QueryFailure<'a> {
    #[serde(flatten)]
    reason: QueryFailureReason<'a>,
    message: &'a str,
}

fn emit_query_failure(
    output: &mut dyn Write,
    format: OutputFormat,
    error: &anyhow::Error,
) -> Result<()> {
    let unsupported_prefix = error
        .chain()
        .find_map(|cause| cause.downcast_ref::<inspection_commands::UnsupportedIdentifierPrefix>());
    let reason = if let Some(failure) = unsupported_prefix {
        QueryFailureReason::UnsupportedIdentifierPrefix {
            identifier: &failure.identifier,
            prefix: &failure.prefix,
            suggestion: failure.suggestion.as_deref(),
            accepted_prefixes: inspection_commands::ACCEPTED_IDENTIFIER_PREFIXES,
            examples: inspection_commands::ACCEPTED_IDENTIFIER_EXAMPLES,
        }
    } else if error
        .chain()
        .any(|cause| cause.downcast_ref::<serde_json::Error>().is_some())
    {
        QueryFailureReason::InvalidJson
    } else if error
        .chain()
        .any(|cause| cause.downcast_ref::<std::io::Error>().is_some())
    {
        QueryFailureReason::Io
    } else if error.chain().any(|cause| {
        cause
            .downcast_ref::<peel::plan::SourceUnitNotFound>()
            .is_some()
    }) {
        QueryFailureReason::SourceUnitNotFound
    } else {
        QueryFailureReason::QueryFailed
    };
    let message = format!("{error:#}");
    let failure = QueryFailure {
        reason,
        message: &message,
    };
    write_report(&failure, format, output, |_, _| Ok(())).context("writing query failure output")
}

fn top_level_query_format(command: &DebundleCommand) -> Option<OutputFormat> {
    match command {
        DebundleCommand::Atoms(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::Coverage(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::GraphSummary(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::Describe(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::InspectSource(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::StaticImports(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::ExplainIntegrity(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::Scc(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::Cluster(args) => Some(OutputFormat::resolve(args.format)),
        DebundleCommand::Spec(args) => args.query_format(),
        _ => None,
    }
}

fn verify_mixed_graph_command(
    command: &DebundleCommand,
    roots: &CurrentRoots,
) -> Result<Option<graph_provenance::VerifiedGraphSourceContext>> {
    match command {
        DebundleCommand::Bindings(args) => args.verify_graph(roots),
        DebundleCommand::Modules(args) => args.verify_graph(roots),
        DebundleCommand::Coverage(args) => verify_graph_compatibility(
            &args.common.dependency_graph_path,
            &args.common.modules_root,
            args.source_root.as_deref(),
            roots,
            None,
        )
        .map(Some),
        DebundleCommand::GraphSummary(args) if args.include_proposals => {
            verify_graph_compatibility(
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                args.source_root.as_deref(),
                roots,
                None,
            )
            .map(Some)
        }
        DebundleCommand::Describe(args) => verify_graph_compatibility(
            &args.common.dependency_graph_path,
            &args.common.modules_root,
            args.source_root.as_deref(),
            roots,
            args.tree_config.as_deref(),
        )
        .map(Some),
        DebundleCommand::InspectSource(args) if args.id.is_some() => {
            let graph = args.graph.as_deref().context("--id requires --graph")?;
            let modules = args.modules.as_deref().context("--id requires --modules")?;
            inspection_commands::validate_query_id(
                args.id.as_deref().expect("guarded by is_some"),
                modules,
            )?;
            verify_graph_compatibility_with_scope(
                graph,
                Some(modules),
                args.source_root.as_deref(),
                roots,
                if args.id.as_deref().is_some_and(|id| id.starts_with("unit:")) {
                    GraphCompatibilityScope::ExactSourceUnit
                } else {
                    GraphCompatibilityScope::AuthoredModuleTree { tree_config: None }
                },
            )
            .map(Some)
        }
        DebundleCommand::Spec(args) => args.verify_graph(roots),
        _ => Ok(None),
    }
}

/// Shared tail of every report subcommand: resolve `format` (tty-aware default),
/// render `report`, and tag IO errors with `context`.
fn emit_report<T, F>(
    output: &mut dyn Write,
    format: Option<OutputFormat>,
    report: &T,
    text_render: F,
    context: &'static str,
) -> Result<()>
where
    T: serde::Serialize,
    F: FnOnce(&T, &mut String) -> Result<()>,
{
    write_report(report, OutputFormat::resolve(format), output, text_render).context(context)
}

/// One flattened, tagged NDJSON row. Each caller owns its section order and payload schema.
fn write_section<T: serde::Serialize>(
    output: &mut dyn Write,
    section: &str,
    payload: &T,
) -> Result<()> {
    #[derive(serde::Serialize)]
    struct Line<'a, T> {
        section: &'a str,
        #[serde(flatten)]
        payload: &'a T,
    }
    write_report(
        &Line { section, payload },
        OutputFormat::Ndjson,
        output,
        |_, _| Ok(()),
    )
    .context("writing NDJSON section")
}

#[cfg(test)]
mod tests {
    use anyhow::Error;
    use std::io;
    use std::path::PathBuf;

    use clap::Parser;
    use transform_command::TransformArgs;

    use super::{DebundleArgs, DebundleCommand, ReportOutputError, report_output_broken_pipe};

    #[test]
    fn only_stdout_write_and_flush_broken_pipe_is_suppressed() {
        let output_error = Error::new(ReportOutputError::Write(io::Error::new(
            io::ErrorKind::BrokenPipe,
            "closed output",
        )));
        assert!(report_output_broken_pipe(&output_error));

        let callback_error = Error::new(ReportOutputError::Render(Error::new(
            ReportOutputError::Write(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "callback's unrelated pipe",
            )),
        )));
        assert!(!report_output_broken_pipe(&callback_error));
    }

    fn parsed_run_args(argv: &[&str]) -> TransformArgs {
        let parsed = DebundleArgs::try_parse_from(argv).expect("parse cli");
        match parsed.command {
            DebundleCommand::Run(args) => args.transform,
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
            let cli = args.context.resolve().expect("resolve cli");
            assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
        });
    }
}

#[cfg(test)]
mod output_failure_tests {
    use super::*;
    use analysis::diagnostics::{ChunkDiagnostic, InvocationDiagnostic};
    use analysis::reports::schema::PurityCompletionDisagreement;
    use anyhow::Error;
    use command_diagnostics::{DiagnosticOutputError, finish};
    use std::error::Error as _;

    struct FailingWriter {
        flush: bool,
    }

    impl Write for FailingWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.flush {
                Ok(bytes.len())
            } else {
                Err(io::Error::from(io::ErrorKind::BrokenPipe))
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::from(io::ErrorKind::PermissionDenied))
        }
    }

    fn purity_disagreement_diagnostic() -> ChunkDiagnostic {
        ChunkDiagnostic::PurityCompletionDisagreement {
            ordinal: source_identity::SourceUnitOrdinal(3),
            source_location: Some(analysis::SourceLocation {
                source_path: "src/main.js".to_owned(),
                start_line: 4,
                end_line: 4,
                start_column: Some(7),
            }),
            evidence: Box::new(PurityCompletionDisagreement {
                prepared: analysis::Purity::pure(),
                completed: analysis::Purity::NotPure {
                    reasons: vec![analysis::PurityReason {
                        rule: analysis::PurityRule::UnknownCall,
                        source_location: None,
                        detail: Some("injected monitoring evidence".to_owned()),
                        author_guidance: None,
                        cause_ref: None,
                        cause: None,
                        span: Default::default(),
                    }],
                },
            }),
        }
    }

    const PURITY_DISAGREEMENT_DIAGNOSTIC: &str = "warning: chunk main: synchronous purity completion disagreed at statement #3 \
         (src/main.js:4:7): prepared pure, completed not pure \
         (unknown_call: injected monitoring evidence)\n";

    #[test]
    fn diagnostic_write_and_flush_failures_retain_the_operation_and_native_io_causes() {
        for flush in [false, true] {
            for operation_fails in [false, true] {
                let mut stderr = FailingWriter { flush };
                let native_operation = "not-an-integer".parse::<u32>().unwrap_err();
                let result = command_diagnostics::run(&mut stderr, |diagnostics| {
                    diagnostics.record_invocation(InvocationDiagnostic::OwnershipSubset);
                    if operation_fails {
                        Err(Error::new(native_operation).context("operation context"))
                    } else {
                        Ok(())
                    }
                });
                let error = result.unwrap_err();
                assert_eq!(
                    error.downcast_ref::<std::num::ParseIntError>().is_some(),
                    operation_fails
                );
                let emission = error.downcast_ref::<DiagnosticOutputError>().unwrap();
                let native = emission
                    .source()
                    .unwrap()
                    .downcast_ref::<io::Error>()
                    .unwrap();
                assert_eq!(
                    native.kind(),
                    if flush {
                        io::ErrorKind::PermissionDenied
                    } else {
                        io::ErrorKind::BrokenPipe
                    }
                );
                assert!(!report_output_broken_pipe(&error));
            }
        }
    }

    #[test]
    fn purity_completion_disagreement_renders_one_source_and_ordinal_diagnostic() {
        let mut stderr = Vec::new();
        let result = command_diagnostics::run(&mut stderr, |diagnostics| {
            diagnostics.record_chunk("main", purity_disagreement_diagnostic());
            Ok(())
        });

        result.expect("diagnostic write succeeds");
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            PURITY_DISAGREEMENT_DIAGNOSTIC
        );
    }

    #[test]
    fn purity_completion_disagreement_is_rendered_when_analysis_fails() {
        let mut stderr = Vec::new();
        let result = command_diagnostics::run(&mut stderr, |diagnostics| {
            diagnostics.record_chunk("main", purity_disagreement_diagnostic());
            Err(Error::msg("analysis failed"))
        });

        assert_eq!(result.unwrap_err().to_string(), "analysis failed");
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            PURITY_DISAGREEMENT_DIAGNOSTIC
        );
    }

    #[test]
    fn simultaneous_stdout_and_stderr_failures_are_not_successful_pipe_termination() {
        let stdout = Error::new(ReportOutputError::Write(io::Error::from(
            io::ErrorKind::BrokenPipe,
        )));
        let error = finish::<()>(
            Err(stdout),
            Err(DiagnosticOutputError::Write(io::Error::from(
                io::ErrorKind::BrokenPipe,
            ))),
        )
        .unwrap_err();
        assert!(
            error
                .downcast_ref::<ReportOutputError>()
                .unwrap()
                .is_broken_pipe()
        );
        assert!(error.downcast_ref::<DiagnosticOutputError>().is_some());
        assert!(!report_output_broken_pipe(&error));
    }

    #[test]
    fn failed_operation_stays_failed_when_its_stdout_diagnostic_pipe_is_closed() {
        let operation = "invalid".parse::<u32>().unwrap_err();
        let error = command_output::retain_operation_error(
            Error::new(operation),
            Err(ReportOutputError::Write(io::Error::from(io::ErrorKind::BrokenPipe)).into()),
        );
        assert!(error.downcast_ref::<std::num::ParseIntError>().is_some());
        let emission = error
            .downcast_ref::<command_output::FailedOperationOutput>()
            .unwrap();
        assert!(
            emission
                .0
                .downcast_ref::<ReportOutputError>()
                .unwrap()
                .is_broken_pipe()
        );
        assert!(!report_output_broken_pipe(&error));
    }

    #[test]
    fn comment_write_is_applied_before_output_failure() -> Result<()> {
        for flush in [false, true] {
            let directory = tempfile::tempdir()?;
            let module = directory.path().join("module.yaml");
            std::fs::write(&module, "members: [{selector: {binding: {name: a}}}]\n")?;
            let args = DebundleArgs::try_parse_from([
                "debundle",
                "modules",
                "comment",
                "module",
                "persisted",
                "--modules",
                directory.path().to_str().unwrap(),
                "--format",
                "json",
            ])?;
            let mut stderr = Vec::new();
            let error = run_debundle_cli(
                &mut stderr,
                args,
                None,
                "test-engine",
                &mut FailingWriter { flush },
            )
            .unwrap_err();
            let emission = error.downcast_ref::<ReportOutputError>().unwrap();
            assert_eq!(report_output_broken_pipe(&error), !flush);
            assert!(matches!(
                emission,
                ReportOutputError::Write(_) | ReportOutputError::Flush(_)
            ));
            let document = spec_modules::read_module_file(&module)?;
            assert_eq!(document.comment.as_deref(), Some("persisted"));
            assert!(stderr.is_empty());
        }
        Ok(())
    }
}
