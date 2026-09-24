//! The selector resolve every command calls: chunks and the spec entities
//! aimed at each in, one [`SelectorOutcome`] per entity out.
//!
//! The shape matcher ([`ChunkResolver`]) lists every place each `source_match`
//! matches. Those places become candidate tables in one selector program with
//! the name pins and relational selectors, and every entity is assigned at
//! once under `all_different`: each group of interacting entities by CP-SAT,
//! the rest from their own candidates. Semantics:
//! <docs/selector_resolution.md>.

use std::cell::OnceCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};

use analysis::facts::StructuralChunkAnalysis;
use analysis::{ChunkId, DepKind, OwnerId, StatementKind, StatementOrdinal};
use anyhow::{Context, Result, bail};
use js_ast::body_index_for_statement_ordinal;
use rayon::prelude::*;
use selector_ir::{
    ClaimOutcome, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorProgramSliceOptions, SelectorSourceMatchProjectionEvent,
    SelectorSourceMatchProjectionOutcome, SelectorTargetId, SelectorVariableId, SolverClaim,
    SolverResult,
};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, MemberSelectorSpecRef,
};
use selector_outcome::{
    Candidate, Entity, EntityRef, MAX_CANDIDATES_PER_SELECTOR, Outcome, Placement, ResolvedBy,
    SelectorKind, SelectorOutcome,
};
use selector_runtime::solve_global_selector_program;
use source_match::ParsedSourceMatchSelector;
use source_match::chunk_resolver::ChunkResolver;
use spec::{AnonymousStatementSelector, BindingSourceKind, MemberSelectorSpec};
use swc_ecma_ast::{ImportSpecifier, Module, ModuleDecl, ModuleItem};

/// One parsed chunk, analysed once and resolved against any number of entity
/// sets.
pub struct Chunk<'m> {
    name: String,
    id: ChunkId,
    module: &'m Module,
    structural: StructuralChunkAnalysis<'m>,
    matcher: OnceCell<ChunkResolver<'m>>,
    places: OnceCell<Places>,
}

/// Top-level statements by source body index: the owner of each statement,
/// and of each binding it declares.
struct Places {
    owner_by_body: BTreeMap<usize, OwnerId>,
    owner_by_body_and_binding: BTreeMap<(usize, String), OwnerId>,
}

/// One logical module's entities.
#[derive(Debug, Clone)]
pub struct SpecModule {
    /// Module path, e.g. `ui/widgets` (without the chunk).
    pub path: String,
    pub members: Vec<Member>,
    pub anonymous_statements: Vec<AnonymousStatement>,
}

#[derive(Debug, Clone)]
pub struct AnonymousStatement {
    /// Its index in the module's `anonymous_statements[]`.
    pub index: usize,
    pub selector: ParsedSourceMatchSelector,
}

#[derive(Debug, Clone)]
pub struct Member {
    pub export_name: String,
    pub selector: MemberSelector,
}

/// How a member names its declaration. A `source_match` member comes from a
/// `source_matches[].bindings[]` entry; entries of one `source_matches[]`
/// claim share their template and resolve as one group.
#[derive(Debug, Clone)]
pub enum MemberSelector {
    /// A name pin. On an import specifier it names the import binding itself
    /// and is not resolved.
    Binding(spec::BindingSelector),
    SourceMatch(ParsedSourceMatchSelector),
    CrossRef(spec::CrossRefTarget),
    ReadsMember(spec::ReadsMemberTarget),
    MemberOfModule(spec::MemberOfModuleTarget),
    PassedToCall(spec::PassedToCallTarget),
    MakesDecorateCall(spec::MakesDecorateCallTarget),
    IntrinsicAlias(spec::IntrinsicAliasTarget),
}

impl MemberSelector {
    /// A `members[].selector`, with any `source_match` parsed.
    pub fn from_spec(request_id: &str, selector: MemberSelectorSpec) -> Result<Self> {
        Ok(match selector {
            MemberSelectorSpec::Binding(binding) => Self::Binding(binding),
            MemberSelectorSpec::SourceMatch(selector) => {
                Self::SourceMatch(ParsedSourceMatchSelector::parse(
                    request_id,
                    "source_match",
                    format!("<source_match selector in {request_id}>"),
                    &selector,
                    "source_match",
                )?)
            }
            MemberSelectorSpec::CrossRef(target) => Self::CrossRef(target),
            MemberSelectorSpec::ReadsMember(target) => Self::ReadsMember(target),
            MemberSelectorSpec::MemberOfModule(target) => Self::MemberOfModule(target),
            MemberSelectorSpec::PassedToCall(target) => Self::PassedToCall(target),
            MemberSelectorSpec::MakesDecorateCall(target) => Self::MakesDecorateCall(target),
            MemberSelectorSpec::IntrinsicAlias(target) => Self::IntrinsicAlias(target),
        })
    }

    pub fn is_import_specifier(&self) -> bool {
        matches!(self, Self::Binding(binding)
            if binding.kind == Some(BindingSourceKind::ImportSpecifier))
    }

    pub fn source_match(&self) -> Option<&AnonymousStatementSelector> {
        match self {
            Self::SourceMatch(parsed) => Some(parsed.selector()),
            _ => None,
        }
    }

    pub fn kind(&self) -> SelectorKind {
        match self {
            Self::Binding(_) => SelectorKind::Binding,
            Self::SourceMatch(_) => SelectorKind::SourceMatches,
            Self::CrossRef(_) => SelectorKind::CrossRef,
            Self::ReadsMember(_) => SelectorKind::ReadsMember,
            Self::MemberOfModule(_) => SelectorKind::MemberOfModule,
            Self::PassedToCall(_) => SelectorKind::PassedToCall,
            Self::MakesDecorateCall(_) => SelectorKind::MakesDecorateCall,
            Self::IntrinsicAlias(_) => SelectorKind::IntrinsicAlias,
        }
    }

    fn preview(&self) -> String {
        match self {
            Self::Binding(binding) => format!("{binding:?}"),
            Self::SourceMatch(parsed) => {
                source_match::source_match_preview(&parsed.selector().match_source)
            }
            relational => format!("{relational:?}"),
        }
    }

    fn spec_ref(&self) -> MemberSelectorSpecRef<'_> {
        match self {
            Self::Binding(binding) => MemberSelectorSpecRef::Binding(binding),
            Self::SourceMatch(parsed) => MemberSelectorSpecRef::SourceMatch(parsed.selector()),
            Self::CrossRef(target) => MemberSelectorSpecRef::CrossRef(target),
            Self::ReadsMember(target) => MemberSelectorSpecRef::ReadsMember(target),
            Self::MemberOfModule(target) => MemberSelectorSpecRef::MemberOfModule(target),
            Self::PassedToCall(target) => MemberSelectorSpecRef::PassedToCall(target),
            Self::MakesDecorateCall(target) => MemberSelectorSpecRef::MakesDecorateCall(target),
            Self::IntrinsicAlias(target) => MemberSelectorSpecRef::IntrinsicAlias(target),
        }
    }
}

/// Every entity's outcome, in the order they were decided: entities rejected
/// before the solve first, then anonymous statements, then members.
#[derive(Debug, Clone, Default)]
pub struct Resolution {
    pub outcomes: Vec<EntityOutcome>,
}

