//! Checked projection from the final source-unit assignment to syntax placement.
//!
//! This is the common placement kernel used by both candidate assessment and
//! emission. It consumes the retained source-layout facts and returns the
//! exact maps the emitter needs together with typed structural evidence.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::Serialize;
use source_identity::{SourceUnitId, SourceUnitOrdinal, StatementId};
use swc_ecma_ast::{Id, ModuleItem};

use crate::analysis_inputs::StableSetIterableUse;
use crate::{
    BindingKind, DependencyGraph, LocalBindings, ModuleAssignment, ModuleId, SourceUnitClass,
};

/// One post-split source unit's location in the retained raw module body.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct SourceUnitSyntaxLocation {
    pub ordinal: SourceUnitOrdinal,
    pub statement: StatementId,
    /// Present for each top-level variable declarator, including singleton
    /// declarations. Non-variable statements have no declarator offset.
    pub declarator_offset: Option<usize>,
}

/// Exact identity of one destructuring declarator retained by the existing
/// source-AST pass. Names are deliberately absent: SWC `Id` carries hygiene.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct DestructuringDeclaratorGroup {
    pub ordinal: SourceUnitOrdinal,
    pub statement: StatementId,
    pub declarator_offset: usize,
    pub bindings: Vec<Id>,
}

/// Result of classifying the observed owners of one retained destructuring
/// declarator. Request planning supplies explicit claims only; finalized
/// projection supplies every destination after checking completeness. The
/// classification itself is shared, while planning's sibling-pull mechanics
/// remain separate from finalized placement proof.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct DestructuringGroupDestinationCheck {
    pub group: DestructuringDeclaratorGroup,
    pub owners: Vec<(Id, ModuleId)>,
    pub classification: DestructuringGroupOwnerClassification,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum DestructuringGroupOwnerClassification {
    Unclaimed,
    OneOwner { destination: ModuleId },
    Split { destinations: Vec<ModuleId> },
}

/// Classify observed owners for one destructuring declarator. Callers choose
/// the ownership evidence domain: explicit module requests during planning, or
/// complete source-unit destinations after assignment finalization.
pub fn classify_destructuring_group_owners(
    group: &DestructuringDeclaratorGroup,
    owners: impl IntoIterator<Item = (Id, ModuleId)>,
) -> DestructuringGroupDestinationCheck {
    let owners: Vec<_> = owners.into_iter().collect();
    let distinct_destinations: Vec<_> = owners
        .iter()
        .map(|(_, destination)| *destination)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let classification = match distinct_destinations.as_slice() {
        [] => DestructuringGroupOwnerClassification::Unclaimed,
        [destination] => DestructuringGroupOwnerClassification::OneOwner {
            destination: *destination,
        },
        _ => DestructuringGroupOwnerClassification::Split {
            destinations: distinct_destinations,
        },
    };

    DestructuringGroupDestinationCheck {
        group: group.clone(),
        owners,
        classification,
    }
}

/// A top-level control-flow body that owns one or more hoisted `var` bindings.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct NestedVarStatement {
    pub statement: StatementId,
    pub bindings: Vec<Id>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum NestedVarDestinationClassification {
    Unclaimed,
    OneDestination { destination: ModuleId },
    SplitDestinations { destinations: Vec<ModuleId> },
}

impl NestedVarDestinationClassification {
    pub fn destinations(&self) -> &[ModuleId] {
        match self {
            Self::Unclaimed => &[],
            Self::OneDestination { destination } => std::slice::from_ref(destination),
            Self::SplitDestinations { destinations } => destinations,
        }
    }
}

/// Shared classification of the observed owners and whole-statement request
/// for one retained nested-var statement. Planning uses the owner decision to
/// route a complete statement; finalized projection additionally requires
/// every binding owner and a whole-statement claim when the destination is not
/// the entry.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct NestedVarPlacementCheck {
    pub statement: StatementId,
    pub missing_bindings: Vec<Id>,
    pub whole_statement_request: Option<ModuleId>,
    pub classification: NestedVarDestinationClassification,
}

impl NestedVarPlacementCheck {
    pub fn destinations(&self) -> &[ModuleId] {
        self.classification.destinations()
    }

