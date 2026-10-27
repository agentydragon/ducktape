//! `debundle gate` — query the realizability gate's rejected SCCs.
//!
//! When `materialize_logical_modules` rejects a spec, it writes one
//! entry per blocking SCC to `reports/tree/<chunk>/cycles.json` and
//! exits with a stderr summary. The trimmed wire shape (see
//! [`BlockingSccEntry`] and `docs/wire_format.md`) carries
//! `id` / `modules` / `cut` per SCC — enough to dispatch follow-up
//! queries without storing the full evidence block on disk.
//!
//! This CLI surfaces three read-only views over that data:
//!
//! - `gate list` — one line per blocking SCC (id, modules count, cut size).
//! - `gate describe <id>` — full picture: modules list, cut, and
//!   **recomputed evidence**, sourced from `dependency_graph.json` plus
//!   the SCC's module set. Same per-binding-pair render as the
//!   per-rejection stderr summary.
//! - `gate cut <id>` — just the cut edges (already in the trimmed
//!   `cycles.json`). The actionable subset spec authors edit.
//!
//! Name choice: `gate` reads well in error messages ("the gate
//! rejected; run `debundle gate list` for details") and disambiguates
//! from the generic `scc` command, which lists every quotient SCC.
//! The unit is the **blocking** SCC — a multi-module SCC with at
//! least one realizability-constraining cross-module edge — not
//! arbitrary cycles, of which a single SCC can contain exponentially
//! many.

use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use ::gate::{BlockingSccEntry, CycleEdge};
use analysis::{
    CallbackFactoryBarrierReport, CallbackFactoryRejectionReport, DepKind, DependencyGraphReport,
    EdgeRoleReport, ModuleKey, Purity, SequencedSourceUnitCause,
};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use serde::Serialize;
use spec::ModulePath;
use swc_atoms::Atom;

/// Top-level `debundle gate ...` argument node.
#[derive(Debug, ClapArgs)]
pub struct GateArgs {
    #[command(subcommand)]
    command: GateCommand,
}

#[derive(Debug, Subcommand)]
enum GateCommand {
    /// List every blocking SCC. One row per entry in `cycles.json`.
    ///
    /// A missing `cycles.json` is the clean state — the pipeline and
    /// the edit gate write it only on rejection, and a passing edit
    /// gate clears a stale one — so it reports zero blocking SCCs
    /// (`[]`) and exits 0, distinct from a present-but-malformed file
    /// (which errors).
    List(GateListArgs),
    /// Full picture for one blocking SCC: modules, cut, recomputed evidence.
    ///
    /// The per-edge evidence is recomputed on demand from
    /// `dependency_graph.json` and the SCC's module set — the same
    /// per-binding-pair blame view the realizability gate emits to
    /// stderr at rejection time.
    Describe(GateDescribeArgs),
    /// Just the cut edges for one blocking SCC. The actionable subset —
    /// spec authors read this to pick which back-edge to break.
    Cut(GateCutArgs),
    /// Trace one binding's constraining promoted callback edges to a rejected
    /// callback-factory guard when the report contains a supported witness.
    Diagnose(GateDiagnoseArgs),
}

/// Paths for reading blocking SCCs. List/cut need only the cycles report;
/// describe additionally requires the graph to recompute per-edge evidence.
///
/// `cycles.json` defaults to the sibling of `--graph` (the standard
/// per-chunk report layout). Override via `--cycles` if the spec author
/// keeps the two files in non-standard locations.
#[derive(Debug, Clone, ClapArgs)]
pub struct GateCommonArgs {
    /// Path to `dependency_graph.json` (debundler analysis output).
    #[arg(
        long = "graph",
        env = "DEBUNDLE_GRAPH",
        required_unless_present = "cycles_path"
    )]
    pub dependency_graph_path: Option<PathBuf>,

    /// Override the default `cycles.json` location. Defaults to the
    /// sibling of `--graph`.
    #[arg(long = "cycles")]
    pub cycles_path: Option<PathBuf>,
}

impl GateCommonArgs {
    pub fn resolved_cycles_path(&self) -> Result<PathBuf> {
        if let Some(path) = &self.cycles_path {
            return Ok(path.clone());
        }
        let graph = self
            .dependency_graph_path
            .as_deref()
            .context("--graph or --cycles is required")?;
        Ok(graph
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(output_layout::CYCLES_REPORT))
    }
}

