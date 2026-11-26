//! The selector resolve every command calls: chunks and the spec entities
//! aimed at each in, one [`SelectorOutcome`] per entity out.
//!
//! The shape matcher ([`ChunkResolver`]) lists every place each `source_match`
//! matches. Those places become candidate tables in one selector program with
//! the name pins and relational selectors, and every entity is assigned at
//! once under `all_different`: each group of interacting entities by CP-SAT,
//! the rest from their own candidates. Semantics:
//! <docs/selector_resolution.md>.

mod collect;
mod diagnostics;
mod fact_store;
mod interaction;
mod placement;
mod projection;
mod record;
mod references;
mod selector_conflict;

pub use placement::{FrozenImportAnchor, PlacementAnchors};
pub use projection::Projection;

use source_identity::SourceUnitOrdinal;
use std::borrow::Cow;
use std::cell::OnceCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use analysis::StatementKind;
use analysis::facts::StructuralChunkSourceFacts;
use anyhow::{Context, Result};
use js_ast::statement_for_source_unit_ordinal;
use rayon::prelude::*;
use selector_backend_solver::solve_with_backend;
use selector_ir::{SelectorTargetId, SolverClaim, SolverResult};
use selector_ortools_cpsat_backend::{CpSatSettings, OrToolsCpSatBackend};
use selector_outcome::{
    Entity, Outcome, Placement, SelectorKind, SelectorOutcome, TemplateIdentifiers,
};
use source_identity::SourceUnitId;
use source_match::ParsedSourceMatchSelector;
use source_match::chunk_resolver::ChunkResolver;
use spec::{AnonymousStatementSelector, BindingSelector, BindingSourceKind, MemberSelectorSpec};
use swc_ecma_ast::{Module, ModuleDecl, ModuleItem};

use diagnostics::{add_differentiators, add_nearest_unclaimed};
use fact_store::selector_fact_store;
use projection::Resolve;

/// One parsed chunk, analysed once and resolved against any number of entity
/// sets.
pub struct Chunk<'m> {
    name: String,
    module: &'m Module,
    structural: Cow<'m, StructuralChunkSourceFacts<'m>>,
    matcher: OnceCell<ChunkResolver<'m>>,
    places: OnceCell<Places>,
    near_miss_indices: OnceCell<source_match::ItemMatchIndices>,
}

/// Top-level statements by source body index: the source unit of each statement,
/// and of each binding it declares.
struct Places {
    source_unit_by_body: BTreeMap<usize, SourceUnitId>,
    source_unit_by_body_and_binding: BTreeMap<(usize, String), SourceUnitId>,
    /// Each top-level binding name's declaring statements, with their kind.
    /// Repeated `var` declarations intentionally retain every site here.
    declarations: BTreeMap<String, Vec<(SourceUnitId, SourceUnitOrdinal, StatementKind)>>,
    /// Chunk import bindings, indexed once with the declaration sites.
    imports: HashMap<String, ImportBinding>,
    /// Every top-level declared and imported name: what a template's free
    /// identifier can refer to in this chunk.
    shadowing: BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DirectBindingSourceEvidence {
    Local {
        /// All sites remain available because a kind pin is satisfied when
        /// any site has the requested kind.
        declaration_sites: Vec<(SourceUnitOrdinal, StatementKind)>,
    },
    Import,
    Missing,
}

#[derive(Debug, Clone)]
struct ImportBinding {
    source: String,
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
    /// A validated `members[].selector`. A binding pin on an import specifier
    /// names the import binding itself and is not resolved.
    Authored(MemberSelectorSpec),
    SourceMatch(ParsedSourceMatchSelector),
}

impl MemberSelector {
    pub fn is_import_specifier(&self) -> bool {
        matches!(self, Self::Authored(MemberSelectorSpec::Binding(binding))
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
            Self::Authored(selector) => SelectorKind::from(selector),
            Self::SourceMatch(_) => SelectorKind::SourceMatches,
        }
    }

    fn preview(&self) -> String {
        match self {
            Self::Authored(MemberSelectorSpec::Binding(binding)) => format!("{binding:?}"),
            Self::SourceMatch(parsed) => {
                source_match::source_match_preview(&parsed.selector().match_source)
            }
            Self::Authored(relational) => format!("{relational:?}"),
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
    /// Same-module member references made by projected `source_match`
    /// templates. Authoring tools use these typed relationships to preserve
    /// the local readable-name resolution when proposing module moves.
    pub member_references: Vec<ResolvedMemberReference>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct ResolvedMemberReference {
    pub source_module: usize,
    pub source: SourceMatchReferenceOrigin,
    pub referenced_module: usize,
    pub referenced_member: String,
    /// How the canonical free-name classifier resolves this identifier when
    /// it has no same-module export to prefer. Anchor planners must preserve
    /// local co-location only for names that are ambiguous in that view.
    pub module_neutral_meaning: ModuleNeutralReferenceMeaning,
}

/// The resolved source entity whose `source_match` template makes a local
/// member reference. Anonymous indices are the authored selector indices in
/// that module, not positions in a filtered outcomes list.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum SourceMatchReferenceOrigin {
    Member(String),
    AnonymousStatement(usize),
}

/// The module-neutral meaning of a reference that currently resolves to a
/// member in the template's own module.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum ModuleNeutralReferenceMeaning {
    /// The export index has one matching member, so the reference still has a
    /// unique target after the source template is separated from its module.
    Unique,
    /// The export index has several matching members, so the reference needs
    /// its same-module preference to identify its current target.
    Ambiguous,
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

impl<'m> Chunk<'m> {
    /// The source facts must come from `module`. `name` labels the selector program and outcomes.
    pub fn new(
        name: impl Into<String>,
        module: &'m Module,
        structural: Cow<'m, StructuralChunkSourceFacts<'m>>,
    ) -> Self {
        Self {
            name: name.into(),
            module,
            structural,
            matcher: OnceCell::new(),
            places: OnceCell::new(),
            near_miss_indices: OnceCell::new(),
        }
    }

    /// Collect a census when there are no prepared facts for this module.
    pub fn analyze(name: impl Into<String>, module: &'m Module) -> Self {
        let structural = analysis::facts::collect_chunk_source_facts(module, None);
        Self::new(name, module, Cow::Owned(structural))
    }

    pub fn into_structural(self) -> StructuralChunkSourceFacts<'m> {
        self.structural.into_owned()
    }

    /// The shape matcher, for callers that rank candidate selectors by how
    /// many places they match. Deciding what a selector resolves to is
    /// [`Chunk::resolve`]'s job.
    pub fn matcher(&self) -> &ChunkResolver<'m> {
        self.matcher.get_or_init(|| ChunkResolver::new(self.module))
    }

