//! Projecting one chunk's entities onto its places as one selector program.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use selector_ir::{SelectorProgram, SelectorTargetId};
use selector_ir_lowering::MemberSelectorProgramBuilder;
use selector_outcome::{
    Entity, EntityRef, FreeIdentifier, IdentifierMeaning, Outcome, Placement, SelectorKind,
    SelectorOutcome, TemplateIdentifiers,
};
use source_identity::SourceUnitId;
use spec::MemberSelectorSpec;

use crate::collect::{Collected, CollectedShape, source_match_groups};
use crate::placement::FrozenPlacementAnchor;
use crate::references::{
    Meaning, Narrowed, Referent, classify, classify_without_local_preference, module_paths,
    narrow_by_references, reference_entity, settle_references,
};
use crate::{
    Chunk, EntityIndex, EntityOutcome, MemberSelector, ModuleNeutralReferenceMeaning,
    ResolvedMemberReference, SourceMatchReferenceOrigin, SpecModule, member_outcome,
    statement_kind_for_spec,
};

/// A `source_match` projected into the solve: its targets (one, or every
/// binding of a `source_matches[]` group) and its candidate rows, each row the
/// places it would claim, one per target.
pub(crate) struct Projected {
    pub(crate) targets: Vec<SelectorTargetId>,
    pub(crate) rows: Vec<Vec<Place>>,
    /// The entities its template names, each with the chunk identifier every
    /// row bound at that name.
    pub(crate) references: Vec<Reference>,
    /// Distinct rows its selector matched before any reference narrowed them.
    pub(crate) unreferenced_rows: usize,
    /// Domains before any other spec entity narrowed the matcher rows.
    /// A Hall witness on these supersets needs no relational premises.
    pub(crate) original_source_units: Vec<BTreeMap<SourceUnitId, BTreeSet<String>>>,
}

/// A spec entity a template names, as a column of the naming entity's rows.
pub(crate) struct Reference {
    pub(crate) entity: EntityRef,
    /// The referenced entity's target; `None` for a name pin on an import,
    /// which is not resolved.
    pub(crate) target: Option<SelectorTargetId>,
    pub(crate) values: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct Place {
    pub(crate) source_unit: SourceUnitId,
    pub(crate) binding: Option<String>,
}

/// One chunk's entities projected onto its places by [`Chunk::project`].
pub struct Projection {
    pub(crate) program: SelectorProgram,
    /// Entities decided before the solve.
    pub(crate) outcomes: Vec<EntityOutcome>,
    /// Member targets, in declaration order.
    pub(crate) members: BTreeMap<SelectorTargetId, (usize, usize)>,
    /// Anonymous statement targets, by module and position in it.
    pub(crate) anonymous: Vec<(SelectorTargetId, usize, usize)>,
    pub(crate) projected: Vec<Projected>,
    /// SourceMatch member references that resolved to another member in the
    /// same original module, after canonical identifier classification.
    pub(crate) member_references: Vec<ResolvedMemberReference>,
    /// Each name pin's places: the declarations of its name, of its kind.
    pub(crate) pin_places: BTreeMap<SelectorTargetId, BTreeSet<Place>>,
    pub(crate) templates: Vec<TemplateIdentifiers>,
}

/// One [`Chunk::project`].
pub(crate) struct Resolve<'c, 'm> {
    pub(crate) chunk: &'c Chunk<'m>,
    pub(crate) modules: &'c [SpecModule],
    frozen_module_count: usize,
    frozen_anchor_members: BTreeMap<(usize, usize), FrozenPlacementAnchor>,
    pub(crate) occupied_source_units: BTreeSet<SourceUnitId>,
    builder: MemberSelectorProgramBuilder,
    outcomes: Vec<EntityOutcome>,
    members: BTreeMap<SelectorTargetId, (usize, usize)>,
    anonymous: Vec<(SelectorTargetId, usize, usize)>,
    projected: Vec<Projected>,
    member_references: Vec<ResolvedMemberReference>,
    templates: Vec<TemplateIdentifiers>,
}

impl<'c, 'm> Resolve<'c, 'm> {
    pub(crate) fn new(chunk: &'c Chunk<'m>, modules: &'c [SpecModule]) -> Self {
        Self::new_placement(chunk, modules, 0, BTreeMap::new(), BTreeSet::new())
    }

    pub(crate) fn new_placement(
        chunk: &'c Chunk<'m>,
        modules: &'c [SpecModule],
        frozen_module_count: usize,
        frozen_anchor_members: BTreeMap<(usize, usize), FrozenPlacementAnchor>,
        occupied_source_units: BTreeSet<SourceUnitId>,
    ) -> Self {
        let mut builder = MemberSelectorProgramBuilder::default();
        for ((module_index, member_index), anchor) in &frozen_anchor_members {
            builder.declare_frozen_export_anchor(
                &modules[*module_index].members[*member_index].export_name,
                anchor.source_unit,
            );
        }
        Self {
            chunk,
            modules,
            frozen_module_count,
            frozen_anchor_members,
            occupied_source_units,
            builder,
            outcomes: Vec::new(),
            members: BTreeMap::new(),
            anonymous: Vec::new(),
            projected: Vec::new(),
            member_references: Vec::new(),
            templates: Vec::new(),
        }
    }