#[derive(Debug, Clone)]
pub struct EntityOutcome {
    /// Index into the resolved [`SpecModule`]s.
    pub module: usize,
    pub entity: EntityIndex,
    pub outcome: SelectorOutcome,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EntityIndex {
    Member(usize),
    AnonymousStatement(usize),
}

impl Resolution {
    /// The outcome of one entity; `None` for an import-specifier name pin.
    pub fn outcome(&self, module: usize, entity: EntityIndex) -> Option<&SelectorOutcome> {
        self.outcomes
            .iter()
            .find(|outcome| outcome.module == module && outcome.entity == entity)
            .map(|outcome| &outcome.outcome)
    }
}

/// The outcome record of member `export_name` of module `logical_module`.
pub fn member_outcome(
    chunk: &str,
    logical_module: &str,
    export_name: &str,
    selector: &MemberSelector,
    outcome: Outcome,
) -> SelectorOutcome {
    SelectorOutcome {
        chunk: chunk.to_string(),
        placement: Some(Placement {
            logical_module: logical_module.to_string(),
            entity: Some(Entity::Export(export_name.to_string())),
            selector_kind: selector.kind(),
        }),
        target_binding: selector
            .source_match()
            .and_then(|selector| selector.target_binding.clone()),
        selector_preview: Some(selector.preview()),
        outcome,
    }
}

/// A `source_match` projected into the solve: its targets (one, or every
/// binding of a `source_matches[]` group) and its candidate rows, each row the
/// places it would claim, one per target.
struct Projected {
    targets: Vec<SelectorTargetId>,
    rows: Vec<Vec<Place>>,
}

impl Projected {
    /// Without repeated rows: the matcher lists a place once per way the
    /// template aligns with it.
    fn deduped(mut self) -> Self {
        let mut rows = Vec::with_capacity(self.rows.len());
        for row in self.rows {
            if !rows.contains(&row) {
                rows.push(row);
            }
        }
        self.rows = rows;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct Place {
    owner: OwnerId,
    binding: Option<String>,
}

/// The members of one module that share a `source_match` template and each
/// claim one of its bindings.
#[derive(Debug, Clone)]
struct Group {
    parsed: ParsedSourceMatchSelector,
    exports_by_target: BTreeMap<String, String>,
    members_by_target: BTreeMap<String, usize>,
}

impl<'m> Chunk<'m> {
    /// `name` is how outcomes and the selector program name the chunk; `id`
    /// its interned id in the facts.
    pub fn new(
        name: impl Into<String>,
        id: ChunkId,
        module: &'m Module,
        structural: StructuralChunkAnalysis<'m>,
    ) -> Self {
        Self {
            name: name.into(),
            id,
            module,
            structural,
            matcher: OnceCell::new(),
            places: OnceCell::new(),
        }
    }

    /// A chunk read from a file, outside a pipeline run.
    pub fn analyze(name: impl Into<String>, module: &'m Module) -> Self {
        let structural = analysis::facts::analyze_chunk_structural(module, None, |_| None);
        Self::new(name, ChunkId(0), module, structural)
    }

    pub fn into_structural(self) -> StructuralChunkAnalysis<'m> {
        self.structural
    }

    /// The shape matcher, for callers that rank candidate selectors by how
    /// many places they match. Deciding what a selector resolves to is
    /// [`Chunk::resolve`]'s job.
    pub fn matcher(&self) -> &ChunkResolver<'m> {
        self.matcher.get_or_init(|| ChunkResolver::new(self.module))
    }

    fn program_builder(&self) -> MemberSelectorProgramBuilder {
        MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(self.id, &self.name))
    }

    fn places(&self) -> &Places {
        self.places.get_or_init(|| {
            let mut places = Places {
                owner_by_body: BTreeMap::new(),
                owner_by_body_and_binding: BTreeMap::new(),
            };
            for statement in &self.structural.per_statement {
                let Some(body_idx) =
                    body_index_for_statement_ordinal(&self.module.body, statement.ordinal.0)
                else {
                    continue;
                };
                let owner = OwnerId(statement.ordinal.0);
                places.owner_by_body.insert(body_idx, owner);
                for binding in &statement.declared {
                    places
                        .owner_by_body_and_binding
                        .insert((body_idx, binding.0.as_str().to_string()), owner);
                }
            }
            places
        })
    }

    /// [`resolve`] of this chunk alone.
    pub fn resolve(&self, modules: &[SpecModule]) -> Result<Resolution> {
        let [resolution] = <[_; 1]>::try_from(resolve(&[(self, modules)])?)
            .unwrap_or_else(|_| unreachable!("one chunk resolves to one resolution"));
        Ok(resolution)
    }

    /// The first half of [`resolve`] for this chunk: `modules`' entities and
    /// their candidate places, with every entity the shape matcher rejects
    /// already decided. Independent of every other chunk, so chunks can be
    /// projected in parallel.
    pub fn project(&self, modules: &[SpecModule]) -> Result<Projection> {
        Resolve::new(self, modules).project()
    }
}

