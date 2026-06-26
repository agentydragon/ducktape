//! Backend-neutral finite-domain selector constraint model.
//!
//! `selector_ir` remains the lowered selector vocabulary. This module is the
//! exact-assignment boundary a CP/SAT backend should consume after fact/rule
//! derivation has turned IR atoms into finite domains and allowed tuples.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use chunk_facts::NodeId;
use selector_ir::{SelectorTargetId, SelectorVariableId, VariableDomain};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ConstraintVariableId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AllowedTupleConstraintId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AllDifferentConstraintId(pub usize);

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum ConstraintValue {
    Owner(OwnerId),
    AstNode(NodeId),
    String(String),
    StatementOrdinal(StatementOrdinal),
}

impl ConstraintValue {
    pub fn domain(&self) -> VariableDomain {
        match self {
            Self::Owner(_) => VariableDomain::Owner,
            Self::AstNode(_) => VariableDomain::AstNode,
            Self::String(_) => VariableDomain::String,
            Self::StatementOrdinal(_) => VariableDomain::StatementOrdinal,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConstraintVariable {
    pub id: ConstraintVariableId,
    pub source: SelectorVariableId,
    pub domain: VariableDomain,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debug_name: Option<String>,
    pub values: Vec<ConstraintValue>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TargetProjection {
    pub target: SelectorTargetId,
    pub owner_variable: ConstraintVariableId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_variable: Option<ConstraintVariableId>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_const: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AllowedTupleConstraint {
    pub id: AllowedTupleConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub tuples: Vec<Vec<ConstraintValue>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BinaryConstraintKind {
    Equal,
    NotEqual,
    OrdinalBefore,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BinaryConstraint {
    pub left: ConstraintVariableId,
    pub right: ConstraintVariableId,
    pub kind: BinaryConstraintKind,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AllDifferentReason {
    TargetInjectivity { targets: Vec<SelectorTargetId> },
    SelectorSemantics { label: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AllDifferentConstraint {
    pub id: AllDifferentConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub reason: AllDifferentReason,
}

impl AllDifferentConstraint {
    pub fn is_satisfied_by(
        &self,
        assignment: &BTreeMap<ConstraintVariableId, ConstraintValue>,
    ) -> Option<bool> {
        let mut seen = BTreeSet::new();
        for variable in &self.variables {
            let value = assignment.get(variable)?;
            if !seen.insert(value) {
                return Some(false);
            }
        }
        Some(true)
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorConstraintModel {
    pub variables: Vec<ConstraintVariable>,
    pub target_projections: Vec<TargetProjection>,
    pub allowed_tuples: Vec<AllowedTupleConstraint>,
    pub binary_constraints: Vec<BinaryConstraint>,
    pub all_different: Vec<AllDifferentConstraint>,
}

impl SelectorConstraintModel {
    pub fn add_variable(
        &mut self,
        source: SelectorVariableId,
        domain: VariableDomain,
        values: Vec<ConstraintValue>,
        debug_name: Option<String>,
    ) -> Result<ConstraintVariableId, ConstraintModelError> {
        let id = ConstraintVariableId(self.variables.len());
        let variable = ConstraintVariable {
            id,
            source,
            domain,
            debug_name,
            values,
        };
        Self::validate_variable_shape(&variable)?;
        self.variables.push(variable);
        Ok(id)
    }

    pub fn add_target_projection(
        &mut self,
        target: SelectorTargetId,
        owner_variable: ConstraintVariableId,
        binding_variable: Option<ConstraintVariableId>,
    ) -> Result<(), ConstraintModelError> {
        self.add_target_projection_with_binding_const(
            target,
            owner_variable,
            binding_variable,
            None,
        )
    }

    pub fn add_target_projection_with_binding_const(
        &mut self,
        target: SelectorTargetId,
        owner_variable: ConstraintVariableId,
        binding_variable: Option<ConstraintVariableId>,
        binding_const: Option<String>,
    ) -> Result<(), ConstraintModelError> {
        if binding_variable.is_some() && binding_const.is_some() {
            return Err(ConstraintModelError::ConflictingTargetBindingProjection { target });
        }
        self.require_domain(owner_variable, VariableDomain::Owner)?;
        if let Some(binding_variable) = binding_variable {
            self.require_domain(binding_variable, VariableDomain::String)?;
        }
        if self
            .target_projections
            .iter()
            .any(|projection| projection.target == target)
        {
            return Err(ConstraintModelError::DuplicateTargetProjection { target });
        }
        self.target_projections.push(TargetProjection {
            target,
            owner_variable,
            binding_variable,
            binding_const,
        });
        Ok(())
    }

    pub fn add_allowed_tuples(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<ConstraintValue>>,
    ) -> Result<AllowedTupleConstraintId, ConstraintModelError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        let constraint = AllowedTupleConstraint {
            id,
            variables,
            tuples,
        };
        self.validate_allowed_tuple_constraint(&constraint)?;
        self.allowed_tuples.push(constraint);
        Ok(id)
    }

    pub fn add_binary_constraint(
        &mut self,
        left: ConstraintVariableId,
        right: ConstraintVariableId,
        kind: BinaryConstraintKind,
    ) -> Result<(), ConstraintModelError> {
        let constraint = BinaryConstraint { left, right, kind };
        self.validate_binary_constraint(&constraint)?;
        self.binary_constraints.push(constraint);
        Ok(())
    }

    pub fn add_all_different(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        reason: AllDifferentReason,
    ) -> Result<AllDifferentConstraintId, ConstraintModelError> {
        let id = AllDifferentConstraintId(self.all_different.len());
        let constraint = AllDifferentConstraint {
            id,
            variables,
            reason,
        };
        self.validate_all_different_constraint(&constraint)?;
        self.all_different.push(constraint);
        Ok(id)
    }

    pub fn require_target_all_different(
        &mut self,
        targets: Vec<SelectorTargetId>,
    ) -> Result<AllDifferentConstraintId, ConstraintModelError> {
        let variables = targets
            .iter()
            .map(|target| self.target_owner_projection_variable(*target))
            .collect::<Result<Vec<_>, _>>()?;
        self.add_all_different(variables, AllDifferentReason::TargetInjectivity { targets })
    }

    pub fn validate(&self) -> Result<(), ConstraintModelError> {
        for (idx, variable) in self.variables.iter().enumerate() {
            if variable.id != ConstraintVariableId(idx) {
                return Err(ConstraintModelError::NonDenseVariable {
                    expected: ConstraintVariableId(idx),
                    actual: variable.id,
                });
            }
            Self::validate_variable_shape(variable)?;
        }

        let mut targets = BTreeSet::new();
        for projection in &self.target_projections {
            self.require_domain(projection.owner_variable, VariableDomain::Owner)?;
            if let Some(binding_variable) = projection.binding_variable {
                self.require_domain(binding_variable, VariableDomain::String)?;
            }
            if projection.binding_variable.is_some() && projection.binding_const.is_some() {
                return Err(ConstraintModelError::ConflictingTargetBindingProjection {
                    target: projection.target,
                });
            }
            if !targets.insert(projection.target) {
                return Err(ConstraintModelError::DuplicateTargetProjection {
                    target: projection.target,
                });
            }
        }

        for (idx, constraint) in self.allowed_tuples.iter().enumerate() {
            if constraint.id != AllowedTupleConstraintId(idx) {
                return Err(ConstraintModelError::NonDenseAllowedTupleConstraint {
                    expected: AllowedTupleConstraintId(idx),
                    actual: constraint.id,
                });
            }
            self.validate_allowed_tuple_constraint(constraint)?;
        }

        for constraint in &self.binary_constraints {
            self.validate_binary_constraint(constraint)?;
        }

        for (idx, constraint) in self.all_different.iter().enumerate() {
            if constraint.id != AllDifferentConstraintId(idx) {
                return Err(ConstraintModelError::NonDenseAllDifferentConstraint {
                    expected: AllDifferentConstraintId(idx),
                    actual: constraint.id,
                });
            }
            self.validate_all_different_constraint(constraint)?;
        }

        Ok(())
    }

    pub fn all_different_violations<'a>(
        &'a self,
        assignment: &BTreeMap<ConstraintVariableId, ConstraintValue>,
    ) -> Vec<&'a AllDifferentConstraint> {
        self.all_different
            .iter()
            .filter(|constraint| constraint.is_satisfied_by(assignment) == Some(false))
            .collect()
    }

    fn validate_variable_shape(variable: &ConstraintVariable) -> Result<(), ConstraintModelError> {
        if variable.values.is_empty() {
            return Err(ConstraintModelError::EmptyDomain {
                variable: variable.id,
            });
        }

        let mut seen = BTreeSet::new();
        for value in &variable.values {
            let actual = value.domain();
            if actual != variable.domain {
                return Err(ConstraintModelError::DomainValueMismatch {
                    variable: variable.id,
                    expected: variable.domain,
                    actual,
                });
            }
            if !seen.insert(value) {
                return Err(ConstraintModelError::DuplicateDomainValue {
                    variable: variable.id,
                    value: value.clone(),
                });
            }
        }
        Ok(())
    }

    fn validate_allowed_tuple_constraint(
        &self,
        constraint: &AllowedTupleConstraint,
    ) -> Result<(), ConstraintModelError> {
        if constraint.variables.is_empty() {
            return Err(ConstraintModelError::EmptyAllowedTupleVariables {
                constraint: constraint.id,
            });
        }

        let mut domains = Vec::with_capacity(constraint.variables.len());
        let mut seen_variables = BTreeSet::new();
        for variable in &constraint.variables {
            if !seen_variables.insert(*variable) {
                return Err(ConstraintModelError::DuplicateTupleVariable {
                    constraint: constraint.id,
                    variable: *variable,
                });
            }
            domains.push(self.require_variable(*variable)?.domain);
        }

        for (tuple_index, tuple) in constraint.tuples.iter().enumerate() {
            if tuple.len() != constraint.variables.len() {
                return Err(ConstraintModelError::TupleArityMismatch {
                    constraint: constraint.id,
                    tuple_index,
                    expected: constraint.variables.len(),
                    actual: tuple.len(),
                });
            }
            for ((variable, expected), value) in
                constraint.variables.iter().zip(domains.iter()).zip(tuple)
            {
                let actual = value.domain();
                if actual != *expected {
                    return Err(ConstraintModelError::TupleDomainMismatch {
                        constraint: constraint.id,
                        tuple_index,
                        variable: *variable,
                        expected: *expected,
                        actual,
                    });
                }
            }
        }
        Ok(())
    }

    fn validate_binary_constraint(
        &self,
        constraint: &BinaryConstraint,
    ) -> Result<(), ConstraintModelError> {
        let left_domain = self.require_variable(constraint.left)?.domain;
        let right_domain = self.require_variable(constraint.right)?.domain;
        match constraint.kind {
            BinaryConstraintKind::Equal | BinaryConstraintKind::NotEqual => {
                if left_domain != right_domain {
                    return Err(ConstraintModelError::BinaryDomainMismatch {
                        left: constraint.left,
                        right: constraint.right,
                        left_domain,
                        right_domain,
                    });
                }
            }
            BinaryConstraintKind::OrdinalBefore => {
                if left_domain != VariableDomain::StatementOrdinal
                    || right_domain != VariableDomain::StatementOrdinal
                {
                    return Err(ConstraintModelError::OrdinalBeforeDomainMismatch {
                        left: constraint.left,
                        right: constraint.right,
                    });
                }
            }
        }
        Ok(())
    }

    fn validate_all_different_constraint(
        &self,
        constraint: &AllDifferentConstraint,
    ) -> Result<(), ConstraintModelError> {
        if constraint.variables.len() < 2 {
            return Err(ConstraintModelError::DegenerateAllDifferent {
                constraint: constraint.id,
            });
        }

        let mut seen = BTreeSet::new();
        let mut expected_domain = None;
        for variable in &constraint.variables {
            if !seen.insert(*variable) {
                return Err(ConstraintModelError::DuplicateAllDifferentVariable {
                    constraint: constraint.id,
                    variable: *variable,
                });
            }
            let domain = self.require_variable(*variable)?.domain;
            match expected_domain {
                None => expected_domain = Some(domain),
                Some(expected) if expected != domain => {
                    return Err(ConstraintModelError::AllDifferentDomainMismatch {
                        constraint: constraint.id,
                        expected,
                        actual: domain,
                    });
                }
                Some(_) => {}
            }
        }

        if let AllDifferentReason::TargetInjectivity { targets } = &constraint.reason {
            let projected_variables = targets
                .iter()
                .map(|target| self.target_owner_projection_variable(*target))
                .collect::<Result<Vec<_>, _>>()?;
            if projected_variables != constraint.variables {
                return Err(ConstraintModelError::TargetInjectivityProjectionMismatch {
                    constraint: constraint.id,
                });
            }
        }

        Ok(())
    }

    fn target_owner_projection_variable(
        &self,
        target: SelectorTargetId,
    ) -> Result<ConstraintVariableId, ConstraintModelError> {
        self.target_projections
            .iter()
            .find_map(|projection| {
                (projection.target == target).then_some(projection.owner_variable)
            })
            .ok_or(ConstraintModelError::UnknownTargetProjection { target })
    }

    fn require_domain(
        &self,
        variable: ConstraintVariableId,
        expected: VariableDomain,
    ) -> Result<(), ConstraintModelError> {
        let actual = self.require_variable(variable)?.domain;
        if actual != expected {
            return Err(ConstraintModelError::VariableDomainMismatch {
                variable,
                expected,
                actual,
            });
        }
        Ok(())
    }

    fn require_variable(
        &self,
        variable: ConstraintVariableId,
    ) -> Result<&ConstraintVariable, ConstraintModelError> {
        self.variables
            .get(variable.0)
            .ok_or(ConstraintModelError::UnknownVariable { variable })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConstraintModelError {
    NonDenseVariable {
        expected: ConstraintVariableId,
        actual: ConstraintVariableId,
    },
    NonDenseAllowedTupleConstraint {
        expected: AllowedTupleConstraintId,
        actual: AllowedTupleConstraintId,
    },
    NonDenseAllDifferentConstraint {
        expected: AllDifferentConstraintId,
        actual: AllDifferentConstraintId,
    },
    UnknownVariable {
        variable: ConstraintVariableId,
    },
    VariableDomainMismatch {
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    EmptyDomain {
        variable: ConstraintVariableId,
    },
    DomainValueMismatch {
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    DuplicateDomainValue {
        variable: ConstraintVariableId,
        value: ConstraintValue,
    },
    DuplicateTargetProjection {
        target: SelectorTargetId,
    },
    ConflictingTargetBindingProjection {
        target: SelectorTargetId,
    },
    UnknownTargetProjection {
        target: SelectorTargetId,
    },
    EmptyAllowedTupleVariables {
        constraint: AllowedTupleConstraintId,
    },
    DuplicateTupleVariable {
        constraint: AllowedTupleConstraintId,
        variable: ConstraintVariableId,
    },
    TupleArityMismatch {
        constraint: AllowedTupleConstraintId,
        tuple_index: usize,
        expected: usize,
        actual: usize,
    },
    TupleDomainMismatch {
        constraint: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    BinaryDomainMismatch {
        left: ConstraintVariableId,
        right: ConstraintVariableId,
        left_domain: VariableDomain,
        right_domain: VariableDomain,
    },
    OrdinalBeforeDomainMismatch {
        left: ConstraintVariableId,
        right: ConstraintVariableId,
    },
    DegenerateAllDifferent {
        constraint: AllDifferentConstraintId,
    },
    DuplicateAllDifferentVariable {
        constraint: AllDifferentConstraintId,
        variable: ConstraintVariableId,
    },
    AllDifferentDomainMismatch {
        constraint: AllDifferentConstraintId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    TargetInjectivityProjectionMismatch {
        constraint: AllDifferentConstraintId,
    },
}

impl fmt::Display for ConstraintModelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonDenseVariable { expected, actual } => write!(
                f,
                "constraint variable ids must be dense: expected {expected:?}, found {actual:?}"
            ),
            Self::NonDenseAllowedTupleConstraint { expected, actual } => write!(
                f,
                "allowed-tuple constraint ids must be dense: expected {expected:?}, found {actual:?}"
            ),
            Self::NonDenseAllDifferentConstraint { expected, actual } => write!(
                f,
                "all_different constraint ids must be dense: expected {expected:?}, found {actual:?}"
            ),
            Self::UnknownVariable { variable } => {
                write!(f, "constraint references unknown variable {variable:?}")
            }
            Self::VariableDomainMismatch {
                variable,
                expected,
                actual,
            } => write!(
                f,
                "variable {variable:?} expected {expected:?} domain, found {actual:?}"
            ),
            Self::EmptyDomain { variable } => {
                write!(f, "variable {variable:?} has an empty finite domain")
            }
            Self::DomainValueMismatch {
                variable,
                expected,
                actual,
            } => write!(
                f,
                "variable {variable:?} expected {expected:?} domain value, found {actual:?}"
            ),
            Self::DuplicateDomainValue { variable, value } => {
                write!(
                    f,
                    "variable {variable:?} has duplicate domain value {value:?}"
                )
            }
            Self::DuplicateTargetProjection { target } => {
                write!(f, "target {target:?} has multiple projections")
            }
            Self::ConflictingTargetBindingProjection { target } => write!(
                f,
                "target {target:?} projects both a binding variable and a constant binding"
            ),
            Self::UnknownTargetProjection { target } => {
                write!(f, "target {target:?} has no model projection")
            }
            Self::EmptyAllowedTupleVariables { constraint } => write!(
                f,
                "allowed-tuple constraint {constraint:?} must mention at least one variable"
            ),
            Self::DuplicateTupleVariable {
                constraint,
                variable,
            } => write!(
                f,
                "allowed-tuple constraint {constraint:?} mentions variable {variable:?} more than once"
            ),
            Self::TupleArityMismatch {
                constraint,
                tuple_index,
                expected,
                actual,
            } => write!(
                f,
                "allowed-tuple constraint {constraint:?} tuple {tuple_index} has arity {actual}, expected {expected}"
            ),
            Self::TupleDomainMismatch {
                constraint,
                tuple_index,
                variable,
                expected,
                actual,
            } => write!(
                f,
                "allowed-tuple constraint {constraint:?} tuple {tuple_index} variable {variable:?} expected {expected:?}, found {actual:?}"
            ),
            Self::BinaryDomainMismatch {
                left,
                right,
                left_domain,
                right_domain,
            } => write!(
                f,
                "binary constraint domains differ: {left:?} is {left_domain:?}, {right:?} is {right_domain:?}"
            ),
            Self::OrdinalBeforeDomainMismatch { left, right } => write!(
                f,
                "ordinal-before constraint requires statement-ordinal variables, got {left:?} and {right:?}"
            ),
            Self::DegenerateAllDifferent { constraint } => write!(
                f,
                "all_different constraint {constraint:?} requires at least two variables"
            ),
            Self::DuplicateAllDifferentVariable {
                constraint,
                variable,
            } => write!(
                f,
                "all_different constraint {constraint:?} mentions variable {variable:?} more than once"
            ),
            Self::AllDifferentDomainMismatch {
                constraint,
                expected,
                actual,
            } => write!(
                f,
                "all_different constraint {constraint:?} expected {expected:?} variables, found {actual:?}"
            ),
            Self::TargetInjectivityProjectionMismatch { constraint } => write!(
                f,
                "target-injectivity all_different constraint {constraint:?} does not match target projections"
            ),
        }
    }
}

impl Error for ConstraintModelError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    #[test]
    fn target_injectivity_materializes_as_all_different_over_owner_variables() {
        let mut model = SelectorConstraintModel::default();
        let broad = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10), owner(20)],
                Some("X".to_string()),
            )
            .unwrap();
        let strict = model
            .add_variable(
                SelectorVariableId(1),
                VariableDomain::Owner,
                vec![owner(20)],
                Some("Y".to_string()),
            )
            .unwrap();

        model
            .add_target_projection(SelectorTargetId(0), broad, None)
            .unwrap();
        model
            .add_target_projection(SelectorTargetId(1), strict, None)
            .unwrap();

        let all_different = model
            .require_target_all_different(vec![SelectorTargetId(0), SelectorTargetId(1)])
            .unwrap();

        assert_eq!(all_different, AllDifferentConstraintId(0));
        assert_eq!(
            model.all_different[0],
            AllDifferentConstraint {
                id: AllDifferentConstraintId(0),
                variables: vec![broad, strict],
                reason: AllDifferentReason::TargetInjectivity {
                    targets: vec![SelectorTargetId(0), SelectorTargetId(1)]
                },
            }
        );
        model.validate().unwrap();
    }

    #[test]
    fn injective_disambiguation_rejects_duplicate_owner_assignment() {
        let mut model = SelectorConstraintModel::default();
        let broad = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10), owner(20)],
                Some("const x = f(ANYTHING)".to_string()),
            )
            .unwrap();
        let strict = model
            .add_variable(
                SelectorVariableId(1),
                VariableDomain::Owner,
                vec![owner(20)],
                Some("const y = f(123)".to_string()),
            )
            .unwrap();
        model
            .add_target_projection(SelectorTargetId(0), broad, None)
            .unwrap();
        model
            .add_target_projection(SelectorTargetId(1), strict, None)
            .unwrap();
        model
            .require_target_all_different(vec![SelectorTargetId(0), SelectorTargetId(1)])
            .unwrap();

        let duplicate_assignment = BTreeMap::from([(broad, owner(20)), (strict, owner(20))]);
        assert_eq!(
            model.all_different_violations(&duplicate_assignment).len(),
            1
        );

        let forced_assignment = BTreeMap::from([(broad, owner(10)), (strict, owner(20))]);
        assert!(
            model
                .all_different_violations(&forced_assignment)
                .is_empty()
        );
    }

    #[test]
    fn allowed_tuples_are_typed_by_variable_domain() {
        let mut model = SelectorConstraintModel::default();
        let owner_var = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10)],
                None,
            )
            .unwrap();
        let node_var = model
            .add_variable(
                SelectorVariableId(1),
                VariableDomain::AstNode,
                vec![ConstraintValue::AstNode(99)],
                None,
            )
            .unwrap();

        model
            .add_allowed_tuples(
                vec![owner_var, node_var],
                vec![vec![owner(10), ConstraintValue::AstNode(99)]],
            )
            .unwrap();

        let error = model
            .add_allowed_tuples(vec![owner_var], vec![vec![ConstraintValue::AstNode(99)]])
            .unwrap_err();
        assert!(matches!(
            error,
            ConstraintModelError::TupleDomainMismatch { .. }
        ));
    }
}
