//! The selector program of one chunk and the chunk facts it is solved over.
//!
//! `selector_ir_lowering` and `selector_resolve` build a [`SelectorProgram`] and
//! a [`SelectorFactStore`]; `selector_backend_solver` returns one
//! [`ClaimOutcome`] per target in a [`SolverResult`].

use std::collections::{BTreeMap, BTreeSet};

use analysis::{SourceUnitId, StatementOrdinal};

/// Dense id of a solver variable in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SelectorVariableId(pub usize);

/// Dense id of a materializer claim target in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SelectorTargetId(pub usize);

/// The relation domain a solver variable ranges over.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum VariableDomain {
    SourceUnit,
    String,
}

/// One variable in the global selector constraint program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectorVariable {
    pub id: SelectorVariableId,
    pub domain: VariableDomain,
    /// Optional authoring/debug name, e.g. `@Button` or `needle.return`.
    pub debug_name: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceUnitTerm {
    Var { id: SelectorVariableId },
    Const { source_unit: SourceUnitId },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StringTerm {
    Var { id: SelectorVariableId },
    Const { value: String },
}

/// Materializer-facing shape of a target once the solver has selected a source unit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClaimKind {
    /// A normal exported member claim. The source unit should declare `binding`.
    Binding { export_name: Option<String> },
    /// An anonymous top-level statement claim, the `index`th of its module's
    /// `anonymous_statements[]`. The source unit may declare no binding.
    AnonymousStatement { index: usize },
    /// One target in a `source_matches[].bindings[]` selector.
    BindingGroupMember { export_name: String },
}

/// One claim the materializer will consume after solving.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectorTarget {
    pub id: SelectorTargetId,
    pub source_unit: SelectorVariableId,
    /// The target's module as `<chunk>::<path>`, which `selector_resolve`
    /// parses the path back out of.
    pub logical_module: String,
    pub claim: ClaimKind,
}

/// One conjunctive-query atom in the lowered selector program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorAtom {
    SourceUnitKind {
        source_unit: SourceUnitTerm,
        statement_kind: StringTerm,
    },
    SourceUnitDeclaresBinding {
        source_unit: SourceUnitTerm,
        binding: StringTerm,
    },
    ProjectedAllowedTuples {
        variables: Vec<SelectorVariableId>,
        rows: Vec<Vec<SelectorProjectedValue>>,
    },
    SourceUnitReferencesSourceUnit {
        source_unit: SourceUnitTerm,
        referenced: SourceUnitTerm,
    },
    SourceUnitAliasesSourceUnit {
        source_unit: SourceUnitTerm,
        aliased: SourceUnitTerm,
    },
    ReadsMember {
        source_unit: SourceUnitTerm,
        member: StringTerm,
    },
    ReadsMemberOfSourceUnit {
        source_unit: SourceUnitTerm,
        object: SourceUnitTerm,
        member: StringTerm,
    },
    ConsumesModuleMember {
        source_unit: SourceUnitTerm,
        module: StringTerm,
        member: StringTerm,
    },
    PassedToCall {
        source_unit: SourceUnitTerm,
        callee_member: StringTerm,
        arg_index: Option<u32>,
    },
    PassedToCallOfSourceUnit {
        source_unit: SourceUnitTerm,
        callee_object: SourceUnitTerm,
        callee_member: StringTerm,
        arg_index: Option<u32>,
    },
    MakesDecorateCallForSourceUnit {
        source_unit: SourceUnitTerm,
        class_anchor: SourceUnitTerm,
        member: Option<StringTerm>,
    },
    IntrinsicAlias {
        source_unit: SourceUnitTerm,
        property: StringTerm,
        referenced_by: SourceUnitTerm,
    },
}

