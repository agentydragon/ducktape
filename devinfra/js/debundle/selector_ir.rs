//! Engine-facing selector IR and fact-store contract for the global selector
//! solver.
//!
//! This module gives `source_match` lowering, relational selector lowering, the
//! Ascent solver, and materialization one shared vocabulary.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{ChunkId, OwnerId, StatementOrdinal};
use serde::{Deserialize, Serialize};

/// Dense id of a solver variable in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SelectorVariableId(pub usize);

/// Dense id of a materializer claim target in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SelectorTargetId(pub usize);

/// The relation domain a solver variable ranges over.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VariableDomain {
    Owner,
    String,
}

/// One variable in the global selector constraint program.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorVariable {
    pub id: SelectorVariableId,
    pub domain: VariableDomain,
    /// Optional authoring/debug name, e.g. `@Button` or `needle.return`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debug_name: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum OwnerTerm {
    Var { id: SelectorVariableId },
    Const { owner: OwnerId },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum StringTerm {
    Var { id: SelectorVariableId },
    Const { value: String },
}

/// Materializer-facing shape of a target once the solver has selected an owner.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimKind {
    /// A normal exported member claim. The owner should declare `binding`.
    Binding {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        export_name: Option<String>,
    },
    /// An anonymous top-level statement claim. The owner may declare no binding.
    AnonymousStatement,
    /// One target in a `source_matches[].bindings[]` selector.
    BindingGroupMember {
        export_name: String,
        target_binding: String,
    },
}

/// Where a target came from in the spec/lowering pipeline.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimOrigin {
    MemberSelector,
    BindingGroup { group_index: usize },
    AnonymousStatement { index: usize },
    RelationalSelector { relation: RelationalPrimitive },
    Synthetic,
}

/// Current relational primitives that should become solver atoms rather than
/// late materializer bridge passes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelationalPrimitive {
    CrossRef,
    ReadsMember,
    MemberOfModule,
    PassedToCall,
    MakesDecorateCall,
    IntrinsicAlias,
}

/// One claim the materializer will consume after solving.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorTarget {
    pub id: SelectorTargetId,
    pub chunk_id: ChunkId,
    pub owner: SelectorVariableId,
    /// Spec logical module path/key as authored. Later integration can replace
    /// this with the materializer's module id once lowering has that table.
    pub logical_module: String,
    pub claim: ClaimKind,
    pub origin: ClaimOrigin,
}

/// One conjunctive-query atom in the lowered selector program.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SelectorAtom {
    OwnerKind {
        owner: OwnerTerm,
        statement_kind: StringTerm,
    },
    OwnerDeclaresBinding {
        owner: OwnerTerm,
        binding: StringTerm,
    },
    ProjectedAllowedTuples {
        variables: Vec<SelectorVariableId>,
        rows: Vec<Vec<SelectorProjectedValue>>,
        reason: String,
    },
    OwnerExportName {
        owner: OwnerTerm,
        export_name: StringTerm,
    },
    OwnerReferencesBinding {
        owner: OwnerTerm,
        binding: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        edge_kind: Option<StringTerm>,
    },
    OwnerReferencesOwner {
        owner: OwnerTerm,
        referenced: OwnerTerm,
    },
    OwnerAliasesOwner {
        owner: OwnerTerm,
        aliased: OwnerTerm,
    },
    ReadsMember {
        owner: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        object: Option<StringTerm>,
        member: StringTerm,
    },
    ReadsMemberOfOwner {
        owner: OwnerTerm,
        object: OwnerTerm,
        member: StringTerm,
    },
    ConsumesModuleMember {
        owner: OwnerTerm,
        module: StringTerm,
        member: StringTerm,
    },
    PassedToCall {
        owner: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callee_object: Option<StringTerm>,
        callee_member: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        arg_index: Option<u32>,
    },
    PassedToCallOfOwner {
        owner: OwnerTerm,
        callee_object: OwnerTerm,
        callee_member: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        arg_index: Option<u32>,
    },
    MakesDecorateCall {
        owner: OwnerTerm,
        class_anchor: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<StringTerm>,
    },
    MakesDecorateCallForOwner {
        owner: OwnerTerm,
        class_anchor: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<StringTerm>,
    },
    IntrinsicAlias {
        owner: OwnerTerm,
        property: StringTerm,
        referenced_by: OwnerTerm,
    },
}