    pub(crate) fn project(mut self) -> Result<Projection> {
        let modules = self.modules;
        let mut groups = Vec::new();
        let mut source_matches = Vec::new();
        // Members with a name pin or a relational selector.
        let mut constrained = Vec::new();
        for (module_index, module) in modules.iter().enumerate().skip(self.frozen_module_count) {
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
                    MemberSelector::Authored(selector) => {
                        let target = self.builder.declare_member_target(
                            &module.path,
                            &member.export_name,
                            selector,
                        )?;
                        self.members.insert(target, (module_index, member_index));
                        constrained.push((module_index, member_index));
                    }
                }
            }
        }
        let mut collected = Vec::new();
        for (module_index, module) in modules.iter().enumerate().skip(self.frozen_module_count) {
            for position in 0..module.anonymous_statements.len() {
                match self.collect_anonymous(module_index, position) {
                    Ok(entity) => collected.push(entity),
                    Err(rejected) => {
                        self.push_anonymous(module_index, position, rejected.outcome()?);
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
                    let outcome = rejected.outcome()?;
                    for member_index in members {
                        self.push_member(module_index, member_index, outcome.clone());
                    }
                }
            }
        }
        for (module_index, member_index) in source_matches {
            match self.collect_member(module_index, member_index) {
                Ok(entity) => collected.push(entity),
                Err(rejected) => self.push_member(module_index, member_index, rejected.outcome()?),
            }
        }
        self.project_collected(collected)?;
        for (module_index, member_index) in constrained {
            let member = &modules[module_index].members[member_index];
            let MemberSelector::Authored(selector) = &member.selector else {
                unreachable!("constrained members are authored selectors");
            };
            self.builder.lower_member_constraints(
                &self.modules[module_index].path,
                &member.export_name,
                selector,
            )?;
        }
        self.builder
            .exclude_occupied_source_units(&self.occupied_source_units);
        let builder = std::mem::take(&mut self.builder);
        let program = builder.into_program()?;
        let pin_places = self.pin_places();
        for (target, places) in &pin_places {
            if places.is_empty() {
                let (module_index, member_index) = self.members[target];
                self.push_member(module_index, member_index, Outcome::no_match());
            }
        }
        Ok(Projection {
            program,
            outcomes: self.outcomes,
            members: self.members,
            anonymous: self.anonymous,
            projected: self.projected,
            member_references: self.member_references,
            pin_places,
            templates: self.templates,
        })
    }

    pub(crate) fn push_member(
        &mut self,
        module_index: usize,
        member_index: usize,
        outcome: Outcome,
    ) {
        self.outcomes.push(member_entity_outcome(
            &self.chunk.name,
            self.modules,
            module_index,
            member_index,
            outcome,
        ));
    }