impl SelectorAtom {
    pub fn variable_ids(&self) -> BTreeSet<SelectorVariableId> {
        let mut variables = BTreeSet::new();
        match self {
            Self::SourceUnitKind {
                source_unit,
                statement_kind,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(statement_kind, &mut variables);
            }
            Self::SourceUnitDeclaresBinding {
                source_unit,
                binding,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(binding, &mut variables);
            }
            Self::ProjectedAllowedTuples {
                variables: projected,
                ..
            } => {
                variables.extend(projected.iter().copied());
            }
            Self::SourceUnitReferencesSourceUnit {
                source_unit,
                referenced,
            }
            | Self::SourceUnitAliasesSourceUnit {
                source_unit,
                aliased: referenced,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_source_unit_term_variables(referenced, &mut variables);
            }
            Self::ReadsMember {
                source_unit,
                member,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(member, &mut variables);
            }
            Self::ReadsMemberOfSourceUnit {
                source_unit,
                object,
                member,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_source_unit_term_variables(object, &mut variables);
                collect_string_term_variables(member, &mut variables);
            }
            Self::ConsumesModuleMember {
                source_unit,
                module,
                member,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(module, &mut variables);
                collect_string_term_variables(member, &mut variables);
            }
            Self::PassedToCall {
                source_unit,
                callee_member,
                ..
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(callee_member, &mut variables);
            }
            Self::PassedToCallOfSourceUnit {
                source_unit,
                callee_object,
                callee_member,
                ..
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_source_unit_term_variables(callee_object, &mut variables);
                collect_string_term_variables(callee_member, &mut variables);
            }
            Self::MakesDecorateCallForSourceUnit {
                source_unit,
                class_anchor,
                member,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_source_unit_term_variables(class_anchor, &mut variables);
                if let Some(member) = member {
                    collect_string_term_variables(member, &mut variables);
                }
            }
            Self::IntrinsicAlias {
                source_unit,
                property,
                referenced_by,
            } => {
                collect_source_unit_term_variables(source_unit, &mut variables);
                collect_string_term_variables(property, &mut variables);
                collect_source_unit_term_variables(referenced_by, &mut variables);
            }
        }
        variables
    }

    fn remap_variables(
        &self,
        variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
    ) -> Self {
        match self {
            Self::SourceUnitKind {
                source_unit,
                statement_kind,
            } => Self::SourceUnitKind {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                statement_kind: remap_string_term(statement_kind, variable_map),
            },
            Self::SourceUnitDeclaresBinding {
                source_unit,
                binding,
            } => Self::SourceUnitDeclaresBinding {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                binding: remap_string_term(binding, variable_map),
            },
            Self::ProjectedAllowedTuples { variables, rows } => Self::ProjectedAllowedTuples {
                variables: variables
                    .iter()
                    .map(|variable| remap_variable_id(*variable, variable_map))
                    .collect(),
                rows: rows.clone(),
            },
            Self::SourceUnitReferencesSourceUnit {
                source_unit,
                referenced,
            } => Self::SourceUnitReferencesSourceUnit {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                referenced: remap_source_unit_term(referenced, variable_map),
            },
            Self::SourceUnitAliasesSourceUnit {
                source_unit,
                aliased,
            } => Self::SourceUnitAliasesSourceUnit {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                aliased: remap_source_unit_term(aliased, variable_map),
            },
            Self::ReadsMember {
                source_unit,
                member,
            } => Self::ReadsMember {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                member: remap_string_term(member, variable_map),
            },
            Self::ReadsMemberOfSourceUnit {
                source_unit,
                object,
                member,
            } => Self::ReadsMemberOfSourceUnit {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                object: remap_source_unit_term(object, variable_map),
                member: remap_string_term(member, variable_map),
            },
            Self::ConsumesModuleMember {
                source_unit,
                module,
                member,
            } => Self::ConsumesModuleMember {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                module: remap_string_term(module, variable_map),
                member: remap_string_term(member, variable_map),
            },
            Self::PassedToCall {
                source_unit,
                callee_member,
                arg_index,
            } => Self::PassedToCall {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                callee_member: remap_string_term(callee_member, variable_map),
                arg_index: *arg_index,
            },
            Self::PassedToCallOfSourceUnit {
                source_unit,
                callee_object,
                callee_member,
                arg_index,
            } => Self::PassedToCallOfSourceUnit {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                callee_object: remap_source_unit_term(callee_object, variable_map),
                callee_member: remap_string_term(callee_member, variable_map),
                arg_index: *arg_index,
            },
            Self::MakesDecorateCallForSourceUnit {
                source_unit,
                class_anchor,
                member,
            } => Self::MakesDecorateCallForSourceUnit {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                class_anchor: remap_source_unit_term(class_anchor, variable_map),
                member: member
                    .as_ref()
                    .map(|member| remap_string_term(member, variable_map)),
            },
            Self::IntrinsicAlias {
                source_unit,
                property,
                referenced_by,
            } => Self::IntrinsicAlias {
                source_unit: remap_source_unit_term(source_unit, variable_map),
                property: remap_string_term(property, variable_map),
                referenced_by: remap_source_unit_term(referenced_by, variable_map),
            },
        }
    }
}

