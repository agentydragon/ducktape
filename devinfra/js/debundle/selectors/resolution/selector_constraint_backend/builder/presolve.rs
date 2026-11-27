//! Domain propagation over allowed tuples and all-different constraints.

use std::collections::BTreeSet;

use super::*;

impl CompiledSelectorProblemBuilder {
    pub fn simplify_allowed_tuples_against_current_domains(
        &mut self,
    ) -> Result<(), CompiledSelectorProblemError> {
        while self.simplify_allowed_tuple_constraints_once()? && !self.known_unsat {}
        Ok(())
    }

    pub(super) fn simplify_allowed_tuple_constraints_once(
        &mut self,
    ) -> Result<bool, CompiledSelectorProblemError> {
        let old_constraints = std::mem::take(&mut self.allowed_tuples);
        let old_row_sets = std::mem::take(&mut self.allowed_tuple_row_sets);
        self.allowed_tuple_row_sets_by_fingerprint.clear();

        let mut changed = false;
        for constraint in old_constraints {
            let Some(row_set) = old_row_sets.get(constraint.row_set.0) else {
                return Err(CompiledSelectorProblemError::UnknownAllowedTupleRowSet {
                    row_set: constraint.row_set,
                });
            };
            let Some((variables, rows, constraint_changed)) =
                self.simplified_allowed_tuple_constraint(&constraint.variables, &row_set.rows)?
            else {
                changed = true;
                continue;
            };
            changed |= constraint_changed;
            let row_set = self.intern_allowed_tuple_rows(rows);
            self.record_allowed_tuples(self.next_allowed_tuple_id(), variables, row_set);
        }
        Ok(changed)
    }

    pub(super) fn simplified_allowed_tuple_constraint(
        &mut self,
        variables: &[ConstraintVariableId],
        rows: &CompiledAllowedTupleRows,
    ) -> Result<
        Option<(Vec<ConstraintVariableId>, CompiledAllowedTupleRows, bool)>,
        CompiledSelectorProblemError,
    > {
        let arity = variables.len();
        let mut kept_rows = Vec::new();
        for row in rows.iter() {
            let mut row_matches_domains = true;
            for (variable, value) in variables.iter().copied().zip(row.iter().copied()) {
                if !self.encoded_value_matches_variable_domain(variable, value)? {
                    row_matches_domains = false;
                    break;
                }
            }
            if row_matches_domains {
                kept_rows.extend_from_slice(row);
            }
        }
        if kept_rows.is_empty() {
            self.known_unsat = true;
            return Ok(None);
        }

        let mut changed = kept_rows.len() != rows.cell_count();
        let kept_row_count = kept_rows.len() / arity;
        let mut keep_columns = Vec::new();
        let mut column_can_restrict = vec![false; arity];
        let mut column_values = vec![Vec::new(); arity];
        for row in kept_rows.chunks_exact(arity) {
            for (column, value) in row.iter().copied().enumerate() {
                column_values[column].push(value);
            }
        }
        for (column, values) in column_values.iter_mut().enumerate() {
            values.sort_unstable();
            values.dedup();
            let can_restrict =
                self.can_restrict_variable_to_encoded_values(variables[column], values)?;
            column_can_restrict[column] = can_restrict;
            if can_restrict {
                changed |= self.restrict_variable_to_encoded_values_changed(
                    variables[column],
                    values.iter().copied(),
                )?;
            }
            if can_restrict && values.len() == 1 {
                changed = true;
            } else {
                keep_columns.push(column);
            }
        }

        if keep_columns.is_empty() {
            return Ok(None);
        }
        if keep_columns.len() == 1 {
            let column = keep_columns[0];
            if column_can_restrict[column] {
                self.restrict_variable_to_encoded_values_changed(
                    variables[column],
                    column_values[column].iter().copied(),
                )?;
                return Ok(None);
            }
        }

        let reduced_variables = keep_columns
            .iter()
            .map(|column| variables[*column])
            .collect::<Vec<_>>();
        let mut reduced_values =
            Vec::with_capacity(kept_row_count.saturating_mul(reduced_variables.len()));
        for row in kept_rows.chunks_exact(arity) {
            for column in &keep_columns {
                reduced_values.push(row[*column]);
            }
        }
        let reduced_rows =
            CompiledAllowedTupleRows::from_flat_rows(reduced_variables.len(), reduced_values);
        changed |= reduced_variables.len() != variables.len()
            || reduced_rows.arity() != rows.arity()
            || reduced_rows.values() != rows.values();
        Ok(Some((reduced_variables, reduced_rows, changed)))
    }

    pub(super) fn can_restrict_variable_to_encoded_values(
        &self,
        variable: ConstraintVariableId,
        values: &[BackendValueId],
    ) -> Result<bool, CompiledSelectorProblemError> {
        let domain = self.require_variable(variable)?.domain;
        Ok(values
            .iter()
            .all(|value| self.value_dictionary.contains_id(domain, *value)))
    }

    pub(super) fn restrict_variable_to_encoded_values_changed(
        &mut self,
        variable: ConstraintVariableId,
        values: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<bool, CompiledSelectorProblemError> {
        let before = self.variable_domain_values(variable)?;
        self.restrict_variable_to_encoded_values(variable, values)?;
        Ok(self.variable_domain_values(variable)? != before)
    }

    pub(super) fn simplify_all_different_variables(
        &mut self,
        mut variables: Vec<ConstraintVariableId>,
    ) -> Result<Vec<ConstraintVariableId>, CompiledSelectorProblemError> {
        let mut fixed_values = BTreeSet::new();
        loop {
            let mut next_variables = Vec::new();
            let mut learned_fixed_value = false;
            for variable in variables {
                let mut values = self.variable_domain_values(variable)?;
                if !fixed_values.is_empty() {
                    values.retain(|value| !fixed_values.contains(value));
                    self.restrict_variable_to_encoded_values(variable, values.iter().copied())?;
                }
                match values.as_slice() {
                    [] => self.known_unsat = true,
                    [value] => {
                        self.known_unsat |= !fixed_values.insert(*value);
                        learned_fixed_value = true;
                    }
                    _ => next_variables.push(variable),
                }
            }
            variables = next_variables;
            if !learned_fixed_value || self.known_unsat {
                break;
            }
        }
        Ok(variables)
    }
}