    fn places(&self) -> &Places {
        self.places.get_or_init(|| {
            let mut places = Places {
                source_unit_by_body: BTreeMap::new(),
                source_unit_by_body_and_binding: BTreeMap::new(),
                declarations: BTreeMap::new(),
                imports: import_bindings(self.module),
                shadowing: BTreeSet::new(),
            };
            for statement in &self.structural.per_statement {
                for binding in &statement.declared {
                    places
                        .declarations
                        .entry(binding.0.as_str().to_string())
                        .or_default()
                        .push((
                            SourceUnitId(statement.ordinal.0),
                            statement.ordinal,
                            statement.kind,
                        ));
                }
                let Some(body_idx) =
                    statement_for_source_unit_ordinal(&self.module.body, statement.ordinal)
                else {
                    continue;
                };
                let source_unit = SourceUnitId(statement.ordinal.0);
                places.source_unit_by_body.insert(body_idx.0, source_unit);
                for binding in &statement.declared {
                    places
                        .source_unit_by_body_and_binding
                        .insert((body_idx.0, binding.0.as_str().to_string()), source_unit);
                }
            }
            places.shadowing = self
                .structural
                .per_statement
                .iter()
                .flat_map(|statement| statement.declared.iter().map(|id| id.0.to_string()))
                .chain(places.imports.keys().cloned())
                .collect();
            places
        })
    }

    /// Source evidence for a typed direct name pin. This uses the same
    /// declaration table and kind mapping as `pin_places`, including its
    /// repeated-declaration rule: any site may satisfy the kind, while the
    /// first site remains the representative place.
    pub fn direct_binding_source_evidence(
        &self,
        binding: &BindingSelector,
    ) -> DirectBindingSourceEvidence {
        let places = self.places();
        if binding.kind == Some(BindingSourceKind::ImportSpecifier) {
            return places
                .imports
                .get(&binding.name)
                .map_or(DirectBindingSourceEvidence::Missing, |_| {
                    DirectBindingSourceEvidence::Import
                });
        }

        if let Some(declarations) = places.declarations.get(&binding.name) {
            let mut declaration_sites = declarations
                .iter()
                .map(|(_, ordinal, kind)| (*ordinal, *kind))
                .collect::<Vec<_>>();
            declaration_sites.sort_by_key(|(statement_ordinal, _)| *statement_ordinal);
            return DirectBindingSourceEvidence::Local { declaration_sites };
        }
        places
            .imports
            .get(&binding.name)
            .map_or(DirectBindingSourceEvidence::Missing, |_| {
                DirectBindingSourceEvidence::Import
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
    for (index, (_, _, projection)) in chunks.iter().enumerate() {
        for group in projection.interacting_groups() {
            match projection.decide_alone(&group) {
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
    let backend = OrToolsCpSatBackend::new(CpSatSettings::from_env()?);
    let solved = requests
        .par_iter()
        .map(|(index, group)| {
            let slice = programs[*index].slice_for_targets(group)?;
            let facts = facts[*index]
                .as_ref()
                .expect("a chunk with a request has a fact store");
            let result = solve_with_backend(&slice.program, facts, &backend)
                .context("global selector CP-SAT backend failed")?;
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

/// `result` of a program slice, with its targets named as in the whole
/// program.
fn in_program_targets(
    result: SolverResult,
    to_program: &BTreeMap<SelectorTargetId, SelectorTargetId>,
) -> SolverResult {
    SolverResult {
        claims: result
            .claims
            .into_iter()
            .map(|claim| SolverClaim {
                target: to_program[&claim.target],
                outcome: claim.outcome,
            })
            .collect(),
    }
}

pub fn statement_kind_for_spec(kind: BindingSourceKind) -> StatementKind {
    match kind {
        BindingSourceKind::ImportSpecifier => StatementKind::Import,
        BindingSourceKind::VariableDeclarator => StatementKind::VarDecl,
        BindingSourceKind::FunctionDeclaration => StatementKind::FnDecl,
        BindingSourceKind::ClassDeclaration => StatementKind::ClassDecl,
    }
}

/// Local import binding → source and import form, for every import in the chunk.
fn import_bindings(module: &Module) -> HashMap<String, ImportBinding> {
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
                (
                    specifier.local().sym.to_string(),
                    ImportBinding {
                        source: source.clone(),
                    },
                )
            })
        })
        .collect()
}

/// Import local names without constructing structural chunk
/// analysis. The direct-claim edit gate uses this when import pins are the
/// only selectors that need source evidence.
pub fn import_specifier_names(module: &Module) -> HashSet<String> {
    import_bindings(module).into_keys().collect()
}
