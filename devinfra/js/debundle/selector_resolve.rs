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
    SelectorProgramSliceOptions, SelectorTargetId, SelectorVariableId, SolverClaim, SolverResult,
};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, MemberSelectorSpecRef,
};
use selector_outcome::{
    Candidate, Differentiator, Entity, EntityRef, FreeIdentifier, IdentifierMeaning,
    MAX_CANDIDATES_PER_SELECTOR, NearMiss, Outcome, Placement, ResolvedBy, SelectorKind,
    SelectorOutcome, TemplateIdentifiers,
};
use selector_runtime::solve_global_selector_program;
use shape_index::ShapeIndex;
use source_match::ParsedSourceMatchSelector;
use source_match::chunk_resolver::{ChunkResolver, template_free_identifiers};
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
    /// Each top-level binding name's declaring statements, with their kind.
    declarations: BTreeMap<String, Vec<(OwnerId, StatementKind)>>,
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
    /// What each matched template's free identifiers mean,
    /// for templates that have any.
    pub templates: Vec<TemplateIdentifiers>,
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
    /// The entities its template names, each with the chunk identifier every
    /// row bound at that name.
    references: Vec<Reference>,
    /// Distinct rows its selector matched before any reference narrowed them.
    unreferenced_rows: usize,
}

/// A spec entity a template names, as a column of the naming entity's rows.
struct Reference {
    entity: EntityRef,
    /// The referenced entity's target; `None` for a name pin on an import,
    /// which is not resolved.
    target: Option<SelectorTargetId>,
    values: Vec<String>,
}

/// A `source_match` entity's candidates, collected before any target is
/// declared so that templates can name entities projected after them.
struct Collected {
    module_index: usize,
    shape: CollectedShape,
    /// The template's free identifiers.
    free: BTreeSet<String>,
    rows: Vec<CollectedRow>,
}

enum CollectedShape {
    Member(usize),
    Group(Group),
    /// An anonymous statement, by its position in the module's list.
    Anonymous(usize),
}

impl Collected {
    /// The export each row place is claimed as, in place order.
    fn export_names(&self, modules: &[SpecModule]) -> Vec<String> {
        match &self.shape {
            CollectedShape::Member(member_index) => {
                vec![
                    modules[self.module_index].members[*member_index]
                        .export_name
                        .clone(),
                ]
            }
            CollectedShape::Group(group) => group.exports_by_target.values().cloned().collect(),
            CollectedShape::Anonymous(_) => Vec::new(),
        }
    }

    fn member_indices(&self) -> Vec<usize> {
        match &self.shape {
            CollectedShape::Member(member_index) => vec![*member_index],
            CollectedShape::Group(group) => group.members_by_target.values().copied().collect(),
            CollectedShape::Anonymous(_) => Vec::new(),
        }
    }

    /// The entities it places, as outcomes name them.
    fn entities(&self, modules: &[SpecModule]) -> Vec<Entity> {
        match &self.shape {
            CollectedShape::Anonymous(position) => vec![Entity::AnonymousStatement(
                modules[self.module_index].anonymous_statements[*position].index,
            )],
            _ => self
                .export_names(modules)
                .into_iter()
                .map(Entity::Export)
                .collect(),
        }
    }
}

/// One candidate: a place per target, and what each free identifier bound.
#[derive(Clone)]
struct CollectedRow {
    /// One per target; a member's names its binding, an anonymous
    /// statement's none.
    places: Vec<Place>,
    free_bindings: BTreeMap<String, String>,
}

/// The spec entity a free template identifier names.
#[derive(Clone)]
enum Referent {
    /// A projected `source_match` entity, by its index in the collection.
    Projected(usize),
    /// A name pin: its binding is the pinned name.
    Pin {
        module_index: usize,
        member_index: usize,
        name: String,
    },
    /// A member pinned by a relational selector: its binding is known only
    /// in the solve.
    Relational {
        module_index: usize,
        member_index: usize,
    },
    /// A `source_match` rejected before the solve. Its name stays a wildcard.
    Unprojected,
}

