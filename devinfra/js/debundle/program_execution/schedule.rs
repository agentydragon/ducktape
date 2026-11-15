//! Source and emitted ESM schedules share the original module cells. This
//! comparison supplements the graph gate; callable behavior never proves cell
//! readiness and an unresolved loader request never becomes an empty schedule.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail, ensure};
use source_identity::{ProgramEvaluationKind, ProgramLinkResolution, SourceUnitOrdinal};
use spec::NodeBuiltinModuleExt;

use super::ProgramExecutables;
pub use super::effects::ProgramSourceUnitId;
use crate::facts::sync_effects::interpreter::{ExecutionProgramInputs, RuntimeBindingEffects};
use crate::facts::sync_effects::runtime_inference::infer_scheduled_execution;

/// Machine-readable refusal emitted before any accepted output is committed.
#[derive(Debug, Clone, serde::Serialize)]
pub struct EvaluationScheduleRefusal {
    pub source_entry: Option<String>,
    pub reason: String,
}
impl EvaluationScheduleRefusal {
    pub fn from_error(source_entry: Option<&str>, error: anyhow::Error) -> Self {
        error
            .downcast_ref::<Self>()
            .cloned()
            .unwrap_or_else(|| Self {
                source_entry: source_entry.map(str::to_owned),
                reason: format!("{error:#}"),
            })
    }
}
impl std::fmt::Display for EvaluationScheduleRefusal {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.reason)
    }
}
impl std::error::Error for EvaluationScheduleRefusal {}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) enum OwnerCompletion {
    Normal,
    Abrupt,
    #[default]
    Unknown,
}

#[derive(Debug, PartialEq, Eq)]
enum TerminalCompletion {
    Normal,
    Abrupt(ProgramSourceUnitId),
    Unresolved,
}

struct EvaluationTrace {
    owners: BTreeMap<ProgramSourceUnitId, (OwnerCompletion, RuntimeBindingEffects)>,
    terminal: TerminalCompletion,
    unobservable_initializations:
        BTreeMap<ProgramSourceUnitId, super::effects::RuntimeAvailabilityCell>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum EvaluationEvent {
    Owner(ProgramSourceUnitId),
    /// Existing vendor equivalence replaces this module evaluation.
    /// Its retained original file is not part of the emitted file set.
    OpaqueModule(String),
    /// Host/package loader identity, preserved at its exact evaluation cut.
    Opaque {
        module: String,
        specifier: String,
        /// Source chunks are files; emitted chunks are directories. Every
        /// physical directory change affects unresolved package lookup,
        /// including moving app.js into app/entry.js. Only recognized host
        /// builtins have scope-independent opaque identities.
        resolution_directory: String,
    },
}

impl EvaluationEvent {
    /// An unchanged URL is not proof of an unchanged dependency closure: an
    /// unindexed file can import the original application entry again. Only
    /// recognized builtins are closed host leaves; unresolved bare packages
    /// retain their physical lookup directory and must match it exactly.
    pub fn opaque_request(
        module: String,
        specifier: String,
        resolution_directory: String,
    ) -> Result<Self> {
        let builtin = spec::NodeBuiltinModule::from_static_specifier(&specifier).is_some();
        ensure!(
            builtin
                || (!specifier.contains(':')
                    && !specifier.starts_with('/')
                    && !specifier.starts_with('.')),
            "evaluation_schedule_unsupported: unindexed loader closure {module} requests {specifier}"
        );
        Ok(Self::Opaque {
            module,
            specifier,
            resolution_directory: if builtin {
                String::new()
            } else {
                resolution_directory
            },
        })
    }
}

/// Exact author pairs admitted by the graph's contradiction checks. They are
/// materialization inputs, never source-derived analysis or cached receipts.
#[derive(Default, Clone)]
pub struct EvaluationCommutations {
    pairs: BTreeMap<
        (ProgramSourceUnitId, ProgramSourceUnitId),
        crate::analysis_inputs::CommutingStatementsRef,
    >,
}

impl EvaluationCommutations {
    pub fn from_graph(module: &str, graph: &crate::DependencyGraph) -> Self {
        let owner = |id| ProgramSourceUnitId {
            module: module.to_owned(),
            ordinal: graph
                .node(id)
                .expect("validated commuting endpoint")
                .statement_ordinal,
        };
        Self {
            pairs: graph
                .commuting_premises()
                .iter()
                .map(|premise| {
                    let (a, b) = (owner(premise.a), owner(premise.b));
                    // Reversed or repeated declarations certify one pair, not
                    // extra permission to cross an unrelated third owner.
                    let pair = if a < b { (a, b) } else { (b, a) };
                    (pair, premise.reference.clone())
                })
                .collect(),
        }
    }