fn collect_source_unit_term_variables(
    term: &SourceUnitTerm,
    variables: &mut BTreeSet<SelectorVariableId>,
) {
    if let SourceUnitTerm::Var { id } = term {
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

fn remap_source_unit_term(
    term: &SourceUnitTerm,
    variable_map: &BTreeMap<SelectorVariableId, SelectorVariableId>,
) -> SourceUnitTerm {
    match term {
        SourceUnitTerm::Var { id } => SourceUnitTerm::Var {
            id: remap_variable_id(*id, variable_map),
        },
        SourceUnitTerm::Const { source_unit } => SourceUnitTerm::Const {
            source_unit: *source_unit,
        },
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorProjectedValue {
    SourceUnit(SourceUnitId),
    String(String),
}

impl SelectorProjectedValue {
    pub fn domain(&self) -> VariableDomain {
        match self {
            Self::SourceUnit(_) => VariableDomain::SourceUnit,
            Self::String(_) => VariableDomain::String,
        }
    }
}

/// Whole lowered selector program for one chunk/component solve.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SelectorProgram {
    pub variables: Vec<SelectorVariable>,
    pub targets: Vec<SelectorTarget>,
    pub atoms: Vec<SelectorAtom>,
    /// Sets of target ids that must land on distinct source units.
    pub all_different: Vec<Vec<SelectorTargetId>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectorProgramSlice {
    pub program: SelectorProgram,
    pub new_to_old_targets: BTreeMap<SelectorTargetId, SelectorTargetId>,
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
        source_unit: SelectorVariableId,
        logical_module: impl Into<String>,
        claim: ClaimKind,
    ) -> SelectorTargetId {
        let id = SelectorTargetId(self.targets.len());
        self.targets.push(SelectorTarget {
            id,
            source_unit,
            logical_module: logical_module.into(),
            claim,
        });
        id
    }

    pub fn add_atom(&mut self, atom: SelectorAtom) {
        self.atoms.push(atom);
    }

    pub fn require_all_different(&mut self, targets: Vec<SelectorTargetId>) {
        self.all_different.push(targets);
    }

    pub fn slice_for_targets(
        &self,
        targets: &BTreeSet<SelectorTargetId>,
    ) -> Result<SelectorProgramSlice, SelectorProgramError> {
        for target in targets {
            self.require_target(*target)?;
        }

        let mut kept_variables = BTreeSet::<SelectorVariableId>::new();
        for target in targets {
            kept_variables.insert(self.targets[target.0].source_unit);
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
            let source_unit = *variable_map
                .get(&target.source_unit)
                .expect("selected target source unit variable must be kept");
            let remapped = program.add_target(
                source_unit,
                target.logical_module.clone(),
                target.claim.clone(),
            );
            old_to_new_targets.insert(target.id, remapped);
            new_to_old_targets.insert(remapped, target.id);
        }

        for idx in kept_atoms {
            program.add_atom(self.atoms[idx].remap_variables(&variable_map));
        }

        for target_set in &self.all_different {
            let remapped = target_set
                .iter()
                .filter_map(|target| old_to_new_targets.get(target).copied())
                .collect::<Vec<_>>();
            if remapped.len() >= 2 {
                program.require_all_different(remapped);
            }
        }

        program.validate()?;
        Ok(SelectorProgramSlice {
            program,
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
            self.require_domain(
                target.source_unit,
                VariableDomain::SourceUnit,
                "selector target source unit",
            )?;
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
        Ok(())
    }

    fn validate_atom(&self, atom: &SelectorAtom) -> Result<(), SelectorProgramError> {
        match atom {
            SelectorAtom::SourceUnitKind {
                source_unit,
                statement_kind,
            } => {
                self.validate_source_unit_term(source_unit, "source_unit_kind.source unit")?;
                self.validate_string_term(statement_kind, "source_unit_kind.statement_kind")
            }
            SelectorAtom::SourceUnitDeclaresBinding {
                source_unit,
                binding,
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "source_unit_declares_binding.source unit",
                )?;
                self.validate_string_term(binding, "source_unit_declares_binding.binding")
            }
            SelectorAtom::ProjectedAllowedTuples { variables, rows } => {
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
            SelectorAtom::SourceUnitReferencesSourceUnit {
                source_unit,
                referenced,
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "source_unit_references_source_unit.source unit",
                )?;
                self.validate_source_unit_term(
                    referenced,
                    "source_unit_references_source_unit.referenced",
                )
            }
            SelectorAtom::SourceUnitAliasesSourceUnit {
                source_unit,
                aliased,
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "source_unit_aliases_source_unit.source unit",
                )?;
                self.validate_source_unit_term(aliased, "source_unit_aliases_source_unit.aliased")
            }
            SelectorAtom::ReadsMember {
                source_unit,
                member,
            } => {
                self.validate_source_unit_term(source_unit, "reads_member.source unit")?;
                self.validate_string_term(member, "reads_member.member")
            }
            SelectorAtom::ReadsMemberOfSourceUnit {
                source_unit,
                object,
                member,
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "reads_member_of_source_unit.source unit",
                )?;
                self.validate_source_unit_term(object, "reads_member_of_source_unit.object")?;
                self.validate_string_term(member, "reads_member_of_source_unit.member")
            }
            SelectorAtom::ConsumesModuleMember {
                source_unit,
                module,
                member,
            } => {
                self.validate_source_unit_term(source_unit, "consumes_module_member.source unit")?;
                self.validate_string_term(module, "consumes_module_member.module")?;
                self.validate_string_term(member, "consumes_module_member.member")
            }
            SelectorAtom::PassedToCall {
                source_unit,
                callee_member,
                ..
            } => {
                self.validate_source_unit_term(source_unit, "passed_to_call.source unit")?;
                self.validate_string_term(callee_member, "passed_to_call.callee_member")
            }
            SelectorAtom::PassedToCallOfSourceUnit {
                source_unit,
                callee_object,
                callee_member,
                ..
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "passed_to_call_of_source_unit.source unit",
                )?;
                self.validate_source_unit_term(
                    callee_object,
                    "passed_to_call_of_source_unit.callee_object",
                )?;
                self.validate_string_term(
                    callee_member,
                    "passed_to_call_of_source_unit.callee_member",
                )
            }
            SelectorAtom::MakesDecorateCallForSourceUnit {
                source_unit,
                class_anchor,
                member,
            } => {
                self.validate_source_unit_term(
                    source_unit,
                    "makes_decorate_call_for_source_unit.source unit",
                )?;
                self.validate_source_unit_term(
                    class_anchor,
                    "makes_decorate_call_for_source_unit.class_anchor",
                )?;
                if let Some(member) = member {
                    self.validate_string_term(
                        member,
                        "makes_decorate_call_for_source_unit.member",
                    )?;
                }
                Ok(())
            }
            SelectorAtom::IntrinsicAlias {
                source_unit,
                property,
                referenced_by,
            } => {
                self.validate_source_unit_term(source_unit, "intrinsic_alias.source unit")?;
                self.validate_string_term(property, "intrinsic_alias.property")?;
                self.validate_source_unit_term(referenced_by, "intrinsic_alias.referenced_by")
            }
        }
    }

    fn validate_source_unit_term(
        &self,
        term: &SourceUnitTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let SourceUnitTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::SourceUnit, context)?;
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

#[derive(thiserror::Error, Debug, Clone, PartialEq, Eq)]
pub enum SelectorProgramError {
    #[error("selector variable ids must be dense: expected {expected:?}, found {actual:?}")]
    NonDenseVariable {
        expected: SelectorVariableId,
        actual: SelectorVariableId,
    },
    #[error("selector target ids must be dense: expected {expected:?}, found {actual:?}")]
    NonDenseTarget {
        expected: SelectorTargetId,
        actual: SelectorTargetId,
    },
    #[error("{context} references unknown selector variable {id:?}")]
    UnknownVariable {
        context: &'static str,
        id: SelectorVariableId,
    },
    #[error("all_different references unknown selector target {id:?}")]
    UnknownTarget { id: SelectorTargetId },
    #[error("{context} expected {expected:?} variable, found {actual:?}")]
    DomainMismatch {
        context: &'static str,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    #[error("all_different requires at least two entries")]
    DegenerateAllDifferent,
    #[error("projected_allowed_tuples requires at least one variable")]
    EmptyProjectedAllowedTuples,
    #[error("projected_allowed_tuples row {row_index} has arity {actual}, expected {expected}")]
    ProjectedAllowedTupleArity {
        row_index: usize,
        expected: usize,
        actual: usize,
    },
    #[error(
        "projected_allowed_tuples row {row_index} column {column} has domain {actual:?}, expected {expected:?}"
    )]
    ProjectedAllowedTupleDomain {
        row_index: usize,
        column: usize,
        expected: VariableDomain,
        actual: VariableDomain,
    },
}

/// One chunk fact the program's relation tables are built from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorFact {
    SourceUnit {
        source_unit: SourceUnitId,
        statement_ordinal: StatementOrdinal,
        statement_kind: String,
    },
    DeclaredBinding {
        source_unit: SourceUnitId,
        binding: String,
    },
    SourceUnitReferencesBinding {
        source_unit: SourceUnitId,
        binding: String,
        edge_kind: String,
    },
    MemberRead {
        statement_ordinal: StatementOrdinal,
        object: Option<String>,
        member: String,
    },
    ModuleMemberUse {
        statement_ordinal: StatementOrdinal,
        module: String,
        member: String,
    },
    CallArgumentUse {
        argument: String,
        callee_object: Option<String>,
        callee_member: String,
        arg_index: usize,
    },
    DecorateCallUse {
        callee: String,
        class_anchor: String,
        member: Option<String>,
    },
    IntrinsicAliasUse {
        binding: String,
        property: String,
    },
}