/// Resolves the entities of every chunk as one program: one [`Resolution`]
/// per chunk, in order. Each module's entities are scoped to its chunk, so
/// no two entities of one chunk claim the same place, whichever modules they
/// belong to. A name pin on an import specifier gets no outcome; every other
/// entity gets exactly one.
pub fn resolve(chunks: &[(&Chunk<'_>, &[SpecModule])]) -> Result<Vec<Resolution>> {
    solve(
        chunks
            .iter()
            .map(|(chunk, modules)| Ok((*chunk, *modules, chunk.project(modules)?)))
            .collect::<Result<Vec<_>>>()?,
    )
}

/// The second half of [`resolve`]: assigns the projected entities of every
/// chunk. The program splits into groups of entities that interact: a
/// constraint relates them, or they may take the same place under
/// `all_different`. A group that is one `source_match` or anonymous
/// statement is decided from its own candidates, a lone name pin with one
/// place is a constant, and every other group is one CP-SAT request.
pub fn solve(chunks: Vec<(&Chunk<'_>, &[SpecModule], Projection)>) -> Result<Vec<Resolution>> {
    let mut decided = chunks
        .iter()
        .map(|_| SolverResult::default())
        .collect::<Vec<_>>();
    let mut requests = Vec::new();
    for (index, (chunk, _, projection)) in chunks.iter().enumerate() {
        for group in projection.interacting_groups() {
            match projection.decide_alone(chunk.id, &group) {
                Some(claims) => decided[index].claims.extend(claims),
                None => requests.push((index, group)),
            }
        }
    }
    let facts = chunks
        .iter()
        .enumerate()
        .map(|(index, (chunk, _, projection))| {
            requests
                .iter()
                .any(|(request, _)| *request == index)
                .then(|| selector_fact_store(&projection.program, chunk))
        })
        .collect::<Vec<_>>();
    let programs = chunks
        .iter()
        .map(|(_, _, projection)| &projection.program)
        .collect::<Vec<_>>();
    let solved = requests
        .par_iter()
        .map(|(index, group)| {
            let slice = programs[*index].slice_for_targets(
                group,
                SelectorProgramSliceOptions {
                    include_target_all_different: true,
                },
            )?;
            let facts = facts[*index]
                .as_ref()
                .expect("a chunk with a request has a fact store");
            let result = solve_global_selector_program(&slice.program, facts)?;
            Ok((
                *index,
                in_program_targets(result, &slice.new_to_old_targets),
            ))
        })
        .collect::<Vec<Result<_>>>();
    for solved in solved {
        let (index, result) = solved?;
        let decided = &mut decided[index];
        decided.claims.extend(result.claims);
        decided.global_diagnostic = decided
            .global_diagnostic
            .take()
            .or(result.global_diagnostic);
    }
    chunks
        .into_iter()
        .zip(decided)
        .map(|((chunk, modules, projection), result)| projection.record(chunk, modules, &result))
        .collect()
}

/// `result` of a program slice, with its targets named as in the whole
/// program.
fn in_program_targets(
    result: SolverResult,
    to_program: &BTreeMap<SelectorTargetId, SelectorTargetId>,
) -> SolverResult {
    let target = |target: SelectorTargetId| to_program[&target];
    SolverResult {
        claims: result
            .claims
            .into_iter()
            .map(|claim| SolverClaim {
                target: target(claim.target),
                outcome: match claim.outcome {
                    ClaimOutcome::Conflict { with } => ClaimOutcome::Conflict {
                        with: with.into_iter().map(target).collect(),
                    },
                    ClaimOutcome::Duplicate {
                        owner,
                        conflicting_targets,
                    } => ClaimOutcome::Duplicate {
                        owner,
                        conflicting_targets: conflicting_targets.into_iter().map(target).collect(),
                    },
                    outcome => outcome,
                },
            })
            .collect(),
        global_diagnostic: result.global_diagnostic,
    }
}

/// One chunk's entities projected onto its places by [`Chunk::project`].
pub struct Projection {
    /// `<chunk>::<path>` per module: the module's id in the selector program.
    ids: Vec<String>,
    program: SelectorProgram,
    /// Entities decided before the solve.
    outcomes: Vec<EntityOutcome>,
    /// Member targets, in declaration order.
    members: BTreeMap<SelectorTargetId, (usize, usize)>,
    /// Anonymous statement targets, by module and position in it.
    anonymous: Vec<(SelectorTargetId, usize, usize)>,
    projected: Vec<Projected>,
    /// Each name pin's places: the declarations of its name, of its kind.
    pin_places: BTreeMap<SelectorTargetId, BTreeSet<Place>>,
}

impl Projection {
    /// The program's targets, grouped so that no two groups interact: no
    /// atom relates them, and they can never take the same place. A target
    /// whose places are unknown before the solve (a relational selector)
    /// interacts with every target it is kept distinct from.
    fn interacting_groups(&self) -> Vec<BTreeSet<SelectorTargetId>> {
        let program = &self.program;
        let mut sets = UnionFind::new(program.variables.len());
        let variable_sets = program.atoms.iter().map(SelectorAtom::variable_ids).chain(
            program
                .all_different_variables
                .iter()
                .map(|constraint| constraint.variables.iter().copied().collect()),
        );
        for variables in variable_sets {
            if let Some(first) = variables.first() {
                for variable in &variables {
                    sets.union(*first, *variable);
                }
            }
        }
        let places_by_target = self
            .projected
            .iter()
            .flat_map(|entity| {
                let places = entity.rows.iter().flatten().collect::<BTreeSet<_>>();
                entity
                    .targets
                    .iter()
                    .map(move |target| (*target, places.clone()))
            })
            .chain(
                self.pin_places
                    .iter()
                    .map(|(target, places)| (*target, places.iter().collect())),
            )
            .collect::<BTreeMap<_, _>>();
        for distinct in &program.all_different {
            let owner = |target: &SelectorTargetId| program.targets[target.0].owner;
            let mut by_owner = BTreeMap::new();
            let mut by_binding = BTreeMap::new();
            for target in distinct {
                let Some(places) = places_by_target.get(target) else {
                    for other in distinct {
                        sets.union(owner(target), owner(other));
                    }
                    continue;
                };
                for place in places {
                    let first = *by_owner.entry(place.owner).or_insert(owner(target));
                    sets.union(first, owner(target));
                    if let Some(binding) = &place.binding {
                        let first = *by_binding.entry(binding).or_insert(owner(target));
                        sets.union(first, owner(target));
                    }
                }
            }
        }
        let mut groups = BTreeMap::<SelectorVariableId, BTreeSet<SelectorTargetId>>::new();
        for target in &program.targets {
            groups
                .entry(sets.find(target.owner))
                .or_default()
                .insert(target.id);
        }
        let mut groups = groups.into_values().collect::<Vec<_>>();
        groups.sort();
        groups
    }

    /// The claims of `group` when it can be decided without the solver: it
    /// is one `source_match` or anonymous statement, whose candidates are
    /// exactly its own rows, or one name pin with one place.
    fn decide_alone(
        &self,
        chunk: ChunkId,
        group: &BTreeSet<SelectorTargetId>,
    ) -> Option<Vec<SolverClaim>> {
        let claim = |place: &Place| ResolvedClaim {
            chunk_id: chunk,
            owner: place.owner,
            statement_ordinal: StatementOrdinal(place.owner.0),
            binding: place.binding.clone(),
            provenance: Vec::new(),
        };
        if let Some(entity) = self.projected.iter().find(|entity| {
            entity.targets.len() == group.len()
                && entity.targets.iter().all(|target| group.contains(target))
        }) {
            return Some(
                entity
                    .targets
                    .iter()
                    .enumerate()
                    .map(|(position, target)| {
                        // Rows of a multi-binding entity can agree on one
                        // binding's place and differ on another's; each
                        // target is decided by its own distinct places, as
                        // the solver's per-target support search does.
                        let places = entity
                            .rows
                            .iter()
                            .map(|row| &row[position])
                            .collect::<BTreeSet<_>>();
                        SolverClaim {
                            target: *target,
                            outcome: match places.len() {
                                1 => ClaimOutcome::Unique {
                                    claim: claim(places.first().expect("one place")),
                                },
                                _ => ClaimOutcome::Ambiguous {
                                    candidates: places.into_iter().map(claim).collect(),
                                    candidates_truncated: false,
                                },
                            },
                        }
                    })
                    .collect(),
            );
        }
        let [target] = <[_; 1]>::try_from(group.iter().copied().collect::<Vec<_>>()).ok()?;
        let places = self.pin_places.get(&target)?;
        let outcome = match places.len() {
            0 => ClaimOutcome::NoMatch,
            1 => ClaimOutcome::Unique {
                claim: claim(places.first().expect("one place")),
            },
            _ => return None,
        };
        Some(vec![SolverClaim { target, outcome }])
    }
}

/// Disjoint sets of selector variables.
struct UnionFind {
    parent: Vec<usize>,
}

impl UnionFind {
    fn new(len: usize) -> Self {
        Self {
            parent: (0..len).collect(),
        }
    }

    fn find(&mut self, variable: SelectorVariableId) -> SelectorVariableId {
        let mut root = variable.0;
        while self.parent[root] != root {
            root = self.parent[root];
        }
        let mut node = variable.0;
        while self.parent[node] != root {
            node = std::mem::replace(&mut self.parent[node], root);
        }
        SelectorVariableId(root)
    }

    fn union(&mut self, left: SelectorVariableId, right: SelectorVariableId) {
        let (left, right) = (self.find(left), self.find(right));
        self.parent[left.0.max(right.0)] = left.0.min(right.0);
    }
}

/// One [`Chunk::project`].
struct Resolve<'c, 'm> {
    chunk: &'c Chunk<'m>,
    modules: &'c [SpecModule],
    ids: Vec<String>,
    builder: MemberSelectorProgramBuilder,
    outcomes: Vec<EntityOutcome>,
    members: BTreeMap<SelectorTargetId, (usize, usize)>,
    anonymous: Vec<(SelectorTargetId, usize, usize)>,
    projected: Vec<Projected>,
}

impl<'c, 'm> Resolve<'c, 'm> {
    fn new(chunk: &'c Chunk<'m>, modules: &'c [SpecModule]) -> Self {
        Self {
            chunk,
            modules,
            ids: modules
                .iter()
                .map(|module| format!("{}::{}", chunk.name, module.path))
                .collect(),
            builder: chunk.program_builder(),
            outcomes: Vec::new(),
            members: BTreeMap::new(),
            anonymous: Vec::new(),
            projected: Vec::new(),
        }
    }

    fn project(mut self) -> Result<Projection> {
        let modules = self.modules;
        let mut groups = Vec::new();
        let mut source_matches = Vec::new();
        // Members with a name pin or a relational selector.
        let mut constrained = Vec::new();
        for (module_index, module) in modules.iter().enumerate() {
            let module_groups = source_match_groups(module);
            let grouped = module_groups
                .iter()
                .flat_map(|group| group.members_by_target.values().copied())
                .collect::<BTreeSet<_>>();
            groups.extend(module_groups.into_iter().map(|group| (module_index, group)));
            for (member_index, member) in module.members.iter().enumerate() {
                match &member.selector {
                    _ if grouped.contains(&member_index) => {}
                    MemberSelector::SourceMatch(_) => {
                        source_matches.push((module_index, member_index));
                    }
                    selector if selector.is_import_specifier() => {}
                    selector => {
                        let target = self.builder.declare_member_target_in_module_ref(
                            &self.ids[module_index],
                            &member.export_name,
                            selector.spec_ref(),
                        )?;
                        self.members.insert(target, (module_index, member_index));
                        constrained.push((module_index, member_index));
                    }
                }
            }
        }
        for (module_index, module) in modules.iter().enumerate() {
            for position in 0..module.anonymous_statements.len() {
                self.project_anonymous_statement(module_index, position);
            }
        }
        for (module_index, group) in groups {
            self.project_group(module_index, group)?;
        }
        for (module_index, member_index) in source_matches {
            self.project_member(module_index, member_index)?;
        }
        for (module_index, member_index) in constrained {
            let member = &modules[module_index].members[member_index];
            self.builder.lower_member_constraints_in_module_ref(
                &self.ids[module_index],
                &member.export_name,
                member.selector.spec_ref(),
            )?;
        }
        let builder = std::mem::replace(&mut self.builder, self.chunk.program_builder());
        let program = builder.into_program()?;
        let pin_places = self.pin_places();
        for (target, places) in &pin_places {
            if places.is_empty() {
                let (module_index, member_index) = self.members[target];
                self.push_member(module_index, member_index, Outcome::NoMatch);
            }
        }
        Ok(Projection {
            ids: self.ids,
            program,
            outcomes: self.outcomes,
            members: self.members,
            anonymous: self.anonymous,
            projected: self.projected,
            pin_places,
        })
    }

    fn push_member(&mut self, module_index: usize, member_index: usize, outcome: Outcome) {
        self.outcomes.push(member_entity_outcome(
            &self.chunk.name,
            self.modules,
            module_index,
            member_index,
            outcome,
        ));
    }

    fn push_anonymous(&mut self, module_index: usize, position: usize, outcome: Outcome) {
        self.outcomes.push(anonymous_entity_outcome(
            &self.chunk.name,
            self.modules,
            module_index,
            position,
            outcome,
        ));
    }

    fn project_anonymous_statement(&mut self, module_index: usize, position: usize) {
        let modules = self.modules;
        let statement = &modules[module_index].anonymous_statements[position];
        let parsed = &statement.selector;
        let logical_module = self.ids[module_index].clone();
        let logical_module = logical_module.as_str();
        let chunk = self.chunk;
        let places = chunk.places();
        let projection = chunk
            .matcher()
            .anonymous_group_candidates_parsed(logical_module, parsed)
            .map_err(|error| rejection(RejectReason::MatcherError, &error, None))
            .and_then(|candidates| {
                let candidate_count = candidates.len();
                candidates
                    .into_iter()
                    .map(|group| {
                        let [body_idx] = group.as_slice() else {
                            anyhow::bail!(
                                "anonymous source_match candidate group has {} statements; \
                                 projected lowering currently supports one statement per \
                                 anonymous claim",
                                group.len()
                            );
                        };
                        places.owner_by_body.get(body_idx).copied().with_context(|| {
                            format!(
                                "anonymous source_match candidate at body index {body_idx} does \
                                 not map to an owner-graph node",
                            )
                        })
                    })
                    .collect::<Result<Vec<_>>>()
                    .map_err(|error| {
                        rejection(RejectReason::OwnerMapping, &error, Some(candidate_count))
                    })
                    .map(|rows| (candidate_count, rows))
            });
        let event =
            |outcome, category: &str, reason: String, counts: (Option<usize>, Option<usize>)| {
                projection_event(
                    logical_module,
                    "anonymous_statements.source_match",
                    None,
                    BTreeMap::new(),
                    parsed.selector(),
                    outcome,
                    category,
                    reason,
                    counts,
                )
            };
        let rejected = match projection {
            Ok((candidate_count, rows)) if rows.len() > MAX_CANDIDATES_PER_SELECTOR => Rejection {
                reason: RejectReason::TooBroad,
                message: too_broad_reason(rows.len()),
                candidate_count: Some(candidate_count),
                row_count: Some(rows.len()),
            },
            Ok((candidate_count, rows)) if !rows.is_empty() => {
                self.builder.record_source_match_projection_event(event(
                    SelectorSourceMatchProjectionOutcome::Projected,
                    "projected_candidates",
                    "projected anonymous statement candidates into owner rows".to_string(),
                    (Some(candidate_count), Some(rows.len())),
                ));
                let target = self
                    .builder
                    .declare_projected_anonymous_statement_target_in_module(
                        logical_module,
                        statement.index,
                        rows.clone(),
                    );
                self.anonymous.push((target, module_index, position));
                self.projected.push(
                    Projected {
                        targets: vec![target],
                        rows: rows
                            .into_iter()
                            .map(|owner| {
                                vec![Place {
                                    owner,
                                    binding: None,
                                }]
                            })
                            .collect(),
                    }
                    .deduped(),
                );
                return;
            }
            Ok((candidate_count, _)) => Rejection {
                reason: RejectReason::NoCandidates,
                message: "shape matcher returned no anonymous candidates".to_string(),
                candidate_count: Some(candidate_count),
                row_count: Some(0),
            },
            Err(rejection) => rejection,
        };
        self.builder.record_source_match_projection_event(event(
            SelectorSourceMatchProjectionOutcome::NotProjected,
            rejected.reason.category(),
            rejected.message.clone(),
            (rejected.candidate_count, rejected.row_count),
        ));
        self.push_anonymous(module_index, position, rejected.outcome());
    }

    fn project_group(&mut self, module_index: usize, group: Group) -> Result<()> {
        let modules = self.modules;
        let logical_module = self.ids[module_index].clone();
        let chunk = self.chunk;
        let places = chunk.places();
        let projection = chunk
            .matcher()
            .member_group_candidates_parsed(
                &logical_module,
                &group.parsed,
                &group.exports_by_target,
            )
            .map_err(|error| rejection(RejectReason::MatcherError, &error, None))
            .and_then(|candidates| {
                let candidate_count = candidates.len();
                candidates
                    .into_iter()
                    .map(|candidate| {
                        candidate
                            .bindings
                            .iter()
                            .map(|(target_binding, matched)| {
                                member_place(places, matched)
                                    .map(|place| (target_binding.clone(), place))
                            })
                            .collect::<Result<BTreeMap<_, _>>>()
                    })
                    .collect::<Result<Vec<_>>>()
                    .map_err(|error| {
                        rejection(RejectReason::OwnerMapping, &error, Some(candidate_count))
                    })
                    .map(|rows| (candidate_count, rows))
            });
        let event =
            |outcome, category: &str, reason: String, counts: (Option<usize>, Option<usize>)| {
                projection_event(
                    &logical_module,
                    "source_matches",
                    None,
                    group.exports_by_target.clone(),
                    group.parsed.selector(),
                    outcome,
                    category,
                    reason,
                    counts,
                )
            };
        let rejected = match projection {
            Ok((candidate_count, rows)) if rows.len() > MAX_CANDIDATES_PER_SELECTOR => Rejection {
                reason: RejectReason::TooBroad,
                message: too_broad_reason(rows.len()),
                candidate_count: Some(candidate_count),
                row_count: Some(rows.len()),
            },
            Ok((candidate_count, rows)) if !rows.is_empty() => {
                let mut targets = Vec::new();
                for (target_binding, member_index) in &group.members_by_target {
                    let member = &modules[module_index].members[*member_index];
                    let target = self
                        .builder
                        .declare_binding_group_member_target_in_module_ref(
                            &logical_module,
                            &member.export_name,
                            target_binding,
                            member.selector.spec_ref(),
                        )?;
                    self.members.insert(target, (module_index, *member_index));
                    targets.push(target);
                }
                self.builder.record_source_match_projection_event(event(
                    SelectorSourceMatchProjectionOutcome::Projected,
                    "projected_candidates",
                    format!(
                        "projected {candidate_count} shape-matcher candidate group(s) to {} \
                         owner/binding row(s)",
                        rows.len()
                    ),
                    (Some(candidate_count), Some(rows.len())),
                ));
                self.projected.push(
                    Projected {
                        targets,
                        rows: rows
                            .iter()
                            .map(|row| {
                                row.values()
                                    .map(|(owner, binding)| Place {
                                        owner: *owner,
                                        binding: Some(binding.clone()),
                                    })
                                    .collect()
                            })
                            .collect(),
                    }
                    .deduped(),
                );
                self.builder.lower_projected_source_match_group_candidates(
                    &logical_module,
                    &group.exports_by_target,
                    rows,
                );
                return Ok(());
            }
            Ok((candidate_count, _)) => Rejection {
                reason: RejectReason::NoCandidates,
                message: "shape matcher returned no candidate groups".to_string(),
                candidate_count: Some(candidate_count),
                row_count: Some(0),
            },
            Err(rejection) => rejection,
        };
        self.builder.record_source_match_projection_event(event(
            SelectorSourceMatchProjectionOutcome::NotProjected,
            rejected.reason.category(),
            rejected.message.clone(),
            (rejected.candidate_count, rejected.row_count),
        ));
        let outcome = rejected.outcome();
        for member_index in group.members_by_target.values() {
            self.push_member(module_index, *member_index, outcome.clone());
        }
        Ok(())
    }

    fn project_member(&mut self, module_index: usize, member_index: usize) -> Result<()> {
        let modules = self.modules;
        let logical_module = self.ids[module_index].clone();
        let member = &modules[module_index].members[member_index];
        let MemberSelector::SourceMatch(parsed) = &member.selector else {
            unreachable!("only source_match members are projected");
        };
        let chunk = self.chunk;
        let places = chunk.places();
        let projection = chunk
            .matcher()
            .member_candidates_parsed(&logical_module, parsed)
            .map_err(|error| rejection(RejectReason::MatcherError, &error, None))
            .and_then(|candidates| {
                let candidate_count = candidates.len();
                candidates
                    .iter()
                    .map(|matched| member_place(places, matched))
                    .collect::<Result<Vec<_>>>()
                    .map_err(|error| {
                        rejection(RejectReason::OwnerMapping, &error, Some(candidate_count))
                    })
                    .map(|rows| (candidate_count, rows))
            });
        let event =
            |outcome, category: &str, reason: String, counts: (Option<usize>, Option<usize>)| {
                projection_event(
                    &logical_module,
                    "source_matches",
                    Some(&member.export_name),
                    BTreeMap::new(),
                    parsed.selector(),
                    outcome,
                    category,
                    reason,
                    counts,
                )
            };
        let rejected = match projection {
            Ok((candidate_count, rows)) if rows.len() > MAX_CANDIDATES_PER_SELECTOR => Rejection {
                reason: RejectReason::TooBroad,
                message: too_broad_reason(rows.len()),
                candidate_count: Some(candidate_count),
                row_count: Some(rows.len()),
            },
            Ok((candidate_count, rows)) if !rows.is_empty() => {
                self.builder.record_source_match_projection_event(event(
                    SelectorSourceMatchProjectionOutcome::Projected,
                    "projected_candidates",
                    format!(
                        "projected {candidate_count} shape-matcher candidate(s) to {} \
                         owner/binding row(s)",
                        rows.len()
                    ),
                    (Some(candidate_count), Some(rows.len())),
                ));
                let target = self.builder.declare_member_target_in_module_ref(
                    &logical_module,
                    &member.export_name,
                    member.selector.spec_ref(),
                )?;
                self.projected.push(
                    Projected {
                        targets: vec![target],
                        rows: rows
                            .iter()
                            .map(|(owner, binding)| {
                                vec![Place {
                                    owner: *owner,
                                    binding: Some(binding.clone()),
                                }]
                            })
                            .collect(),
                    }
                    .deduped(),
                );
                self.builder.lower_projected_source_match_candidates(
                    &logical_module,
                    &member.export_name,
                    rows,
                );
                self.members.insert(target, (module_index, member_index));
                return Ok(());
            }
            Ok((candidate_count, _)) => Rejection {
                reason: RejectReason::NoCandidates,
                message: "shape matcher returned no candidates".to_string(),
                candidate_count: Some(candidate_count),
                row_count: Some(0),
            },
            Err(rejection) => rejection,
        };
        self.builder.record_source_match_projection_event(event(
            SelectorSourceMatchProjectionOutcome::NotProjected,
            rejected.reason.category(),
            rejected.message.clone(),
            (
                rejected.candidate_count,
                Some(rejected.row_count.unwrap_or(0)),
            ),
        ));
        self.push_member(module_index, member_index, rejected.outcome());
        Ok(())
    }

    /// Each name pin's places: the top-level statements declaring its name,
    /// of its kind when it names one.
    fn pin_places(&self) -> BTreeMap<SelectorTargetId, BTreeSet<Place>> {
        let mut declarations = BTreeMap::<&str, Vec<(OwnerId, StatementKind)>>::new();
        for statement in &self.chunk.structural.per_statement {
            for binding in &statement.declared {
                declarations
                    .entry(binding.0.as_str())
                    .or_default()
                    .push((OwnerId(statement.ordinal.0), statement.kind));
            }
        }
        self.members
            .iter()
            .filter_map(|(target, (module_index, member_index))| {
                let MemberSelector::Binding(pin) =
                    &self.modules[*module_index].members[*member_index].selector
                else {
                    return None;
                };
                let places = declarations
                    .get(pin.name.as_str())
                    .into_iter()
                    .flatten()
                    .filter(|(_, kind)| {
                        pin.kind
                            .is_none_or(|pinned| statement_kind_for_spec(pinned) == *kind)
                    })
                    .map(|(owner, _)| Place {
                        owner: *owner,
                        binding: Some(pin.name.clone()),
                    })
                    .collect();
                Some((*target, places))
            })
            .collect()
    }
}

/// The outcome record of member `member_index` of `modules[module_index]`.
fn member_entity_outcome(
    chunk: &str,
    modules: &[SpecModule],
    module_index: usize,
    member_index: usize,
    outcome: Outcome,
) -> EntityOutcome {
    let module = &modules[module_index];
    let member = &module.members[member_index];
    EntityOutcome {
        module: module_index,
        entity: EntityIndex::Member(member_index),
        outcome: member_outcome(
            chunk,
            &module.path,
            &member.export_name,
            &member.selector,
            outcome,
        ),
    }
}

/// The outcome record of `modules[module_index].anonymous_statements[position]`.
fn anonymous_entity_outcome(
    chunk: &str,
    modules: &[SpecModule],
    module_index: usize,
    position: usize,
    outcome: Outcome,
) -> EntityOutcome {
    let module = &modules[module_index];
    let statement = &module.anonymous_statements[position];
    EntityOutcome {
        module: module_index,
        entity: EntityIndex::AnonymousStatement(statement.index),
        outcome: SelectorOutcome {
            chunk: chunk.to_string(),
            placement: Some(Placement {
                logical_module: module.path.clone(),
                entity: Some(Entity::AnonymousStatement(statement.index)),
                selector_kind: SelectorKind::AnonymousStatement,
            }),
            target_binding: None,
            selector_preview: Some(source_match::source_match_preview(
                &statement.selector.selector().match_source,
            )),
            outcome,
        },
    }
}

impl Projection {
    /// Every entity's outcome, given the claims `result` holds for each
    /// target of the program.
    fn record(
        self,
        chunk: &Chunk<'_>,
        modules: &[SpecModule],
        result: &SolverResult,
    ) -> Result<Resolution> {
        let eliminated = self.eliminations(result);
        let Self {
            ids,
            program,
            mut outcomes,
            members,
            anonymous,
            pin_places,
            ..
        } = self;
        let module = chunk.module;
        for (target, module_index, position) in anonymous {
            let outcome = match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => Outcome::Resolved {
                    owner: claim_candidate(module, claim)?.owner,
                    binding: None,
                    resolved_by: ResolvedBy::OwnSelector,
                },
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => bail!(
                    "logical_module {}: global selector solver assigned anonymous statement to \
                     duplicate owner {owner:?} shared by targets {conflicting_targets:?}",
                    ids[module_index],
                ),
                Some(outcome) => claim_outcome(module, &program, outcome)?,
                None => bail!(
                    "logical_module {}: global selector solver returned no outcome for anonymous \
                     statement selector",
                    ids[module_index],
                ),
            };
            outcomes.push(anonymous_entity_outcome(
                &chunk.name,
                modules,
                module_index,
                position,
                outcome,
            ));
        }
        for (target, (module_index, member_index)) in members {
            // Recorded `no_match` by the projection.
            if pin_places.get(&target).is_some_and(BTreeSet::is_empty) {
                continue;
            }
            let member = &modules[module_index].members[member_index];
            let outcome = match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    let Candidate { owner, binding } = claim_candidate(module, claim)?;
                    Outcome::Resolved {
                        owner,
                        binding,
                        resolved_by: match eliminated.get(&target) {
                            Some(claimers) => ResolvedBy::Elimination {
                                claimers: claimers.clone(),
                            },
                            None => ResolvedBy::OwnSelector,
                        },
                    }
                }
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => bail!(
                    "logical_module {}: global selector solver assigned selector member `{}` to \
                     duplicate owner {owner:?} shared by targets {conflicting_targets:?}",
                    ids[module_index],
                    member.export_name,
                ),
                Some(outcome) => claim_outcome(module, &program, outcome)?,
                None => bail!(
                    "logical_module {}: global selector solver returned no outcome for selector \
                     member `{}`",
                    ids[module_index],
                    member.export_name,
                ),
            };
            outcomes.push(member_entity_outcome(
                &chunk.name,
                modules,
                module_index,
                member_index,
                outcome,
            ));
        }
        Ok(Resolution { outcomes })
    }

    /// The claimers of every target resolved by elimination: a projected
    /// entity with several candidate rows of which exactly one survives
    /// dropping every row whose owner or binding another exclusive target's
    /// solved value holds.
    fn eliminations(&self, result: &SolverResult) -> BTreeMap<SelectorTargetId, Vec<EntityRef>> {
        let mut exclusive = self
            .program
            .all_different
            .iter()
            .flatten()
            .copied()
            .collect::<BTreeSet<_>>();
        // `all_different` names one representative per `source_matches[]`
        // group; the group's other bindings are claimed with it.
        for entity in &self.projected {
            if entity
                .targets
                .iter()
                .any(|target| exclusive.contains(target))
            {
                exclusive.extend(entity.targets.iter().copied());
            }
        }
        let mut eliminated = BTreeMap::new();
        for entity in &self.projected {
            if let Some(claimers) = elimination_claimers(entity, result, &exclusive) {
                let claimers = target_entity_refs(&self.program, &claimers);
                for target in &entity.targets {
                    eliminated.insert(*target, claimers.clone());
                }
            }
        }
        eliminated
    }
}

