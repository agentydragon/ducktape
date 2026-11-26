//! Builder input validation and variable lookup.

use std::collections::BTreeSet;

use super::*;

impl CompiledSelectorProblemBuilder {
    pub(super) fn validate_allowed_tuple_variables(
        &self,
        variables: &[ConstraintVariableId],
    ) -> Result<Vec<VariableDomain>, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        if variables.is_empty() {
            return Err(CompiledSelectorProblemError::EmptyAllowedTupleVariables { id });
        }
        let mut seen_variables = BTreeSet::new();
        variables
            .iter()
            .map(|variable| {
                if !seen_variables.insert(*variable) {
                    return Err(CompiledSelectorProblemError::DuplicateTupleVariable {
                        id,
                        variable: *variable,
                    });
                }
                Ok(self.require_variable(*variable)?.domain)
            })
            .collect()
    }

    pub(super) fn validate_encoded_value_domain(
        &self,
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if !self.value_dictionary.contains_id(domain, value) {
            return Err(CompiledSelectorProblemError::EncodedTupleValueOutOfDomain {
                id,
                tuple_index,
                variable,
                domain,
                value,
            });
        }
        Ok(())
    }

    pub(super) fn validate_encoded_domain_value(
        &self,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if !self.value_dictionary.contains_id(domain, value) {
            return Err(
                CompiledSelectorProblemError::EncodedSharedDomainValueOutOfDomain { domain, value },
            );
        }
        Ok(())
    }

    pub(super) fn encoded_value_matches_variable_domain(
        &self,
        variable: ConstraintVariableId,
        value: BackendValueId,
    ) -> Result<bool, CompiledSelectorProblemError> {
        match &self.require_variable(variable)?.values {
            CompiledVariableDomain::Full(_) => Ok(true),
            CompiledVariableDomain::Sparse(values) => Ok(values.binary_search(&value).is_ok()),
            CompiledVariableDomain::SharedSparse(id) => Ok(self.shared_variable_domains[id.0]
                .values
                .binary_search(&value)
                .is_ok()),
        }
    }

    pub(super) fn validate_encoded_variable_domain_value(
        &self,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if !self.value_dictionary.contains_id(domain, value) {
            return Err(
                CompiledSelectorProblemError::EncodedVariableDomainValueOutOfDomain {
                    variable,
                    domain,
                    value,
                },
            );
        }
        Ok(())
    }

    pub(super) fn ensure_full_domain_contains(
        &mut self,
        domain: VariableDomain,
        value: BackendValueId,
    ) {
        let values = self.full_domains.get_mut(domain);
        // compile_selector_problem preloads every fact/constant domain before
        // lowering; no CLI candidate loss was established. The public builder also
        // permits interning values before adding tuples. An ID is not then an
        // index into this subset. Preserve the dense O(1) path without silently
        // dropping valid IDs in sparse or out-of-order domains.
        let index = usize::try_from(value.0).expect("validated nonnegative backend value ID");
        if values.get(index) == Some(&value) {
            return;
        }
        if values.last().is_none_or(|last| *last < value) {
            values.push(value);
        } else if let Err(index) = values.binary_search(&value) {
            values.insert(index, value);
        }
    }

    pub(super) fn validate_all_different_constraint(
        &self,
        id: AllDifferentConstraintId,
        variables: &[ConstraintVariableId],
    ) -> Result<(), CompiledSelectorProblemError> {
        if variables.len() < 2 {
            return Err(CompiledSelectorProblemError::DegenerateAllDifferent { id });
        }
        let mut seen = BTreeSet::new();
        let mut expected_domain = None;
        for variable in variables {
            if !seen.insert(*variable) {
                return Err(
                    CompiledSelectorProblemError::DuplicateAllDifferentVariable {
                        id,
                        variable: *variable,
                    },
                );
            }
            let domain = self.require_variable(*variable)?.domain;
            match expected_domain {
                None => expected_domain = Some(domain),
                Some(expected) if expected != domain => {
                    return Err(CompiledSelectorProblemError::AllDifferentDomainMismatch {
                        id,
                        expected,
                        actual: domain,
                    });
                }
                Some(_) => {}
            }
        }

        Ok(())
    }

    pub(super) fn target_source_unit_projection_variable(
        &self,
        target: SelectorTargetId,
    ) -> Result<ConstraintVariableId, CompiledSelectorProblemError> {
        self.target_projections
            .iter()
            .find_map(|projection| {
                (projection.target == target).then_some(projection.source_unit_variable)
            })
            .ok_or(CompiledSelectorProblemError::UnknownTargetProjection { target })
    }

    pub(super) fn require_domain(
        &self,
        variable: ConstraintVariableId,
        expected: VariableDomain,
    ) -> Result<(), CompiledSelectorProblemError> {
        let actual = self.require_variable(variable)?.domain;
        if actual != expected {
            return Err(CompiledSelectorProblemError::VariableDomainMismatch {
                variable,
                expected,
                actual,
            });
        }
        Ok(())
    }

    pub(super) fn require_variable(
        &self,
        variable: ConstraintVariableId,
    ) -> Result<&CompiledVariableBuilder, CompiledSelectorProblemError> {
        self.variables
            .get(variable.0)
            .ok_or(CompiledSelectorProblemError::UnknownVariable { variable })
    }

    pub(super) fn require_variable_mut(
        &mut self,
        variable: ConstraintVariableId,
    ) -> Result<&mut CompiledVariableBuilder, CompiledSelectorProblemError> {
        self.variables
            .get_mut(variable.0)
            .ok_or(CompiledSelectorProblemError::UnknownVariable { variable })
    }
}