/// Names that, unless the chunk declares or imports them at top level, are
/// the runtime's globals. A free template identifier spelled like one
/// matches only that spelling.
const JS_GLOBALS: &[&str] = &[
    "AbortController",
    "Array",
    "ArrayBuffer",
    "Atomics",
    "BigInt",
    "Blob",
    "Boolean",
    "Buffer",
    "DataView",
    "Date",
    "Error",
    "EvalError",
    "Event",
    "EventTarget",
    "FinalizationRegistry",
    "Float32Array",
    "Float64Array",
    "FormData",
    "Function",
    "Headers",
    "Infinity",
    "Int16Array",
    "Int32Array",
    "Int8Array",
    "Intl",
    "JSON",
    "Map",
    "Math",
    "NaN",
    "Number",
    "Object",
    "Promise",
    "Proxy",
    "RangeError",
    "ReferenceError",
    "Reflect",
    "RegExp",
    "Request",
    "Response",
    "Set",
    "SharedArrayBuffer",
    "String",
    "Symbol",
    "SyntaxError",
    "TextDecoder",
    "TextEncoder",
    "TypeError",
    "URIError",
    "URL",
    "URLSearchParams",
    "Uint16Array",
    "Uint32Array",
    "Uint8Array",
    "Uint8ClampedArray",
    "WeakMap",
    "WeakRef",
    "WeakSet",
    "WebAssembly",
    "atob",
    "btoa",
    "cancelAnimationFrame",
    "clearInterval",
    "clearTimeout",
    "console",
    "crypto",
    "decodeURI",
    "decodeURIComponent",
    "document",
    "encodeURI",
    "encodeURIComponent",
    "fetch",
    "globalThis",
    "isFinite",
    "isNaN",
    "localStorage",
    "location",
    "navigator",
    "parseFloat",
    "parseInt",
    "performance",
    "process",
    "queueMicrotask",
    "requestAnimationFrame",
    "sessionStorage",
    "setInterval",
    "setTimeout",
    "structuredClone",
    "undefined",
    "window",
];

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
                declarations: BTreeMap::new(),
            };
            for statement in &self.structural.per_statement {
                for binding in &statement.declared {
                    places
                        .declarations
                        .entry(binding.0.as_str().to_string())
                        .or_default()
                        .push((OwnerId(statement.ordinal.0), statement.kind));
                }
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
        .map(|((chunk, modules, projection), result)| {
            let mut resolution = projection.record(chunk, modules, &result)?;
            add_nearest_unclaimed(chunk, modules, &mut resolution)?;
            add_differentiators(chunk, &mut resolution);
            Ok(resolution)
        })
        .collect()
}

/// Near misses scoring below this are too far off to help a repair.
const NEAREST_UNCLAIMED_MIN_SCORE: usize = 30;
const NEAREST_UNCLAIMED_LIMIT: usize = 3;

/// Gives each `no_match` template entity the top-level statements, among
/// those no entity of the chunk claimed, that its template comes closest to.
fn add_nearest_unclaimed(
    chunk: &Chunk<'_>,
    modules: &[SpecModule],
    resolution: &mut Resolution,
) -> Result<()> {
    let claimed = resolution
        .outcomes
        .iter()
        .filter_map(|entity| match &entity.outcome.outcome {
            Outcome::Resolved { owner, .. } => Some(*owner),
            _ => None,
        })
        .collect::<BTreeSet<_>>();
    let unclaimed = (0..chunk.module.body.len())
        .filter(|body_idx| !claimed.contains(body_idx))
        .collect::<Vec<_>>();
    for entity in &mut resolution.outcomes {
        let Outcome::NoMatch { nearest_unclaimed } = &mut entity.outcome.outcome else {
            continue;
        };
        let module = &modules[entity.module];
        let template = match entity.entity {
            EntityIndex::Member(member_index) => match &module.members[member_index].selector {
                MemberSelector::SourceMatch(parsed) => Some(parsed),
                _ => None,
            },
            EntityIndex::AnonymousStatement(index) => module
                .anonymous_statements
                .iter()
                .find(|statement| statement.index == index)
                .map(|statement| &statement.selector),
        };
        let Some(template) = template else {
            continue;
        };
        *nearest_unclaimed = source_match::fact_near_misses(
            chunk.module,
            template,
            unclaimed.iter().copied(),
            NEAREST_UNCLAIMED_MIN_SCORE,
            NEAREST_UNCLAIMED_LIMIT,
        )?
        .into_iter()
        .map(|near_miss| NearMiss {
            owner: near_miss.body_idx,
            bindings: near_miss.declared_bindings,
            score: near_miss.score,
            reason: near_miss.reason,
        })
        .collect();
    }
    Ok(())
}