/// Why a `source_match` was rejected before the solve.
#[derive(Debug, Clone, Copy)]
enum RejectReason {
    NoCandidates,
    TooBroad,
    MatcherError,
    OwnerMapping,
}

impl RejectReason {
    /// The projection event's `reason_category`.
    fn category(self) -> &'static str {
        match self {
            Self::NoCandidates => "shape_matcher_no_candidates",
            Self::TooBroad => "too_broad",
            Self::MatcherError => "shape_matcher_error",
            Self::OwnerMapping => "projection_owner_mapping_error",
        }
    }
}

struct Rejection {
    reason: RejectReason,
    message: String,
    candidate_count: Option<usize>,
    row_count: Option<usize>,
}

impl Rejection {
    fn outcome(&self) -> Outcome {
        match (self.reason, self.row_count) {
            (RejectReason::NoCandidates, _) => Outcome::NoMatch,
            (RejectReason::TooBroad, Some(count)) => Outcome::too_broad(count),
            _ => Outcome::Invalid {
                error: self.message.clone(),
            },
        }
    }
}

fn rejection(
    reason: RejectReason,
    error: &anyhow::Error,
    candidate_count: Option<usize>,
) -> Rejection {
    let message = error.to_string();
    let first_line = message
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or(&message);
    Rejection {
        reason,
        message: format!("{}: {first_line}", reason.category()),
        candidate_count,
        row_count: None,
    }
}