/// Append-only fact store for one solver invocation.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SelectorFactStore {
    pub facts: Vec<SelectorFact>,
}

impl SelectorFactStore {
    pub fn push(&mut self, fact: SelectorFact) {
        self.facts.push(fact);
    }
}

/// Solver output projected to materializer targets.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SolverResult {
    pub claims: Vec<SolverClaim>,
}

impl SolverResult {
    pub fn outcome_for(&self, target: SelectorTargetId) -> Option<&ClaimOutcome> {
        self.claims
            .iter()
            .find(|claim| claim.target == target)
            .map(|claim| &claim.outcome)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SolverClaim {
    pub target: SelectorTargetId,
    pub outcome: ClaimOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq)]
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
    /// The targets solved together with this one admit no joint assignment;
    /// which of them are at fault is not determined.
    Unsatisfiable {
        reason: String,
    },
    /// The backend stopped (a time limit, or UNKNOWN) before proving the
    /// target's value unique or listing its alternatives.
    Undecided {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedClaim {
    pub source_unit: SourceUnitId,
    pub statement_ordinal: StatementOrdinal,
    pub binding: Option<String>,
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
    fn validates_source_unit_target_and_atom_domains() {
        let mut program = SelectorProgram::default();
        let source_unit =
            program.add_variable(VariableDomain::SourceUnit, Some("@Widget".to_string()));
        let member = program.add_target(
            source_unit,
            "runtime/widgets",
            ClaimKind::Binding {
                export_name: Some("Widget".to_string()),
            },
        );
        program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
            source_unit: SourceUnitTerm::Var { id: source_unit },
            binding: const_str("a"),
        });
        program.require_all_different(vec![member, member]);

        assert_eq!(program.validate(), Ok(()));
    }

