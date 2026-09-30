//! A [`CompiledSelectorProblem`] as OR-Tools' `CpModelProto`.
//!
//! Variables and constraints keep the problem's own order: CP-SAT's search
//! follows the model's order, and with it which values a capped support listing
//! shows.

use std::collections::{BTreeSet, HashMap};
use std::error::Error;
use std::fmt;
use std::hash::Hash;

use cp_model_proto::operations_research::sat::constraint_proto::Constraint;
use cp_model_proto::operations_research::sat::{
    AllDifferentConstraintProto, ConstraintProto, CpModelProto, IntegerVariableProto,
    LinearExpressionProto, TableConstraintProto,
};
use selector_constraint_backend::{
    AllDifferentConstraintId, AllowedTupleConstraintId, AllowedTupleRowsId, BackendValueId,
    CompiledSelectorProblem, CompiledVariableDomain, ConstraintVariableId, SharedVariableDomainId,
    TargetBindingProjection,
};

/// A projected variable: an owner or binding variable of a target, whose
/// supported values the search reports.
#[derive(Debug, Clone, Copy)]
pub struct ProjectionVariable {
    pub id: ConstraintVariableId,
    /// Position in `CpModelProto::variables`.
    pub index: i32,
    pub domain_size: usize,
}

#[derive(Debug)]
pub struct SelectorCpModel {
    pub proto: CpModelProto,
    /// Ascending by `id`.
    pub projection: Vec<ProjectionVariable>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum InvalidProblem {
    DuplicateVariable(ConstraintVariableId),
    EmptyDomain(ConstraintVariableId),
    DuplicateDomainValues(ConstraintVariableId),
    DuplicateSharedDomain(SharedVariableDomainId),
    UnknownSharedDomain {
        variable: ConstraintVariableId,
        domain: SharedVariableDomainId,
    },
    UnknownVariable(ConstraintVariableId),
    DuplicateRowSet(AllowedTupleRowsId),
    UnknownRowSet {
        constraint: AllowedTupleConstraintId,
        row_set: AllowedTupleRowsId,
    },
    TableWithoutVariables(AllowedTupleConstraintId),
    RowSetArityMismatch {
        constraint: AllowedTupleConstraintId,
        row_set: AllowedTupleRowsId,
        arity: usize,
        expected: usize,
    },
    RowSetNotWholeRows {
        row_set: AllowedTupleRowsId,
        values: usize,
        arity: usize,
    },
    AllDifferentTooSmall(AllDifferentConstraintId),
}

impl fmt::Display for InvalidProblem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateVariable(id) => write!(f, "duplicate variable id {}", id.0),
            Self::EmptyDomain(id) => write!(f, "variable {} has an empty domain", id.0),
            Self::DuplicateDomainValues(id) => {
                write!(f, "variable {} has duplicate domain values", id.0)
            }
            Self::DuplicateSharedDomain(id) => {
                write!(f, "duplicate shared sparse domain id {}", id.0)
            }
            Self::UnknownSharedDomain { variable, domain } => write!(
                f,
                "variable {} references unknown shared sparse domain {}",
                variable.0, domain.0
            ),
            Self::UnknownVariable(id) => write!(f, "unknown variable id {}", id.0),
            Self::DuplicateRowSet(id) => write!(f, "duplicate allowed row set id {}", id.0),
            Self::UnknownRowSet {
                constraint,
                row_set,
            } => write!(
                f,
                "table constraint {} references unknown row set {}",
                constraint.0, row_set.0
            ),
            Self::TableWithoutVariables(id) => {
                write!(f, "table constraint {} has no variables", id.0)
            }
            Self::RowSetArityMismatch {
                constraint,
                row_set,
                arity,
                expected,
            } => write!(
                f,
                "table constraint {} row set {} has arity {arity}, expected {expected}",
                constraint.0, row_set.0
            ),
            Self::RowSetNotWholeRows {
                row_set,
                values,
                arity,
            } => write!(
                f,
                "row set {} has {values} values, not a multiple of arity {arity}",
                row_set.0
            ),
            Self::AllDifferentTooSmall(id) => {
                write!(
                    f,
                    "all_different constraint {} has fewer than two variables",
                    id.0
                )
            }
        }
    }
}

impl Error for InvalidProblem {}

/// A variable's domain as OR-Tools' `Domain` proto encodes it.
#[derive(Clone)]
struct EncodedDomain {
    /// `[start, end]` of each maximal run of consecutive values, flattened.
    bounds: Vec<i64>,
    size: usize,
}

fn sparse_domain(
    variable: ConstraintVariableId,
    values: &[BackendValueId],
) -> Result<EncodedDomain, InvalidProblem> {
    if values.is_empty() {
        return Err(InvalidProblem::EmptyDomain(variable));
    }
    let mut sorted = values.iter().map(|value| value.0).collect::<Vec<_>>();
    sorted.sort_unstable();
    if sorted.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(InvalidProblem::DuplicateDomainValues(variable));
    }
    Ok(EncodedDomain {
        bounds: sorted
            .chunk_by(|left, right| left.checked_add(1) == Some(*right))
            .flat_map(|run| [run[0], run[run.len() - 1]])
            .collect(),
        size: sorted.len(),
    })
}

/// Items by id; the id of the first repeat when two share one.
fn index_by_id<T, Id: Hash + Eq + Copy>(
    items: &[T],
    id: impl Fn(&T) -> Id,
) -> Result<HashMap<Id, &T>, Id> {
    let mut index = HashMap::with_capacity(items.len());
    for item in items {
        if index.insert(id(item), item).is_some() {
            return Err(id(item));
        }
    }
    Ok(index)
}