fn too_broad_reason(row_count: usize) -> String {
    format!("{row_count} candidate rows exceed the cap of {MAX_CANDIDATES_PER_SELECTOR}")
}

#[allow(clippy::too_many_arguments)]
fn projection_event(
    logical_module: &str,
    selector_kind: &str,
    export_name: Option<&str>,
    exports_by_target: BTreeMap<String, String>,
    selector: &AnonymousStatementSelector,
    outcome: SelectorSourceMatchProjectionOutcome,
    reason_category: &str,
    reason: String,
    (candidate_count, projected_row_count): (Option<usize>, Option<usize>),
) -> SelectorSourceMatchProjectionEvent {
    SelectorSourceMatchProjectionEvent {
        selector_kind: selector_kind.to_string(),
        logical_module: logical_module.to_string(),
        export_name: export_name.map(ToString::to_string),
        target_binding: selector.target_binding.clone(),
        exports_by_target,
        outcome,
        reason_category: reason_category.to_string(),
        reason,
        candidate_count,
        projected_row_count,
        selector_preview: source_match::source_match_preview(&selector.match_source),
        selector_hash: source_match::selector_key(selector),
        selector_body_hash: source_match::selector_body_key(selector),
    }
}

fn member_place(
    places: &Places,
    matched: &source_match::MemberBindingMatch,
) -> Result<(OwnerId, String)> {
    let binding = matched.binding.binding_name.clone();
    let owner = places
        .owner_by_body_and_binding
        .get(&(matched.body_idx, binding.clone()))
        .copied()
        .with_context(|| {
            format!(
                "source_match candidate at body index {} binding `{binding}` does not map to an \
                 owner-graph node",
                matched.body_idx
            )
        })?;
    Ok((owner, binding))
}