    pub fn extend(&mut self, other: &Self) {
        self.pairs.extend(
            other
                .pairs
                .iter()
                .map(|(pair, reference)| (pair.clone(), reference.clone())),
        );
    }
}

impl ProgramExecutables {
    pub fn evaluation_owners(&self, module: &str) -> Result<Vec<ProgramSourceUnitId>> {
        let source = self.sources.get(module).with_context(|| {
            format!("evaluation_schedule_unsupported: no prepared source for {module}")
        })?;
        Ok((0..source.prepared.facts.len())
            .map(|ordinal| ProgramSourceUnitId {
                module: module.to_owned(),
                ordinal: SourceUnitOrdinal(ordinal),
            })
            .collect())
    }

    pub fn source_schedule(
        &self,
        entry: &str,
        opaque_modules: &BTreeSet<String>,
    ) -> Result<Vec<EvaluationEvent>> {
        self.resolve_source_schedule(entry, opaque_modules)
            .map_err(|error| EvaluationScheduleRefusal::from_error(Some(entry), error).into())
    }

    fn resolve_source_schedule(
        &self,
        entry: &str,
        opaque_modules: &BTreeSet<String>,
    ) -> Result<Vec<EvaluationEvent>> {
        let links = self
            .links
            .as_ref()
            .context("evaluation_schedule_unsupported: no source module links")?;
        let root = links
            .chunks
            .get(entry)
            .context("evaluation_schedule_missing: source entry")?;
        enum Task {
            Enter(source_identity::ChunkId),
            Finish(source_identity::ChunkId),
            Opaque(EvaluationEvent),
        }
        let mut tasks = vec![Task::Enter(root)];
        let mut visited = BTreeSet::new();
        let mut result = Vec::new();
        let mut emitted_opaque = BTreeSet::new();
        while let Some(task) = tasks.pop() {
            match task {
                Task::Opaque(identity) => {
                    if emitted_opaque.insert(identity.clone()) {
                        result.push(identity);
                    }
                }
                Task::Finish(module) => result.extend(
                    self.evaluation_owners(links.chunks.name(module))?
                        .into_iter()
                        .map(EvaluationEvent::Owner),
                ),
                Task::Enter(module) => {
                    if !visited.insert(module) {
                        continue;
                    }
                    if opaque_modules.contains(links.chunks.name(module)) {
                        tasks.push(Task::Opaque(EvaluationEvent::OpaqueModule(
                            links.chunks.name(module).to_owned(),
                        )));
                    } else {
                        ensure!(
                            links.index.evaluation_kind(module)
                                == Some(ProgramEvaluationKind::SynchronousEsm),
                            "evaluation_schedule_unsupported: module {} is not synchronous ESM",
                            links.chunks.name(module)
                        );
                        tasks.push(Task::Finish(module));
                    }
                    for request in links
                        .index
                        .module_requests(module)
                        .context(
                            "evaluation_schedule_unsupported: module request closure unavailable",
                        )?
                        .iter()
                        .rev()
                    {
                        match &request.provider {
                            ProgramLinkResolution::Resolved(provider) => {
                                tasks.push(Task::Enter(*provider))
                            }
                            ProgramLinkResolution::Unsupported => {
                                ensure!(
                                    !request.source_specifier.starts_with('.'),
                                    "evaluation_schedule_unsupported: {} requests {}",
                                    links.chunks.name(module),
                                    request.source_specifier
                                );
                                tasks.push(Task::Opaque(EvaluationEvent::opaque_request(
                                    links.chunks.name(module).to_owned(),
                                    request.source_specifier.clone(),
                                    links
                                        .chunks
                                        .name(module)
                                        .rsplit_once('/')
                                        .map_or("", |(directory, _)| directory)
                                        .to_owned(),
                                )?));
                            }
                            ProgramLinkResolution::Cyclic | ProgramLinkResolution::Ambiguous => {
                                bail!(
                                    "evaluation_schedule_unsupported: unresolved module request {}",
                                    request.source_specifier
                                )
                            }
                            ProgramLinkResolution::Missing => bail!(
                                "evaluation_schedule_missing: {} requests {}",
                                links.chunks.name(module),
                                request.source_specifier
                            ),
                        }
                    }
                }
            }
        }
        Ok(result)
    }