    #[test]
    fn rejects_non_source_unit_target_variable() {
        let mut program = SelectorProgram::default();
        let binding = program.add_variable(VariableDomain::String, None);
        program.add_target(
            binding,
            "runtime/widgets",
            ClaimKind::AnonymousStatement { index: 0 },
        );

        assert_eq!(
            program.validate(),
            Err(SelectorProgramError::DomainMismatch {
                context: "selector target source unit",
                expected: VariableDomain::SourceUnit,
                actual: VariableDomain::String,
            })
        );
    }

    #[test]
    fn slice_for_targets_remaps_dense_variable_and_target_ids() {
        let mut program = SelectorProgram::default();
        let ignored_source_unit =
            program.add_variable(VariableDomain::SourceUnit, Some("ignored".to_string()));
        let kept_source_unit =
            program.add_variable(VariableDomain::SourceUnit, Some("kept".to_string()));
        let referenced_source_unit =
            program.add_variable(VariableDomain::SourceUnit, Some("referenced".to_string()));
        let ignored_target = program.add_target(
            ignored_source_unit,
            "ignored/module",
            ClaimKind::Binding {
                export_name: Some("Ignored".to_string()),
            },
        );
        let kept_target = program.add_target(
            kept_source_unit,
            "kept/module",
            ClaimKind::Binding {
                export_name: Some("Kept".to_string()),
            },
        );
        program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
            source_unit: SourceUnitTerm::Var {
                id: ignored_source_unit,
            },
            binding: const_str("ignored"),
        });
        program.add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
            source_unit: SourceUnitTerm::Var {
                id: kept_source_unit,
            },
            referenced: SourceUnitTerm::Var {
                id: referenced_source_unit,
            },
        });
        program.add_atom(SelectorAtom::SourceUnitKind {
            source_unit: SourceUnitTerm::Var {
                id: referenced_source_unit,
            },
            statement_kind: const_str("function"),
        });
        program.require_all_different(vec![ignored_target, kept_target]);

        let slice = program
            .slice_for_targets(&BTreeSet::from([kept_target]))
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
        assert_eq!(slice.program.targets[0].source_unit, SelectorVariableId(0));
        assert_eq!(
            slice.new_to_old_targets.get(&SelectorTargetId(0)),
            Some(&kept_target)
        );
        assert_eq!(slice.program.atoms.len(), 2);
        assert!(slice.program.all_different.is_empty());
    }

    #[test]
    fn slice_for_targets_keeps_all_different_among_selected_targets() {
        let mut program = SelectorProgram::default();
        let [left_target, right_target] = ["Left", "Right"].map(|export_name| {
            let source_unit =
                program.add_variable(VariableDomain::SourceUnit, Some(export_name.to_string()));
            program.add_target(
                source_unit,
                "module",
                ClaimKind::Binding {
                    export_name: Some(export_name.to_string()),
                },
            )
        });
        program.require_all_different(vec![left_target, right_target]);

        let alone = program
            .slice_for_targets(&BTreeSet::from([left_target]))
            .unwrap();
        assert!(alone.program.all_different.is_empty());

        let together = program
            .slice_for_targets(&BTreeSet::from([left_target, right_target]))
            .unwrap();
        assert_eq!(
            together.program.all_different,
            vec![vec![SelectorTargetId(0), SelectorTargetId(1)]]
        );
    }
}
