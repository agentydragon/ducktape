//! `debundle explain-integrity`: which census witnesses leave a native
//! integrity slot unknown, from the saved graph and its program graph.

use crate::emit_report;
use analysis::ProgramGraphReport;
use analysis::analysis_inputs::NativeIntrinsic;
use analysis::facts::abstract_domain::integrity::{CensusExplanation, SlotName, SlotReach};
use analysis::facts::standard_globals::is_standard_global;
use analysis::reports::schema::{
    BindingReport, ExecutionPrecisionLoss, ExecutionPrecisionLossReason, NativeIntegrityCheck,
    NativeIntegrityGuard, NativeMutationOperation, NativeMutationWitness,
    NativePrototypeMutationKind, NativeWriteSlot, SourceLocation,
};
use anyhow::{Context, Result, bail};
use clap::{ArgGroup, Args as ClapArgs};
use graph_provenance::{CurrentRoots, GraphProvenance};
use peel::OutputFormat;
use peel::plan::LimitSectionReport;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// Args for `debundle explain-integrity`.
#[derive(Debug, ClapArgs)]
#[command(group(ArgGroup::new("query").required(true).args(["slot", "at", "id"])))]
pub(super) struct ExplainIntegrityArgs {
    /// Saved `dependency_graph.json` (or DEBUNDLE_GRAPH); the census is read
    /// from the `program_graph.json` it references.
    #[arg(long, env = "DEBUNDLE_GRAPH")]
    graph: PathBuf,

    /// Every census witness reaching this slot: `%Array.prototype%`,
    /// `Object.prototype`, `Array.prototype.map`, `Array[@@species]`,
    /// `Object.defineProperty`, `Set`, `ArrayIteratorPrototype`.
    #[arg(long, value_parser = parse_slot)]
    slot: Option<SlotName>,

    /// Precision losses located on this line and the source units whose
    /// execution recorded them, or else the source units spanning it.
    #[arg(long, value_name = "PATH:LINE")]
    at: Option<String>,

    /// A source unit (`unit:N`) or a binding it declares.
    #[arg(long)]
    id: Option<String>,

    /// Witnesses shown per slot (default 10); counts stay complete.
    #[arg(
        long,
        default_value = "10",
        default_value_if("all", "true", None),
        conflicts_with = "all"
    )]
    limit: Option<usize>,

    /// Show every witness. Conflicts with --limit.
    #[arg(long)]
    all: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub(super) format: Option<OutputFormat>,
}

fn parse_slot(text: &str) -> Result<SlotName, String> {
    text.parse()
}

/// The chunk-graph fields this command reads; the rest is skipped unparsed.
#[derive(Deserialize)]
struct GraphView {
    chunk_id: String,
    program_graph: PathBuf,
    nodes: Vec<NodeView>,
}

#[derive(Deserialize)]
struct NodeView {
    id: String,
    source_location: Option<SourceLocation>,
    declared_bindings: Vec<BindingReport>,
    synchronous_execution: Option<ExecutionView>,
}

#[derive(Deserialize)]
struct ExecutionView {
    #[serde(default)]
    native_integrity_checks: Vec<NativeIntegrityCheck>,
    #[serde(default)]
    native_prefix_observations: Vec<PrefixObservationView>,
    complete: bool,
    precision_losses: Vec<ExecutionPrecisionLoss>,
}

#[derive(Deserialize)]
struct PrefixObservationView {
    guard: NativeIntegrityGuard,
}

#[derive(Debug, Serialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
enum Query {
    Slot(String),
    Location(String),
    SourceUnit(String),
}

#[derive(Debug, Serialize)]
struct IntegrityExplanationReport {
    query: Query,
    #[serde(skip_serializing_if = "Option::is_none")]
    chunk_id: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    source_units: Vec<SourceUnitIntegrity>,
    /// A direct `eval` or `with` leaves the census open: every slot is unknown.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    dynamic_scope: Vec<Located>,
    slots: Vec<SlotIntegrity>,
}

#[derive(Debug, Serialize)]
struct SourceUnitIntegrity {
    id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_location: Option<SourceLocation>,
    complete: bool,
    /// For `--at`, only the losses on that line.
    precision_losses: Vec<Loss>,
    /// Every guard this unit's execution evaluated, failed or not.
    evaluated_guards: BTreeSet<NativeIntegrityGuard>,
    failed_guards: Vec<FailedGuard>,
}

#[derive(Debug, Serialize)]
struct Loss {
    reason: ExecutionPrecisionLossReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    excerpt: Option<String>,
}