fn expression(variable: i32) -> LinearExpressionProto {
    LinearExpressionProto {
        vars: vec![variable],
        coeffs: vec![1],
        offset: 0,
    }
}

fn table(
    variables: impl IntoIterator<Item = i32>,
    values: Vec<i64>,
    negated: bool,
) -> ConstraintProto {
    ConstraintProto {
        constraint: Some(Constraint::Table(TableConstraintProto {
            exprs: variables.into_iter().map(expression).collect(),
            values,
            negated,
            ..Default::default()
        })),
        ..Default::default()
    }
}

/// Forbids `variables` taking any of the tuples in `values` (flat, one tuple
/// after another).
pub fn forbidden_table(
    variables: impl IntoIterator<Item = i32>,
    values: Vec<i64>,
) -> ConstraintProto {
    table(variables, values, true)
}

impl SelectorCpModel {
    pub fn build(problem: &CompiledSelectorProblem) -> Result<Self, InvalidProblem> {
        let shared_domains = index_by_id(&problem.shared_variable_domains, |domain| domain.id)
            .map_err(InvalidProblem::DuplicateSharedDomain)?;
        let mut encoded_shared_domains = HashMap::new();
        let mut variable_index = HashMap::with_capacity(problem.variables.len());
        let mut domain_sizes = Vec::with_capacity(problem.variables.len());
        let mut proto = CpModelProto::default();

        for variable in &problem.variables {
            let domain = match &variable.values {
                // A full domain is the dense range of its value ids.
                CompiledVariableDomain::Full(domain) => {
                    let count = problem.full_domains.get(*domain).len();
                    if count == 0 {
                        return Err(InvalidProblem::EmptyDomain(variable.id));
                    }
                    EncodedDomain {
                        bounds: vec![
                            0,
                            i64::try_from(count - 1).expect("a domain has fewer than 2^63 values"),
                        ],
                        size: count,
                    }
                }
                CompiledVariableDomain::Sparse(values) => sparse_domain(variable.id, values)?,
                CompiledVariableDomain::SharedSparse(id) => {
                    if !encoded_shared_domains.contains_key(id) {
                        let shared =
                            shared_domains
                                .get(id)
                                .ok_or(InvalidProblem::UnknownSharedDomain {
                                    variable: variable.id,
                                    domain: *id,
                                })?;
                        encoded_shared_domains
                            .insert(*id, sparse_domain(variable.id, &shared.values)?);
                    }
                    encoded_shared_domains[id].clone()
                }
            };
            let index = i32::try_from(proto.variables.len())
                .expect("a model has fewer than 2^31 variables");
            if variable_index.insert(variable.id, index).is_some() {
                return Err(InvalidProblem::DuplicateVariable(variable.id));
            }
            domain_sizes.push(domain.size);
            proto.variables.push(IntegerVariableProto {
                name: variable.debug_name.clone().unwrap_or_default(),
                domain: domain.bounds,
            });
        }

        let indices = |ids: &[ConstraintVariableId]| {
            ids.iter()
                .map(|id| {
                    variable_index
                        .get(id)
                        .copied()
                        .ok_or(InvalidProblem::UnknownVariable(*id))
                })
                .collect::<Result<Vec<_>, _>>()
        };

        let row_sets = index_by_id(&problem.allowed_tuple_row_sets, |row_set| row_set.id)
            .map_err(InvalidProblem::DuplicateRowSet)?;
        for constraint in &problem.allowed_tuples {
            let variables = indices(&constraint.variables)?;
            if variables.is_empty() {
                return Err(InvalidProblem::TableWithoutVariables(constraint.id));
            }
            let row_set =
                row_sets
                    .get(&constraint.row_set)
                    .ok_or(InvalidProblem::UnknownRowSet {
                        constraint: constraint.id,
                        row_set: constraint.row_set,
                    })?;
            let arity = row_set.rows.arity();
            if arity != variables.len() {
                return Err(InvalidProblem::RowSetArityMismatch {
                    constraint: constraint.id,
                    row_set: row_set.id,
                    arity,
                    expected: variables.len(),
                });
            }
            let values = row_set.rows.values();
            if values.len() % arity != 0 {
                return Err(InvalidProblem::RowSetNotWholeRows {
                    row_set: row_set.id,
                    values: values.len(),
                    arity,
                });
            }
            proto.constraints.push(table(
                variables,
                values.iter().map(|value| value.0).collect(),
                false,
            ));
        }

        for constraint in &problem.all_different {
            if constraint.variables.len() < 2 {
                return Err(InvalidProblem::AllDifferentTooSmall(constraint.id));
            }
            proto.constraints.push(ConstraintProto {
                constraint: Some(Constraint::AllDiff(AllDifferentConstraintProto {
                    exprs: indices(&constraint.variables)?
                        .into_iter()
                        .map(expression)
                        .collect(),
                })),
                ..Default::default()
            });
        }

        let mut projected = BTreeSet::new();
        for projection in &problem.target_projections {
            projected.insert(projection.owner_variable);
            if let Some(TargetBindingProjection::Variable(binding)) = &projection.binding_projection
            {
                projected.insert(*binding);
            }
        }
        let projection = projected
            .into_iter()
            .map(|id| {
                let index = *variable_index
                    .get(&id)
                    .ok_or(InvalidProblem::UnknownVariable(id))?;
                Ok(ProjectionVariable {
                    id,
                    index,
                    domain_size: domain_sizes
                        [usize::try_from(index).expect("a variable index is not negative")],
                })
            })
            .collect::<Result<Vec<_>, InvalidProblem>>()?;

        Ok(Self { proto, projection })
    }
}
