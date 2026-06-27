//! Solver-facing contract for exact selector assignment backends.
//!
//! `SelectorConstraintModel` is the semantic model. This module canonicalizes
//! it into the integer-domain shape expected by external CP/SAT backends:
//! variables keep finite integer domains, allowed-tuples reference the same
//! integer ids, and `all_different` compares globally-canonical values rather
//! than per-variable local indexes.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use selector_constraint_model::{
    AllDifferentConstraintId, AllDifferentReason, AllowedTupleConstraintId, BinaryConstraintKind,
    ConstraintModelError, ConstraintValue, ConstraintVariableId, SelectorConstraintModel,
    TargetBindingProjection,
};
use selector_ir::{SelectorTargetId, SelectorVariableId, VariableDomain};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BackendValueId(pub i64);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendVariable {
    pub id: ConstraintVariableId,
    pub source: SelectorVariableId,
    pub domain: VariableDomain,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debug_name: Option<String>,
    pub values: Vec<BackendValueId>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendAllowedTupleConstraint {
    pub id: AllowedTupleConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub tuples: Vec<Vec<BackendValueId>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendBinaryConstraint {
    pub left: ConstraintVariableId,
    pub right: ConstraintVariableId,
    pub kind: BinaryConstraintKind,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendAllDifferentConstraint {
    pub id: AllDifferentConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub reason: AllDifferentReason,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendTargetProjection {
    pub target: SelectorTargetId,
    pub owner_variable: ConstraintVariableId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_projection: Option<TargetBindingProjection>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorBackendProblem {
    pub value_dictionary: Vec<ConstraintValue>,
    pub variables: Vec<BackendVariable>,
    pub target_projections: Vec<BackendTargetProjection>,
    pub allowed_tuples: Vec<BackendAllowedTupleConstraint>,
    pub binary_constraints: Vec<BackendBinaryConstraint>,
    pub all_different: Vec<BackendAllDifferentConstraint>,
}

impl SelectorBackendProblem {
    pub fn from_model(
        model: &SelectorConstraintModel,
    ) -> Result<Self, SelectorBackendProblemError> {
        model
            .validate()
            .map_err(SelectorBackendProblemError::InvalidModel)?;

        let mut value_ids = BTreeMap::new();
        let mut value_dictionary = Vec::new();
        for variable in &model.variables {
            for value in &variable.values {
                intern_value(value, &mut value_ids, &mut value_dictionary)?;
            }
        }

        let mut variable_domains = BTreeMap::new();
        let variables = model
            .variables
            .iter()
            .map(|variable| {
                let domain_values = variable.values.iter().cloned().collect::<BTreeSet<_>>();
                variable_domains.insert(variable.id, domain_values);
                Ok(BackendVariable {
                    id: variable.id,
                    source: variable.source,
                    domain: variable.domain,
                    debug_name: variable.debug_name.clone(),
                    values: variable
                        .values
                        .iter()
                        .map(|value| value_id(value, &value_ids))
                        .collect::<Result<Vec<_>, _>>()?,
                })
            })
            .collect::<Result<Vec<_>, SelectorBackendProblemError>>()?;

        let allowed_tuples = model
            .allowed_tuples
            .iter()
            .map(|constraint| {
                let tuples = constraint
                    .tuples
                    .iter()
                    .enumerate()
                    .map(|(tuple_index, tuple)| {
                        constraint
                            .variables
                            .iter()
                            .zip(tuple.iter())
                            .map(|(variable, value)| {
                                let domain_values = variable_domains.get(variable).ok_or(
                                    SelectorBackendProblemError::UnknownVariable {
                                        variable: *variable,
                                    },
                                )?;
                                if !domain_values.contains(value) {
                                    return Err(
                                        SelectorBackendProblemError::TupleValueOutsideDomain {
                                            constraint: constraint.id,
                                            tuple_index,
                                            variable: *variable,
                                            value: value.clone(),
                                        },
                                    );
                                }
                                value_id(value, &value_ids)
                            })
                            .collect::<Result<Vec<_>, _>>()
                    })
                    .collect::<Result<Vec<_>, _>>()?;

                Ok(BackendAllowedTupleConstraint {
                    id: constraint.id,
                    variables: constraint.variables.clone(),
                    tuples,
                })
            })
            .collect::<Result<Vec<_>, SelectorBackendProblemError>>()?;

        Ok(Self {
            value_dictionary,
            variables,
            target_projections: model
                .target_projections
                .iter()
                .map(|projection| BackendTargetProjection {
                    target: projection.target,
                    owner_variable: projection.owner_variable,
                    binding_projection: projection.binding_projection.clone(),
                })
                .collect(),
            allowed_tuples,
            binary_constraints: model
                .binary_constraints
                .iter()
                .map(|constraint| BackendBinaryConstraint {
                    left: constraint.left,
                    right: constraint.right,
                    kind: constraint.kind,
                })
                .collect(),
            all_different: model
                .all_different
                .iter()
                .map(|constraint| BackendAllDifferentConstraint {
                    id: constraint.id,
                    variables: constraint.variables.clone(),
                    reason: constraint.reason.clone(),
                })
                .collect(),
        })
    }

    pub fn decode_assignment(
        &self,
        assignment: &BackendAssignment,
    ) -> Result<BTreeMap<ConstraintVariableId, ConstraintValue>, BackendAssignmentError> {
        let domains = self
            .variables
            .iter()
            .map(|variable| {
                (
                    variable.id,
                    variable.values.iter().copied().collect::<BTreeSet<_>>(),
                )
            })
            .collect::<BTreeMap<_, _>>();

        let mut decoded = BTreeMap::new();
        for entry in &assignment.values {
            let Some(domain) = domains.get(&entry.variable) else {
                return Err(BackendAssignmentError::UnknownVariable {
                    variable: entry.variable,
                });
            };
            if !domain.contains(&entry.value) {
                return Err(BackendAssignmentError::ValueOutsideDomain {
                    variable: entry.variable,
                    value: entry.value,
                });
            }
            let value = self
                .value_dictionary
                .get(backend_value_index(entry.value)?)
                .cloned()
                .ok_or(BackendAssignmentError::UnknownValue { value: entry.value })?;
            if decoded.insert(entry.variable, value).is_some() {
                return Err(BackendAssignmentError::DuplicateVariable {
                    variable: entry.variable,
                });
            }
        }
        Ok(decoded)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendAssignment {
    pub values: Vec<BackendVariableAssignment>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendVariableAssignment {
    pub variable: ConstraintVariableId,
    pub value: BackendValueId,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendSolveStatus {
    Unsatisfiable,
    Satisfiable,
    Ambiguous,
    Unknown,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendAssignmentCoverage {
    /// Returned assignments are examples only. They must not be used to classify
    /// selector claims as unique or ambiguous.
    #[default]
    Sample,
    /// Returned assignments cover every owner/binding support value that any
    /// target projection can take across all satisfying global assignments.
    TargetSupportComplete,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendSolveResult {
    pub status: BackendSolveStatus,
    #[serde(default)]
    pub assignment_coverage: BackendAssignmentCoverage,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub assignments: Vec<BackendAssignment>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diagnostic: Option<String>,
}

pub trait SelectorConstraintBackend {
    type Error;

    fn solve(&self, problem: &SelectorBackendProblem) -> Result<BackendSolveResult, Self::Error>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorBackendProblemError {
    InvalidModel(ConstraintModelError),
    TooManyValues {
        count: usize,
    },
    UnknownValue {
        value: ConstraintValue,
    },
    UnknownVariable {
        variable: ConstraintVariableId,
    },
    TupleValueOutsideDomain {
        constraint: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        value: ConstraintValue,
    },
}

impl fmt::Display for SelectorBackendProblemError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidModel(err) => write!(f, "invalid selector constraint model: {err}"),
            Self::TooManyValues { count } => write!(
                f,
                "selector backend problem has {count} distinct values, exceeding i64 ids"
            ),
            Self::UnknownValue { value } => {
                write!(f, "selector backend value dictionary is missing {value:?}")
            }
            Self::UnknownVariable { variable } => {
                write!(
                    f,
                    "selector backend problem references unknown variable {variable:?}"
                )
            }
            Self::TupleValueOutsideDomain {
                constraint,
                tuple_index,
                variable,
                value,
            } => write!(
                f,
                "allowed-tuple constraint {constraint:?} tuple {tuple_index} gives variable {variable:?} out-of-domain value {value:?}"
            ),
        }
    }
}

impl Error for SelectorBackendProblemError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidModel(err) => Some(err),
            Self::TooManyValues { .. }
            | Self::UnknownValue { .. }
            | Self::UnknownVariable { .. }
            | Self::TupleValueOutsideDomain { .. } => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendAssignmentError {
    UnknownVariable {
        variable: ConstraintVariableId,
    },
    UnknownValue {
        value: BackendValueId,
    },
    NegativeValue {
        value: BackendValueId,
    },
    ValueOutsideDomain {
        variable: ConstraintVariableId,
        value: BackendValueId,
    },
    DuplicateVariable {
        variable: ConstraintVariableId,
    },
}

impl fmt::Display for BackendAssignmentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownVariable { variable } => {
                write!(
                    f,
                    "backend assignment references unknown variable {variable:?}"
                )
            }
            Self::UnknownValue { value } => {
                write!(f, "backend assignment references unknown value {value:?}")
            }
            Self::NegativeValue { value } => {
                write!(
                    f,
                    "backend assignment references negative value id {value:?}"
                )
            }
            Self::ValueOutsideDomain { variable, value } => write!(
                f,
                "backend assignment gives variable {variable:?} out-of-domain value {value:?}"
            ),
            Self::DuplicateVariable { variable } => write!(
                f,
                "backend assignment gives variable {variable:?} more than one value"
            ),
        }
    }
}

impl Error for BackendAssignmentError {}

fn intern_value(
    value: &ConstraintValue,
    value_ids: &mut BTreeMap<ConstraintValue, BackendValueId>,
    value_dictionary: &mut Vec<ConstraintValue>,
) -> Result<BackendValueId, SelectorBackendProblemError> {
    if let Some(id) = value_ids.get(value) {
        return Ok(*id);
    }
    let count = value_dictionary.len();
    let id = BackendValueId(
        i64::try_from(count).map_err(|_| SelectorBackendProblemError::TooManyValues { count })?,
    );
    value_ids.insert(value.clone(), id);
    value_dictionary.push(value.clone());
    Ok(id)
}

fn value_id(
    value: &ConstraintValue,
    value_ids: &BTreeMap<ConstraintValue, BackendValueId>,
) -> Result<BackendValueId, SelectorBackendProblemError> {
    value_ids
        .get(value)
        .copied()
        .ok_or_else(|| SelectorBackendProblemError::UnknownValue {
            value: value.clone(),
        })
}

fn backend_value_index(value: BackendValueId) -> Result<usize, BackendAssignmentError> {
    usize::try_from(value.0).map_err(|_| BackendAssignmentError::NegativeValue { value })
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::OwnerId;
    use selector_constraint_model::{AllDifferentReason, ConstraintValue, SelectorConstraintModel};

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    fn backend_value(value: i64) -> BackendValueId {
        BackendValueId(value)
    }

    #[test]
    fn backend_problem_uses_global_value_ids_for_overlapping_domains() {
        let mut model = SelectorConstraintModel::default();
        let broad = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10), owner(20)],
                Some("broad".to_string()),
            )
            .unwrap();
        let strict = model
            .add_variable(
                SelectorVariableId(1),
                VariableDomain::Owner,
                vec![owner(20)],
                Some("strict".to_string()),
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
        model
            .add_allowed_tuples(vec![broad], vec![vec![owner(10)], vec![owner(20)]])
            .unwrap();
        model
            .add_allowed_tuples(vec![strict], vec![vec![owner(20)]])
            .unwrap();

        let problem = SelectorBackendProblem::from_model(&model).unwrap();

        assert_eq!(problem.value_dictionary, vec![owner(10), owner(20)]);
        assert_eq!(
            problem.variables[0].values,
            vec![backend_value(0), backend_value(1)]
        );
        assert_eq!(problem.variables[1].values, vec![backend_value(1)]);
        assert_eq!(
            problem.allowed_tuples[1].tuples,
            vec![vec![backend_value(1)]],
            "allowed tuples must use the same global id as all_different"
        );
        assert_eq!(
            problem.all_different[0],
            BackendAllDifferentConstraint {
                id: AllDifferentConstraintId(0),
                variables: vec![broad, strict],
                reason: AllDifferentReason::TargetInjectivity {
                    targets: vec![SelectorTargetId(0), SelectorTargetId(1)]
                },
            }
        );
    }

    #[test]
    fn backend_problem_rejects_tuple_values_outside_variable_domain() {
        let mut model = SelectorConstraintModel::default();
        let variable = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10)],
                None,
            )
            .unwrap();
        model
            .add_allowed_tuples(vec![variable], vec![vec![owner(20)]])
            .unwrap();

        let err = SelectorBackendProblem::from_model(&model).unwrap_err();

        assert!(matches!(
            err,
            SelectorBackendProblemError::TupleValueOutsideDomain {
                constraint: AllowedTupleConstraintId(0),
                tuple_index: 0,
                variable: ConstraintVariableId(0),
                value: ConstraintValue::Owner(OwnerId(20)),
            }
        ));
    }

    #[test]
    fn backend_assignment_decodes_to_constraint_values() {
        let mut model = SelectorConstraintModel::default();
        let variable = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10), owner(20)],
                None,
            )
            .unwrap();
        let problem = SelectorBackendProblem::from_model(&model).unwrap();
        let assignment = BackendAssignment {
            values: vec![BackendVariableAssignment {
                variable,
                value: backend_value(1),
            }],
        };

        assert_eq!(
            problem.decode_assignment(&assignment).unwrap(),
            BTreeMap::from([(variable, owner(20))])
        );
    }

    #[test]
    fn backend_assignment_rejects_local_value_ids() {
        let mut model = SelectorConstraintModel::default();
        let broad = model
            .add_variable(
                SelectorVariableId(0),
                VariableDomain::Owner,
                vec![owner(10), owner(20)],
                None,
            )
            .unwrap();
        let strict = model
            .add_variable(
                SelectorVariableId(1),
                VariableDomain::Owner,
                vec![owner(20)],
                None,
            )
            .unwrap();
        let problem = SelectorBackendProblem::from_model(&model).unwrap();
        let assignment = BackendAssignment {
            values: vec![
                BackendVariableAssignment {
                    variable: broad,
                    value: backend_value(0),
                },
                BackendVariableAssignment {
                    variable: strict,
                    value: backend_value(0),
                },
            ],
        };

        assert_eq!(
            problem.decode_assignment(&assignment).unwrap_err(),
            BackendAssignmentError::ValueOutsideDomain {
                variable: strict,
                value: backend_value(0),
            }
        );
    }
}