impl SelectorAtom {
    fn variable_ids(&self) -> BTreeSet<SelectorVariableId> {
        let mut variables = BTreeSet::new();
        match self {
            Self::OwnerKind {
                owner,
                statement_kind,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(statement_kind, &mut variables);
            }
            Self::OwnerDeclaresBinding { owner, binding }
            | Self::OwnerExportName {
                owner,
                export_name: binding,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(binding, &mut variables);
            }
            Self::ProjectedAllowedTuples {
                variables: projected,
                ..
            } => {
                variables.extend(projected.iter().copied());
            }
            Self::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(binding, &mut variables);
                if let Some(edge_kind) = edge_kind {
                    collect_string_term_variables(edge_kind, &mut variables);
                }
            }
            Self::OwnerReferencesOwner { owner, referenced }
            | Self::OwnerAliasesOwner {
                owner,
                aliased: referenced,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_owner_term_variables(referenced, &mut variables);
            }
            Self::ReadsMember {
                owner,
                object,
                member,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                if let Some(object) = object {
                    collect_string_term_variables(object, &mut variables);
                }
                collect_string_term_variables(member, &mut variables);
            }
            Self::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_owner_term_variables(object, &mut variables);
                collect_string_term_variables(member, &mut variables);
            }
            Self::ConsumesModuleMember {
                owner,
                module,
                member,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(module, &mut variables);
                collect_string_term_variables(member, &mut variables);
            }
            Self::PassedToCall {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                collect_owner_term_variables(owner, &mut variables);
                if let Some(callee_object) = callee_object {
                    collect_string_term_variables(callee_object, &mut variables);
                }
                collect_string_term_variables(callee_member, &mut variables);
            }
            Self::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_owner_term_variables(callee_object, &mut variables);
                collect_string_term_variables(callee_member, &mut variables);
            }
            Self::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(class_anchor, &mut variables);
                if let Some(member) = member {
                    collect_string_term_variables(member, &mut variables);
                }
            }
            Self::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_owner_term_variables(class_anchor, &mut variables);
                if let Some(member) = member {
                    collect_string_term_variables(member, &mut variables);
                }
            }
            Self::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => {
                collect_owner_term_variables(owner, &mut variables);
                collect_string_term_variables(property, &mut variables);
                collect_owner_term_variables(referenced_by, &mut variables);
            }
        }
        variables
    }

    fn remap_variables(
        &self,
        variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
    ) -> Self {
        match self {
            Self::OwnerKind {
                owner,
                statement_kind,
            } => Self::OwnerKind {
                owner: remap_owner_term(owner, variable_map),
                statement_kind: remap_string_term(statement_kind, variable_map),
            },
            Self::OwnerDeclaresBinding { owner, binding } => Self::OwnerDeclaresBinding {
                owner: remap_owner_term(owner, variable_map),
                binding: remap_string_term(binding, variable_map),
            },
            Self::ProjectedAllowedTuples {
                variables,
                rows,
                reason,
            } => Self::ProjectedAllowedTuples {
                variables: variables
                    .iter()
                    .map(|variable| remap_variable_id(*variable, variable_map))
                    .collect(),
                rows: rows.clone(),
                reason: reason.clone(),
            },
            Self::OwnerExportName { owner, export_name } => Self::OwnerExportName {
                owner: remap_owner_term(owner, variable_map),
                export_name: remap_string_term(export_name, variable_map),
            },
            Self::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => Self::OwnerReferencesBinding {
                owner: remap_owner_term(owner, variable_map),
                binding: remap_string_term(binding, variable_map),
                edge_kind: edge_kind
                    .as_ref()
                    .map(|edge_kind| remap_string_term(edge_kind, variable_map)),
            },
            Self::OwnerReferencesOwner { owner, referenced } => Self::OwnerReferencesOwner {
                owner: remap_owner_term(owner, variable_map),
                referenced: remap_owner_term(referenced, variable_map),
            },
            Self::OwnerAliasesOwner { owner, aliased } => Self::OwnerAliasesOwner {
                owner: remap_owner_term(owner, variable_map),
                aliased: remap_owner_term(aliased, variable_map),
            },
            Self::ReadsMember {
                owner,
                object,
                member,
            } => Self::ReadsMember {
                owner: remap_owner_term(owner, variable_map),
                object: object
                    .as_ref()
                    .map(|object| remap_string_term(object, variable_map)),
                member: remap_string_term(member, variable_map),
            },
            Self::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => Self::ReadsMemberOfOwner {
                owner: remap_owner_term(owner, variable_map),
                object: remap_owner_term(object, variable_map),
                member: remap_string_term(member, variable_map),
            },
            Self::ConsumesModuleMember {
                owner,
                module,
                member,
            } => Self::ConsumesModuleMember {
                owner: remap_owner_term(owner, variable_map),
                module: remap_string_term(module, variable_map),
                member: remap_string_term(member, variable_map),
            },
            Self::PassedToCall {
                owner,
                callee_object,
                callee_member,
                arg_index,
            } => Self::PassedToCall {
                owner: remap_owner_term(owner, variable_map),
                callee_object: callee_object
                    .as_ref()
                    .map(|callee_object| remap_string_term(callee_object, variable_map)),
                callee_member: remap_string_term(callee_member, variable_map),
                arg_index: *arg_index,
            },
            Self::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                arg_index,
            } => Self::PassedToCallOfOwner {
                owner: remap_owner_term(owner, variable_map),
                callee_object: remap_owner_term(callee_object, variable_map),
                callee_member: remap_string_term(callee_member, variable_map),
                arg_index: *arg_index,
            },
            Self::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => Self::MakesDecorateCall {
                owner: remap_owner_term(owner, variable_map),
                class_anchor: remap_string_term(class_anchor, variable_map),
                member: member
                    .as_ref()
                    .map(|member| remap_string_term(member, variable_map)),
            },
            Self::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => Self::MakesDecorateCallForOwner {
                owner: remap_owner_term(owner, variable_map),
                class_anchor: remap_owner_term(class_anchor, variable_map),
                member: member
                    .as_ref()
                    .map(|member| remap_string_term(member, variable_map)),
            },
            Self::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => Self::IntrinsicAlias {
                owner: remap_owner_term(owner, variable_map),
                property: remap_string_term(property, variable_map),
                referenced_by: remap_owner_term(referenced_by, variable_map),
            },
        }
    }
}