    pub fn requires_whole_statement_claim(&self, entry: ModuleId) -> bool {
        self.whole_statement_request.is_none()
            && self
                .destinations()
                .iter()
                .any(|destination| *destination != entry)
    }
}

/// Classify nested-var placement from the retained statement identity, exact
/// SWC binding owners, and any existing whole-statement request. The entry
/// destination is intentionally checked by the finalized consumer only; it
/// is not a planning owner and cannot satisfy a missing explicit owner.
pub fn classify_nested_var_placement(
    nested: &NestedVarStatement,
    owners: Vec<(Id, ModuleId)>,
    whole_statement_request: Option<ModuleId>,
) -> NestedVarPlacementCheck {
    let present_bindings: HashSet<_> = owners.iter().map(|(binding, _)| binding.clone()).collect();
    let missing_bindings = nested
        .bindings
        .iter()
        .filter(|binding| !present_bindings.contains(*binding))
        .cloned()
        .collect();
    let destinations: BTreeSet<_> = owners
        .iter()
        .map(|(_, destination)| *destination)
        .chain(whole_statement_request)
        .collect();
    let destinations: Vec<_> = destinations.into_iter().collect();
    let classification = match destinations.as_slice() {
        [] => NestedVarDestinationClassification::Unclaimed,
        [destination] => NestedVarDestinationClassification::OneDestination {
            destination: *destination,
        },
        destinations => NestedVarDestinationClassification::SplitDestinations {
            destinations: destinations.to_vec(),
        },
    };
    NestedVarPlacementCheck {
        statement: nested.statement,
        missing_bindings,
        whole_statement_request,
        classification,
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct SourceUnitLocation {
    pub source_unit: SourceUnitId,
    pub ordinal: SourceUnitOrdinal,
    pub statement: StatementId,
    pub declarator_offset: Option<usize>,
}

/// A checked graph-ID keyed layout, built once by joining the retained AST
/// locations to the exact graph domain. It keeps an ordinal index for the
/// existing plan-routing consumers.
#[derive(Debug, Clone, Default)]
pub struct SourceUnitLayout {
    by_id: BTreeMap<SourceUnitId, SourceUnitLocation>,
    by_ordinal: BTreeMap<SourceUnitOrdinal, SourceUnitId>,
    by_statement: BTreeMap<StatementId, Vec<SourceUnitId>>,
    issues: Vec<SourceUnitLayoutIssue>,
    body_len: usize,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SourceUnitLayoutIssue {
    DuplicateSyntaxOrdinal {
        ordinal: SourceUnitOrdinal,
    },
    DuplicateGraphOrdinal {
        ordinal: SourceUnitOrdinal,
    },
    BodyStatementOutOfRange {
        source_unit: SourceUnitId,
        statement: StatementId,
        body_len: usize,
    },
    BodyLengthMismatch {
        layout_body_len: usize,
        projection_body_len: usize,
    },
    MissingSyntaxLocation {
        source_unit: SourceUnitId,
        ordinal: SourceUnitOrdinal,
    },
}

impl SourceUnitLayout {
    pub fn checked(
        graph: &DependencyGraph,
        syntax_locations: &[SourceUnitSyntaxLocation],
        body_len: usize,
    ) -> Self {
        let mut syntax_by_ordinal = BTreeMap::<SourceUnitOrdinal, SourceUnitSyntaxLocation>::new();
        let mut duplicate_ordinals = BTreeSet::new();
        for location in syntax_locations {
            if syntax_by_ordinal
                .insert(location.ordinal, *location)
                .is_some()
            {
                duplicate_ordinals.insert(location.ordinal);
            }
        }

        let mut layout = Self {
            body_len,
            ..Self::default()
        };
        let mut graph_ordinals = BTreeSet::new();
        for unit in graph.iter_nodes() {
            if !graph_ordinals.insert(unit.statement_ordinal) {
                layout
                    .issues
                    .push(SourceUnitLayoutIssue::DuplicateGraphOrdinal {
                        ordinal: unit.statement_ordinal,
                    });
                continue;
            }
            if duplicate_ordinals.contains(&unit.statement_ordinal) {
                layout
                    .issues
                    .push(SourceUnitLayoutIssue::DuplicateSyntaxOrdinal {
                        ordinal: unit.statement_ordinal,
                    });
                continue;
            }
            let Some(location) = syntax_by_ordinal.get(&unit.statement_ordinal) else {
                layout
                    .issues
                    .push(SourceUnitLayoutIssue::MissingSyntaxLocation {
                        source_unit: unit.id,
                        ordinal: unit.statement_ordinal,
                    });
                continue;
            };
            if location.statement.0 >= body_len {
                layout
                    .issues
                    .push(SourceUnitLayoutIssue::BodyStatementOutOfRange {
                        source_unit: unit.id,
                        statement: location.statement,
                        body_len,
                    });
                continue;
            }
            let row = SourceUnitLocation {
                source_unit: unit.id,
                ordinal: location.ordinal,
                statement: location.statement,
                declarator_offset: location.declarator_offset,
            };
            layout.by_ordinal.insert(row.ordinal, row.source_unit);
            layout
                .by_statement
                .entry(row.statement)
                .or_default()
                .push(row.source_unit);
            layout.by_id.insert(row.source_unit, row);
        }
        layout
    }

    pub fn by_id(&self) -> &BTreeMap<SourceUnitId, SourceUnitLocation> {
        &self.by_id
    }

    pub fn by_ordinal(&self, ordinal: SourceUnitOrdinal) -> Option<&SourceUnitLocation> {
        self.by_ordinal
            .get(&ordinal)
            .and_then(|source_unit| self.by_id.get(source_unit))
    }

    pub fn by_source_unit(&self, source_unit: SourceUnitId) -> Option<&SourceUnitLocation> {
        self.by_id.get(&source_unit)
    }

    pub fn for_statement(&self, statement: StatementId) -> Vec<&SourceUnitLocation> {
        let mut locations: Vec<_> = self
            .by_statement
            .get(&statement)
            .into_iter()
            .flatten()
            .filter_map(|source_unit| self.by_id.get(source_unit))
            .collect();
        locations.sort_by_key(|location| location.ordinal);
        locations
    }

    pub fn issues(&self) -> &[SourceUnitLayoutIssue] {
        &self.issues
    }

    pub fn body_len(&self) -> usize {
        self.body_len
    }
}

#[derive(Debug, Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StructuralPlacementCheck {
    DestructuringDeclaratorGrouping,
    SourceUnitToBodyMapping,
    BindingAndSharedVarDestinationProjection,
    WholeStatementDestinationProjection,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PlacementProjectionViolation {
    SourceUnitLayout {
        issue: SourceUnitLayoutIssue,
    },
    DestinationOutOfRange {
        source_unit: SourceUnitId,
        destination: ModuleId,
    },
    DeclaratorGroupLocationMismatch {
        group: DestructuringDeclaratorGroup,
    },
    DeclaratorGroupMissingLayout {
        group: DestructuringDeclaratorGroup,
    },
    DeclaratorGroupBindingsMismatch {
        group: DestructuringDeclaratorGroup,
        source_unit: SourceUnitId,
    },
    BindingDestinationMismatch {
        binding: Id,
        source_units: Vec<SourceUnitId>,
        destinations: Vec<ModuleId>,
    },
    OwnedBindingMissingSourceProjection {
        binding: Id,
        destination: ModuleId,
    },
    OwnedBindingDestinationMismatch {
        binding: Id,
        declared_destination: ModuleId,
        source_destination: ModuleId,
    },
    DestructuringDestinationSplit {
        group: DestructuringDeclaratorGroup,
        destinations: Vec<ModuleId>,
    },
    NestedVarMissingOwner {
        statement: StatementId,
        binding: Id,
    },
    NestedVarMissingSourceProjection {
        statement: StatementId,
        binding: Id,
    },
    NestedVarMissingWholeStatementClaim {
        statement: StatementId,
    },
    NestedVarDestinationSplit {
        statement: StatementId,
        destinations: Vec<ModuleId>,
    },
    StatementSourceDestinationSplit {
        statement: StatementId,
        source_units: Vec<SourceUnitId>,
        destinations: Vec<ModuleId>,
    },
    MissingWholeStatementClaim {
        statement: StatementId,
        source_unit: SourceUnitId,
        destination: ModuleId,
    },
    WholeStatementWithoutSourceUnit {
        statement: StatementId,
        requested_destination: usize,
    },
    WholeStatementDestinationMismatch {
        statement: StatementId,
        requested_destination: usize,
        source_destination: ModuleId,
    },
    StableSetIntervalDestinationSplit {
        interval_index: usize,
        bindings: Vec<Id>,
        destinations: Vec<ModuleId>,
    },
    StableSetIntervalMissingSourceProjection {
        interval_index: usize,
        binding: Id,
    },
}

impl PlacementProjectionViolation {
    /// Render one typed violation for a human-facing refusal message. Policy
    /// decisions consume the enum above, never this diagnostic text.
    pub fn diagnostic(&self) -> String {
        match self {
            Self::StableSetIntervalDestinationSplit {
                interval_index,
                bindings,
                ..
            } => format!(
                "certified Set spread interval {interval_index} (bindings {}) spans several output modules",
                bindings
                    .iter()
                    .map(|binding| format!("`{}`", binding.0))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            _ => format!("{self:?}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct PlacementProjectionAssessment {
    /// Checks remain unresolved only when their canonical input domain could
    /// not be checked (for example, a graph unit has no syntax location).
    unresolved_checks: BTreeSet<StructuralPlacementCheck>,
    violations: Vec<PlacementProjectionViolation>,
}

impl PlacementProjectionAssessment {
    pub fn is_complete(&self) -> bool {
        self.unresolved_checks.is_empty()
    }

    pub fn has_blockers(&self) -> bool {
        !self.violations.is_empty()
    }

    pub fn unresolved_checks(&self) -> &BTreeSet<StructuralPlacementCheck> {
        &self.unresolved_checks
    }

    pub fn violations(&self) -> &[PlacementProjectionViolation] {
        &self.violations
    }
}

#[derive(Debug, Clone)]
pub struct PlacementEmissionMaps {
    bindings: HashMap<Id, usize>,
    statements: BTreeMap<StatementId, usize>,
}

impl PlacementEmissionMaps {
    pub fn bindings(&self) -> &HashMap<Id, usize> {
        &self.bindings
    }

    pub fn statements(&self) -> &BTreeMap<StatementId, usize> {
        &self.statements
    }
}

#[derive(Debug, Clone)]
pub enum PlacementProjectionOutcome {
    Mappable(PlacementEmissionMaps),
    Unmappable,
}

#[derive(Debug, Clone)]
pub struct PlacementProjection<'a> {
    /// The exact immutable assignment used to compute the maps.
    assignment: &'a ModuleAssignment,
    outcome: PlacementProjectionOutcome,
    assessment: PlacementProjectionAssessment,
}

impl PlacementProjection<'_> {
    pub fn assignment(&self) -> &ModuleAssignment {
        self.assignment
    }

    pub fn assessment(&self) -> &PlacementProjectionAssessment {
        &self.assessment
    }

    pub fn emission_maps(&self) -> Option<&PlacementEmissionMaps> {
        match &self.outcome {
            PlacementProjectionOutcome::Mappable(maps) => Some(maps),
            PlacementProjectionOutcome::Unmappable => None,
        }
    }

    pub fn outcome(&self) -> &PlacementProjectionOutcome {
        &self.outcome
    }
}

/// Inputs are all final, canonical products already present in materialization:
/// the assignment, retained source-layout pass, complete AST body, final
/// whole-statement requests, ownership catalogue, and typed stable intervals.
pub struct PlacementProjectionInput<'a> {
    pub graph: &'a DependencyGraph,
    pub assignment: &'a ModuleAssignment,
    pub local_bindings: LocalBindings<'a>,
    pub body: &'a [ModuleItem],
    pub layout: &'a SourceUnitLayout,
    pub whole_statements: &'a BTreeMap<StatementId, usize>,
    pub module_count: usize,
    pub destructuring_groups: &'a [DestructuringDeclaratorGroup],
    pub nested_var_statements: &'a [NestedVarStatement],
    pub stable_set_intervals: &'a [StableSetIterableUse],
}

/// Project source-unit owners into the exact destination maps used by the
/// emitter while checking every scoped structural-placement obligation.
pub fn project_placement<'a>(input: PlacementProjectionInput<'a>) -> PlacementProjection<'a> {
    let entry = input.assignment.entry_destination().module_id();
    let mut unresolved_checks = BTreeSet::new();
    let mut violations = Vec::new();
    let mut unit_destinations = BTreeMap::<SourceUnitId, ModuleId>::new();
    let mut bindings_by_id = HashMap::<Id, (ModuleId, Vec<SourceUnitId>)>::new();
    let mut statement_sources = BTreeMap::<StatementId, Vec<(SourceUnitId, ModuleId, bool)>>::new();

    for issue in input.layout.issues() {
        violations.push(PlacementProjectionViolation::SourceUnitLayout {
            issue: issue.clone(),
        });
    }
    let layout_complete = input.layout.issues().is_empty()
        && input.layout.by_id().len() == input.graph.num_nodes()
        && input.layout.body_len() == input.body.len();
    if input.layout.body_len() != input.body.len() {
        violations.push(PlacementProjectionViolation::SourceUnitLayout {
            issue: SourceUnitLayoutIssue::BodyLengthMismatch {
                layout_body_len: input.layout.body_len(),
                projection_body_len: input.body.len(),
            },
        });
    }
    if !layout_complete {
        unresolved_checks.extend([
            StructuralPlacementCheck::DestructuringDeclaratorGrouping,
            StructuralPlacementCheck::SourceUnitToBodyMapping,
            StructuralPlacementCheck::BindingAndSharedVarDestinationProjection,
            StructuralPlacementCheck::WholeStatementDestinationProjection,
        ]);
    }

    let graph_units_by_id: BTreeMap<_, _> = input
        .graph
        .iter_nodes()
        .map(|unit| (unit.id, unit))
        .collect();
    for unit in input.graph.iter_nodes() {
        let Some(location) = input.layout.by_source_unit(unit.id) else {
            continue;
        };
        if location.ordinal != unit.statement_ordinal {
            // A mismatched ordinal is represented as a missing canonical
            // location; the layout's constructor normally prevents this.
            unresolved_checks.extend([
                StructuralPlacementCheck::SourceUnitToBodyMapping,
                StructuralPlacementCheck::DestructuringDeclaratorGrouping,
                StructuralPlacementCheck::BindingAndSharedVarDestinationProjection,
                StructuralPlacementCheck::WholeStatementDestinationProjection,
            ]);
            continue;
        }
        let destination = input.assignment.of(unit.id);
        unit_destinations.insert(unit.id, destination);
        if destination != entry && destination.0 >= input.module_count {
            violations.push(PlacementProjectionViolation::DestinationOutOfRange {
                source_unit: unit.id,
                destination,
            });
        }
        for binding in &unit.declared {
            let row = bindings_by_id
                .entry(binding.clone())
                .or_insert_with(|| (destination, Vec::new()));
            row.1.push(unit.id);
        }
        statement_sources
            .entry(location.statement)
            .or_default()
            .push((
                unit.id,
                destination,
                unit.class() == SourceUnitClass::AnonymousStatement,
            ));
    }

    let mut binding_destinations = HashMap::<Id, ModuleId>::new();
    for (binding, (first_destination, source_units)) in &bindings_by_id {
        let mut destinations: Vec<_> = source_units
            .iter()
            .filter_map(|unit| unit_destinations.get(unit).copied())
            .collect();
        destinations.sort_unstable();
        destinations.dedup();
        if destinations.len() > 1 {
            violations.push(PlacementProjectionViolation::BindingDestinationMismatch {
                binding: binding.clone(),
                source_units: source_units.clone(),
                destinations,
            });
        }
        binding_destinations.insert(binding.clone(), *first_destination);
    }

    for (binding, kind) in input.local_bindings.as_map() {
        if let BindingKind::Owned { module } = kind {
            match binding_destinations.get(binding) {
                None => violations.push(
                    PlacementProjectionViolation::OwnedBindingMissingSourceProjection {
                        binding: binding.clone(),
                        destination: *module,
                    },
                ),
                Some(source_destination) if source_destination != module => violations.push(
                    PlacementProjectionViolation::OwnedBindingDestinationMismatch {
                        binding: binding.clone(),
                        declared_destination: *module,
                        source_destination: *source_destination,
                    },
                ),
                Some(_) => {}
            }
        }
    }

    for group in input.destructuring_groups {
        let Some(location) = input.layout.by_ordinal(group.ordinal) else {
            unresolved_checks.insert(StructuralPlacementCheck::DestructuringDeclaratorGrouping);
            violations.push(PlacementProjectionViolation::DeclaratorGroupMissingLayout {
                group: group.clone(),
            });
            continue;
        };
        if location.statement != group.statement
            || location.declarator_offset != Some(group.declarator_offset)
        {
            violations.push(
                PlacementProjectionViolation::DeclaratorGroupLocationMismatch {
                    group: group.clone(),
                },
            );
            continue;
        }
        let Some(unit) = graph_units_by_id.get(&location.source_unit).copied() else {
            violations.push(
                PlacementProjectionViolation::DeclaratorGroupLocationMismatch {
                    group: group.clone(),
                },
            );
            continue;
        };
        let group_bindings: BTreeSet<_> = group.bindings.iter().cloned().collect();
        if group_bindings != unit.declared {
            violations.push(
                PlacementProjectionViolation::DeclaratorGroupBindingsMismatch {
                    group: group.clone(),
                    source_unit: unit.id,
                },
            );
        }
        let owners: Vec<_> = group
            .bindings
            .iter()
            .filter_map(|binding| {
                let source_destination = binding_destinations.get(binding).copied()?;
                let destination = match input.local_bindings.as_map().get(binding) {
                    // A source unit's graph assignment can collapse several
                    // explicit claims to one destination. Classify the
                    // retained hygienic requests only after the canonical
                    // source projection proves this binding is present.
                    Some(BindingKind::Owned { module }) => *module,
                    _ => source_destination,
                };
                Some((binding.clone(), destination))
            })
            .collect();
        let present_bindings: BTreeSet<_> =
            owners.iter().map(|(binding, _)| binding.clone()).collect();
        if group
            .bindings
            .iter()
            .any(|binding| !present_bindings.contains(binding))
        {
            unresolved_checks.insert(StructuralPlacementCheck::DestructuringDeclaratorGrouping);
            continue;
        }
        let decision = classify_destructuring_group_owners(group, owners);
        match decision.classification {
            DestructuringGroupOwnerClassification::Split { destinations } => {
                violations.push(
                    PlacementProjectionViolation::DestructuringDestinationSplit {
                        group: decision.group,
                        destinations,
                    },
                );
            }
            DestructuringGroupOwnerClassification::Unclaimed => {
                unresolved_checks.insert(StructuralPlacementCheck::DestructuringDeclaratorGrouping);
            }
            DestructuringGroupOwnerClassification::OneOwner { .. } => {}
        }
    }

    for nested in input.nested_var_statements {
        for binding in &nested.bindings {
            if !binding_destinations.contains_key(binding) {
                violations.push(
                    PlacementProjectionViolation::NestedVarMissingSourceProjection {
                        statement: nested.statement,
                        binding: binding.clone(),
                    },
                );
            }
        }
        let explicit_owners: Vec<_> = nested
            .bindings
            .iter()
            .filter_map(|binding| match input.local_bindings.as_map().get(binding) {
                Some(BindingKind::Owned { module }) => Some((binding.clone(), *module)),
                _ => None,
            })
            .collect();
        let whole_statement_request = input
            .whole_statements
            .get(&nested.statement)
            .copied()
            .map(ModuleId);
        let has_ownership_request =
            !explicit_owners.is_empty() || whole_statement_request.is_some();
        // Retain conflicting/partial authored claims even when one graph node
        // projects all of its bindings to a single destination. With no claims,
        // the completed source projection supplies ordinary entry placement.
        let owners = if has_ownership_request {
            explicit_owners
        } else {
            nested
                .bindings
                .iter()
                .filter_map(|binding| {
                    binding_destinations
                        .get(binding)
                        .copied()
                        .map(|destination| (binding.clone(), destination))
                })
                .collect()
        };
        let check = classify_nested_var_placement(nested, owners, whole_statement_request);
        if has_ownership_request {
            for binding in &check.missing_bindings {
                violations.push(PlacementProjectionViolation::NestedVarMissingOwner {
                    statement: check.statement,
                    binding: binding.clone(),
                });
            }
        }
        if check.requires_whole_statement_claim(entry) {
            violations.push(
                PlacementProjectionViolation::NestedVarMissingWholeStatementClaim {
                    statement: check.statement,
                },
            );
        }
        if let NestedVarDestinationClassification::SplitDestinations { destinations } =
            &check.classification
        {
            violations.push(PlacementProjectionViolation::NestedVarDestinationSplit {
                statement: check.statement,
                destinations: destinations.clone(),
            });
        }
    }

    let mut statement_destinations = BTreeMap::<StatementId, ModuleId>::new();
    for (statement, sources) in &statement_sources {
        let destinations: BTreeSet<_> = sources
            .iter()
            .map(|(_, destination, _)| *destination)
            .collect();
        if input.whole_statements.contains_key(statement) {
            if destinations.len() > 1 {
                violations.push(
                    PlacementProjectionViolation::StatementSourceDestinationSplit {
                        statement: *statement,
                        source_units: sources.iter().map(|(unit, _, _)| *unit).collect(),
                        destinations: destinations.iter().copied().collect(),
                    },
                );
            }
            if let Some(destination) = destinations.first() {
                statement_destinations.insert(*statement, *destination);
            }
        } else {
            for (source_unit, destination, anonymous) in sources {
                if *anonymous && *destination != entry {
                    violations.push(PlacementProjectionViolation::MissingWholeStatementClaim {
                        statement: *statement,
                        source_unit: *source_unit,
                        destination: *destination,
                    });
                }
            }
        }
    }

    let mut statement_map = BTreeMap::new();
    for (&statement, &requested_destination) in input.whole_statements {
        let Some(source_destination) = statement_destinations.get(&statement).copied() else {
            violations.push(
                PlacementProjectionViolation::WholeStatementWithoutSourceUnit {
                    statement,
                    requested_destination,
                },
            );
            continue;
        };
        if source_destination == entry || source_destination.0 != requested_destination {
            violations.push(
                PlacementProjectionViolation::WholeStatementDestinationMismatch {
                    statement,
                    requested_destination,
                    source_destination,
                },
            );
            continue;
        }
        statement_map.insert(statement, source_destination.0);
    }

    for (interval_index, interval) in input.stable_set_intervals.iter().enumerate() {
        let mut destinations = BTreeSet::new();
        for binding in &interval.interval_bindings {
            match binding_destinations.get(binding) {
                Some(destination) => {
                    destinations.insert(*destination);
                }
                None => violations.push(
                    PlacementProjectionViolation::StableSetIntervalMissingSourceProjection {
                        interval_index,
                        binding: binding.clone(),
                    },
                ),
            }
        }
        if destinations.len() > 1 {
            violations.push(
                PlacementProjectionViolation::StableSetIntervalDestinationSplit {
                    interval_index,
                    bindings: interval.interval_bindings.clone(),
                    destinations: destinations.into_iter().collect(),
                },
            );
        }
    }

    let complete = unresolved_checks.is_empty();
    let outcome = if violations.is_empty() && complete {
        PlacementProjectionOutcome::Mappable(PlacementEmissionMaps {
            bindings: binding_destinations
                .into_iter()
                .filter(|(_, destination)| *destination != entry)
                .map(|(binding, destination)| (binding, destination.0))
                .collect(),
            statements: statement_map,
        })
    } else {
        PlacementProjectionOutcome::Unmappable
    };
    PlacementProjection {
        assignment: input.assignment,
        outcome,
        assessment: PlacementProjectionAssessment {
            unresolved_checks,
            violations,
        },
    }
}