    pub fn is_noop(&self, owner: &ProgramSourceUnitId) -> bool {
        self.sources.get(&owner.module).is_some_and(|source| {
            source
                .prepared
                .prepared_sync_index
                .execution_index()
                .evaluation_noop_owners
                .contains(&owner.ordinal)
        })
    }

    /// Recover execution order from the final syntax using origins captured
    /// during canonical preparation. Never infer it by sorting assignments.
    /// Synthetic executable statements without a retained origin are refused.
    pub fn emitted_owner_order(
        &self,
        module: &str,
        body: &[swc_ecma_ast::ModuleItem],
    ) -> Result<Vec<ProgramSourceUnitId>> {
        let Some(source) = self.sources.get(module) else {
            ensure!(
                body.iter()
                    .all(|item| evaluation_item_origins(item).is_empty()),
                "evaluation_schedule_unsupported: unowned executable file in {module}"
            );
            return Ok(Vec::new());
        };
        let index = source.prepared.prepared_sync_index.execution_index();
        let origins = &index.evaluation_origins;
        let mut result = Vec::new();
        for anchors in body.iter().flat_map(evaluation_item_origins) {
            let candidates: BTreeSet<_> = anchors
                .iter()
                .filter_map(|anchor| origins.get(anchor))
                .flatten()
                .collect();
            ensure!(
                candidates.len() == 1,
                "evaluation_schedule_unsupported: emitted executable has no unique source origin in {module}: {anchors:?}"
            );
            result.push(ProgramSourceUnitId {
                module: module.to_owned(),
                ordinal: **candidates.first().expect("one candidate"),
            });
        }
        Ok(result)
    }

    /// Compare owner rows recovered from the finalized emitted AST against
    /// source ESM DFS, with no output committed yet.
    pub fn validate_schedule(
        &self,
        entry: &str,
        emitted: &[EvaluationEvent],
        opaque_modules: &BTreeSet<String>,
        commutations: &EvaluationCommutations,
    ) -> Result<()> {
        self.compare_schedule(entry, emitted, opaque_modules, commutations)
            .map_err(|error| EvaluationScheduleRefusal::from_error(Some(entry), error).into())
    }