#[derive(Debug, Serialize)]
struct FailedGuard {
    guard: NativeIntegrityGuard,
    affected_roots: BTreeSet<NativeIntrinsic>,
    slots: Vec<GuardSlot>,
}

#[derive(Debug, Serialize)]
struct GuardSlot {
    slot: String,
    intact: bool,
}

#[derive(Debug, Serialize)]
struct SlotIntegrity {
    slot: String,
    intact: bool,
    affected_roots: BTreeSet<NativeIntrinsic>,
    reach_counts: BTreeMap<SlotReach, usize>,
    witnesses: Vec<WitnessExplanation>,
    witness_limit: LimitSectionReport,
}

#[derive(Debug, Serialize)]
struct Located {
    chunk_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    excerpt: Option<String>,
}

#[derive(Debug, Serialize)]
struct WitnessExplanation {
    #[serde(flatten)]
    site: Located,
    reach: SlotReach,
    operation: NativeMutationOperation,
    receiver_roots: BTreeSet<NativeIntrinsic>,
    #[serde(skip_serializing_if = "Option::is_none")]
    receiver_global: Option<String>,
    target_slots: BTreeSet<NativeWriteSlot>,
    /// Author premises or host keys that could remove this witness from the
    /// slot's census; empty for a write the census sees naming the slot or
    /// its family.
    discharged_by: Vec<Discharge>,
}

/// A premise or host key that removes an unknown-receiver witness.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
enum Discharge {
    /// `host_behavior.predefined_globals: standard`, under which the value of
    /// a bundle-owned global property reaches no intrinsic slot (SPEC.md §
    /// Bundle-owned globals).
    PredefinedGlobalsBundleOwnership,
    /// `chunk_analysis_options.<chunk>.no_accessor_install_callables`, when a
    /// computed-key method call is what keeps that property from being
    /// bundle-owned.
    NoAccessorInstallCallables,
    /// `chunk_analysis_options.<chunk>.value_inputs`: the receiver is a
    /// captured ordinary object (SPEC.md § Captured ordinary-object values).
    CapturedValueInput,
}

/// Premises that could remove an unknown-receiver witness. A property the
/// host predefines is never bundle-owned. A program-wide premise that no
/// analyzed write reaches an intrinsic belongs here once the spec has one.
fn discharges(witness: &NativeMutationWitness, reach: SlotReach) -> Vec<Discharge> {
    if reach != SlotReach::UnknownReceiver {
        return Vec::new();
    }
    match &witness.receiver_global {
        Some(property) if is_standard_global(property) => Vec::new(),
        Some(_) => vec![
            Discharge::PredefinedGlobalsBundleOwnership,
            Discharge::NoAccessorInstallCallables,
        ],
        None if witness.through_local_alias
            && witness
                .receiver_roots
                .contains(&NativeIntrinsic::UnknownGlobalValue) =>
        {
            vec![Discharge::CapturedValueInput]
        }
        None => Vec::new(),
    }
}

pub(super) fn run(args: ExplainIntegrityArgs, roots: CurrentRoots) -> Result<()> {
    let limit = args.limit.filter(|_| !args.all);
    let report = explain(&args, limit, &roots)?;
    emit_report(
        args.format,
        &report,
        render_text,
        "writing explain-integrity output",
    )
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_slice(&bytes).with_context(|| format!("parsing {}", path.display()))
}