#[derive(Debug, ClapArgs)]
pub struct GateListArgs {
    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct GateDescribeArgs {
    /// Blocking-SCC id (zero-based index into `cycles.json`).
    #[arg(requires = "dependency_graph_path")]
    pub id: usize,

    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Restrict the recomputed evidence to edges that touch this
    /// binding (either source or target). Useful for narrowing a
    /// 1000-module SCC down to one symbol's contribution.
    #[arg(long = "binding")]
    pub binding: Option<String>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct GateCutArgs {
    /// Blocking-SCC id (zero-based index into `cycles.json`).
    pub id: usize,

    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

/// A bounded, source-backed trace for one requested binding. Paths are
/// required explicitly so the query and its follow-up command do not depend
/// on ambient Debundle environment variables.
#[derive(Debug, ClapArgs)]
pub struct GateDiagnoseArgs {
    /// Binding whose constraining promoted callback dependencies to inspect.
    pub binding: String,

    /// Path to `dependency_graph.json`.
    #[arg(long, required = true)]
    pub graph: PathBuf,

    /// Root of the YAML module spec tree used by `inspect-source`.
    #[arg(long, required = true)]
    pub modules: PathBuf,

    /// Root passed to the generated `inspect-source` command.
    #[arg(long = "source-root", required = true)]
    pub source_root: PathBuf,

    /// Maximum traces to emit. Defaults to 5; `--all` opts into exhaustive output.
    #[arg(long, conflicts_with = "all")]
    pub limit: Option<usize>,

    /// Emit every matching trace instead of the bounded default.
    #[arg(long, conflicts_with = "limit")]
    pub all: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

/// Root paths that the command used to produce a diagnosis should forward to
/// its generated `inspect-source` command. Consumers that do not run from a
/// relocated bundle environment pass the empty default.
#[derive(Debug, Clone, Default)]
pub struct InspectionRoots {
    pub spec_root: Option<PathBuf>,
    pub graph_source_root: Option<PathBuf>,
    pub graph_package_roots: BTreeMap<String, PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateListEntry {
    pub id: usize,
    pub module_count: usize,
    pub cut_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateListReport {
    pub blocking_sccs: Vec<GateListEntry>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDescribeReport {
    pub id: usize,
    pub modules: Vec<ModulePath>,
    pub cut: Vec<CycleEdge>,
    /// Every constraining cross-module edge inside the SCC,
    /// recomputed from `dependency_graph.json` + `modules` (the wire
    /// entry carries only `modules` + `cut`).
    pub evidence: Vec<CycleEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateCutReport {
    pub id: usize,
    pub cut: Vec<CycleEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseReport {
    pub query_binding: String,
    pub returned_count: usize,
    pub total_count: usize,
    pub truncated: bool,
    pub expansion: GateDiagnoseExpansion,
    pub query_status: GateDiagnoseStatus,
    pub coverage_scope: GateDiagnoseCoverageScope,
    /// Coverage is local to these rows; it does not establish an only or
    /// globally earliest cause.
    pub traces: Vec<GateDiagnoseTrace>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseStatus {
    InconclusiveNoSupportedPromotedDependency,
    LocalSupportedWitnessFound,
    InconclusiveForSupportedGuardFamily,
}

impl GateDiagnoseStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::InconclusiveNoSupportedPromotedDependency => {
                "inconclusive_no_supported_promoted_dependency"
            }
            Self::LocalSupportedWitnessFound => "local_supported_witness_found",
            Self::InconclusiveForSupportedGuardFamily => "inconclusive_for_supported_guard_family",
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseCoverageScope {
    CallbackFactoryNamespaceMemberWriteGuardOnly,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseExpansion {
    Bounded,
    All,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseTrace {
    pub ownership_target: GateDiagnoseOwnershipTarget,
    pub constraining_dependency: GateDiagnoseDependency,
    pub call_site_source_location: Option<analysis::SourceLocation>,
    pub coverage: GateDiagnoseCoverage,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub matched_contract: Option<analysis::CallbackFactoryContractReport>,
    pub first_rejected_guard: GateDiagnoseGuard,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseOwnershipTarget {
    pub binding: String,
    pub source_unit: String,
    pub module: ModulePath,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseDependency {
    pub edge: String,
    pub source_unit: String,
    pub target_source_unit: String,
    pub constrains_init_order: bool,
    pub role: GateDiagnoseEdgeRole,
    pub callee_source_unit: String,
    pub binding: Option<Atom>,
    pub edge_kind: DepKind,
    pub statement_ordinal: SourceUnitOrdinal,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseEdgeRole {
    PromotedAtInit,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseCoverage {
    SupportedNamespaceMemberWrite,
    InconclusiveUnsupportedGuard,
    InconclusiveNoSupportedCallbackFactoryBarrier,
    InconclusiveAmbiguousCallbackFactoryBarrierLink,
    InconclusiveUnresolvedCallbackArgumentSource,
    InconclusiveCallbackArgumentSourceMismatch,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseGuardCode {
    NoSupportedCallbackFactoryBarrier,
    AmbiguousCallbackFactoryBarrierLink,
    CallbackArgumentSourceUnresolved,
    CallbackArgumentSourceMismatch,
    NamespaceMemberWrite,
    UnsupportedGuard,
}

impl GateDiagnoseGuardCode {
    fn as_str(self) -> &'static str {
        match self {
            Self::NoSupportedCallbackFactoryBarrier => "no_supported_callback_factory_barrier",
            Self::AmbiguousCallbackFactoryBarrierLink => "ambiguous_callback_factory_barrier_link",
            Self::CallbackArgumentSourceUnresolved => "callback_argument_source_unresolved",
            Self::CallbackArgumentSourceMismatch => "callback_argument_source_mismatch",
            Self::NamespaceMemberWrite => "namespace_member_write",
            Self::UnsupportedGuard => "unsupported_guard",
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GateDiagnoseWitnessSelection {
    FirstSourceUnitOrderDeterministicWithinUnit,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseGuard {
    pub code: GateDiagnoseGuardCode,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness: Option<GateDiagnoseWitness>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDiagnoseWitness {
    pub source_unit: String,
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<analysis::SourceLocation>,
    pub receiver_binding: Atom,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub property: Option<String>,
    pub witness_selection: GateDiagnoseWitnessSelection,
    pub inspection_command: String,
}

pub fn run_gate_cli(args: GateArgs, inspection_roots: InspectionRoots) -> Result<()> {
    match args.command {
        GateCommand::List(a) => run_list(a),
        GateCommand::Describe(a) => run_describe(a),
        GateCommand::Cut(a) => run_cut(a),
        GateCommand::Diagnose(a) => run_diagnose(a, inspection_roots),
    }
}

fn run_diagnose(args: GateDiagnoseArgs, inspection_roots: InspectionRoots) -> Result<()> {
    let graph = crate::load_dependency_graph_report(&args.graph)?;
    let limit = args.limit.unwrap_or(5);
    if limit == 0 && !args.all {
        anyhow::bail!("--limit must be greater than zero; use --all for exhaustive output");
    }
    let (returned, total_count, has_supported_write) = diagnose_binding(
        &graph,
        &args,
        &inspection_roots,
        (!args.all).then_some(limit),
    )?;
    let report = GateDiagnoseReport {
        query_binding: args.binding,
        returned_count: returned.len(),
        total_count,
        truncated: returned.len() < total_count,
        expansion: if args.all {
            GateDiagnoseExpansion::All
        } else {
            GateDiagnoseExpansion::Bounded
        },
        query_status: if total_count == 0 {
            GateDiagnoseStatus::InconclusiveNoSupportedPromotedDependency
        } else if has_supported_write {
            GateDiagnoseStatus::LocalSupportedWitnessFound
        } else {
            GateDiagnoseStatus::InconclusiveForSupportedGuardFamily
        },
        coverage_scope: GateDiagnoseCoverageScope::CallbackFactoryNamespaceMemberWriteGuardOnly,
        traces: returned,
    };
    let format = peel::OutputFormat::resolve(args.format);
    peel::print_report(&report, format, render_diagnose_text)
        .context("writing gate diagnose output")
}

fn diagnose_binding(
    graph: &DependencyGraphReport,
    args: &GateDiagnoseArgs,
    inspection_roots: &InspectionRoots,
    limit: Option<usize>,
) -> Result<(Vec<GateDiagnoseTrace>, usize, bool)> {
    let node_by_id: HashMap<&str, _> = graph
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect();
    let module_by_key: HashMap<&ModuleKey, _> = graph
        .quotient
        .nodes
        .iter()
        .map(|module| (&module.key, module))
        .collect();
    let target_ids: BTreeSet<&str> = graph
        .nodes
        .iter()
        .filter(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding.as_ref() == args.binding)
        })
        .map(|node| node.id.as_str())
        .collect();
    let mut promoted = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.constrains_init_order
                && target_ids.contains(edge.target.as_str())
                && edge
                    .binding
                    .as_ref()
                    .is_some_and(|b| b.as_ref() == args.binding)
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect::<Vec<_>>();
    promoted.sort_by(|a, b| a.id.cmp(&b.id));

    let mut traces = Vec::new();
    let mut total_count = 0;
    let mut has_supported_write = false;
    for edge in promoted {
        let target = node_by_id
            .get(edge.target.as_str())
            .copied()
            .with_context(|| format!("edge {} has missing target {}", edge.id, edge.target))?;
        let target_module = module_by_key
            .get(&target.destination)
            .copied()
            .with_context(|| {
                format!(
                    "source unit {} refers to missing module {}",
                    target.id, target.destination
                )
            })?
            .path
            .clone();
        let call_site = node_by_id
            .get(edge.source.as_str())
            .copied()
            .with_context(|| format!("edge {} has missing source {}", edge.id, edge.source))?;
        let Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) = edge.role.as_ref() else {
            unreachable!("filtered promoted role")
        };

        if call_site.callback_factory_barriers.is_empty() {
            let trace = GateDiagnoseTrace {
                ownership_target: GateDiagnoseOwnershipTarget {
                    binding: args.binding.clone(),
                    source_unit: target.id.clone(),
                    module: target_module,
                },
                constraining_dependency: dependency_report(edge, callee_source_unit),
                call_site_source_location: call_site.source_location.clone(),
                coverage: GateDiagnoseCoverage::InconclusiveNoSupportedCallbackFactoryBarrier,
                matched_contract: None,
                first_rejected_guard: GateDiagnoseGuard {
                    code: GateDiagnoseGuardCode::NoSupportedCallbackFactoryBarrier,
                    witness: None,
                },
            };
            total_count += 1;
            if limit.is_none_or(|limit| traces.len() < limit) {
                traces.push(trace);
            }
            continue;
        }

        if call_site.callback_factory_barriers.len() != 1 {
            total_count += 1;
            if limit.is_none_or(|limit| traces.len() < limit) {
                traces.push(GateDiagnoseTrace {
                    ownership_target: GateDiagnoseOwnershipTarget {
                        binding: args.binding.clone(),
                        source_unit: target.id.clone(),
                        module: target_module,
                    },
                    constraining_dependency: dependency_report(edge, callee_source_unit),
                    call_site_source_location: call_site.source_location.clone(),
                    coverage: GateDiagnoseCoverage::InconclusiveAmbiguousCallbackFactoryBarrierLink,
                    matched_contract: None,
                    first_rejected_guard: GateDiagnoseGuard {
                        code: GateDiagnoseGuardCode::AmbiguousCallbackFactoryBarrierLink,
                        witness: None,
                    },
                });
            }
            continue;
        }

        let barrier = &call_site.callback_factory_barriers[0];
        total_count += 1;
        let callback_link =
            barrier
                .contract
                .callback_source_units
                .as_ref()
                .map(|callback_source_units| {
                    callback_source_units
                        .iter()
                        .any(|source| source.source_unit == *callee_source_unit)
                });
        has_supported_write |= callback_link == Some(true)
            && matches!(
                &barrier.rejection,
                CallbackFactoryRejectionReport::NamespaceMemberWrite { .. }
            );

        // Summary status describes all matching promoted edges, including rows
        // omitted from the returned page. Keep expensive witness materialization
        // and the trace list itself bounded by the requested output limit.
        if limit.is_some_and(|limit| traces.len() >= limit) {
            continue;
        }
        if callback_link.is_none() {
            traces.push(GateDiagnoseTrace {
                ownership_target: GateDiagnoseOwnershipTarget {
                    binding: args.binding.clone(),
                    source_unit: target.id.clone(),
                    module: target_module,
                },
                constraining_dependency: dependency_report(edge, callee_source_unit),
                call_site_source_location: call_site.source_location.clone(),
                coverage: GateDiagnoseCoverage::InconclusiveUnresolvedCallbackArgumentSource,
                matched_contract: None,
                first_rejected_guard: GateDiagnoseGuard {
                    code: GateDiagnoseGuardCode::CallbackArgumentSourceUnresolved,
                    witness: None,
                },
            });
            continue;
        }
        if callback_link == Some(false) {
            traces.push(GateDiagnoseTrace {
                ownership_target: GateDiagnoseOwnershipTarget {
                    binding: args.binding.clone(),
                    source_unit: target.id.clone(),
                    module: target_module,
                },
                constraining_dependency: dependency_report(edge, callee_source_unit),
                call_site_source_location: call_site.source_location.clone(),
                coverage: GateDiagnoseCoverage::InconclusiveCallbackArgumentSourceMismatch,
                matched_contract: None,
                first_rejected_guard: GateDiagnoseGuard {
                    code: GateDiagnoseGuardCode::CallbackArgumentSourceMismatch,
                    witness: None,
                },
            });
            continue;
        }
        let (coverage, code, witness) = diagnose_barrier_witness(barrier, args, inspection_roots)?;
        traces.push(GateDiagnoseTrace {
            ownership_target: GateDiagnoseOwnershipTarget {
                binding: args.binding.clone(),
                source_unit: target.id.clone(),
                module: target_module,
            },
            constraining_dependency: dependency_report(edge, callee_source_unit),
            call_site_source_location: call_site.source_location.clone(),
            coverage,
            matched_contract: Some(barrier.contract.clone()),
            first_rejected_guard: GateDiagnoseGuard { code, witness },
        });
    }
    Ok((traces, total_count, has_supported_write))
}

fn dependency_report(
    edge: &analysis::DependencyEdgeReport,
    callee_source_unit: &str,
) -> GateDiagnoseDependency {
    GateDiagnoseDependency {
        edge: edge.id.clone(),
        source_unit: edge.source.clone(),
        target_source_unit: edge.target.clone(),
        constrains_init_order: edge.constrains_init_order,
        role: GateDiagnoseEdgeRole::PromotedAtInit,
        callee_source_unit: callee_source_unit.to_string(),
        binding: edge.binding.clone(),
        edge_kind: edge.edge_kind,
        statement_ordinal: edge.statement_ordinal,
    }
}

fn diagnose_barrier_witness(
    barrier: &CallbackFactoryBarrierReport,
    args: &GateDiagnoseArgs,
    inspection_roots: &InspectionRoots,
) -> Result<(
    GateDiagnoseCoverage,
    GateDiagnoseGuardCode,
    Option<GateDiagnoseWitness>,
)> {
    match &barrier.rejection {
        CallbackFactoryRejectionReport::NamespaceMemberWrite { witness } => Ok((
            GateDiagnoseCoverage::SupportedNamespaceMemberWrite,
            GateDiagnoseGuardCode::NamespaceMemberWrite,
            Some(GateDiagnoseWitness {
                source_unit: witness.source_unit.clone(),
                statement_ordinal: witness.statement_ordinal,
                source_location: witness.source_location.clone(),
                receiver_binding: witness.receiver_binding.clone(),
                property: witness.property.clone(),
                witness_selection:
                    GateDiagnoseWitnessSelection::FirstSourceUnitOrderDeterministicWithinUnit,
                inspection_command: inspection_command(
                    &witness.source_unit,
                    &args.graph,
                    &args.modules,
                    &args.source_root,
                    inspection_roots,
                )?,
            }),
        )),
        CallbackFactoryRejectionReport::UnsupportedGuard => Ok((
            GateDiagnoseCoverage::InconclusiveUnsupportedGuard,
            GateDiagnoseGuardCode::UnsupportedGuard,
            None,
        )),
    }
}

fn inspection_command(
    source_unit: &str,
    graph: &Path,
    modules: &Path,
    source_root: &Path,
    inspection_roots: &InspectionRoots,
) -> Result<String> {
    let graph = std::fs::canonicalize(graph).context("resolving --graph for inspection command")?;
    let modules =
        std::fs::canonicalize(modules).context("resolving --modules for inspection command")?;
    let source_root = std::fs::canonicalize(source_root)
        .context("resolving --source-root for inspection command")?;
    let executable = std::env::current_exe()
        .context("resolving running debundle executable for inspection command")?;
    Ok(format!(
        "{}{}inspect-source --id {} --graph {} --modules {} --source-root {} --format json",
        shell_quote(&executable.display().to_string()),
        inspection_root_args(inspection_roots),
        shell_quote(source_unit),
        shell_quote(&graph.display().to_string()),
        shell_quote(&modules.display().to_string()),
        shell_quote(&source_root.display().to_string()),
    ))
}

fn inspection_root_args(roots: &InspectionRoots) -> String {
    let mut args = Vec::new();
    if let Some(spec_root) = &roots.spec_root {
        args.push(format!(
            "--spec-root {}",
            shell_quote(&spec_root.display().to_string())
        ));
    }
    if let Some(graph_source_root) = &roots.graph_source_root {
        args.push(format!(
            "--graph-source-root {}",
            shell_quote(&graph_source_root.display().to_string())
        ));
    }
    for (package, path) in &roots.graph_package_roots {
        args.push(format!(
            "--graph-package-root {}",
            shell_quote(&format!("{}={}", package, path.display()))
        ));
    }
    if args.is_empty() {
        " ".to_owned()
    } else {
        format!(" {} ", args.join(" "))
    }
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod inspection_command_tests {
    use super::{InspectionRoots, inspection_root_args};
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    #[test]
    fn inspection_roots_are_shell_quoted_with_spaces_and_sorted_package_roots() {
        let roots = InspectionRoots {
            spec_root: Some(PathBuf::from("spec root")),
            graph_source_root: Some(PathBuf::from("graph source root")),
            graph_package_roots: BTreeMap::from([
                ("package one".to_owned(), PathBuf::from("package root one")),
                ("package two".to_owned(), PathBuf::from("package root two")),
            ]),
        };

        assert_eq!(
            inspection_root_args(&roots),
            concat!(
                " --spec-root 'spec root' --graph-source-root 'graph source root' ",
                "--graph-package-root 'package one=package root one' ",
                "--graph-package-root 'package two=package root two' ",
            )
        );
        assert_eq!(inspection_root_args(&InspectionRoots::default()), " ");
    }
}

fn render_diagnose_text(report: &GateDiagnoseReport, out: &mut String) {
    out.push_str(&format!(
        "binding {}: returned {} of {} trace(s){}\n",
        report.query_binding,
        report.returned_count,
        report.total_count,
        if report.truncated { " (truncated)" } else { "" },
    ));
    out.push_str(&format!("  status: {}\n", report.query_status.as_str()));
    for trace in &report.traces {
        out.push_str(&format!(
            "  {} in {} <- {} via {} [{}]\n",
            trace.ownership_target.binding,
            trace.ownership_target.module,
            trace.constraining_dependency.source_unit,
            trace.constraining_dependency.callee_source_unit,
            trace.first_rejected_guard.code.as_str(),
        ));
        if let Some(contract) = &trace.matched_contract {
            out.push_str(&format!(
                "    contract {}.{}({} args; callbacks {:?})\n",
                contract.namespace_binding,
                contract.member,
                contract.argument_count,
                contract.callback_arguments,
            ));
        }
        if let Some(witness) = &trace.first_rejected_guard.witness {
            out.push_str(&format!(
                "    write {}.{} at {:?}; inspect: {}\n",
                witness.receiver_binding,
                witness.property.as_deref().unwrap_or("<unknown>"),
                witness.source_location,
                witness.inspection_command,
            ));
        }
    }
}

fn load_cycles(common: &GateCommonArgs) -> Result<Vec<BlockingSccEntry>> {
    let path = common.resolved_cycles_path()?;
    // The pipeline writes `cycles.json` only when the gate rejects, so
    // its absence is the clean state: zero blocking SCCs. Returning an
    // empty list (rather than a read error) makes `gate list` on a
    // realizable spec print `[]` / "0 blocking SCC(s)" and exit 0,
    // distinguishable from a present-but-malformed file (which still
    // errors below). `gate describe` / `gate cut` then report
    // "no blocking SCC with id N (found 0 entries)" for any id.
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text =
        std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text)
        .with_context(|| format!("parsing blocking-SCC report {}", path.display()))
}

fn run_list(args: GateListArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let report = GateListReport {
        blocking_sccs: entries
            .iter()
            .map(|e| GateListEntry {
                id: e.id,
                module_count: e.modules.len(),
                cut_count: e.cut.len(),
            })
            .collect(),
    };
    let format = peel::OutputFormat::resolve(args.format);
    if peel::print_ndjson_list(&report.blocking_sccs, format)? {
        return Ok(());
    }
    peel::print_report(&report, format, render_list_text).context("writing gate list output")
}

fn render_list_text(report: &GateListReport, out: &mut String) {
    out.push_str(&format!("{} blocking SCC(s)\n", report.blocking_sccs.len()));
    for entry in &report.blocking_sccs {
        out.push_str(&format!(
            "  {}  modules={}  cut={}\n",
            entry.id, entry.module_count, entry.cut_count,
        ));
    }
}

fn find_entry(entries: &[BlockingSccEntry], id: usize) -> Result<&BlockingSccEntry> {
    entries.iter().find(|e| e.id == id).ok_or_else(|| {
        anyhow::anyhow!(
            "no blocking SCC with id {id} in cycles.json (found {} entries)",
            entries.len(),
        )
    })
}

fn run_describe(args: GateDescribeArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let entry = find_entry(&entries, args.id)?;
    let graph = crate::load_dependency_graph_report(
        args.common
            .dependency_graph_path
            .as_deref()
            .context("gate describe requires --graph")?,
    )?;

    let mut evidence = recompute_evidence(&graph, &entry.modules)?;
    if let Some(binding) = &args.binding {
        evidence.retain(|e| edge_touches_binding(e, binding));
    }

    let report = GateDescribeReport {
        id: entry.id,
        modules: entry.modules.clone(),
        cut: entry.cut.clone(),
        evidence,
    };
    let format = peel::OutputFormat::resolve(args.format);
    peel::print_report(&report, format, render_describe_text)
        .context("writing gate describe output")
}

fn render_describe_text(report: &GateDescribeReport, out: &mut String) {
    out.push_str(&format!(
        "blocking SCC #{}: {} module(s), cut of {}, {} evidence edge(s).\n",
        report.id,
        report.modules.len(),
        report.cut.len(),
        report.evidence.len(),
    ));

    if !report.modules.is_empty() {
        out.push_str("  modules:\n");
        for m in &report.modules {
            out.push_str(&format!("    {m}\n"));
        }
    }

    if !report.cut.is_empty() {
        out.push_str("  cut (actionable; break any of these by co-locating the binding pair):\n");
        for edge in &report.cut {
            render_edge(edge, out);
        }
    }

    if !report.evidence.is_empty() {
        // Group evidence by binding-pair blame key, same shape as
        // `render_cycle_summary`'s stderr block. Anonymous endpoints
        // fall back to `<anon stmt #ord>` / `<side-effect>` so every
        // row has a stable label.
        let mut groups: BTreeMap<BlamePairKey, BlamePairAgg> = BTreeMap::new();
        for edge in &report.evidence {
            let key = BlamePairKey::of(edge);
            let agg = groups.entry(key).or_insert(BlamePairAgg {
                kind: edge.kind,
                count: 0,
            });
            agg.count += 1;
        }
        let mut ranked: Vec<(BlamePairKey, BlamePairAgg)> = groups.into_iter().collect();
        ranked.sort_by(|a, b| b.1.count.cmp(&a.1.count).then(a.0.cmp(&b.0)));

        out.push_str("  evidence (grouped by binding pair):\n");
        for (key, agg) in &ranked {
            out.push_str(&format!(
                "    {n:>4}x  {fb} ({fm})  --{k}-->  {tb} ({tm})\n",
                n = agg.count,
                fb = key.from_label,
                fm = key.from,
                k = agg.kind.diagnostic_label(),
                tb = key.to_label,
                tm = key.to,
            ));
        }
    }
}

fn run_cut(args: GateCutArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let entry = find_entry(&entries, args.id)?;
    let report = GateCutReport {
        id: entry.id,
        cut: entry.cut.clone(),
    };
    let format = peel::OutputFormat::resolve(args.format);
    if peel::print_ndjson_list(&report.cut, format)? {
        return Ok(());
    }
    peel::print_report(&report, format, render_cut_text).context("writing gate cut output")
}

fn render_cut_text(report: &GateCutReport, out: &mut String) {
    out.push_str(&format!(
        "blocking SCC #{}: cut of {}\n",
        report.id,
        report.cut.len(),
    ));
    for edge in &report.cut {
        render_edge(edge, out);
    }
}

fn render_edge(edge: &CycleEdge, out: &mut String) {
    let from_b = match &edge.from_binding {
        Some(a) => a.as_ref().to_string(),
        None => format!("<anon stmt #{}>", edge.statement_ordinal.0),
    };
    let to_b = match &edge.binding {
        Some(a) => a.as_ref().to_string(),
        None => "<side-effect>".to_string(),
    };
    out.push_str(&format!(
        "    {from_b} ({fm})  --{k}-->  {to_b} ({tm})  [stmt #{ord}]\n",
        fm = edge.from,
        k = edge.kind.diagnostic_label(),
        tm = edge.to,
        ord = edge.statement_ordinal.0,
    ));
    if let Some(cause) = &edge.sequenced_source_unit
        && let Purity::NotPure { reasons } = &cause.purity
    {
        for reason in reasons {
            let binding = if cause.binding_names.is_empty() {
                cause.source_unit_id.clone()
            } else {
                cause
                    .binding_names
                    .iter()
                    .map(Atom::as_ref)
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let location = reason
                .source_location
                .as_ref()
                .or(cause.source_location.as_ref())
                .map(|loc| {
                    format!(
                        "{}:{}:{}",
                        loc.source_path,
                        loc.start_line,
                        loc.start_column
                            .map_or_else(|| "?".to_string(), |column| column.to_string())
                    )
                })
                .unwrap_or_else(|| "<location unavailable>".to_string());
            out.push_str(&format!(
                "      impure initializer `{binding}` at {location}: {}{}\n",
                reason.rule.as_str(),
                reason
                    .detail
                    .as_deref()
                    .map(|detail| format!(" ({detail})"))
                    .unwrap_or_default()
            ));
            if let Some(guidance) = &reason.author_guidance {
                out.push_str(&format!("        {guidance}\n"));
            }
        }
    }
}

fn edge_touches_binding(edge: &CycleEdge, binding: &str) -> bool {
    edge.binding.as_ref().map(|a| a.as_ref()) == Some(binding)
        || edge.from_binding.as_ref().map(|a| a.as_ref()) == Some(binding)
}

/// Reconstruct diagnostic context for an SCC from `dependency_graph.json`.
/// Unlike gate validation, this includes lazy edges as well as constraining
/// edges. Preserve the report projection: omit intra-module/outside-SCC edges
/// and promoted edges whose callee is in another module; dedup sequencing by
/// module pair. This is diagnostic context, not a lossless reconstruction of
/// gate provenance; do not replace it with the gate's constraining-edge set.
///
/// Source labels prefer the first declared binding at the edge's statement
/// ordinal, falling back to the source source unit's first binding. Anonymous
/// statements retain `None` and render as `<anon stmt #N>`.
///
/// `cycles.json` uses canonical [`ModulePath`]s; resolve each source unit's interned
/// destination [`ModuleKey`] through the report's module table. A missing table
/// entry is malformed and errors; edges with absent source units are skipped.
fn recompute_evidence(
    graph: &DependencyGraphReport,
    modules: &[ModulePath],
) -> Result<Vec<CycleEdge>> {
    // Module table: interned key -> canonical path.
    let path_by_key: HashMap<&ModuleKey, &ModulePath> = graph
        .quotient
        .nodes
        .iter()
        .map(|entry| (&entry.key, &entry.path))
        .collect();
    // SourceUnit id -> report node and canonical destination. Reuse this index for
    // source labels and sequencing causes instead of scanning nodes per edge.
    let source_units: HashMap<&str, _> = graph
        .nodes
        .iter()
        .map(|n| {
            let path = path_by_key.get(&n.destination).copied().ok_or_else(|| {
                anyhow::anyhow!(
                    "source unit {} has destination {} with no module-table entry",
                    n.id,
                    n.destination
                )
            })?;
            Ok((n.id.as_str(), (n, path)))
        })
        .collect::<Result<_>>()?;
    // Statement ordinal -> first declared binding of any source unit
    // declaring at that ordinal. The materializer indexes by ordinal
    // (not source unit) when labeling the source side; we match that here.
    let from_binding_by_ordinal: HashMap<SourceUnitOrdinal, Atom> = graph
        .nodes
        .iter()
        .filter_map(|n| {
            n.declared_bindings
                .first()
                .map(|b| (n.statement_ordinal, b.binding.clone()))
        })
        .collect();

    let scc_modules: BTreeSet<&ModulePath> = modules.iter().collect();

    let mut out = Vec::new();
    let mut seen_sequenced_pairs: BTreeSet<(&ModulePath, &ModulePath)> = BTreeSet::new();
    for edge in &graph.edges {
        let Some(&(source, from_mod)) = source_units.get(edge.source.as_str()) else {
            continue;
        };
        let Some(&(_, to_mod)) = source_units.get(edge.target.as_str()) else {
            continue;
        };
        if from_mod == to_mod {
            // Intra-module edges cannot explain a module-quotient cycle.
            continue;
        }
        if !scc_modules.contains(from_mod) || !scc_modules.contains(to_mod) {
            continue;
        }
        // Preserve the diagnostic projection's lenient promotion policy:
        // cross-module callees are represented by the direct caller->callee
        // edge, not the manufactured caller->target edge.
        if let Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) = &edge.role
            && let Some(&(_, callee_mod)) = source_units.get(callee_source_unit.as_str())
            && callee_mod != from_mod
        {
            continue;
        }
        if matches!(edge.edge_kind, DepKind::Sequenced) {
            // Mirror `chunk_constraining_module_edges`'s sequenced-edge
            // dedup: collapse parallel sequenced edges between the same
            // module pair into one evidence row.
            if !seen_sequenced_pairs.insert((from_mod, to_mod)) {
                continue;
            }
        }
        let from_binding = from_binding_by_ordinal
            .get(&edge.statement_ordinal)
            .cloned()
            .or_else(|| source.declared_bindings.first().map(|b| b.binding.clone()));
        out.push(CycleEdge {
            from: from_mod.clone(),
            to: to_mod.clone(),
            statement_ordinal: edge.statement_ordinal,
            binding: edge.binding.clone(),
            from_binding,
            kind: edge.edge_kind,
            sequenced_source_unit: (edge.edge_kind == DepKind::Sequenced
                && matches!(&source.purity, Purity::NotPure { .. }))
            .then(|| SequencedSourceUnitCause {
                source_unit_id: source.id.clone(),
                binding_names: source
                    .declared_bindings
                    .iter()
                    .map(|binding| binding.binding.clone())
                    .collect(),
                source_location: source.source_location.clone(),
                purity: source.purity.clone(),
            }),
        });
    }
    CycleEdge::sort_for_report(&mut out);
    Ok(out)
}

#[derive(Debug, Clone, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct BlamePairKey {
    from_label: String,
    from: ModulePath,
    to: ModulePath,
    to_label: String,
}

impl BlamePairKey {
    fn of(edge: &CycleEdge) -> Self {
        let from_label = match &edge.from_binding {
            Some(a) => a.as_ref().to_string(),
            None => format!("<anon stmt #{}>", edge.statement_ordinal.0),
        };
        let to_label = match &edge.binding {
            Some(a) => a.as_ref().to_string(),
            None => "<side-effect>".to_string(),
        };
        Self {
            from_label,
            from: edge.from.clone(),
            to: edge.to.clone(),
            to_label,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct BlamePairAgg {
    kind: DepKind,
    count: usize,
}