fn collect_owner_term_variables(term: &OwnerTerm, variables: &mut BTreeSet<SelectorVariableId>) {
    if let OwnerTerm::Var { id } = term {
        variables.insert(*id);
    }
}

fn collect_string_term_variables(term: &StringTerm, variables: &mut BTreeSet<SelectorVariableId>) {
    if let StringTerm::Var { id } = term {
        variables.insert(*id);
    }
}

fn remap_variable_id(
    variable: SelectorVariableId,
    variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
) -> SelectorVariableId {
    *variable_map
        .get(&variable)
        .expect("slice atom variable must be present in remap")
}

fn remap_owner_term(
    term: &OwnerTerm,
    variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
) -> OwnerTerm {
    match term {
        OwnerTerm::Var { id } => OwnerTerm::Var {
            id: remap_variable_id(*id, variable_map),
        },
        OwnerTerm::Const { owner } => OwnerTerm::Const { owner: *owner },
    }
}

fn remap_string_term(
    term: &StringTerm,
    variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
) -> StringTerm {
    match term {
        StringTerm::Var { id } => StringTerm::Var {
            id: remap_variable_id(*id, variable_map),
        },
        StringTerm::Const { value } => StringTerm::Const {
            value: value.clone(),
        },
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum SelectorProjectedValue {
    Owner(OwnerId),
    String(String),
}

impl SelectorProjectedValue {
    pub fn domain(&self) -> VariableDomain {
        match self {
            Self::Owner(_) => VariableDomain::Owner,
            Self::String(_) => VariableDomain::String,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectorSourceMatchProjectionOutcome {
    Projected,
    /// The matcher placed the selector nowhere (or failed); it is reported
    /// unmatched without entering the solve.
    NotProjected,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorSourceMatchProjectionEvent {
    pub selector_kind: String,
    pub logical_module: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub export_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_binding: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub exports_by_target: BTreeMap<String, String>,
    pub outcome: SelectorSourceMatchProjectionOutcome,
    pub reason_category: String,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub candidate_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projected_row_count: Option<usize>,
    pub selector_preview: String,
    pub selector_hash: String,
    pub selector_body_hash: String,
}

/// Whole lowered selector program for one chunk/component solve.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorProgram {
    pub variables: Vec<SelectorVariable>,
    pub targets: Vec<SelectorTarget>,
    pub atoms: Vec<SelectorAtom>,
    /// Sets of target ids that must land on distinct owners.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub all_different: Vec<Vec<SelectorTargetId>>,
    /// Sets of variables that must land on distinct values.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub all_different_variables: Vec<SelectorVariableAllDifferent>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub source_match_projection: Vec<SelectorSourceMatchProjectionEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorVariableAllDifferent {
    pub variables: Vec<SelectorVariableId>,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectorProgramSlice {
    pub program: SelectorProgram,
    pub old_to_new_targets: BTreeMap<SelectorTargetId, SelectorTargetId>,
    pub new_to_old_targets: BTreeMap<SelectorTargetId, SelectorTargetId>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SelectorProgramSliceOptions {
    pub include_target_all_different: bool,
}

impl SelectorProgram {
    pub fn add_variable(
        &mut self,
        domain: VariableDomain,
        debug_name: Option<String>,
    ) -> SelectorVariableId {
        let id = SelectorVariableId(self.variables.len());
        self.variables.push(SelectorVariable {
            id,
            domain,
            debug_name,
        });
        id
    }

    pub fn add_target(
        &mut self,
        chunk_id: ChunkId,
        owner: SelectorVariableId,
        logical_module: impl Into<String>,
        claim: ClaimKind,
        origin: ClaimOrigin,
    ) -> SelectorTargetId {
        let id = SelectorTargetId(self.targets.len());
        self.targets.push(SelectorTarget {
            id,
            chunk_id,
            owner,
            logical_module: logical_module.into(),
            claim,
            origin,
        });
        id
    }

    pub fn add_atom(&mut self, atom: SelectorAtom) {
        self.atoms.push(atom);
    }

    pub fn add_source_match_projection_event(&mut self, event: SelectorSourceMatchProjectionEvent) {
        self.source_match_projection.push(event);
    }

    pub fn require_all_different(&mut self, targets: Vec<SelectorTargetId>) {
        self.all_different.push(targets);
    }

    pub fn require_variables_all_different(
        &mut self,
        variables: Vec<SelectorVariableId>,
        label: impl Into<String>,
    ) {
        self.all_different_variables
            .push(SelectorVariableAllDifferent {
                variables,
                label: label.into(),
            });
    }

    pub fn slice_for_targets(
        &self,
        targets: &BTreeSet<SelectorTargetId>,
        options: SelectorProgramSliceOptions,
    ) -> Result<SelectorProgramSlice, SelectorProgramError> {
        for target in targets {
            self.require_target(*target)?;
        }

        let mut kept_variables = BTreeSet::<SelectorVariableId>::new();
        for target in targets {
            kept_variables.insert(self.targets[target.0].owner);
        }

        let mut kept_atoms = BTreeSet::<usize>::new();
        loop {
            let mut changed = false;
            for (idx, atom) in self.atoms.iter().enumerate() {
                let atom_variables = atom.variable_ids();
                if atom_variables.is_empty()
                    || !atom_variables
                        .iter()
                        .any(|variable| kept_variables.contains(variable))
                {
                    continue;
                }
                if kept_atoms.insert(idx) {
                    changed = true;
                }
                for variable in atom_variables {
                    changed |= kept_variables.insert(variable);
                }
            }
            for constraint in &self.all_different_variables {
                if !constraint
                    .variables
                    .iter()
                    .any(|variable| kept_variables.contains(variable))
                {
                    continue;
                }
                for variable in &constraint.variables {
                    changed |= kept_variables.insert(*variable);
                }
            }
            if !changed {
                break;
            }
        }

        let mut program = SelectorProgram::default();
        let mut variable_map = BTreeMap::<SelectorVariableId, SelectorVariableId>::new();
        for variable in &self.variables {
            if kept_variables.contains(&variable.id) {
                let remapped = program.add_variable(variable.domain, variable.debug_name.clone());
                variable_map.insert(variable.id, remapped);
            }
        }

        let mut old_to_new_targets = BTreeMap::<SelectorTargetId, SelectorTargetId>::new();
        let mut new_to_old_targets = BTreeMap::<SelectorTargetId, SelectorTargetId>::new();
        for target in &self.targets {
            if !targets.contains(&target.id) {
                continue;
            }
            let owner = *variable_map
                .get(&target.owner)
                .expect("selected target owner variable must be kept");
            let remapped = program.add_target(
                target.chunk_id,
                owner,
                target.logical_module.clone(),
                target.claim.clone(),
                target.origin.clone(),
            );
            old_to_new_targets.insert(target.id, remapped);
            new_to_old_targets.insert(remapped, target.id);
        }

        for idx in kept_atoms {
            program.add_atom(self.atoms[idx].remap_variables(&variable_map));
        }

        if options.include_target_all_different {
            for target_set in &self.all_different {
                let remapped = target_set
                    .iter()
                    .filter_map(|target| old_to_new_targets.get(target).copied())
                    .collect::<Vec<_>>();
                if remapped.len() >= 2 {
                    program.require_all_different(remapped);
                }
            }
        }
        for constraint in &self.all_different_variables {
            if !constraint
                .variables
                .iter()
                .any(|variable| variable_map.contains_key(variable))
            {
                continue;
            }
            let remapped = constraint
                .variables
                .iter()
                .filter_map(|variable| variable_map.get(variable).copied())
                .collect::<Vec<_>>();
            if remapped.len() >= 2 {
                program.require_variables_all_different(remapped, constraint.label.clone());
            }
        }

        program.validate()?;
        Ok(SelectorProgramSlice {
            program,
            old_to_new_targets,
            new_to_old_targets,
        })
    }

    pub fn validate(&self) -> Result<(), SelectorProgramError> {
        for (idx, variable) in self.variables.iter().enumerate() {
            if variable.id != SelectorVariableId(idx) {
                return Err(SelectorProgramError::NonDenseVariable {
                    expected: SelectorVariableId(idx),
                    actual: variable.id,
                });
            }
        }
        for (idx, target) in self.targets.iter().enumerate() {
            if target.id != SelectorTargetId(idx) {
                return Err(SelectorProgramError::NonDenseTarget {
                    expected: SelectorTargetId(idx),
                    actual: target.id,
                });
            }
            self.require_domain(target.owner, VariableDomain::Owner, "selector target owner")?;
        }
        for atom in &self.atoms {
            self.validate_atom(atom)?;
        }
        for target_set in &self.all_different {
            if target_set.len() < 2 {
                return Err(SelectorProgramError::DegenerateAllDifferent);
            }
            for target in target_set {
                self.require_target(*target)?;
            }
        }
        for variable_set in &self.all_different_variables {
            if variable_set.variables.len() < 2 {
                return Err(SelectorProgramError::DegenerateAllDifferent);
            }
            let mut expected_domain = None;
            for variable in &variable_set.variables {
                let domain = self.require_variable(*variable, "all_different_variables")?;
                match expected_domain {
                    None => expected_domain = Some(domain),
                    Some(expected) if expected != domain => {
                        return Err(SelectorProgramError::DomainMismatch {
                            context: "all_different_variables",
                            expected,
                            actual: domain,
                        });
                    }
                    Some(_) => {}
                }
            }
        }
        Ok(())
    }

    fn validate_atom(&self, atom: &SelectorAtom) -> Result<(), SelectorProgramError> {
        match atom {
            SelectorAtom::OwnerKind {
                owner,
                statement_kind,
            } => {
                self.validate_owner_term(owner, "owner_kind.owner")?;
                self.validate_string_term(statement_kind, "owner_kind.statement_kind")
            }
            SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
                self.validate_owner_term(owner, "owner_declares_binding.owner")?;
                self.validate_string_term(binding, "owner_declares_binding.binding")
            }
            SelectorAtom::ProjectedAllowedTuples {
                variables,
                rows,
                reason: _,
            } => {
                if variables.is_empty() {
                    return Err(SelectorProgramError::EmptyProjectedAllowedTuples);
                }
                let domains = variables
                    .iter()
                    .map(|variable| {
                        self.require_variable(*variable, "projected_allowed_tuples.variable")
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                for (row_index, row) in rows.iter().enumerate() {
                    if row.len() != variables.len() {
                        return Err(SelectorProgramError::ProjectedAllowedTupleArity {
                            row_index,
                            expected: variables.len(),
                            actual: row.len(),
                        });
                    }
                    for (column, (value, expected)) in row.iter().zip(domains.iter()).enumerate() {
                        let actual = value.domain();
                        if actual != *expected {
                            return Err(SelectorProgramError::ProjectedAllowedTupleDomain {
                                row_index,
                                column,
                                expected: *expected,
                                actual,
                            });
                        }
                    }
                }
                Ok(())
            }
            SelectorAtom::OwnerExportName { owner, export_name } => {
                self.validate_owner_term(owner, "owner_export_name.owner")?;
                self.validate_string_term(export_name, "owner_export_name.export_name")
            }
            SelectorAtom::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => {
                self.validate_owner_term(owner, "owner_references_binding.owner")?;
                self.validate_string_term(binding, "owner_references_binding.binding")?;
                if let Some(edge_kind) = edge_kind {
                    self.validate_string_term(edge_kind, "owner_references_binding.edge_kind")?;
                }
                Ok(())
            }
            SelectorAtom::OwnerReferencesOwner { owner, referenced } => {
                self.validate_owner_term(owner, "owner_references_owner.owner")?;
                self.validate_owner_term(referenced, "owner_references_owner.referenced")
            }
            SelectorAtom::OwnerAliasesOwner { owner, aliased } => {
                self.validate_owner_term(owner, "owner_aliases_owner.owner")?;
                self.validate_owner_term(aliased, "owner_aliases_owner.aliased")
            }
            SelectorAtom::ReadsMember {
                owner,
                object,
                member,
            } => {
                self.validate_owner_term(owner, "reads_member.owner")?;
                if let Some(object) = object {
                    self.validate_string_term(object, "reads_member.object")?;
                }
                self.validate_string_term(member, "reads_member.member")
            }
            SelectorAtom::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => {
                self.validate_owner_term(owner, "reads_member_of_owner.owner")?;
                self.validate_owner_term(object, "reads_member_of_owner.object")?;
                self.validate_string_term(member, "reads_member_of_owner.member")
            }
            SelectorAtom::ConsumesModuleMember {
                owner,
                module,
                member,
            } => {
                self.validate_owner_term(owner, "consumes_module_member.owner")?;
                self.validate_string_term(module, "consumes_module_member.module")?;
                self.validate_string_term(member, "consumes_module_member.member")
            }
            SelectorAtom::PassedToCall {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.validate_owner_term(owner, "passed_to_call.owner")?;
                if let Some(callee_object) = callee_object {
                    self.validate_string_term(callee_object, "passed_to_call.callee_object")?;
                }
                self.validate_string_term(callee_member, "passed_to_call.callee_member")
            }
            SelectorAtom::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.validate_owner_term(owner, "passed_to_call_of_owner.owner")?;
                self.validate_owner_term(callee_object, "passed_to_call_of_owner.callee_object")?;
                self.validate_string_term(callee_member, "passed_to_call_of_owner.callee_member")
            }
            SelectorAtom::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => {
                self.validate_owner_term(owner, "makes_decorate_call.owner")?;
                self.validate_string_term(class_anchor, "makes_decorate_call.class_anchor")?;
                if let Some(member) = member {
                    self.validate_string_term(member, "makes_decorate_call.member")?;
                }
                Ok(())
            }
            SelectorAtom::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => {
                self.validate_owner_term(owner, "makes_decorate_call_for_owner.owner")?;
                self.validate_owner_term(
                    class_anchor,
                    "makes_decorate_call_for_owner.class_anchor",
                )?;
                if let Some(member) = member {
                    self.validate_string_term(member, "makes_decorate_call_for_owner.member")?;
                }
                Ok(())
            }
            SelectorAtom::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => {
                self.validate_owner_term(owner, "intrinsic_alias.owner")?;
                self.validate_string_term(property, "intrinsic_alias.property")?;
                self.validate_owner_term(referenced_by, "intrinsic_alias.referenced_by")
            }
        }
    }

    fn validate_owner_term(
        &self,
        term: &OwnerTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let OwnerTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::Owner, context)?;
        }
        Ok(())
    }

    fn validate_string_term(
        &self,
        term: &StringTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let StringTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::String, context)?;
        }
        Ok(())
    }

    fn require_domain(
        &self,
        id: SelectorVariableId,
        expected: VariableDomain,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        let actual = self.require_variable(id, context)?;
        if actual != expected {
            return Err(SelectorProgramError::DomainMismatch {
                context,
                expected,
                actual,
            });
        }
        Ok(())
    }

    fn require_variable(
        &self,
        id: SelectorVariableId,
        context: &'static str,
    ) -> Result<VariableDomain, SelectorProgramError> {
        self.variables
            .get(id.0)
            .map(|v| v.domain)
            .ok_or(SelectorProgramError::UnknownVariable { context, id })
    }

    fn require_target(&self, id: SelectorTargetId) -> Result<(), SelectorProgramError> {
        if self.targets.get(id.0).is_none() {
            return Err(SelectorProgramError::UnknownTarget { id });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorProgramError {
    NonDenseVariable {
        expected: SelectorVariableId,
        actual: SelectorVariableId,
    },
    NonDenseTarget {
        expected: SelectorTargetId,
        actual: SelectorTargetId,
    },
    UnknownVariable {
        context: &'static str,
        id: SelectorVariableId,
    },
    UnknownTarget {
        id: SelectorTargetId,
    },
    DomainMismatch {
        context: &'static str,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    DegenerateAllDifferent,
    EmptyProjectedAllowedTuples,
    ProjectedAllowedTupleArity {
        row_index: usize,
        expected: usize,
        actual: usize,
    },
    ProjectedAllowedTupleDomain {
        row_index: usize,
        column: usize,
        expected: VariableDomain,
        actual: VariableDomain,
    },
}

impl fmt::Display for SelectorProgramError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonDenseVariable { expected, actual } => {
                write!(
                    f,
                    "selector variable ids must be dense: expected {expected:?}, found {actual:?}"
                )
            }
            Self::NonDenseTarget { expected, actual } => {
                write!(
                    f,
                    "selector target ids must be dense: expected {expected:?}, found {actual:?}"
                )
            }
            Self::UnknownVariable { context, id } => {
                write!(f, "{context} references unknown selector variable {id:?}")
            }
            Self::UnknownTarget { id } => {
                write!(f, "all_different references unknown selector target {id:?}")
            }
            Self::DomainMismatch {
                context,
                expected,
                actual,
            } => {
                write!(
                    f,
                    "{context} expected {expected:?} variable, found {actual:?}"
                )
            }
            Self::DegenerateAllDifferent => {
                write!(f, "all_different requires at least two entries")
            }
            Self::EmptyProjectedAllowedTuples => {
                write!(f, "projected_allowed_tuples requires at least one variable")
            }
            Self::ProjectedAllowedTupleArity {
                row_index,
                expected,
                actual,
            } => write!(
                f,
                "projected_allowed_tuples row {row_index} has arity {actual}, expected {expected}"
            ),
            Self::ProjectedAllowedTupleDomain {
                row_index,
                column,
                expected,
                actual,
            } => write!(
                f,
                "projected_allowed_tuples row {row_index} column {column} has domain {actual:?}, expected {expected:?}"
            ),
        }
    }
}

impl Error for SelectorProgramError {}

/// Solver EDB row. These are the stable serialization boundary between program
/// analysis and the fixed Ascent rule library.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SelectorFact {
    Owner {
        chunk_id: ChunkId,
        owner: OwnerId,
        statement_ordinal: StatementOrdinal,
        statement_kind: String,
    },
    DeclaredBinding {
        chunk_id: ChunkId,
        owner: OwnerId,
        binding: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        export_name: Option<String>,
    },
    OwnerReferencesBinding {
        chunk_id: ChunkId,
        owner: OwnerId,
        binding: String,
        edge_kind: String,
    },
    MemberRead {
        chunk_id: ChunkId,
        statement_ordinal: StatementOrdinal,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        object: Option<String>,
        member: String,
    },
    ModuleMemberUse {
        chunk_id: ChunkId,
        statement_ordinal: StatementOrdinal,
        module: String,
        member: String,
    },
    CallArgumentUse {
        chunk_id: ChunkId,
        argument: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callee_object: Option<String>,
        callee_member: String,
        arg_index: usize,
    },
    DecorateCallUse {
        chunk_id: ChunkId,
        callee: String,
        class_anchor: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<String>,
    },
    IntrinsicAliasUse {
        chunk_id: ChunkId,
        binding: String,
        property: String,
    },
}

impl SelectorFact {
    pub fn relation(&self) -> &'static str {
        match self {
            Self::Owner { .. } => "owner",
            Self::DeclaredBinding { .. } => "declared_binding",
            Self::OwnerReferencesBinding { .. } => "owner_references_binding",
            Self::MemberRead { .. } => "member_read",
            Self::ModuleMemberUse { .. } => "module_member_use",
            Self::CallArgumentUse { .. } => "call_argument_use",
            Self::DecorateCallUse { .. } => "decorate_call_use",
            Self::IntrinsicAliasUse { .. } => "intrinsic_alias_use",
        }
    }
}

/// Append-only EDB relation store for one solver invocation.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorFactStore {
    pub facts: Vec<SelectorFact>,
}

impl SelectorFactStore {
    pub fn push(&mut self, fact: SelectorFact) {
        self.facts.push(fact);
    }

    pub fn len(&self) -> usize {
        self.facts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.facts.is_empty()
    }

    pub fn counts_by_relation(&self) -> BTreeMap<&'static str, usize> {
        let mut counts = BTreeMap::new();
        for fact in &self.facts {
            *counts.entry(fact.relation()).or_insert(0) += 1;
        }
        counts
    }

    /// Import owner-graph facts that connect statement owners to their declared
    /// bindings and binding-use edges.
    pub fn extend_owner_graph_facts(
        &mut self,
        chunk_id: ChunkId,
        owner_graph: &analysis::OwnerGraph,
    ) {
        for node in owner_graph.iter_nodes() {
            self.push(SelectorFact::Owner {
                chunk_id,
                owner: node.id,
                statement_ordinal: node.statement_ordinal,
                statement_kind: node.kind.to_string(),
            });
            for binding in &node.declared {
                self.push(SelectorFact::DeclaredBinding {
                    chunk_id,
                    owner: node.id,
                    binding: binding.0.as_str().to_string(),
                    export_name: None,
                });
            }
        }
        for edge in owner_graph.iter_edges() {
            if let Some(binding) = edge.reason.binding() {
                self.push(SelectorFact::OwnerReferencesBinding {
                    chunk_id,
                    owner: edge.from,
                    binding: binding.0.as_str().to_string(),
                    edge_kind: edge.reason.kind().to_string(),
                });
            }
        }
    }
}

