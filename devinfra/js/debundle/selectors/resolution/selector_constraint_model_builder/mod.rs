//! Lowering from selector IR plus facts into a compact finite-domain problem.

mod atom_lowering;
mod fact_domains;
#[cfg(test)]
mod tests;

use std::collections::BTreeMap;

use atom_lowering::{EncodedSupportCache, lower_atom_constraint};
use fact_domains::FactDomains;
use selector_constraint_backend::{
    BindingProjection, CompiledSelectorProblem, CompiledSelectorProblemBuilder,
    CompiledSelectorProblemError, ConstraintVariableId, TargetBindingProjection,
};
use selector_ir::{
    SelectorAtom, SelectorFactStore, SelectorProgram, SelectorProgramError, SelectorVariableId,
    SourceUnitTerm, StringTerm, VariableDomain,
};

pub fn compile_selector_problem(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblem, CompiledSelectorProblemBuildError> {
    program
        .validate()
        .map_err(CompiledSelectorProblemBuildError::InvalidProgram)?;

    let mut domains = FactDomains::from_program_and_facts(program, facts);

    domains.discard_unneeded_raw_relations();
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = CompiledSelectorProblemBuilder::default();
    for domain in [VariableDomain::SourceUnit, VariableDomain::String] {
        model.add_full_domain_values(domain, domains.values_for(domain))?;
    }
    domains.discard_full_domain_source_sets();
    let mut variables = Vec::with_capacity(program.variables.len());
    for variable in &program.variables {
        variables.push(model.add_variable(variable.domain, variable.debug_name.clone()));
    }

    for target in &program.targets {
        let source_unit_variable = model_variable(&variables, target.source_unit)?;
        let binding_projection =
            match target_binding_projections.binding_projection(target.source_unit) {
                Some(SourceBindingProjection::Const(binding)) => {
                    Some(TargetBindingProjection::Const(binding.clone()))
                }
                Some(SourceBindingProjection::Variable(binding)) => Some(
                    TargetBindingProjection::Variable(model_variable(&variables, *binding)?),
                ),
                None => None,
            };
        model.add_target_projection(target.id, source_unit_variable, binding_projection)?;
    }

    let mut support_cache = EncodedSupportCache::default();
    for atom in &program.atoms {
        lower_atom_constraint(atom, &domains, &variables, &mut model, &mut support_cache)?;
        if model.known_unsat() {
            break;
        }
    }

    if !model.known_unsat() {
        model.simplify_allowed_tuples_against_current_domains()?;
    }

    if !model.known_unsat() {
        for targets in &program.all_different {
            model.require_target_all_different(targets.clone())?;
        }
    }

    model
        .finish()
        .map_err(CompiledSelectorProblemBuildError::from)
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CompiledSelectorProblemBuildError {
    #[error("invalid selector program: {0}")]
    InvalidProgram(#[source] SelectorProgramError),
    #[error("invalid compiled selector problem: {0}")]
    InvalidModel(#[source] CompiledSelectorProblemError),
    #[error("selector variable {variable:?} has no model variable")]
    UnknownSelectorVariable { variable: SelectorVariableId },
    #[error("selector atom is not supported by the compiled selector problem builder: {atom}")]
    UnsupportedAtom { atom: String },
    #[error("constant-only selector atom has no matching fact: {atom}")]
    ConstantOnlyAtomUnsatisfied { atom: String },
    #[error(
        "selector source unit variable {source_unit:?} has conflicting target binding projections: {existing:?} vs {actual:?}"
    )]
    ConflictingTargetBindingProjection {
        source_unit: SelectorVariableId,
        existing: SourceBindingProjection,
        actual: SourceBindingProjection,
    },
}

impl From<CompiledSelectorProblemError> for CompiledSelectorProblemBuildError {
    fn from(err: CompiledSelectorProblemError) -> Self {
        Self::InvalidModel(err)
    }
}

fn model_variable(
    variables: &[ConstraintVariableId],
    variable: SelectorVariableId,
) -> Result<ConstraintVariableId, CompiledSelectorProblemBuildError> {
    variables
        .get(variable.0)
        .copied()
        .ok_or(CompiledSelectorProblemBuildError::UnknownSelectorVariable { variable })
}

/// Binding projection before selector variables are lowered to solver variables.
pub type SourceBindingProjection = BindingProjection<SelectorVariableId>;

#[derive(Debug, Default)]
struct TargetBindingProjections {
    by_unit: BTreeMap<SelectorVariableId, SourceBindingProjection>,
}

impl TargetBindingProjections {
    fn from_program(program: &SelectorProgram) -> Result<Self, CompiledSelectorProblemBuildError> {
        let mut projections = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::SourceUnitDeclaresBinding {
                    source_unit: SourceUnitTerm::Var { id: source_unit },
                    binding: StringTerm::Const { value },
                } => projections
                    .insert(*source_unit, SourceBindingProjection::Const(value.clone()))?,
                SelectorAtom::SourceUnitDeclaresBinding {
                    source_unit: SourceUnitTerm::Var { id: source_unit },
                    binding: StringTerm::Var { id: binding },
                } => {
                    projections.insert(*source_unit, SourceBindingProjection::Variable(*binding))?
                }
                _ => {}
            }
        }
        Ok(projections)
    }

    fn insert(
        &mut self,
        source_unit: SelectorVariableId,
        binding: SourceBindingProjection,
    ) -> Result<(), CompiledSelectorProblemBuildError> {
        match self.by_unit.get(&source_unit) {
            Some(existing) if existing != &binding => Err(
                CompiledSelectorProblemBuildError::ConflictingTargetBindingProjection {
                    source_unit,
                    existing: existing.clone(),
                    actual: binding,
                },
            ),
            Some(_) => Ok(()),
            None => {
                self.by_unit.insert(source_unit, binding);
                Ok(())
            }
        }
    }

    fn binding_projection(
        &self,
        source_unit: SelectorVariableId,
    ) -> Option<&SourceBindingProjection> {
        self.by_unit.get(&source_unit)
    }
}
