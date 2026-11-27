use selector_ir::{SelectorTargetId, VariableDomain};

use crate::*;

#[derive(thiserror::Error, Debug, Clone, PartialEq, Eq)]
pub enum CompiledSelectorProblemError {
    #[error("constraint references unknown variable {variable:?}")]
    UnknownVariable { variable: ConstraintVariableId },
    #[error("constraint references unknown shared domain {domain_id:?}")]
    UnknownSharedVariableDomain { domain_id: SharedVariableDomainId },
    #[error("variable {variable:?} expected {expected:?} domain, found {actual:?}")]
    VariableDomainMismatch {
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    #[error("expected {expected:?} value, found {actual:?}")]
    DomainValueMismatch {
        expected: VariableDomain,
        actual: VariableDomain,
    },
    #[error("target {target:?} has more than one projection")]
    DuplicateTargetProjection { target: SelectorTargetId },
    #[error("target {target:?} has no projection")]
    UnknownTargetProjection { target: SelectorTargetId },
    #[error("allowed tuple constraint {id:?} has no variables")]
    EmptyAllowedTupleVariables { id: AllowedTupleConstraintId },
    #[error("allowed tuple constraint {id:?} references variable {variable:?} more than once")]
    DuplicateTupleVariable {
        id: AllowedTupleConstraintId,
        variable: ConstraintVariableId,
    },
    #[error("allowed tuple row set {row_set:?} does not exist")]
    UnknownAllowedTupleRowSet { row_set: AllowedTupleRowsId },
    #[error(
        "allowed tuple constraint {id:?} row {tuple_index} has arity {actual}, expected {expected}"
    )]
    TupleArityMismatch {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        expected: usize,
        actual: usize,
    },
    #[error(
        "allowed tuple constraint {id:?} row {tuple_index} variable {variable:?} expected {expected:?}, found {actual:?}"
    )]
    TupleDomainMismatch {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    #[error(
        "allowed tuple constraint {id:?} row {tuple_index} variable {variable:?} has encoded value {value:?} outside {domain:?} domain"
    )]
    EncodedTupleValueOutOfDomain {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    },
    #[error(
        "variable {variable:?} restriction contains encoded value {value:?} outside {domain:?} domain"
    )]
    EncodedVariableDomainValueOutOfDomain {
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    },
    #[error("shared {domain:?} domain contains encoded value {value:?} outside its dictionary")]
    EncodedSharedDomainValueOutOfDomain {
        domain: VariableDomain,
        value: BackendValueId,
    },
    #[error("all_different constraint {id:?} has fewer than two variables")]
    DegenerateAllDifferent { id: AllDifferentConstraintId },
    #[error("all_different constraint {id:?} references variable {variable:?} more than once")]
    DuplicateAllDifferentVariable {
        id: AllDifferentConstraintId,
        variable: ConstraintVariableId,
    },
    #[error("all_different constraint {id:?} mixes {expected:?} and {actual:?} domains")]
    AllDifferentDomainMismatch {
        id: AllDifferentConstraintId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    #[error("compiled selector problem has too many values: {count}")]
    TooManyValues { count: usize },
}

#[derive(thiserror::Error, Debug, Clone, PartialEq, Eq)]
pub enum BackendAssignmentError {
    #[error("assignment references unknown variable {variable:?}")]
    UnknownVariable { variable: ConstraintVariableId },
    #[error("assignment references unknown value {value:?}")]
    UnknownValue { value: BackendValueId },
    #[error("assignment value {value:?} is outside variable {variable:?} domain")]
    ValueOutsideDomain {
        variable: ConstraintVariableId,
        value: BackendValueId,
    },
    #[error("assignment includes variable {variable:?} more than once")]
    DuplicateVariable { variable: ConstraintVariableId },
    #[error("assignment value id {value:?} is negative")]
    NegativeValue { value: BackendValueId },
    #[error("assignment value id {value:?} does not fit in usize")]
    ValueIndexOutOfRange { value: BackendValueId },
}