/// Solver output projected to materializer targets.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SolverResult {
    pub claims: Vec<SolverClaim>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub global_diagnostic: Option<SolverGlobalDiagnostic>,
}

impl SolverResult {
    pub fn outcome_for(&self, target: SelectorTargetId) -> Option<&ClaimOutcome> {
        self.claims
            .iter()
            .find(|claim| claim.target == target)
            .map(|claim| &claim.outcome)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SolverGlobalDiagnostic {
    pub category: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SolverClaim {
    pub target: SelectorTargetId,
    pub outcome: ClaimOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimOutcome {
    Unique {
        claim: ResolvedClaim,
    },
    NoMatch,
    Ambiguous {
        candidates: Vec<ResolvedClaim>,
        /// The backend stopped listing alternatives, so there may be more candidates.
        candidates_truncated: bool,
    },
    Duplicate {
        owner: OwnerId,
        conflicting_targets: Vec<SelectorTargetId>,
    },
    /// The target is in an unsatisfiable core: it cannot resolve together
    /// with `with`, the other targets of that core. The core need not be
    /// minimal.
    Conflict {
        with: Vec<SelectorTargetId>,
    },
    Unsupported {
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedClaim {
    pub chunk_id: ChunkId,
    pub owner: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub provenance: Vec<ProvenanceFact>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProvenanceFact {
    pub relation: String,
    pub summary: String,
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;

    fn const_str(value: &str) -> StringTerm {
        StringTerm::Const {
            value: value.to_string(),
        }
    }

    #[test]
    fn validates_owner_target_and_atom_domains() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("@Widget".to_string()));
        let member = program.add_target(
            ChunkId(0),
            owner,
            "runtime/widgets",
            ClaimKind::Binding {
                export_name: Some("Widget".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: const_str("a"),
        });
        program.require_all_different(vec![member, member]);

        assert_eq!(program.validate(), Ok(()));
    }

    #[test]
    fn rejects_non_owner_target_variable() {
        let mut program = SelectorProgram::default();
        let binding = program.add_variable(VariableDomain::String, None);
        program.add_target(
            ChunkId(0),
            binding,
            "runtime/widgets",
            ClaimKind::AnonymousStatement,
            ClaimOrigin::AnonymousStatement { index: 0 },
        );

        assert_eq!(
            program.validate(),
            Err(SelectorProgramError::DomainMismatch {
                context: "selector target owner",
                expected: VariableDomain::Owner,
                actual: VariableDomain::String,
            })
        );
    }

    #[test]
    fn validates_variable_all_different_domains() {
        let mut program = SelectorProgram::default();
        let left = program.add_variable(VariableDomain::String, Some("left".to_string()));
        let right = program.add_variable(VariableDomain::String, Some("right".to_string()));
        program.require_variables_all_different(vec![left, right], "alpha frame");

        assert_eq!(program.validate(), Ok(()));
    }

    #[test]
    fn rejects_variable_all_different_domain_mismatch() {
        let mut program = SelectorProgram::default();
        let string = program.add_variable(VariableDomain::String, Some("string".to_string()));
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        program.require_variables_all_different(vec![string, owner], "mixed");

        assert_eq!(
            program.validate(),
            Err(SelectorProgramError::DomainMismatch {
                context: "all_different_variables",
                expected: VariableDomain::String,
                actual: VariableDomain::Owner,
            })
        );
    }

    #[test]
    fn slice_for_targets_remaps_dense_variable_and_target_ids() {
        let mut program = SelectorProgram::default();
        let ignored_owner =
            program.add_variable(VariableDomain::Owner, Some("ignored".to_string()));
        let kept_owner = program.add_variable(VariableDomain::Owner, Some("kept".to_string()));
        let referenced_owner =
            program.add_variable(VariableDomain::Owner, Some("referenced".to_string()));
        let ignored_target = program.add_target(
            ChunkId(0),
            ignored_owner,
            "ignored/module",
            ClaimKind::Binding {
                export_name: Some("Ignored".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        let kept_target = program.add_target(
            ChunkId(0),
            kept_owner,
            "kept/module",
            ClaimKind::Binding {
                export_name: Some("Kept".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: ignored_owner },
            binding: const_str("ignored"),
        });
        program.add_atom(SelectorAtom::OwnerReferencesOwner {
            owner: OwnerTerm::Var { id: kept_owner },
            referenced: OwnerTerm::Var {
                id: referenced_owner,
            },
        });
        program.add_atom(SelectorAtom::OwnerKind {
            owner: OwnerTerm::Var {
                id: referenced_owner,
            },
            statement_kind: const_str("function"),
        });
        program.require_all_different(vec![ignored_target, kept_target]);

        let slice = program
            .slice_for_targets(
                &BTreeSet::from([kept_target]),
                SelectorProgramSliceOptions {
                    include_target_all_different: false,
                },
            )
            .unwrap();

        assert_eq!(slice.program.variables.len(), 2);
        assert_eq!(slice.program.variables[0].id, SelectorVariableId(0));
        assert_eq!(
            slice.program.variables[0].debug_name.as_deref(),
            Some("kept")
        );
        assert_eq!(slice.program.variables[1].id, SelectorVariableId(1));
        assert_eq!(
            slice.program.variables[1].debug_name.as_deref(),
            Some("referenced")
        );
        assert_eq!(slice.program.targets.len(), 1);
        assert_eq!(slice.program.targets[0].id, SelectorTargetId(0));
        assert_eq!(slice.program.targets[0].owner, SelectorVariableId(0));
        assert_eq!(
            slice.old_to_new_targets.get(&kept_target),
            Some(&SelectorTargetId(0))
        );
        assert_eq!(
            slice.new_to_old_targets.get(&SelectorTargetId(0)),
            Some(&kept_target)
        );
        assert_eq!(slice.program.atoms.len(), 2);
        assert!(slice.program.all_different.is_empty());
        assert!(slice.program.validate().is_ok());
    }

    #[test]
    fn slice_for_targets_preserves_projected_allowed_tuples() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner,
            "projected/module",
            ClaimKind::BindingGroupMember {
                export_name: "Projected".to_string(),
                target_binding: "projected".to_string(),
            },
            ClaimOrigin::BindingGroup { group_index: 0 },
        );
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![owner, binding],
            rows: vec![vec![
                SelectorProjectedValue::Owner(OwnerId(7)),
                SelectorProjectedValue::String("minified".to_string()),
            ]],
            reason: "projected source_match group".to_string(),
        });
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Var { id: binding },
        });

        let slice = program
            .slice_for_targets(
                &BTreeSet::from([target]),
                SelectorProgramSliceOptions {
                    include_target_all_different: false,
                },
            )
            .unwrap();

        assert_eq!(slice.program.variables.len(), 2);
        assert!(slice.program.atoms.iter().any(|atom| {
            matches!(
                atom,
                SelectorAtom::ProjectedAllowedTuples {
                    variables,
                    rows,
                    reason,
                } if variables == &vec![SelectorVariableId(0), SelectorVariableId(1)]
                    && rows
                        == &vec![vec![
                            SelectorProjectedValue::Owner(OwnerId(7)),
                            SelectorProjectedValue::String("minified".to_string())
                        ]]
                    && reason == "projected source_match group"
            )
        }));
        assert!(slice.program.validate().is_ok());
    }

    #[test]
    fn slice_for_targets_handles_variable_and_target_all_different() {
        let mut program = SelectorProgram::default();
        let left_owner = program.add_variable(VariableDomain::Owner, Some("left".to_string()));
        let right_owner = program.add_variable(VariableDomain::Owner, Some("right".to_string()));
        let left_name = program.add_variable(VariableDomain::String, Some("left_name".to_string()));
        let right_name =
            program.add_variable(VariableDomain::String, Some("right_name".to_string()));
        let left_target = program.add_target(
            ChunkId(0),
            left_owner,
            "left/module",
            ClaimKind::Binding {
                export_name: Some("Left".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        let right_target = program.add_target(
            ChunkId(0),
            right_owner,
            "right/module",
            ClaimKind::Binding {
                export_name: Some("Right".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: left_owner },
            binding: StringTerm::Var { id: left_name },
        });
        program.require_variables_all_different(
            vec![left_name, right_name],
            "source_match alpha frame",
        );
        program.require_all_different(vec![left_target, right_target]);

        let root_slice = program
            .slice_for_targets(
                &BTreeSet::from([left_target]),
                SelectorProgramSliceOptions {
                    include_target_all_different: false,
                },
            )
            .unwrap();
        assert_eq!(root_slice.program.variables.len(), 3);
        assert_eq!(root_slice.program.all_different_variables.len(), 1);
        assert_eq!(
            root_slice.program.all_different_variables[0].variables,
            vec![SelectorVariableId(1), SelectorVariableId(2)]
        );
        assert!(root_slice.program.all_different.is_empty());

        let coupled_slice = program
            .slice_for_targets(
                &BTreeSet::from([left_target, right_target]),
                SelectorProgramSliceOptions {
                    include_target_all_different: true,
                },
            )
            .unwrap();
        assert_eq!(coupled_slice.program.targets.len(), 2);
        assert_eq!(
            coupled_slice.program.all_different,
            vec![vec![SelectorTargetId(0), SelectorTargetId(1)]]
        );
        assert!(coupled_slice.program.validate().is_ok());
    }

    #[test]
    fn serde_round_trips_program_and_result() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("@Class".to_string()));
        let target = program.add_target(
            ChunkId(2),
            owner,
            "runtime/classes",
            ClaimKind::Binding {
                export_name: Some("Class".to_string()),
            },
            ClaimOrigin::RelationalSelector {
                relation: RelationalPrimitive::MakesDecorateCall,
            },
        );
        program.add_atom(SelectorAtom::MakesDecorateCall {
            owner: OwnerTerm::Var { id: owner },
            class_anchor: const_str("C"),
            member: Some(const_str("ready")),
        });
        program.validate().unwrap();

        let json = serde_json::to_string(&program).unwrap();
        let decoded: SelectorProgram = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded, program);

        let result = SolverResult {
            claims: vec![SolverClaim {
                target,
                outcome: ClaimOutcome::Unique {
                    claim: ResolvedClaim {
                        chunk_id: ChunkId(2),
                        owner: OwnerId(9),
                        statement_ordinal: StatementOrdinal(9),
                        binding: Some("a".to_string()),
                        provenance: vec![ProvenanceFact {
                            relation: "makes_decorate_call".to_string(),
                            summary: "decorates C.ready".to_string(),
                        }],
                    },
                },
            }],
            global_diagnostic: None,
        };
        let json = serde_json::to_string(&result).unwrap();
        let decoded: SolverResult = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.outcome_for(target), result.outcome_for(target));
    }
}
