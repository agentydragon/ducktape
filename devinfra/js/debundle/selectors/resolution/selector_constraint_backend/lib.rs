//! Compact compiled selector problem consumed by exact-assignment backends.
//!
//! This is the production boundary between selector/fact lowering and a CP/SAT
//! backend. Values are interned once, variables hold compact full-domain handles
//! or sparse candidate sets, and allowed tuples are stored as interned ids.

use std::collections::BTreeMap;

use selector_ir::{SelectorProjectedValue, SelectorTargetId, VariableDomain};
use source_identity::SourceUnitId;

mod backend;
mod builder;
mod error;
mod rows;
#[cfg(test)]
mod tests;

pub use backend::{
    BackendAssignment, BackendAssignmentCoverage, BackendSolveResult, BackendSolveStatus,
    BackendVariableAssignment, MAX_ALTERNATIVES_PER_VARIABLE, SelectorProblemBackend,
};
pub use builder::CompiledSelectorProblemBuilder;
pub use error::{BackendAssignmentError, CompiledSelectorProblemError};
pub use rows::CompiledAllowedTupleRows;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ConstraintVariableId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AllowedTupleConstraintId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AllowedTupleRowsId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SharedVariableDomainId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AllDifferentConstraintId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct BackendValueId(pub i64);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompiledVariableDomain {
    Full(VariableDomain),
    Sparse(Vec<BackendValueId>),
    SharedSparse(SharedVariableDomainId),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledVariable {
    pub id: ConstraintVariableId,
    pub domain: VariableDomain,
    pub debug_name: Option<String>,
    pub values: CompiledVariableDomain,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TargetProjection {
    pub target: SelectorTargetId,
    pub source_unit_variable: ConstraintVariableId,
    pub binding_projection: Option<TargetBindingProjection>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BindingProjection<V> {
    Variable(V),
    Const(String),
}

/// Binding projection after selector variables have been lowered to solver variables.
pub type TargetBindingProjection = BindingProjection<ConstraintVariableId>;

impl BindingProjection<ConstraintVariableId> {
    pub fn variable(&self) -> Option<ConstraintVariableId> {
        match self {
            Self::Variable(variable) => Some(*variable),
            Self::Const(_) => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledAllowedTupleConstraint {
    pub id: AllowedTupleConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub row_set: AllowedTupleRowsId,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledAllowedTupleRowSet {
    pub id: AllowedTupleRowsId,
    pub rows: CompiledAllowedTupleRows,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledSharedVariableDomain {
    pub id: SharedVariableDomainId,
    pub domain: VariableDomain,
    pub values: Vec<BackendValueId>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledAllDifferentConstraint {
    pub id: AllDifferentConstraintId,
    pub variables: Vec<ConstraintVariableId>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FullDomainValues {
    pub source_units: Vec<BackendValueId>,
    pub strings: Vec<BackendValueId>,
}

impl FullDomainValues {
    pub fn get(&self, domain: VariableDomain) -> &[BackendValueId] {
        match domain {
            VariableDomain::SourceUnit => &self.source_units,
            VariableDomain::String => &self.strings,
        }
    }

    fn get_mut(&mut self, domain: VariableDomain) -> &mut Vec<BackendValueId> {
        match domain {
            VariableDomain::SourceUnit => &mut self.source_units,
            VariableDomain::String => &mut self.strings,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DomainValueDictionary {
    pub source_units: Vec<SourceUnitId>,
    pub strings: Vec<String>,
}

impl DomainValueDictionary {
    /// Whether `value` names an interned value of `domain`.
    fn contains_id(&self, domain: VariableDomain, value: BackendValueId) -> bool {
        let len = match domain {
            VariableDomain::SourceUnit => self.source_units.len(),
            VariableDomain::String => self.strings.len(),
        };
        usize::try_from(value.0).is_ok_and(|index| index < len)
    }

    pub fn encode(&self, value: &SelectorProjectedValue) -> Option<BackendValueId> {
        let index = match value {
            SelectorProjectedValue::SourceUnit(value) => self
                .source_units
                .iter()
                .position(|candidate| candidate == value),
            SelectorProjectedValue::String(value) => {
                self.strings.iter().position(|candidate| candidate == value)
            }
        }?;
        Some(BackendValueId(index.try_into().ok()?))
    }

    fn decode(
        &self,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Option<SelectorProjectedValue> {
        let index = backend_value_index(value).ok()?;
        match domain {
            VariableDomain::SourceUnit => self
                .source_units
                .get(index)
                .copied()
                .map(SelectorProjectedValue::SourceUnit),
            VariableDomain::String => self
                .strings
                .get(index)
                .cloned()
                .map(SelectorProjectedValue::String),
        }
    }
}

/// The values `values` admits, resolving full and shared handles.
fn domain_values<'a>(
    full_domains: &'a FullDomainValues,
    shared_variable_domains: &'a [CompiledSharedVariableDomain],
    values: &'a CompiledVariableDomain,
) -> &'a [BackendValueId] {
    match values {
        CompiledVariableDomain::Full(domain) => full_domains.get(*domain),
        CompiledVariableDomain::Sparse(values) => values,
        CompiledVariableDomain::SharedSparse(id) => &shared_variable_domains[id.0].values,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledSelectorProblem {
    pub value_dictionary: DomainValueDictionary,
    pub full_domains: FullDomainValues,
    pub shared_variable_domains: Vec<CompiledSharedVariableDomain>,
    pub variables: Vec<CompiledVariable>,
    pub target_projections: Vec<TargetProjection>,
    pub allowed_tuple_row_sets: Vec<CompiledAllowedTupleRowSet>,
    pub allowed_tuples: Vec<CompiledAllowedTupleConstraint>,
    pub all_different: Vec<CompiledAllDifferentConstraint>,
    /// Presolve proved the problem unsatisfiable.
    pub known_unsat: bool,
}

impl CompiledSelectorProblem {
    pub fn variable_domain_values<'a>(
        &'a self,
        variable: &'a CompiledVariable,
    ) -> &'a [BackendValueId] {
        domain_values(
            &self.full_domains,
            &self.shared_variable_domains,
            &variable.values,
        )
    }

    pub fn decode_assignment(
        &self,
        assignment: &BackendAssignment,
    ) -> Result<BTreeMap<ConstraintVariableId, SelectorProjectedValue>, BackendAssignmentError>
    {
        let variables = self
            .variables
            .iter()
            .map(|variable| (variable.id, variable))
            .collect::<BTreeMap<_, _>>();

        let mut decoded = BTreeMap::new();
        for entry in &assignment.values {
            let variable =
                variables
                    .get(&entry.variable)
                    .ok_or(BackendAssignmentError::UnknownVariable {
                        variable: entry.variable,
                    })?;
            if !self.variable_domain_contains(variable, entry.value) {
                return Err(BackendAssignmentError::ValueOutsideDomain {
                    variable: entry.variable,
                    value: entry.value,
                });
            }
            let value = self
                .value_dictionary
                .decode(variable.domain, entry.value)
                .ok_or(BackendAssignmentError::UnknownValue { value: entry.value })?;
            if decoded.insert(entry.variable, value).is_some() {
                return Err(BackendAssignmentError::DuplicateVariable {
                    variable: entry.variable,
                });
            }
        }
        Ok(decoded)
    }

    pub fn allowed_tuple_rows(
        &self,
        constraint: &CompiledAllowedTupleConstraint,
    ) -> &CompiledAllowedTupleRows {
        &self.allowed_tuple_row_sets[constraint.row_set.0].rows
    }

    fn variable_domain_contains(&self, variable: &CompiledVariable, value: BackendValueId) -> bool {
        match &variable.values {
            CompiledVariableDomain::Full(domain) => backend_value_index(value)
                .is_ok_and(|index| index < self.full_domains.get(*domain).len()),
            CompiledVariableDomain::Sparse(values) => values.binary_search(&value).is_ok(),
            CompiledVariableDomain::SharedSparse(id) => self.shared_variable_domains[id.0]
                .values
                .binary_search(&value)
                .is_ok(),
        }
    }

    pub fn decode_value(
        &self,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Option<SelectorProjectedValue> {
        self.value_dictionary.decode(domain, value)
    }
}

fn backend_value_index(value: BackendValueId) -> Result<usize, BackendAssignmentError> {
    if value.0 < 0 {
        return Err(BackendAssignmentError::NegativeValue { value });
    }
    usize::try_from(value.0).map_err(|_| BackendAssignmentError::ValueIndexOutOfRange { value })
}

fn backend_value_id(count: usize) -> Result<BackendValueId, CompiledSelectorProblemError> {
    Ok(BackendValueId(i64::try_from(count).map_err(|_| {
        CompiledSelectorProblemError::TooManyValues { count }
    })?))
}