/// A module's `source_match` members grouped by shared template: members
/// whose templates differ only in their `target_binding`, when there are at
/// least two and no two claim the same binding.
fn source_match_groups(module: &SpecModule) -> Vec<Group> {
    let mut by_template = BTreeMap::<AnonymousStatementSelector, Vec<usize>>::new();
    for (index, member) in module.members.iter().enumerate() {
        let Some(selector) = member.selector.source_match() else {
            continue;
        };
        if selector.target_binding.is_none() {
            continue;
        }
        let mut template = selector.clone();
        template.target_binding = None;
        by_template.entry(template).or_default().push(index);
    }
    let mut groups = by_template
        .into_values()
        .filter(|indices| indices.len() >= 2)
        .filter_map(|indices| {
            let mut exports_by_target = BTreeMap::new();
            let mut members_by_target = BTreeMap::new();
            for index in &indices {
                let member = &module.members[*index];
                let target = member
                    .selector
                    .source_match()
                    .and_then(|selector| selector.target_binding.clone())
                    .expect("grouped selectors have a target_binding");
                if exports_by_target
                    .insert(target.clone(), member.export_name.clone())
                    .is_some()
                    || members_by_target.insert(target, *index).is_some()
                {
                    return None;
                }
            }
            let MemberSelector::SourceMatch(parsed) = &module.members[indices[0]].selector else {
                unreachable!("grouped members are source_match members");
            };
            Some((
                indices[0],
                Group {
                    parsed: parsed.with_target_binding(None),
                    exports_by_target,
                    members_by_target,
                },
            ))
        })
        .collect::<Vec<_>>();
    groups.sort_by_key(|(first, _)| *first);
    groups.into_iter().map(|(_, group)| group).collect()
}