fn explain(
    args: &ExplainIntegrityArgs,
    limit: Option<usize>,
    roots: &CurrentRoots,
) -> Result<IntegrityExplanationReport> {
    let (chunk_id, program_graph_path, selected) = match &args.slot {
        Some(_) => {
            let header = graph_provenance::read_graph_header(&args.graph)?;
            (None, header.program_graph, Vec::new())
        }
        None => {
            let graph: GraphView = read_json(&args.graph)?;
            let selected = select_source_units(args, graph.nodes)?;
            (Some(graph.chunk_id), graph.program_graph, selected)
        }
    };
    let program: ProgramGraphReport = read_json(&graph_provenance::program_graph_path(
        &args.graph,
        &program_graph_path,
    ))?;
    let mut sources = Sources::new(program.provenance.as_ref(), roots);
    let dynamic_scope: Vec<_> = program
        .native_prototype_mutations
        .iter()
        .filter(|mutation| mutation.kind == NativePrototypeMutationKind::DynamicScope)
        .map(|mutation| sources.located(&mutation.chunk_id, mutation.source_location.as_ref()))
        .collect();
    let census = CensusExplanation::new(
        &program.native_integrity_witnesses,
        !dynamic_scope.is_empty(),
    );

    // Report order: the guards' slots as they fail, then a queried slot.
    let mut slot_names = Vec::new();
    let source_units = selected
        .into_iter()
        .map(|(node, losses)| {
            let execution = node.synchronous_execution.as_ref();
            let failed_guards = execution
                .map(|execution| &execution.native_integrity_checks[..])
                .unwrap_or_default()
                .iter()
                .map(|check| {
                    let slots = check
                        .slots
                        .iter()
                        .map(|slot| {
                            let name: SlotName = slot.parse().map_err(anyhow::Error::msg)?;
                            let intact = census.explain(&name).intact;
                            if !intact && !slot_names.contains(&name) {
                                slot_names.push(name);
                            }
                            Ok(GuardSlot {
                                slot: slot.clone(),
                                intact,
                            })
                        })
                        .collect::<Result<_>>()?;
                    Ok(FailedGuard {
                        guard: check.guard,
                        affected_roots: check.affected_roots.clone(),
                        slots,
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            let losses = losses
                .into_iter()
                .map(|loss| Loss {
                    excerpt: loss
                        .source_location
                        .as_ref()
                        .and_then(|location| sources.excerpt_at_path(location)),
                    reason: loss.reason,
                    source_location: loss.source_location,
                })
                .collect();
            Ok(SourceUnitIntegrity {
                id: node.id,
                source_location: node.source_location,
                complete: execution.is_none_or(|execution| execution.complete),
                precision_losses: losses,
                evaluated_guards: execution
                    .into_iter()
                    .flat_map(|execution| {
                        execution
                            .native_prefix_observations
                            .iter()
                            .map(|observation| observation.guard)
                            .chain(
                                execution
                                    .native_integrity_checks
                                    .iter()
                                    .map(|check| check.guard),
                            )
                    })
                    .collect(),
                failed_guards,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let query = match (&args.slot, &args.at, &args.id) {
        (Some(slot), _, _) => {
            slot_names.push(slot.clone());
            Query::Slot(slot.to_string())
        }
        (_, Some(at), _) => Query::Location(at.clone()),
        (_, _, Some(id)) => Query::SourceUnit(id.clone()),
        _ => unreachable!("clap requires one query"),
    };
    let slots = slot_names
        .iter()
        .map(|slot| {
            let explanation = census.explain(slot);
            let mut reach_counts = BTreeMap::new();
            for (_, reach) in &explanation.reaches {
                *reach_counts.entry(*reach).or_default() += 1;
            }
            let mut reaches = explanation.reaches;
            // Named writes first: they are the source edits that would have
            // to change; unknown receivers are the widest and least specific.
            reaches.sort_by_key(|(index, reach)| (*reach, *index));
            let total = reaches.len();
            reaches.truncate(limit.unwrap_or(total));
            let witnesses = reaches
                .into_iter()
                .map(|(index, reach)| {
                    let witness = &program.native_integrity_witnesses[index];
                    WitnessExplanation {
                        site: sources.located(&witness.chunk_id, witness.source_location.as_ref()),
                        reach,
                        operation: witness.operation,
                        receiver_roots: witness.receiver_roots.clone(),
                        receiver_global: witness.receiver_global.clone(),
                        target_slots: witness.target_slots.clone(),
                        discharged_by: discharges(witness, reach),
                    }
                })
                .collect::<Vec<_>>();
            SlotIntegrity {
                slot: slot.to_string(),
                intact: explanation.intact,
                affected_roots: explanation.affected_roots,
                reach_counts,
                witness_limit: LimitSectionReport {
                    total,
                    emitted: witnesses.len(),
                    truncated: witnesses.len() < total,
                },
                witnesses,
            }
        })
        .collect();
    Ok(IntegrityExplanationReport {
        query,
        chunk_id,
        source_units,
        dynamic_scope,
        slots,
    })
}

/// The selected source units, each with the precision losses to show.
fn select_source_units(
    args: &ExplainIntegrityArgs,
    nodes: Vec<NodeView>,
) -> Result<Vec<(NodeView, Vec<ExecutionPrecisionLoss>)>> {
    let all_losses = |node: &NodeView| {
        node.synchronous_execution
            .as_ref()
            .map(|execution| execution.precision_losses.clone())
            .unwrap_or_default()
    };
    if let Some(id) = &args.id {
        let selected: Vec<_> = nodes
            .into_iter()
            .filter(|node| {
                node.id == *id
                    || node
                        .declared_bindings
                        .iter()
                        .any(|binding| binding.binding == *id || binding.export_name == *id)
            })
            .map(|node| {
                let losses = all_losses(&node);
                (node, losses)
            })
            .collect();
        if selected.is_empty() {
            bail!(
                "no source unit {id:?} or declaring binding {id:?} in {}",
                args.graph.display()
            );
        }
        return Ok(selected);
    }
    let at = args.at.as_deref().expect("clap requires one query");
    let (path, line) = at
        .rsplit_once(':')
        .and_then(|(path, line)| Some((path, line.parse::<usize>().ok()?)))
        .with_context(|| format!("--at {at:?} is not PATH:LINE"))?;
    let on_line = |location: &SourceLocation| {
        (location.source_path == path || location.source_path.ends_with(&format!("/{path}")))
            && (location.start_line..=location.end_line).contains(&line)
    };
    let mut by_loss = Vec::new();
    let mut spanning = Vec::new();
    for node in nodes {
        let losses: Vec<_> = all_losses(&node)
            .into_iter()
            .filter(|loss| {
                loss.source_location
                    .as_ref()
                    .is_some_and(|location| location.start_line == line && on_line(location))
            })
            .collect();
        if !losses.is_empty() {
            by_loss.push((node, losses));
        } else if node.source_location.as_ref().is_some_and(on_line) {
            spanning.push((node, Vec::new()));
        }
    }
    let selected = if by_loss.is_empty() {
        spanning
    } else {
        by_loss
    };
    if selected.is_empty() {
        bail!(
            "no precision loss or source unit at {at} in {}",
            args.graph.display()
        );
    }
    Ok(selected)
}

/// Source bytes for excerpts, read once per chunk from the graph's recorded
/// inputs. A relocated or changed input yields no excerpt.
struct Sources<'a> {
    provenance: Option<&'a GraphProvenance>,
    roots: &'a CurrentRoots,
    /// Per chunk: its text and line start offsets.
    files: BTreeMap<String, Option<(String, Vec<usize>)>>,
}

impl<'a> Sources<'a> {
    fn new(provenance: Option<&'a GraphProvenance>, roots: &'a CurrentRoots) -> Self {
        Self {
            provenance,
            roots,
            files: BTreeMap::new(),
        }
    }

    fn located(&mut self, chunk_id: &str, location: Option<&SourceLocation>) -> Located {
        Located {
            chunk_id: chunk_id.to_owned(),
            excerpt: location.and_then(|location| self.excerpt(chunk_id, location)),
            source_location: location.cloned(),
        }
    }

    fn excerpt_at_path(&mut self, location: &SourceLocation) -> Option<String> {
        let chunk_id = self
            .provenance?
            .chunk_sources
            .iter()
            .find(|source| source.source_path == Path::new(&location.source_path))?
            .chunk_id
            .clone();
        self.excerpt(&chunk_id, location)
    }

    fn excerpt(&mut self, chunk_id: &str, location: &SourceLocation) -> Option<String> {
        let (provenance, roots) = (self.provenance, self.roots);
        let text = self
            .files
            .entry(chunk_id.to_owned())
            .or_insert_with(|| {
                let provenance = provenance?;
                let source = provenance
                    .chunk_sources
                    .iter()
                    .find(|source| source.chunk_id == chunk_id)?;
                let path = provenance
                    .resolve(&source.input_file.root, &source.input_file.path, roots)
                    .ok()?;
                let bytes = std::fs::read(&path).ok()?;
                if graph_provenance::sha256(&bytes) != source.sha256 {
                    eprintln!(
                        "warning: {} changed since the graph was generated; omitting its excerpts",
                        path.display()
                    );
                    return None;
                }
                let text = String::from_utf8(bytes).ok()?;
                let line_starts = std::iter::once(0)
                    .chain(text.match_indices('\n').map(|(index, _)| index + 1))
                    .collect();
                Some((text, line_starts))
            })
            .as_ref()?;
        let (text, line_starts) = (&text.0, &text.1);
        let line_start = *line_starts.get(location.start_line.checked_sub(1)?)?;
        let line = text[line_start..].lines().next().unwrap_or_default();
        let start = location.start_column.unwrap_or(1).saturating_sub(1);
        let tail = line.get(start..).unwrap_or(line);
        let mut end = tail.len().min(EXCERPT_BYTES);
        while !tail.is_char_boundary(end) {
            end -= 1;
        }
        let excerpt = tail[..end].trim_end();
        Some(if end < tail.len() {
            format!("{excerpt}…")
        } else {
            excerpt.to_owned()
        })
    }
}

const EXCERPT_BYTES: usize = 100;

fn render_location(location: Option<&SourceLocation>) -> String {
    location.map_or_else(
        || "<unlocated>".to_owned(),
        |location| match location.start_column {
            Some(column) => format!("{}:{}:{column}", location.source_path, location.start_line),
            None => format!("{}:{}", location.source_path, location.start_line),
        },
    )
}

fn snake<T: Serialize>(value: &T) -> String {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.as_str().map(str::to_owned))
        .unwrap_or_else(|| "?".to_owned())
}

fn snake_set<T: Serialize>(values: impl IntoIterator<Item = T>) -> String {
    let names: Vec<_> = values.into_iter().map(|value| snake(&value)).collect();
    if names.is_empty() {
        "none".to_owned()
    } else {
        names.join(", ")
    }
}

fn render_text(report: &IntegrityExplanationReport, out: &mut String) {
    use std::fmt::Write;
    for unit in &report.source_units {
        let _ = writeln!(
            out,
            "{} {} {}",
            unit.id,
            render_location(unit.source_location.as_ref()),
            if unit.complete {
                "complete"
            } else {
                "incomplete"
            }
        );
        for loss in &unit.precision_losses {
            let _ = writeln!(
                out,
                "  loss {} at {}{}",
                snake_tag(&loss.reason),
                render_location(loss.source_location.as_ref()),
                loss.excerpt
                    .as_ref()
                    .map(|excerpt| format!("  `{excerpt}`"))
                    .unwrap_or_default()
            );
        }
        let _ = writeln!(
            out,
            "  evaluated guards: {}",
            snake_set(&unit.evaluated_guards)
        );
        if unit.failed_guards.is_empty() {
            let _ = writeln!(out, "  no failed guard");
        }
        for guard in &unit.failed_guards {
            let _ = writeln!(
                out,
                "  failed {} (affected roots: {})",
                snake(&guard.guard),
                snake_set(&guard.affected_roots)
            );
            for slot in &guard.slots {
                let _ = writeln!(
                    out,
                    "    {} {}",
                    slot.slot,
                    if slot.intact { "intact" } else { "unknown" }
                );
            }
        }
    }
    for site in &report.dynamic_scope {
        let _ = writeln!(
            out,
            "dynamic scope at {} leaves every slot unknown",
            render_location(site.source_location.as_ref())
        );
    }
    // Slots with the same verdict and witnesses render once, in report order.
    let mut groups: Vec<(Vec<&str>, String, String)> = Vec::new();
    for slot in &report.slots {
        let counts: Vec<_> = slot
            .reach_counts
            .iter()
            .map(|(reach, count)| format!("{}={count}", snake(reach)))
            .collect();
        let verdict = format!(
            "{} (affected roots: {}); {} witness(es): {}",
            if slot.intact { "intact" } else { "unknown" },
            snake_set(&slot.affected_roots),
            slot.witness_limit.total,
            if counts.is_empty() {
                "none".to_owned()
            } else {
                counts.join(" ")
            }
        );
        let mut block = String::new();
        for witness in &slot.witnesses {
            let _ = writeln!(
                block,
                "  {} {} {}{}",
                snake(&witness.reach),
                snake(&witness.operation),
                render_location(witness.site.source_location.as_ref()),
                witness
                    .site
                    .excerpt
                    .as_ref()
                    .map(|excerpt| format!("  `{excerpt}`"))
                    .unwrap_or_default()
            );
            if let Some(property) = &witness.receiver_global {
                let _ = writeln!(block, "    receiver: global `{property}`");
            }
            if !witness.discharged_by.is_empty() {
                let _ = writeln!(
                    block,
                    "    discharged by: {}",
                    snake_set(&witness.discharged_by)
                );
            }
        }
        if slot.witness_limit.truncated {
            let _ = writeln!(
                block,
                "  showing {} of {} witnesses; use --all for the rest",
                slot.witness_limit.emitted, slot.witness_limit.total
            );
        }
        match groups.iter_mut().find(|(_, other_verdict, other_block)| {
            *other_verdict == verdict && *other_block == block
        }) {
            Some((names, _, _)) => names.push(&slot.slot),
            None => groups.push((vec![&slot.slot], verdict, block)),
        }
    }
    for (names, verdict, block) in groups {
        let _ = writeln!(
            out,
            "{} {}: {verdict}",
            if names.len() == 1 { "slot" } else { "slots" },
            names.join(", ")
        );
        out.push_str(&block);
    }
}

/// The `code` tag of an internally tagged enum.
fn snake_tag<T: Serialize>(value: &T) -> String {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.get("code")?.as_str().map(str::to_owned))
        .unwrap_or_else(|| "?".to_owned())
}