    fn compare_schedule(
        &self,
        entry: &str,
        emitted: &[EvaluationEvent],
        opaque_modules: &BTreeSet<String>,
        commutations: &EvaluationCommutations,
    ) -> Result<()> {
        let source = self.source_schedule(entry, opaque_modules)?;
        let meaningful = |events: &[EvaluationEvent]| {
            events
                .iter()
                .filter(|event| match event {
                    EvaluationEvent::Owner(owner) => !self.is_noop(owner),
                    EvaluationEvent::Opaque { .. } | EvaluationEvent::OpaqueModule(_) => true,
                })
                .cloned()
                .collect::<Vec<_>>()
        };
        let source_meaningful = meaningful(&source);
        let emitted_meaningful = meaningful(emitted);
        if source_meaningful == emitted_meaningful {
            return Ok(());
        }
        let event_set =
            |events: &[EvaluationEvent]| events.iter().cloned().collect::<BTreeSet<_>>();
        ensure!(
            source_meaningful.len() == event_set(&source_meaningful).len()
                && emitted_meaningful.len() == event_set(&emitted_meaningful).len(),
            "evaluation_schedule_duplicate_owner: {entry}"
        );
        ensure!(
            event_set(&source_meaningful) == event_set(&emitted_meaningful),
            "evaluation_schedule_owner_mismatch: {entry}: source and emitted closures differ: {:?}",
            event_set(&source_meaningful)
                .symmetric_difference(&event_set(&emitted_meaningful))
                .collect::<Vec<_>>()
        );
        // Unique owner and opaque events admit a linear prefix-set equality
        // check: equal prefix cardinality plus maximum original rank below
        // this boundary. Do not copy a growing owner set for every statement.
        let preserved_cuts = preserved_evaluation_cuts(&source_meaningful, &emitted_meaningful);
        let authorized_cuts =
            authorized_evaluation_cuts(&source_meaningful, &emitted_meaningful, commutations);
        for event in &source_meaningful {
            if matches!(
                event,
                EvaluationEvent::Opaque { .. } | EvaluationEvent::OpaqueModule(_)
            ) {
                ensure!(
                    preserved_cuts.contains(event),
                    "evaluation_schedule_opaque_reordered: {entry}: {event:?}"
                );
            }
        }
        let original = self.execute_schedule(entry, &source)?;
        let candidate = self.execute_schedule(entry, emitted)?;
        // An unknown host operation may throw or reenter. Crossing a normal
        // noninvoking initialization is harmless only when its cell is private
        // and absent from every callable capture, in both executions. Keep all
        // availability comparisons, and never relax opaque loader barriers or
        // the frontier of a definite throw.
        let unobservable: BTreeSet<_> = original
            .unobservable_initializations
            .iter()
            .filter(|(owner, cell)| {
                candidate.unobservable_initializations.get(*owner) == Some(*cell)
            })
            .map(|(owner, _)| EvaluationEvent::Owner(owner.clone()))
            .collect();
        let private_cells: BTreeSet<_> = original
            .unobservable_initializations
            .iter()
            .filter(|(owner, _)| unobservable.contains(&EvaluationEvent::Owner((*owner).clone())))
            .map(|(_, cell)| cell)
            .collect();
        let does_not_read_private_cells = |effects: &RuntimeBindingEffects| {
            effects
                .availability_reads
                .keys()
                .all(|cell| !private_cells.contains(cell))
                && effects
                    .availability_points
                    .keys()
                    .all(|(_, _, cell)| !private_cells.contains(cell))
        };
        let visible = |events: &[EvaluationEvent]| {
            events
                .iter()
                .filter(|event| !unobservable.contains(*event))
                .cloned()
                .collect::<Vec<_>>()
        };
        let private_initialization_cuts = authorized_evaluation_cuts(
            &visible(&source_meaningful),
            &visible(&emitted_meaningful),
            commutations,
        );

        ensure!(
            original.terminal == candidate.terminal,
            "evaluation_schedule_terminal_completion_changed: {entry}: {:?} -> {:?}",
            original.terminal,
            candidate.terminal
        );
        ensure!(
            original.owners.keys().eq(candidate.owners.keys()),
            "evaluation_schedule_abrupt_frontier_changed: {entry}: different owners execute before uncaught completion"
        );
        for (owner, (completion, availability)) in &original.owners {
            let (planned_completion, planned_availability) = &candidate.owners[owner];
            ensure!(
                completion == planned_completion,
                "evaluation_schedule_completion_changed: {owner:?}: {completion:?} -> {planned_completion:?}"
            );
            let event = if opaque_modules.contains(&owner.module) {
                EvaluationEvent::OpaqueModule(owner.module.clone())
            } else {
                EvaluationEvent::Owner(owner.clone())
            };
            if self.is_noop(owner) {
                continue;
            }
            if *completion != OwnerCompletion::Normal
                || !availability.availability_complete
                || !planned_availability.availability_complete
            {
                let private_crossing = *completion == OwnerCompletion::Unknown
                    && availability.availability_complete
                    && planned_availability.availability_complete
                    && private_initialization_cuts.contains(&event);
                ensure!(
                    authorized_cuts.contains(&event) || private_crossing,
                    "evaluation_schedule_unknown_reordered: {owner:?}"
                );
            }
            let same_availability = availability.availability_points
                == planned_availability.availability_points
                && availability.availability_reads == planned_availability.availability_reads;
            // Exact authored commutation may discharge hypothesized fallback
            // reads, never an observed cell state (including unresolved reads).
            let authorized_assumptions = !commutations.pairs.is_empty()
                && authorized_cuts.contains(&event)
                && availability.observed_availability_matches(planned_availability);
            ensure!(
                same_availability || authorized_assumptions,
                "evaluation_schedule_availability_changed: {owner:?}: {}",
                availability_difference(availability, planned_availability)
            );
            if availability
                .availability_reads
                .values()
                .flat_map(|observers| observers.values())
                .chain(
                    availability
                        .availability_points
                        .values()
                        .flat_map(|trace| trace.states()),
                )
                .any(|states| {
                    states.contains(&super::effects::AvailabilityState::Unknown)
                        || states.contains(&super::effects::AvailabilityState::Assumed)
                })
            {
                ensure!(
                    authorized_cuts.contains(&event)
                        || (private_initialization_cuts.contains(&event)
                            && does_not_read_private_cells(availability)
                            && does_not_read_private_cells(planned_availability)),
                    "evaluation_schedule_availability_unknown: {owner:?}"
                );
            }
        }
        Ok(())
    }