/// The targets whose solved claims made `entity` unique: `Some` when it had
/// several candidate rows and exactly one survives dropping every row whose
/// owner or binding another exclusive target's solved value holds. `exclusive`
/// are the targets the solve keeps on distinct owners; a claim by any other
/// target never took a row away.
fn elimination_claimers(
    entity: &Projected,
    result: &SolverResult,
    exclusive: &BTreeSet<SelectorTargetId>,
) -> Option<BTreeSet<SelectorTargetId>> {
    if entity.rows.len() < 2
        || !entity.targets.iter().all(|target| {
            matches!(
                result.outcome_for(*target),
                Some(ClaimOutcome::Unique { .. })
            )
        })
    {
        return None;
    }
    let other_claims = exclusive
        .iter()
        .filter(|target| !entity.targets.contains(target))
        .filter_map(|target| match result.outcome_for(*target) {
            Some(ClaimOutcome::Unique { claim }) => Some((*target, claim)),
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut survivors = 0;
    let mut claimers = BTreeSet::new();
    for row in &entity.rows {
        let takers = other_claims
            .iter()
            .filter(|(_, claim)| {
                row.iter().any(|place| {
                    place.owner == claim.owner
                        || (place.binding.is_some() && place.binding == claim.binding)
                })
            })
            .map(|(target, _)| *target)
            .collect::<Vec<_>>();
        if takers.is_empty() {
            survivors += 1;
        }
        claimers.extend(takers);
    }
    (survivors == 1).then_some(claimers)
}

/// A solver claim as a candidate place: its source body index and binding.
fn claim_candidate(module: &Module, claim: &ResolvedClaim) -> Result<Candidate> {
    Ok(Candidate {
        owner: body_index_for_statement_ordinal(&module.body, claim.statement_ordinal.0)
            .with_context(|| {
                format!(
                    "global selector solver claimed post-split ordinal {} which has no source \
                     body item",
                    claim.statement_ordinal.0
                )
            })?,
        binding: claim.binding.clone(),
    })
}

/// The outcome of a target the solve did not resolve.
fn claim_outcome(
    module: &Module,
    program: &SelectorProgram,
    outcome: &ClaimOutcome,
) -> Result<Outcome> {
    Ok(match outcome {
        ClaimOutcome::NoMatch => Outcome::NoMatch,
        ClaimOutcome::Conflict { with } => Outcome::Conflict {
            with: target_entity_refs(program, with),
        },
        ClaimOutcome::Ambiguous {
            candidates,
            candidates_truncated,
        } => Outcome::ambiguous(
            candidates
                .iter()
                .map(|claim| claim_candidate(module, claim))
                .collect::<Result<_>>()?,
            *candidates_truncated,
        ),
        ClaimOutcome::Undecided { reason } => Outcome::Undecided {
            reason: reason.clone(),
        },
        ClaimOutcome::Unique { .. } | ClaimOutcome::Duplicate { .. } => {
            unreachable!("resolved and duplicate claims are handled by the caller")
        }
    })
}

/// The module path of a `<chunk>::<path>` logical module id.
fn logical_module_path(id: &str) -> String {
    id.split_once("::")
        .map(|(_, path)| path.to_string())
        .unwrap_or_else(|| panic!("logical module id {id:?} is not `<chunk>::<path>`"))
}

/// Names a solver target the way its own outcome names it.
fn target_entity_ref(target: &selector_ir::SelectorTarget) -> EntityRef {
    EntityRef {
        logical_module: logical_module_path(&target.logical_module),
        entity: match (&target.claim, &target.origin) {
            (
                selector_ir::ClaimKind::Binding {
                    export_name: Some(export_name),
                },
                _,
            )
            | (selector_ir::ClaimKind::BindingGroupMember { export_name, .. }, _) => {
                Some(Entity::Export(export_name.clone()))
            }
            (_, selector_ir::ClaimOrigin::AnonymousStatement { index }) => {
                Some(Entity::AnonymousStatement(*index))
            }
            _ => None,
        },
    }
}

fn target_entity_refs<'a>(
    program: &SelectorProgram,
    targets: impl IntoIterator<Item = &'a SelectorTargetId>,
) -> Vec<EntityRef> {
    targets
        .into_iter()
        .map(|target| target_entity_ref(&program.targets[target.0]))
        .collect()
}

fn statement_kind_for_spec(kind: BindingSourceKind) -> StatementKind {
    match kind {
        BindingSourceKind::ImportSpecifier => StatementKind::Import,
        BindingSourceKind::VariableDeclarator => StatementKind::VarDecl,
        BindingSourceKind::FunctionDeclaration => StatementKind::FnDecl,
        BindingSourceKind::ClassDeclaration => StatementKind::ClassDecl,
    }
}

/// Local import binding → module specifier, for every import in the chunk.
fn import_sources(module: &Module) -> HashMap<String, String> {
    module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => Some(import),
            _ => None,
        })
        .flat_map(|import| {
            let source = js_ast::str_value(&import.src);
            import.specifiers.iter().map(move |specifier| {
                let local = match specifier {
                    ImportSpecifier::Named(named) => &named.local,
                    ImportSpecifier::Default(default) => &default.local,
                    ImportSpecifier::Namespace(namespace) => &namespace.local,
                };
                (local.sym.to_string(), source.clone())
            })
        })
        .collect()
}