    pub(crate) fn push_anonymous(
        &mut self,
        module_index: usize,
        position: usize,
        outcome: Outcome,
    ) {
        self.outcomes.push(anonymous_entity_outcome(
            &self.chunk.name,
            self.modules,
            module_index,
            position,
            outcome,
        ));
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
                let referent = match self
                    .frozen_anchor_members
                    .get(&(module_index, member_index))
                {
                    Some(anchor) => Referent::Frozen {
                        binding: anchor.binding.clone(),
                    },
                    None => match (
                        &member.selector,
                        projected_by_member.get(&(module_index, member_index)),
                    ) {
                        (_, Some(index)) => Referent::Projected(*index),
                        (MemberSelector::Authored(MemberSelectorSpec::Binding(pin)), None) => {
                            Referent::Pin {
                                module_index,
                                member_index,
                                name: pin.name.clone(),
                            }
                        }
                        (MemberSelector::SourceMatch(_), None) => Referent::Unprojected,
                        (_, None) => Referent::Relational {
                            module_index,
                            member_index,
                        },
                    },
                };
                exports
                    .entry(member.export_name.as_str())
                    .or_default()
                    .push((module_index, referent));
            }
        }
        let shadowing = &self.chunk.places().shadowing;

        for (index, entity) in collected.iter().enumerate() {
            let identifiers = entity
                .free
                .iter()
                .map(|name| FreeIdentifier {
                    name: name.clone(),
                    meaning: match classify(name, entity.module_index, &exports, shadowing) {
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
                index, entity, modules, &exports, shadowing,
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
                            rejection.clone().outcome()?,
                        );
                    }
                    for member_index in entity.member_indices() {
                        self.push_member(
                            entity.module_index,
                            member_index,
                            rejection.clone().outcome()?,
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
            let logical_module = &self.modules[entity.module_index].path;
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
                                    .projected_binding_variable(&modules[*module_index].path, name),
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
                                .relational_binding_variable(&modules[*module_index].path, name),
                        );
                        column_names.push(name.clone());
                        target_by_member
                            .get(&(*module_index, *member_index))
                            .copied()
                    }
                    Referent::Frozen { .. } => None,
                    Referent::Unprojected => continue,
                };
                referenced.push((name.clone(), *module_index, target));
            }
            let source_origins = targets
                .iter()
                .filter_map(|target| {
                    if let Some((module_index, member_index)) = self.members.get(target)
                        && *module_index == entity.module_index
                    {
                        return Some(SourceMatchReferenceOrigin::Member(
                            modules[*module_index].members[*member_index]
                                .export_name
                                .clone(),
                        ));
                    }
                    self.anonymous
                        .iter()
                        .find(|(anonymous_target, module_index, _)| {
                            anonymous_target == target && *module_index == entity.module_index
                        })
                        .map(|(_, module_index, position)| {
                            SourceMatchReferenceOrigin::AnonymousStatement(
                                modules[*module_index].anonymous_statements[*position].index,
                            )
                        })
                })
                .collect::<Vec<_>>();
            for (name, referenced_module, target) in &referenced {
                if *referenced_module != entity.module_index {
                    continue;
                }
                let Some((target_module, target_member)) =
                    target.and_then(|target| self.members.get(&target))
                else {
                    continue;
                };
                if *target_module != entity.module_index {
                    continue;
                }
                let referenced_member = modules[*target_module].members[*target_member]
                    .export_name
                    .clone();
                let module_neutral_meaning =
                    match classify_without_local_preference(name, &exports, shadowing) {
                        Meaning::Ambiguous(_) => ModuleNeutralReferenceMeaning::Ambiguous,
                        Meaning::Reference(_, _) => ModuleNeutralReferenceMeaning::Unique,
                        Meaning::Global | Meaning::Wildcard => {
                            unreachable!("a resolved member reference has an exported target")
                        }
                    };
                self.member_references
                    .extend(source_origins.iter().map(|source| ResolvedMemberReference {
                        source_module: entity.module_index,
                        source: source.clone(),
                        referenced_module: *target_module,
                        referenced_member: referenced_member.clone(),
                        module_neutral_meaning,
                    }));
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
                original_source_units: (0..entity.rows[0].places.len())
                    .map(|position| {
                        let mut source_units = BTreeMap::<SourceUnitId, BTreeSet<String>>::new();
                        for row in &entity.rows {
                            let place = &row.places[position];
                            source_units
                                .entry(place.source_unit)
                                .or_default()
                                .extend(place.binding.clone());
                        }
                        source_units
                    })
                    .collect(),
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
                        logical_module,
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
                        logical_module,
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
                        logical_module,
                        modules[entity.module_index].anonymous_statements[*position].index,
                        &columns,
                        table
                            .into_iter()
                            .map(|(places, referenced)| (places[0].source_unit, referenced))
                            .collect(),
                    );
                }
            }
        }
        Ok(())
    }

    /// Declares `entity`'s targets, in row-place order.
    fn declare_collected(&mut self, entity: &Collected) -> Result<Vec<SelectorTargetId>> {
        let logical_module = &self.modules[entity.module_index].path;
        let module = &self.modules[entity.module_index];
        let mut targets = Vec::new();
        match &entity.shape {
            CollectedShape::Member(member_index) => {
                let member = &module.members[*member_index];
                let target = self
                    .builder
                    .declare_source_match_member_target(logical_module, &member.export_name)?;
                self.members
                    .insert(target, (entity.module_index, *member_index));
                targets.push(target);
            }
            CollectedShape::Group(group) => {
                for member_index in group.members_by_target.values() {
                    let member = &module.members[*member_index];
                    let target = self
                        .builder
                        .declare_binding_group_member_target(logical_module, &member.export_name)?;
                    self.members
                        .insert(target, (entity.module_index, *member_index));
                    targets.push(target);
                }
            }
            CollectedShape::Anonymous(position) => {
                let target = self
                    .builder
                    .declare_projected_anonymous_statement_target_in_module(
                        logical_module,
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
                let MemberSelector::Authored(MemberSelectorSpec::Binding(pin)) =
                    &self.modules[*module_index].members[*member_index].selector
                else {
                    return None;
                };
                let places = declarations
                    .get(pin.name.as_str())
                    .and_then(|declarations| {
                        let has_matching_site = declarations.iter().any(|(_, _, kind)| {
                            pin.kind
                                .is_none_or(|pinned| statement_kind_for_spec(pinned) == *kind)
                        });
                        has_matching_site.then(|| declarations.first()).flatten()
                    })
                    .map(|(source_unit, _, _)| Place {
                        source_unit: *source_unit,
                        binding: Some(pin.name.clone()),
                    })
                    .into_iter()
                    .collect();
                Some((*target, places))
            })
            .collect()
    }
}

/// The outcome record of member `member_index` of `modules[module_index]`.
pub(crate) fn member_entity_outcome(
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
pub(crate) fn anonymous_entity_outcome(
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

/// A member place as the lowering takes it: its source unit and binding.
pub(crate) fn bound(place: &Place) -> (SourceUnitId, String) {
    (
        place.source_unit,
        place
            .binding
            .clone()
            .expect("a member's place names the binding it claims"),
    )
}