    fn execute_schedule(
        &self,
        entry: &str,
        schedule: &[EvaluationEvent],
    ) -> Result<EvaluationTrace> {
        let source = self
            .sources
            .get(entry)
            .context("evaluation_schedule_missing: prepared entry")?;
        let (mut root, mut state) = source
            .prepared
            .execution_inputs(&source.hints, &source.export_reentry_policy);
        root.executables = Some(self);
        let mut sources = BTreeMap::new();
        for (module, prepared, hints, policy) in self.sources() {
            if module == entry {
                continue;
            }
            let (mut view, initial) = prepared.execution_inputs(hints, policy);
            view.executables = Some(self);
            state.install_source(view.index.source_scope(), initial);
            sources.insert(module.to_owned(), view);
        }
        let private_cells: BTreeMap<_, _> = std::iter::once((entry, &root))
            .chain(sources.iter().map(|(module, view)| (module.as_str(), view)))
            .flat_map(|(module, view)| {
                view.unobservable_initialization_cells()
                    .into_iter()
                    .map(move |(ordinal, cell)| {
                        (
                            ProgramSourceUnitId {
                                module: module.to_owned(),
                                ordinal,
                            },
                            super::effects::RuntimeAvailabilityCell::Binding(cell),
                        )
                    })
            })
            .collect();
        let mut unobservable_initializations = BTreeMap::new();
        let inputs = ExecutionProgramInputs {
            root: &root,
            program_sources: Some(&sources),
        };
        let mut scheduled_owners = Vec::new();
        for event in schedule {
            match event {
                EvaluationEvent::Owner(owner) => scheduled_owners.push(owner.clone()),
                EvaluationEvent::OpaqueModule(module) => {
                    scheduled_owners.extend(self.evaluation_owners(module)?)
                }
                EvaluationEvent::Opaque { .. } => {}
            }
        }
        let runtime_schedule = scheduled_owners
            .iter()
            .filter(|owner| !self.is_noop(owner))
            .map(|owner| {
                let source = self.sources.get(&owner.module).with_context(|| {
                    format!("evaluation_schedule_unsupported: {}", owner.module)
                })?;
                ensure!(
                    owner.ordinal.0 < source.prepared.facts.len(),
                    "evaluation_schedule_invalid_owner: {owner:?}"
                );
                Ok(source
                    .prepared
                    .prepared_sync_index
                    .execution_index()
                    .source_scope()
                    .unit(owner.ordinal))
            })
            .collect::<Result<Vec<_>>>()?;
        let owners: BTreeMap<_, _> =
            infer_scheduled_execution(&inputs, state, runtime_schedule, true)
                .into_iter()
                .map(|(owner, (calls, _))| {
                    let owner = ProgramSourceUnitId {
                        module: self
                            .module_name_for_scope(owner.source)
                            .expect("prepared source identity")
                            .to_owned(),
                        ordinal: owner.ordinal,
                    };
                    if calls.completion == OwnerCompletion::Normal
                        && private_cells.contains_key(&owner)
                        && calls
                            .completed_initialization
                            .as_ref()
                            .is_some_and(|receipt| {
                                receipt.covers(owner.ordinal) && receipt.is_noninvoking()
                            })
                    {
                        unobservable_initializations
                            .insert(owner.clone(), private_cells[&owner].clone());
                    }
                    (owner, (calls.completion, calls.runtime_binding_effects))
                })
                .collect();
        let mut terminal = TerminalCompletion::Normal;
        for owner in &scheduled_owners {
            let Some((completion, _)) = owners.get(owner) else {
                continue;
            };
            match completion {
                OwnerCompletion::Normal => {}
                OwnerCompletion::Unknown => terminal = TerminalCompletion::Unresolved,
                OwnerCompletion::Abrupt => {
                    if terminal == TerminalCompletion::Normal {
                        terminal = TerminalCompletion::Abrupt(owner.clone());
                    }
                    break;
                }
            }
        }
        Ok(EvaluationTrace {
            owners,
            terminal,
            unobservable_initializations,
        })
    }
}

/// Report one differing observation, rather than dumping the whole execution
/// ledger. Counts and order inside a point's trace are part of the comparison.
fn availability_difference(
    original: &RuntimeBindingEffects,
    candidate: &RuntimeBindingEffects,
) -> String {
    if let Some(point) = original
        .availability_points
        .keys()
        .chain(candidate.availability_points.keys())
        .find(|point| {
            original.availability_points.get(*point) != candidate.availability_points.get(*point)
        })
    {
        return format!(
            "point {point:?}: {:?} -> {:?}",
            original.availability_points.get(point),
            candidate.availability_points.get(point)
        );
    }
    let cell = original
        .availability_reads
        .keys()
        .chain(candidate.availability_reads.keys())
        .find(|cell| {
            original.availability_reads.get(*cell) != candidate.availability_reads.get(*cell)
        })
        .expect("an availability mismatch changes a point or an aggregate read");
    format!(
        "cell {cell:?}: {:?} -> {:?}",
        original.availability_reads.get(cell),
        candidate.availability_reads.get(cell)
    )
}

/// Per-declarator origins survive export removal, variable splitting, default
/// materialization and identifier renaming. Directives and hoisted functions
/// have no body evaluation; every other final item must match an original row.
pub(crate) fn evaluation_item_origins(item: &swc_ecma_ast::ModuleItem) -> Vec<Vec<(u32, u32)>> {
    use swc_common::Spanned;
    use swc_ecma_ast::{Decl, DefaultDecl, ModuleDecl, ModuleItem, Stmt};
    let span = |span: swc_common::Span| (span.lo.0, span.hi.0);
    let declaration = match item {
        ModuleItem::ModuleDecl(
            ModuleDecl::Import(_) | ModuleDecl::ExportNamed(_) | ModuleDecl::ExportAll(_),
        )
        | ModuleItem::Stmt(Stmt::Empty(_)) => return Vec::new(),
        ModuleItem::Stmt(Stmt::Decl(declaration)) => Some(declaration),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => Some(&export.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => {
            return match &export.decl {
                DefaultDecl::Fn(_) => Vec::new(),
                DefaultDecl::Class(class) => vec![vec![span(class.class.span)]],
                _ => vec![vec![span(item.span())]],
            };
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export)) => {
            return vec![vec![span(export.expr.span())]];
        }
        _ => None,
    };
    match declaration {
        Some(Decl::Fn(_)) => Vec::new(),
        Some(Decl::Var(var)) => var
            .decls
            .iter()
            .map(|declarator| {
                let mut spans = vec![span(declarator.span)];
                if let Some(init) = &declarator.init {
                    spans.push(span(init.span()));
                }
                spans.sort_unstable();
                spans.dedup();
                spans
            })
            .collect(),
        Some(Decl::Class(class)) => vec![vec![span(class.class.span)]],
        _ => vec![vec![span(item.span())]],
    }
}

/// Equal prefix sets from unique event ranks, without quadratic set copies.
fn preserved_evaluation_cuts(
    source: &[EvaluationEvent],
    emitted: &[EvaluationEvent],
) -> BTreeSet<EvaluationEvent> {
    let ranks: BTreeMap<_, _> = source
        .iter()
        .enumerate()
        .map(|(index, event)| (event, index))
        .collect();
    let mut max_rank = None;
    let mut preserved = BTreeSet::new();
    for (position, event) in emitted.iter().enumerate() {
        let rank = ranks[event];
        if rank == position && max_rank.is_none_or(|prior| prior < rank) {
            preserved.insert(event.clone());
        }
        max_rank = Some(max_rank.map_or(rank, |prior: usize| prior.max(rank)));
    }
    preserved
}

/// Prefix differences are allowed only when every crossed owner is an exact
/// admitted pair. Counting inversions avoids copying growing prefixes or
/// treating transitive pairs as author promises. O((owners + pairs) log owners).
fn authorized_evaluation_cuts(
    source: &[EvaluationEvent],
    emitted: &[EvaluationEvent],
    commutations: &EvaluationCommutations,
) -> BTreeSet<EvaluationEvent> {
    if commutations.pairs.is_empty() {
        return preserved_evaluation_cuts(source, emitted);
    }
    let ranks: BTreeMap<_, _> = source
        .iter()
        .enumerate()
        .map(|(rank, event)| (event, rank))
        .collect();
    let positions: BTreeMap<_, _> = emitted
        .iter()
        .enumerate()
        .map(|(position, event)| (event, position))
        .collect();
    let mut permitted = BTreeMap::<EvaluationEvent, usize>::new();
    for (a, b) in commutations.pairs.keys() {
        let (a, b) = (
            EvaluationEvent::Owner(a.clone()),
            EvaluationEvent::Owner(b.clone()),
        );
        let (Some(ar), Some(br), Some(ap), Some(bp)) = (
            ranks.get(&a),
            ranks.get(&b),
            positions.get(&a),
            positions.get(&b),
        ) else {
            continue;
        };
        if (ar < br) != (ap < bp) {
            *permitted.entry(a).or_default() += 1;
            *permitted.entry(b).or_default() += 1;
        }
    }
    let mut tree = vec![0usize; source.len() + 1];
    let mut authorized = BTreeSet::new();
    for (position, event) in emitted.iter().enumerate() {
        let rank = ranks[event];
        let mut index = rank;
        let mut common = 0;
        while index > 0 {
            common += tree[index];
            index &= index - 1;
        }
        if rank + position - 2 * common == permitted.get(event).copied().unwrap_or(0) {
            authorized.insert(event.clone());
        }
        let mut index = rank + 1;
        while index < tree.len() {
            tree[index] += 1;
            index += index.isolate_lowest_one();
        }
    }
    authorized
}