/// The chunk facts `program`'s tables are built from: every statement's owner,
/// kind and declared bindings, its references to other statements' bindings,
/// and the relation facts only the relational selectors in `program` read.
fn selector_fact_store(program: &SelectorProgram, chunk: &Chunk<'_>) -> SelectorFactStore {
    let chunk_id = chunk.id;
    let structural = &chunk.structural;
    let module = chunk.module;
    let mut store = SelectorFactStore::default();
    let binding_owner = structural
        .per_statement
        .iter()
        .flat_map(|statement| {
            statement
                .declared
                .iter()
                .map(|binding| (binding.clone(), OwnerId(statement.ordinal.0)))
        })
        .collect::<HashMap<_, _>>();
    let kind_by_owner = structural
        .per_statement
        .iter()
        .map(|statement| (OwnerId(statement.ordinal.0), statement.kind))
        .collect::<HashMap<_, _>>();
    let is_hoisted = |id: &swc_ecma_ast::Id| {
        binding_owner
            .get(id)
            .and_then(|owner| kind_by_owner.get(owner))
            .is_some_and(|kind| *kind == StatementKind::FnDecl)
    };

    for statement in &structural.per_statement {
        let owner = OwnerId(statement.ordinal.0);
        store.push(SelectorFact::Owner {
            chunk_id,
            owner,
            statement_ordinal: statement.ordinal,
            statement_kind: statement.kind.to_string(),
        });
        for binding in &statement.declared {
            store.push(SelectorFact::DeclaredBinding {
                chunk_id,
                owner,
                binding: binding.0.as_str().to_string(),
            });
        }
    }

    for statement in &structural.per_statement {
        let owner = OwnerId(statement.ordinal.0);
        let references = statement
            .reads
            .eager
            .iter()
            .filter(|binding| !is_hoisted(binding))
            .map(|binding| (binding, DepKind::EagerUse))
            .chain(
                statement
                    .reads
                    .lazy
                    .iter()
                    .map(|binding| (binding, DepKind::LazyUse)),
            )
            .chain(
                statement
                    .rebinds
                    .eager
                    .iter()
                    .map(|binding| (binding, DepKind::EagerRebind)),
            )
            .chain(
                statement
                    .rebinds
                    .first_order_lazy
                    .iter()
                    .map(|binding| (binding, DepKind::LazyRebind)),
            )
            .chain(
                statement
                    .rebinds
                    .lazy
                    .iter()
                    .filter(|binding| !statement.rebinds.first_order_lazy.contains(*binding))
                    .map(|binding| (binding, DepKind::DeferredRebind)),
            );
        for (binding, edge_kind) in references {
            if binding_owner
                .get(binding)
                .is_some_and(|target_owner| *target_owner != owner)
            {
                store.push(SelectorFact::OwnerReferencesBinding {
                    chunk_id,
                    owner,
                    binding: binding.0.as_str().to_string(),
                    edge_kind: edge_kind.to_string(),
                });
            }
        }
    }

    let reads = |atom: &SelectorAtom| {
        matches!(
            atom,
            SelectorAtom::ReadsMember { .. } | SelectorAtom::ReadsMemberOfOwner { .. }
        )
    };
    if program.atoms.iter().any(reads) {
        for (ordinal, member_reads) in chunk_facts::member_reads_by_ordinal(module) {
            for read in member_reads {
                store.push(SelectorFact::MemberRead {
                    chunk_id,
                    statement_ordinal: StatementOrdinal(ordinal),
                    object: read.object,
                    member: read.member,
                });
            }
        }
    }
    if program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::ConsumesModuleMember { .. }))
    {
        for (ordinal, uses) in
            chunk_facts::module_member_uses_by_ordinal(module, &import_sources(module))
        {
            for use_site in uses {
                store.push(SelectorFact::ModuleMemberUse {
                    chunk_id,
                    statement_ordinal: StatementOrdinal(ordinal),
                    module: use_site.module,
                    member: use_site.member,
                });
            }
        }
    }
    if program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::PassedToCall { .. } | SelectorAtom::PassedToCallOfOwner { .. }
        )
    }) {
        for call in chunk_facts::call_argument_uses(module) {
            store.push(SelectorFact::CallArgumentUse {
                chunk_id,
                argument: call.argument,
                callee_object: call.callee_object,
                callee_member: call.callee_member,
                arg_index: call.arg_index,
            });
        }
    }
    if program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::MakesDecorateCallForOwner { .. }))
    {
        for call in chunk_facts::decorate_call_uses(module) {
            store.push(SelectorFact::DecorateCallUse {
                chunk_id,
                callee: call.callee,
                class_anchor: call.class_anchor,
                member: call.member,
            });
        }
    }
    if program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::IntrinsicAlias { .. }))
    {
        for alias in chunk_facts::intrinsic_alias_uses(module) {
            store.push(SelectorFact::IntrinsicAliasUse {
                chunk_id,
                binding: alias.binding,
                property: alias.property,
            });
        }
    }
    store
}