/// Gives each `ambiguous` entity whose candidates are all listed the anchor
/// that sets each candidate's statement apart from the other candidates':
/// its own best distinguishing feature, else one the statement just before it
/// has and the statements just before the others lack, else likewise after.
/// Candidates sharing a statement get none.
fn add_differentiators(chunk: &Chunk<'_>, resolution: &mut Resolution) {
    let body = &chunk.module.body;
    for entity in &mut resolution.outcomes {
        let Outcome::Ambiguous {
            candidates,
            truncated: false,
            differentiators,
        } = &mut entity.outcome.outcome
        else {
            continue;
        };
        let mut owners = BTreeSet::new();
        let mut shared = BTreeSet::new();
        for candidate in candidates.iter() {
            if !owners.insert(candidate.owner) {
                shared.insert(candidate.owner);
            }
        }
        let mut pending = owners.difference(&shared).copied().collect::<BTreeSet<_>>();
        for offset in [0, -1, 1] {
            if pending.is_empty() {
                break;
            }
            let statements = owners
                .iter()
                .filter_map(|&owner| {
                    let statement = owner
                        .checked_add_signed(offset)
                        .filter(|statement| *statement < body.len())?;
                    Some((owner, statement))
                })
                .collect::<Vec<_>>();
            let index = ShapeIndex::new(&Module {
                span: Default::default(),
                body: statements
                    .iter()
                    .map(|(_, statement)| body[*statement].clone())
                    .collect(),
                shebang: None,
            });
            for (item, &(owner, statement)) in statements.iter().enumerate() {
                if !pending.contains(&owner) {
                    continue;
                }
                if let Some(feature) = index.distinguishing_feature(item) {
                    pending.remove(&owner);
                    differentiators.push(Differentiator {
                        owner,
                        statement,
                        anchor: feature.to_string(),
                    });
                }
            }
        }
        differentiators.sort();
    }
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
    templates: Vec<TemplateIdentifiers>,
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
    templates: Vec<TemplateIdentifiers>,
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
            templates: Vec::new(),
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
        let mut collected = Vec::new();
        for (module_index, module) in modules.iter().enumerate() {
            for position in 0..module.anonymous_statements.len() {
                match self.collect_anonymous(module_index, position) {
                    Ok(entity) => collected.push(entity),
                    Err(rejected) => {
                        self.push_anonymous(module_index, position, rejected.outcome());
                    }
                }
            }
        }
        for (module_index, group) in groups {
            let members = group
                .members_by_target
                .values()
                .copied()
                .collect::<Vec<_>>();
            match self.collect_group(module_index, group) {
                Ok(entity) => collected.push(entity),
                Err(rejected) => {
                    let outcome = rejected.outcome();
                    for member_index in members {
                        self.push_member(module_index, member_index, outcome.clone());
                    }
                }
            }
        }
        for (module_index, member_index) in source_matches {
            match self.collect_member(module_index, member_index) {
                Ok(entity) => collected.push(entity),
                Err(rejected) => self.push_member(module_index, member_index, rejected.outcome()),
            }
        }
        self.project_collected(collected)?;
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
                self.push_member(module_index, member_index, Outcome::no_match());
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
            templates: self.templates,
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

    fn collect_anonymous(
        &self,
        module_index: usize,
        position: usize,
    ) -> Result<Collected, Rejection> {
        let statement = &self.modules[module_index].anonymous_statements[position];
        let places = self.chunk.places();
        let rows =
            self.chunk
                .matcher()
                .anonymous_group_candidates_parsed(&self.ids[module_index], &statement.selector)
                .map_err(|error| Rejection::invalid(MATCHER_ERROR, &error))?
                .into_iter()
                .map(|group| {
                    let [body_idx] = group.body_indices.as_slice() else {
                        bail!(
                            "anonymous source_match candidate group has {} statements; projected \
                         lowering currently supports one statement per anonymous claim",
                            group.body_indices.len()
                        );
                    };
                    let owner = places.owner_by_body.get(body_idx).copied().with_context(|| {
                    format!(
                        "anonymous source_match candidate at body index {body_idx} does not \
                         map to an owner-graph node",
                    )
                })?;
                    Ok(CollectedRow {
                        places: vec![Place {
                            owner,
                            binding: None,
                        }],
                        free_bindings: group.free_bindings,
                    })
                })
                .collect::<Result<Vec<_>>>()
                .map_err(|error| Rejection::invalid(OWNER_MAPPING_ERROR, &error))?;
        Ok(Collected {
            module_index,
            free: template_free_identifiers(&statement.selector),
            shape: CollectedShape::Anonymous(position),
            rows,
        })
    }

    fn collect_group(&self, module_index: usize, group: Group) -> Result<Collected, Rejection> {
        let (rows, free) =
            self.collect_rows(module_index, &group.parsed, &group.exports_by_target)?;
        Ok(Collected {
            module_index,
            free,
            shape: CollectedShape::Group(group),
            rows,
        })
    }

    /// The candidate rows of `template` (without a target binding) claiming
    /// the locals of `exports_by_target`, one place per local in key order,
    /// and the template's free identifiers that are not claimed. A declared
    /// local's place comes from the matcher. A free local pins by use site:
    /// its place is the top-level declaration its identifier binds to in
    /// that match, one row per declaring statement, and no row when it binds
    /// nothing declared at top level or different identifiers in different
    /// scopes.
    fn collect_rows(
        &self,
        module_index: usize,
        template: &ParsedSourceMatchSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<(Vec<CollectedRow>, BTreeSet<String>), Rejection> {
        let places = self.chunk.places();
        let matcher = self.chunk.matcher();
        let logical_module = &self.ids[module_index];
        let declared_names = template
            .declared_binding_names()
            .into_iter()
            .collect::<BTreeSet<_>>();
        let (declared, used): (BTreeMap<_, _>, BTreeMap<_, _>) = exports_by_target
            .iter()
            .map(|(local, export)| (local.clone(), export.clone()))
            .partition(|(local, _)| declared_names.contains(local));
        let matched = if declared.is_empty() {
            matcher
                .anonymous_group_candidates_parsed(logical_module, template)
                .map_err(|error| Rejection::invalid(MATCHER_ERROR, &error))?
                .into_iter()
                .map(|group| Ok((BTreeMap::new(), group.free_bindings)))
                .collect::<Result<Vec<_>>>()
        } else {
            matcher
                .member_group_candidates_parsed(logical_module, template, &declared)
                .map_err(|error| Rejection::invalid(MATCHER_ERROR, &error))?
                .into_iter()
                .map(|candidate| {
                    Ok((
                        candidate
                            .bindings
                            .iter()
                            .map(|(local, matched)| {
                                Ok((local.clone(), member_place(places, matched)?))
                            })
                            .collect::<Result<BTreeMap<_, _>>>()?,
                        candidate.free_bindings,
                    ))
                })
                .collect::<Result<Vec<_>>>()
        }
        .map_err(|error| Rejection::invalid(OWNER_MAPPING_ERROR, &error))?;
        let mut rows = Vec::new();
        for (declared_places, free_bindings) in matched {
            let mut partial = vec![declared_places];
            for local in used.keys() {
                let owners = free_bindings
                    .get(local)
                    .and_then(|name| places.declarations.get(name).map(|owners| (name, owners)))
                    .into_iter()
                    .flat_map(|(name, owners)| {
                        owners
                            .iter()
                            .filter(|(_, kind)| *kind != StatementKind::Import)
                            .map(move |(owner, _)| Place {
                                owner: *owner,
                                binding: Some(name.clone()),
                            })
                    })
                    .collect::<Vec<_>>();
                partial = partial
                    .into_iter()
                    .flat_map(|row| {
                        owners.iter().map(move |place| {
                            let mut row = row.clone();
                            row.insert(local.clone(), place.clone());
                            row
                        })
                    })
                    .collect();
            }
            rows.extend(partial.into_iter().map(|row| CollectedRow {
                places: row.into_values().collect(),
                free_bindings: free_bindings.clone(),
            }));
        }
        let free = template_free_identifiers(template)
            .into_iter()
            .filter(|name| !used.contains_key(name))
            .collect();
        Ok((rows, free))
    }

    fn collect_member(
        &self,
        module_index: usize,
        member_index: usize,
    ) -> Result<Collected, Rejection> {
        let member = &self.modules[module_index].members[member_index];
        let MemberSelector::SourceMatch(parsed) = &member.selector else {
            unreachable!("only source_match members are collected");
        };
        if let Some(local) = &parsed.selector().target_binding {
            let template = parsed.with_target_binding(None);
            if !template.declared_binding_names().contains(local) {
                let (rows, free) = self.collect_rows(
                    module_index,
                    &template,
                    &BTreeMap::from([(local.clone(), member.export_name.clone())]),
                )?;
                return Ok(Collected {
                    module_index,
                    free,
                    shape: CollectedShape::Member(member_index),
                    rows,
                });
            }
        }
        let places = self.chunk.places();
        let rows = self
            .chunk
            .matcher()
            .member_candidates_parsed(&self.ids[module_index], parsed)
            .map_err(|error| Rejection::invalid(MATCHER_ERROR, &error))?
            .into_iter()
            .map(|matched| {
                Ok(CollectedRow {
                    places: vec![member_place(places, &matched)?],
                    free_bindings: matched.free_bindings,
                })
            })
            .collect::<Result<Vec<_>>>()
            .map_err(|error| Rejection::invalid(OWNER_MAPPING_ERROR, &error))?;
        Ok(Collected {
            module_index,
            free: template_free_identifiers(parsed),
            shape: CollectedShape::Member(member_index),
            rows,
        })
    }

    /// Declares and lowers every collected entity whose rows survive its
    /// template's references: a free identifier naming an unshadowed global
    /// keeps only rows that bound that spelling, one naming a spec entity only
    /// rows that bound it consistently, and a name pin's only rows that bound
    /// the pinned name. A reference to another projected entity becomes a
    /// column of the row table on that entity's binding.
    fn project_collected(&mut self, collected: Vec<Collected>) -> Result<()> {
        let modules = self.modules;
        let mut exports = BTreeMap::<&str, Vec<(usize, Referent)>>::new();
        let mut projected_by_member = BTreeMap::new();
        for (index, entity) in collected.iter().enumerate() {
            for member_index in entity.member_indices() {
                projected_by_member.insert((entity.module_index, member_index), index);
            }
        }
        for (module_index, module) in modules.iter().enumerate() {
            for (member_index, member) in module.members.iter().enumerate() {
                let referent = match (
                    &member.selector,
                    projected_by_member.get(&(module_index, member_index)),
                ) {
                    (_, Some(index)) => Referent::Projected(*index),
                    (MemberSelector::Binding(pin), None) => Referent::Pin {
                        module_index,
                        member_index,
                        name: pin.name.clone(),
                    },
                    (MemberSelector::SourceMatch(_), None) => Referent::Unprojected,
                    (_, None) => Referent::Relational {
                        module_index,
                        member_index,
                    },
                };
                exports
                    .entry(member.export_name.as_str())
                    .or_default()
                    .push((module_index, referent));
            }
        }
        let shadowing = self
            .chunk
            .structural
            .per_statement
            .iter()
            .flat_map(|statement| statement.declared.iter().map(|id| id.0.to_string()))
            .chain(import_sources(self.chunk.module).into_keys())
            .collect::<BTreeSet<_>>();

        for (index, entity) in collected.iter().enumerate() {
            let identifiers = entity
                .free
                .iter()
                .map(|name| FreeIdentifier {
                    name: name.clone(),
                    meaning: match classify(name, entity.module_index, &exports, &shadowing) {
                        Meaning::Reference(_, Referent::Projected(referenced))
                            if referenced == index =>
                        {
                            IdentifierMeaning::Wildcard
                        }
                        Meaning::Reference(_, Referent::Unprojected) | Meaning::Wildcard => {
                            IdentifierMeaning::Wildcard
                        }
                        Meaning::Reference(module_index, _) => IdentifierMeaning::Reference {
                            entity: reference_entity(modules, name, module_index),
                        },
                        Meaning::Ambiguous(exporters) => IdentifierMeaning::Ambiguous {
                            modules: module_paths(modules, &exporters),
                        },
                        Meaning::Global => IdentifierMeaning::Global,
                    },
                })
                .collect::<Vec<_>>();
            if !identifiers.is_empty() {
                self.templates.push(TemplateIdentifiers {
                    chunk: self.chunk.name.clone(),
                    logical_module: modules[entity.module_index].path.clone(),
                    entities: entity.entities(modules),
                    identifiers,
                });
            }
        }

        // Each entity's rows narrowed by its references, or why it has none.
        let mut narrowed = Vec::with_capacity(collected.len());
        for (index, entity) in collected.iter().enumerate() {
            narrowed.push(narrow_by_references(
                index, entity, modules, &exports, &shadowing,
            ));
        }
        let settled = settle_references(&collected, &mut narrowed, modules);
        let mut targets_by_collected = BTreeMap::new();
        for (index, (entity, narrowed)) in collected.iter().zip(&narrowed).enumerate() {
            match narrowed {
                Ok(_) => {
                    targets_by_collected.insert(index, self.declare_collected(entity)?);
                }
                Err(rejection) => {
                    if let CollectedShape::Anonymous(position) = entity.shape {
                        self.push_anonymous(
                            entity.module_index,
                            position,
                            rejection.clone().outcome(),
                        );
                    }
                    for member_index in entity.member_indices() {
                        self.push_member(
                            entity.module_index,
                            member_index,
                            rejection.clone().outcome(),
                        );
                    }
                }
            }
        }
        let target_by_member = self
            .members
            .iter()
            .map(|(target, member)| (*member, *target))
            .collect::<BTreeMap<_, _>>();
        for (index, (entity, narrowed)) in collected.into_iter().zip(narrowed).enumerate() {
            let Ok(Narrowed {
                rows,
                unreferenced_rows,
                references,
            }) = narrowed
            else {
                continue;
            };
            let logical_module = self.ids[entity.module_index].clone();
            let targets = targets_by_collected[&index].clone();
            // A reference to a projected entity that survived is a column on
            // that entity's binding; one to a pin only narrowed the rows.
            let mut columns = Vec::new();
            let mut column_names = Vec::new();
            let mut referenced = Vec::new();
            for (name, module_index, referent) in &references {
                let target = match referent {
                    Referent::Projected(collected_index) => {
                        let Some(candidates) = targets_by_collected.get(collected_index) else {
                            continue;
                        };
                        // A referent every row of which binds the same name
                        // already narrowed these rows; only an open one
                        // needs the solver.
                        if !settled.contains_key(&(*collected_index, name.clone())) {
                            columns.push(
                                self.builder
                                    .projected_binding_variable(&self.ids[*module_index], name),
                            );
                            column_names.push(name.clone());
                        }
                        candidates.iter().copied().find(|target| {
                            let (_, member_index) = self.members[target];
                            modules[*module_index].members[member_index].export_name == *name
                        })
                    }
                    Referent::Pin {
                        module_index,
                        member_index,
                        ..
                    } => target_by_member
                        .get(&(*module_index, *member_index))
                        .copied(),
                    Referent::Relational {
                        module_index,
                        member_index,
                    } => {
                        columns.push(
                            self.builder
                                .relational_binding_variable(&self.ids[*module_index], name),
                        );
                        column_names.push(name.clone());
                        target_by_member
                            .get(&(*module_index, *member_index))
                            .copied()
                    }
                    Referent::Unprojected => continue,
                };
                referenced.push((name.clone(), *module_index, target));
            }
            // Rows without repeats, telling rows apart by their places and
            // their column values.
            let key = |at: usize| {
                (
                    &rows[at].places,
                    column_names
                        .iter()
                        .map(|name| &rows[at].free_bindings[name])
                        .collect::<Vec<_>>(),
                )
            };
            let mut distinct = Vec::<usize>::new();
            for at in 0..rows.len() {
                if !distinct.iter().any(|seen| key(*seen) == key(at)) {
                    distinct.push(at);
                }
            }
            self.projected.push(Projected {
                targets,
                rows: distinct.iter().map(|at| rows[*at].places.clone()).collect(),
                references: referenced
                    .into_iter()
                    .map(|(name, module_index, target)| Reference {
                        entity: reference_entity(modules, &name, module_index),
                        target,
                        values: distinct
                            .iter()
                            .map(|at| rows[*at].free_bindings[&name].clone())
                            .collect(),
                    })
                    .collect(),
                unreferenced_rows,
            });
            let table = distinct
                .iter()
                .map(|at| {
                    (
                        rows[*at].places.clone(),
                        column_names
                            .iter()
                            .map(|name| rows[*at].free_bindings[name].clone())
                            .collect::<Vec<_>>(),
                    )
                })
                .collect::<Vec<_>>();
            match &entity.shape {
                CollectedShape::Member(member_index) => {
                    let export_name =
                        &modules[entity.module_index].members[*member_index].export_name;
                    self.builder.lower_projected_source_match_candidates(
                        &logical_module,
                        export_name,
                        &columns,
                        table
                            .into_iter()
                            .map(|(places, referenced)| (bound(&places[0]), referenced))
                            .collect(),
                    );
                }
                CollectedShape::Group(group) => {
                    self.builder.lower_projected_source_match_group_candidates(
                        &logical_module,
                        &group.exports_by_target,
                        &columns,
                        table
                            .into_iter()
                            .map(|(places, referenced)| {
                                (places.iter().map(bound).collect(), referenced)
                            })
                            .collect(),
                    );
                }
                CollectedShape::Anonymous(position) => {
                    self.builder.lower_projected_anonymous_statement_candidates(
                        &logical_module,
                        modules[entity.module_index].anonymous_statements[*position].index,
                        &columns,
                        table
                            .into_iter()
                            .map(|(places, referenced)| (places[0].owner, referenced))
                            .collect(),
                    );
                }
            }
        }
        Ok(())
    }

    /// Declares `entity`'s targets, in row-place order.
    fn declare_collected(&mut self, entity: &Collected) -> Result<Vec<SelectorTargetId>> {
        let logical_module = self.ids[entity.module_index].clone();
        let module = &self.modules[entity.module_index];
        let mut targets = Vec::new();
        match &entity.shape {
            CollectedShape::Member(member_index) => {
                let member = &module.members[*member_index];
                let target = self.builder.declare_member_target_in_module_ref(
                    &logical_module,
                    &member.export_name,
                    member.selector.spec_ref(),
                )?;
                self.members
                    .insert(target, (entity.module_index, *member_index));
                targets.push(target);
            }
            CollectedShape::Group(group) => {
                for (target_binding, member_index) in &group.members_by_target {
                    let member = &module.members[*member_index];
                    let target = self
                        .builder
                        .declare_binding_group_member_target_in_module_ref(
                            &logical_module,
                            &member.export_name,
                            target_binding,
                            member.selector.spec_ref(),
                        )?;
                    self.members
                        .insert(target, (entity.module_index, *member_index));
                    targets.push(target);
                }
            }
            CollectedShape::Anonymous(position) => {
                let target = self
                    .builder
                    .declare_projected_anonymous_statement_target_in_module(
                        &logical_module,
                        module.anonymous_statements[*position].index,
                    );
                self.anonymous
                    .push((target, entity.module_index, *position));
                targets.push(target);
            }
        }
        Ok(targets)
    }

    /// Each name pin's places: the top-level statements declaring its name,
    /// of its kind when it names one.
    fn pin_places(&self) -> BTreeMap<SelectorTargetId, BTreeSet<Place>> {
        let declarations = &self.chunk.places().declarations;
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
        let resolved_by = self.resolved_by(result);
        let Self {
            ids,
            program,
            mut outcomes,
            members,
            anonymous,
            pin_places,
            templates,
            ..
        } = self;
        let module = chunk.module;
        for (target, module_index, position) in anonymous {
            let outcome = match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => Outcome::Resolved {
                    owner: claim_candidate(module, claim)?.owner,
                    binding: None,
                    resolved_by: how_resolved(&resolved_by, target),
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
                        resolved_by: how_resolved(&resolved_by, target),
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
        Ok(Resolution {
            outcomes,
            templates,
        })
    }

    /// How each resolved projected target was made unique, where not by its
    /// own selector: of the rows that agree with the solved bindings of the
    /// entities its template references, exactly one (by its references), or
    /// exactly one no other exclusive target's solved claim takes (by
    /// elimination).
    fn resolved_by(&self, result: &SolverResult) -> BTreeMap<SelectorTargetId, ResolvedBy> {
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
        let solved_binding = |target: SelectorTargetId| match result.outcome_for(target) {
            Some(ClaimOutcome::Unique { claim }) => claim.binding.as_deref(),
            _ => None,
        };
        let mut resolved = BTreeMap::new();
        for entity in &self.projected {
            if entity.unreferenced_rows < 2
                || !entity.targets.iter().all(|target| {
                    matches!(
                        result.outcome_for(*target),
                        Some(ClaimOutcome::Unique { .. })
                    )
                })
            {
                continue;
            }
            let agreeing = (0..entity.rows.len())
                .filter(|at| {
                    entity.references.iter().all(|reference| {
                        reference
                            .target
                            .and_then(solved_binding)
                            .is_none_or(|binding| reference.values[*at] == binding)
                    })
                })
                .map(|at| &entity.rows[at])
                .collect::<Vec<_>>();
            let distinct = agreeing.iter().collect::<BTreeSet<_>>().len();
            let how = if !entity.references.is_empty() && distinct == 1 {
                Some(ResolvedBy::OwnReferences {
                    references: entity
                        .references
                        .iter()
                        .map(|reference| reference.entity.clone())
                        .collect(),
                })
            } else {
                elimination_claimers(&agreeing, &entity.targets, result, &exclusive)
                    .map(|claimers| ResolvedBy::Elimination {
                        claimers: target_entity_refs(&self.program, &claimers),
                    })
                    .or_else(|| {
                        // Several of its places survive every claim, yet it
                        // resolved: a template naming it picked one.
                        let referrers = self
                            .projected
                            .iter()
                            .filter(|referrer| {
                                referrer.references.iter().any(|reference| {
                                    reference
                                        .target
                                        .is_some_and(|target| entity.targets.contains(&target))
                                })
                            })
                            .flat_map(|referrer| referrer.targets.iter().copied())
                            .collect::<BTreeSet<_>>();
                        (!referrers.is_empty()).then(|| ResolvedBy::ReferencedBy {
                            referrers: target_entity_refs(&self.program, &referrers),
                        })
                    })
            };
            if let Some(how) = how {
                for target in &entity.targets {
                    resolved.insert(*target, how.clone());
                }
            }
        }
        resolved
    }
}

/// How `target`, resolved, was made unique, given [`Projection::resolved_by`].
fn how_resolved(
    resolved_by: &BTreeMap<SelectorTargetId, ResolvedBy>,
    target: SelectorTargetId,
) -> ResolvedBy {
    resolved_by
        .get(&target)
        .cloned()
        .unwrap_or(ResolvedBy::OwnSelector)
}

/// The prefix of an `invalid` outcome whose matcher failed.
const MATCHER_ERROR: &str = "shape_matcher_error";
/// The prefix of an `invalid` outcome whose match maps to no place.
const OWNER_MAPPING_ERROR: &str = "projection_owner_mapping_error";

/// Why a `source_match` was rejected before the solve.
#[derive(Debug, Clone)]
enum Rejection {
    NoCandidates,
    /// Its matches all disagree with where these referenced entities are.
    Conflict(Vec<EntityRef>),
    TooBroad(usize),
    Invalid(String),
}

impl Rejection {
    /// `error`'s first line, after `category`.
    fn invalid(category: &str, error: &anyhow::Error) -> Self {
        let message = error.to_string();
        let first_line = message
            .lines()
            .map(str::trim)
            .find(|line| !line.is_empty())
            .unwrap_or(&message);
        Self::Invalid(format!("{category}: {first_line}"))
    }

    /// `rows`, unless there are none or more than the cap.
    fn check_count<T>(rows: Vec<T>) -> Result<Vec<T>, Self> {
        match rows.len() {
            0 => Err(Self::NoCandidates),
            count if count > MAX_CANDIDATES_PER_SELECTOR => Err(Self::TooBroad(count)),
            _ => Ok(rows),
        }
    }

    fn outcome(self) -> Outcome {
        match self {
            Self::NoCandidates => Outcome::no_match(),
            Self::Conflict(with) => Outcome::Conflict { with },
            Self::TooBroad(count) => Outcome::too_broad(count),
            Self::Invalid(error) => Outcome::Invalid { error },
        }
    }
}

/// A collected entity's rows once its template's references narrowed them.
struct Narrowed {
    rows: Vec<CollectedRow>,
    /// Distinct rows before any spec-entity reference narrowed them.
    unreferenced_rows: usize,
    /// Each free identifier naming a spec entity: the name, the module that
    /// exports it, and what it names.
    references: Vec<(String, usize, Referent)>,
}

/// Classifies `entity`'s free identifiers and narrows its rows by them. A
/// name exported in the entity's own module names that export; otherwise a
/// name exported by exactly one module names it, and one exported by several
/// is an authoring error. A name no module exports that spells a runtime
/// global the chunk does not shadow matches only itself. Any other name
/// stays a wildcard.
fn narrow_by_references(
    index: usize,
    entity: &Collected,
    modules: &[SpecModule],
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Result<Narrowed, Rejection> {
    let mut globals = Vec::new();
    let mut references = Vec::new();
    for name in &entity.free {
        match classify(name, entity.module_index, exports, shadowing) {
            Meaning::Reference(_, Referent::Projected(referenced)) if referenced == index => {}
            Meaning::Reference(module_index, referent) => {
                references.push((name.clone(), module_index, referent));
            }
            Meaning::Ambiguous(exporters) => {
                return Err(Rejection::Invalid(format!(
                    "ambiguous_reference: template identifier `{name}` is exported by modules \
                     {}; rename it in the template or rename one export",
                    module_paths(modules, &exporters).join(", ")
                )));
            }
            Meaning::Global => globals.push(name),
            Meaning::Wildcard => {}
        }
    }
    let candidates = entity
        .rows
        .iter()
        .filter(|row| {
            globals
                .iter()
                .all(|global| row.free_bindings.get(*global) == Some(*global))
        })
        .cloned()
        .collect::<Vec<_>>();
    let unreferenced_rows = candidates
        .iter()
        .map(|row| &row.places)
        .collect::<BTreeSet<_>>()
        .len();
    // A referenced name the row does not report bound different chunk
    // identifiers in different scopes of the match: no one entity.
    let rows = candidates
        .iter()
        .filter(|row| {
            references.iter().all(|(name, _, referent)| match referent {
                Referent::Unprojected => true,
                Referent::Pin { name: pinned, .. } => row.free_bindings.get(name) == Some(pinned),
                Referent::Projected(_) | Referent::Relational { .. } => {
                    row.free_bindings.contains_key(name)
                }
            })
        })
        .cloned()
        .collect::<Vec<_>>();
    if rows.is_empty() {
        let with = references
            .iter()
            .filter(|(name, _, referent)| {
                matches!(referent, Referent::Pin { name: pinned, .. }
                    if candidates.iter().all(|row| row.free_bindings.get(name) != Some(pinned)))
            })
            .map(|(name, module_index, _)| reference_entity(modules, name, *module_index))
            .collect::<Vec<_>>();
        if !with.is_empty() {
            return Err(Rejection::Conflict(with));
        }
    }
    Ok(Narrowed {
        rows: Rejection::check_count(rows)?,
        unreferenced_rows,
        references,
    })
}

/// What a free template identifier of a template in module `module_index`
/// means (<../SPEC.md> § Matching).
enum Meaning {
    /// The spec entity exported under that name, and the module exporting it.
    Reference(usize, Referent),
    /// Exported by these modules, none of them the template's own.
    Ambiguous(Vec<usize>),
    Global,
    Wildcard,
}

fn classify(
    name: &str,
    module_index: usize,
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Meaning {
    match exports.get(name).map(Vec::as_slice) {
        Some(named) => match (
            named.iter().find(|(exporter, _)| *exporter == module_index),
            named,
        ) {
            (Some((exporter, referent)), _) | (None, [(exporter, referent)]) => {
                Meaning::Reference(*exporter, referent.clone())
            }
            (None, several) => {
                Meaning::Ambiguous(several.iter().map(|(exporter, _)| *exporter).collect())
            }
        },
        None if JS_GLOBALS.contains(&name) && !shadowing.contains(name) => Meaning::Global,
        None => Meaning::Wildcard,
    }
}

fn module_paths(modules: &[SpecModule], indices: &[usize]) -> Vec<String> {
    indices
        .iter()
        .map(|index| modules[*index].path.clone())
        .collect()
}

/// Narrows every entity's rows by the references whose referent every one of
/// its own rows already places at one binding, until nothing changes, and
/// returns those settled bindings by (collected entity, export name). Only
/// references to entities still open after this reach the solver, so
/// entities referencing a settled one stay in small groups.
fn settle_references(
    collected: &[Collected],
    narrowed: &mut [Result<Narrowed, Rejection>],
    modules: &[SpecModule],
) -> BTreeMap<(usize, String), String> {
    loop {
        let mut settled = BTreeMap::new();
        for (index, (entity, narrowed)) in collected.iter().zip(narrowed.iter()).enumerate() {
            let Ok(narrowed) = narrowed else {
                continue;
            };
            for (position, export_name) in entity.export_names(modules).into_iter().enumerate() {
                let bindings = narrowed
                    .rows
                    .iter()
                    .map(|row| &row.places[position].binding)
                    .collect::<BTreeSet<_>>();
                if let [Some(binding)] = bindings.into_iter().collect::<Vec<_>>().as_slice() {
                    settled.insert((index, export_name), binding.clone());
                }
            }
        }
        let mut changed = false;
        for entry in narrowed.iter_mut() {
            let Ok(narrowed) = entry else {
                continue;
            };
            let Narrowed {
                rows, references, ..
            } = narrowed;
            let agrees = |row: &CollectedRow, name: &String, referent: &Referent| match referent {
                Referent::Projected(referenced) => settled
                    .get(&(*referenced, name.clone()))
                    .is_none_or(|binding| row.free_bindings.get(name) == Some(binding)),
                _ => true,
            };
            let with = references
                .iter()
                .filter(|(name, _, referent)| !rows.iter().any(|row| agrees(row, name, referent)))
                .map(|(name, module_index, _)| reference_entity(modules, name, *module_index))
                .collect::<Vec<_>>();
            let before = rows.len();
            rows.retain(|row| {
                references
                    .iter()
                    .all(|(name, _, referent)| agrees(row, name, referent))
            });
            if rows.len() != before {
                changed = true;
                if rows.is_empty() {
                    *entry = Err(if with.is_empty() {
                        Rejection::NoCandidates
                    } else {
                        Rejection::Conflict(with)
                    });
                }
            }
        }
        if !changed {
            return settled;
        }
    }
}

/// The spec entity `name` names in `modules[module_index]`.
fn reference_entity(modules: &[SpecModule], name: &str, module_index: usize) -> EntityRef {
    EntityRef {
        logical_module: modules[module_index].path.clone(),
        entity: Some(Entity::Export(name.to_string())),
    }
}

/// A member place as the lowering takes it: its owner and binding.
fn bound(place: &Place) -> (OwnerId, String) {
    (
        place.owner,
        place
            .binding
            .clone()
            .expect("a member's place names the binding it claims"),
    )
}

fn member_place(places: &Places, matched: &source_match::MemberBindingMatch) -> Result<Place> {
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
    Ok(Place {
        owner,
        binding: Some(binding),
    })
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

/// The targets whose solved claims made an entity unique: `Some` when it has
/// several candidate `rows` and exactly one survives dropping every row whose
/// owner or binding another exclusive target's solved value holds.
/// `exclusive` are the targets the solve keeps on distinct owners; a claim by
/// any other target never took a row away.
fn elimination_claimers(
    rows: &[&Vec<Place>],
    targets: &[SelectorTargetId],
    result: &SolverResult,
    exclusive: &BTreeSet<SelectorTargetId>,
) -> Option<BTreeSet<SelectorTargetId>> {
    if rows.len() < 2 {
        return None;
    }
    let other_claims = exclusive
        .iter()
        .filter(|target| !targets.contains(target))
        .filter_map(|target| match result.outcome_for(*target) {
            Some(ClaimOutcome::Unique { claim }) => Some((*target, claim)),
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut survivors = 0;
    let mut claimers = BTreeSet::new();
    for row in rows {
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
        ClaimOutcome::NoMatch => Outcome::no_match(),
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
