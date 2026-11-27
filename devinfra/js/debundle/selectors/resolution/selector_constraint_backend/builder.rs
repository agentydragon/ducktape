//! Incremental construction and presolve of a [`CompiledSelectorProblem`].

use std::collections::HashMap;
use std::hash::{Hash, Hasher};

use selector_ir::{SelectorProjectedValue, SelectorTargetId, VariableDomain};
use source_identity::SourceUnitId;

use crate::rows::AllowedTupleRowsFingerprint;
use crate::*;

mod presolve;
mod validate;

const SHARED_SPARSE_DOMAIN_THRESHOLD: usize = 1024;

#[derive(Debug, Default)]
pub struct CompiledSelectorProblemBuilder {
    value_ids: DomainValueIds,
    value_dictionary: DomainValueDictionary,
    full_domains: FullDomainValues,
    variables: Vec<CompiledVariableBuilder>,
    target_projections: Vec<TargetProjection>,
    shared_variable_domains: Vec<CompiledSharedVariableDomain>,
    shared_variable_domains_by_fingerprint:
        HashMap<SharedVariableDomainFingerprint, Vec<SharedVariableDomainId>>,
    shared_variable_domain_intersections:
        HashMap<SharedVariableDomainIntersectionKey, CompiledVariableDomain>,
    allowed_tuple_row_sets: Vec<CompiledAllowedTupleRowSet>,
    allowed_tuple_row_sets_by_fingerprint:
        HashMap<AllowedTupleRowsFingerprint, Vec<AllowedTupleRowsId>>,
    allowed_tuples: Vec<CompiledAllowedTupleConstraint>,
    all_different: Vec<CompiledAllDifferentConstraint>,
    known_unsat: bool,
}

#[derive(Debug, Default)]
struct DomainValueIds {
    source_units: HashMap<SourceUnitId, BackendValueId>,
    strings: HashMap<String, BackendValueId>,
}

#[derive(Debug, Clone)]
struct CompiledVariableBuilder {
    id: ConstraintVariableId,
    domain: VariableDomain,
    debug_name: Option<String>,
    values: CompiledVariableDomain,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct SharedVariableDomainFingerprint {
    domain: VariableDomain,
    len: usize,
    hash: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct SharedVariableDomainIntersectionKey {
    left: SharedVariableDomainId,
    right: SharedVariableDomainId,
}

impl SharedVariableDomainIntersectionKey {
    fn new(left: SharedVariableDomainId, right: SharedVariableDomainId) -> Self {
        if left <= right {
            Self { left, right }
        } else {
            Self {
                left: right,
                right: left,
            }
        }
    }
}

fn intersect_sorted_encoded_values(
    left: &[BackendValueId],
    right: &[BackendValueId],
) -> Vec<BackendValueId> {
    let mut restricted = Vec::new();
    let mut left_index = 0;
    let mut right_index = 0;
    while left_index < left.len() && right_index < right.len() {
        match left[left_index].cmp(&right[right_index]) {
            std::cmp::Ordering::Less => left_index += 1,
            std::cmp::Ordering::Greater => right_index += 1,
            std::cmp::Ordering::Equal => {
                restricted.push(left[left_index]);
                left_index += 1;
                right_index += 1;
            }
        }
    }
    restricted
}

fn sparse_variable_domain_fingerprint(
    domain: VariableDomain,
    values: &[BackendValueId],
) -> SharedVariableDomainFingerprint {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    domain.hash(&mut hasher);
    values.hash(&mut hasher);
    SharedVariableDomainFingerprint {
        domain,
        len: values.len(),
        hash: hasher.finish(),
    }
}

impl CompiledSelectorProblemBuilder {
    /// The identity of the next allowed-tuple constraint: its position in the
    /// list, which is how callers name it to the solver.
    fn next_allowed_tuple_id(&self) -> AllowedTupleConstraintId {
        AllowedTupleConstraintId(self.allowed_tuples.len())
    }

    /// Record one allowed-tuple constraint over already-interned rows.
    fn record_allowed_tuples(
        &mut self,
        id: AllowedTupleConstraintId,
        variables: Vec<ConstraintVariableId>,
        row_set: AllowedTupleRowsId,
    ) {
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
    }

    pub fn known_unsat(&self) -> bool {
        self.known_unsat
    }

    /// Installs a narrowed domain for `variable`; an empty one proves the
    /// program unsatisfiable.
    fn set_variable_domain(
        &mut self,
        variable: ConstraintVariableId,
        values: CompiledVariableDomain,
    ) -> Result<(), CompiledSelectorProblemError> {
        let empty = self.compiled_variable_domain_is_empty(&values);
        self.require_variable_mut(variable)?.values = values;
        self.known_unsat |= empty;
        Ok(())
    }

    pub fn add_full_domain_values(
        &mut self,
        domain: VariableDomain,
        values: impl IntoIterator<Item = SelectorProjectedValue>,
    ) -> Result<(), CompiledSelectorProblemError> {
        let mut ids = Vec::new();
        for value in values {
            let actual = value.domain();
            if actual != domain {
                return Err(CompiledSelectorProblemError::DomainValueMismatch {
                    expected: domain,
                    actual,
                });
            }
            ids.push(self.intern_value(value)?);
        }
        ids.sort_unstable();
        ids.dedup();
        *self.full_domains.get_mut(domain) = ids;
        Ok(())
    }

    pub fn add_variable(
        &mut self,
        domain: VariableDomain,
        debug_name: Option<String>,
    ) -> ConstraintVariableId {
        let id = ConstraintVariableId(self.variables.len());
        self.variables.push(CompiledVariableBuilder {
            id,
            domain,
            debug_name,
            values: CompiledVariableDomain::Full(domain),
        });
        id
    }

    pub fn add_target_projection(
        &mut self,
        target: SelectorTargetId,
        source_unit_variable: ConstraintVariableId,
        binding_projection: Option<TargetBindingProjection>,
    ) -> Result<(), CompiledSelectorProblemError> {
        self.require_domain(source_unit_variable, VariableDomain::SourceUnit)?;
        if let Some(binding_variable) = binding_projection
            .as_ref()
            .and_then(TargetBindingProjection::variable)
        {
            self.require_domain(binding_variable, VariableDomain::String)?;
        }
        if self
            .target_projections
            .iter()
            .any(|projection| projection.target == target)
        {
            return Err(CompiledSelectorProblemError::DuplicateTargetProjection { target });
        }
        self.target_projections.push(TargetProjection {
            target,
            source_unit_variable,
            binding_projection,
        });
        Ok(())
    }

    pub fn add_allowed_tuples(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<SelectorProjectedValue>>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let domains = self.validate_allowed_tuple_variables(&variables)?;
        let id = self.next_allowed_tuple_id();
        let mut compiled_tuples = Vec::with_capacity(tuples.len());
        for (tuple_index, tuple) in tuples.into_iter().enumerate() {
            if tuple.len() != variables.len() {
                return Err(CompiledSelectorProblemError::TupleArityMismatch {
                    id,
                    tuple_index,
                    expected: variables.len(),
                    actual: tuple.len(),
                });
            }
            let mut compiled = Vec::with_capacity(tuple.len());
            for (column, (value, expected)) in tuple.into_iter().zip(domains.iter()).enumerate() {
                let actual = value.domain();
                if actual != *expected {
                    return Err(CompiledSelectorProblemError::TupleDomainMismatch {
                        id,
                        tuple_index,
                        variable: variables[column],
                        expected: *expected,
                        actual,
                    });
                }
                compiled.push(self.intern_value(value)?);
            }
            let mut row_matches_variable_domains = true;
            for ((variable, domain), value_id) in variables.iter().zip(&domains).zip(&compiled) {
                if !self.encoded_value_matches_variable_domain(*variable, *value_id)? {
                    row_matches_variable_domains = false;
                    break;
                }
                self.ensure_full_domain_contains(*domain, *value_id);
            }
            if row_matches_variable_domains {
                compiled_tuples.push(compiled);
            }
        }
        compiled_tuples.sort();
        compiled_tuples.dedup();

        self.known_unsat |= compiled_tuples.is_empty();

        let row_set = self.intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_rows(
            variables.len(),
            compiled_tuples,
        ));
        self.record_allowed_tuples(id, variables, row_set);
        Ok(id)
    }

    pub fn intern_encoded_allowed_binary_row_set(
        &mut self,
        variables: [ConstraintVariableId; 2],
        domains: [VariableDomain; 2],
        tuples: Vec<(BackendValueId, BackendValueId)>,
    ) -> Result<AllowedTupleRowsId, CompiledSelectorProblemError> {
        let id = self.next_allowed_tuple_id();
        let mut compiled_tuples = Vec::with_capacity(tuples.len());
        for (tuple_index, (left, right)) in tuples.into_iter().enumerate() {
            self.validate_encoded_value_domain(id, tuple_index, variables[0], domains[0], left)?;
            self.ensure_full_domain_contains(domains[0], left);
            self.validate_encoded_value_domain(id, tuple_index, variables[1], domains[1], right)?;
            self.ensure_full_domain_contains(domains[1], right);
            compiled_tuples.push((left, right));
        }
        compiled_tuples.sort_unstable();
        compiled_tuples.dedup();
        Ok(self
            .intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_binary_rows(compiled_tuples)))
    }

    pub fn add_encoded_allowed_row_set(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        row_set: AllowedTupleRowsId,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let id = self.next_allowed_tuple_id();
        self.validate_allowed_tuple_variables(&variables)?;
        let Some(rows) = self
            .allowed_tuple_row_sets
            .get(row_set.0)
            .map(|row_set| &row_set.rows)
        else {
            return Err(CompiledSelectorProblemError::UnknownAllowedTupleRowSet { row_set });
        };
        if rows.arity() != variables.len() {
            return Err(CompiledSelectorProblemError::TupleArityMismatch {
                id,
                tuple_index: 0,
                expected: variables.len(),
                actual: rows.arity(),
            });
        }
        self.known_unsat |= rows.is_empty();
        self.record_allowed_tuples(id, variables, row_set);
        Ok(id)
    }

    pub fn restrict_variable_to_encoded_values(
        &mut self,
        variable: ConstraintVariableId,
        values: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<(), CompiledSelectorProblemError> {
        let domain = self.require_variable(variable)?.domain;
        let mut values = values.into_iter().collect::<Vec<_>>();
        values.sort_unstable();
        values.dedup();
        for value in &values {
            self.validate_encoded_variable_domain_value(variable, domain, *value)?;
        }

        let full_domain = self.full_domains.get(domain);
        values.retain(|value| full_domain.binary_search(value).is_ok());
        let full_domain_len = full_domain.len();
        if matches!(
            self.require_variable(variable)?.values,
            CompiledVariableDomain::Full(_)
        ) && values.len() == full_domain_len
        {
            return Ok(());
        }
        let restricted = match self.require_variable(variable)?.values.clone() {
            CompiledVariableDomain::Full(_) => values,
            CompiledVariableDomain::Sparse(existing) => {
                intersect_sorted_encoded_values(existing.as_slice(), values.as_slice())
            }
            CompiledVariableDomain::SharedSparse(existing_id) => {
                let existing = &self.shared_variable_domains[existing_id.0].values;
                intersect_sorted_encoded_values(existing, values.as_slice())
            }
        };
        let replacement = self.compiled_sparse_variable_domain(domain, restricted);
        self.set_variable_domain(variable, replacement)
    }

    /// Removes values from a variable's current finite domain. Unlike an
    /// allowed-values table, this stays proportional to the excluded set and
    /// the variable's current sparse domain.
    pub fn exclude_variable_encoded_values(
        &mut self,
        variable: ConstraintVariableId,
        excluded: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<(), CompiledSelectorProblemError> {
        let domain = self.require_variable(variable)?.domain;
        let mut excluded = excluded.into_iter().collect::<Vec<_>>();
        excluded.sort_unstable();
        excluded.dedup();
        for value in &excluded {
            self.validate_encoded_variable_domain_value(variable, domain, *value)?;
        }

        let remaining = self
            .variable_domain_values(variable)?
            .into_iter()
            .filter(|value| excluded.binary_search(value).is_err())
            .collect::<Vec<_>>();
        let replacement = self.compiled_sparse_variable_domain(domain, remaining);
        self.set_variable_domain(variable, replacement)
    }

    pub fn intern_source_unit(
        &mut self,
        value: SourceUnitId,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        if let Some(id) = self.value_ids.source_units.get(&value) {
            return Ok(*id);
        }
        let count = self.value_dictionary.source_units.len();
        let id = backend_value_id(count)?;
        self.value_ids.source_units.insert(value, id);
        self.value_dictionary.source_units.push(value);
        Ok(id)
    }

    pub fn intern_string(
        &mut self,
        value: &str,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        if let Some(id) = self.value_ids.strings.get(value) {
            return Ok(*id);
        }
        let count = self.value_dictionary.strings.len();
        let id = backend_value_id(count)?;
        let value = value.to_string();
        self.value_ids.strings.insert(value.clone(), id);
        self.value_dictionary.strings.push(value);
        Ok(id)
    }

    pub fn variable_domain_values(
        &self,
        variable: ConstraintVariableId,
    ) -> Result<Vec<BackendValueId>, CompiledSelectorProblemError> {
        Ok(domain_values(
            &self.full_domains,
            &self.shared_variable_domains,
            &self.require_variable(variable)?.values,
        )
        .to_vec())
    }

    pub fn require_target_all_different(
        &mut self,
        targets: Vec<SelectorTargetId>,
    ) -> Result<Option<AllDifferentConstraintId>, CompiledSelectorProblemError> {
        let variables = targets
            .iter()
            .map(|target| self.target_source_unit_projection_variable(*target))
            .collect::<Result<Vec<_>, _>>()?;
        let id = AllDifferentConstraintId(self.all_different.len());
        self.validate_all_different_constraint(id, &variables)?;
        let variables = self.simplify_all_different_variables(variables)?;
        if variables.len() < 2 {
            return Ok(None);
        }
        self.all_different
            .push(CompiledAllDifferentConstraint { id, variables });
        Ok(Some(id))
    }

    pub fn finish(self) -> Result<CompiledSelectorProblem, CompiledSelectorProblemError> {
        let variables: Vec<CompiledVariable> = self
            .variables
            .iter()
            .map(|variable| CompiledVariable {
                id: variable.id,
                domain: variable.domain,
                debug_name: variable.debug_name.clone(),
                values: variable.values.clone(),
            })
            .collect();

        Ok(CompiledSelectorProblem {
            value_dictionary: self.value_dictionary,
            full_domains: self.full_domains,
            shared_variable_domains: self.shared_variable_domains,
            variables,
            target_projections: self.target_projections,
            allowed_tuple_row_sets: self.allowed_tuple_row_sets,
            allowed_tuples: self.allowed_tuples,
            all_different: self.all_different,
            known_unsat: self.known_unsat,
        })
    }

    fn intern_value(
        &mut self,
        value: SelectorProjectedValue,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        match value {
            SelectorProjectedValue::SourceUnit(value) => self.intern_source_unit(value),
            SelectorProjectedValue::String(value) => self.intern_string(&value),
        }
    }

    fn intern_allowed_tuple_rows(&mut self, rows: CompiledAllowedTupleRows) -> AllowedTupleRowsId {
        let fingerprint = rows.fingerprint();
        if let Some(ids) = self.allowed_tuple_row_sets_by_fingerprint.get(&fingerprint) {
            for id in ids {
                if self.allowed_tuple_row_sets[id.0].rows == rows {
                    return *id;
                }
            }
        }

        let id = AllowedTupleRowsId(self.allowed_tuple_row_sets.len());
        self.allowed_tuple_row_sets
            .push(CompiledAllowedTupleRowSet { id, rows });
        self.allowed_tuple_row_sets_by_fingerprint
            .entry(fingerprint)
            .or_default()
            .push(id);
        id
    }

    pub fn intern_shared_sparse_variable_domain(
        &mut self,
        domain: VariableDomain,
        values: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<SharedVariableDomainId, CompiledSelectorProblemError> {
        let mut values = values.into_iter().collect::<Vec<_>>();
        values.sort_unstable();
        values.dedup();
        for value in values.iter().copied() {
            self.validate_encoded_domain_value(domain, value)?;
        }
        let full_domain = self.full_domains.get(domain);
        values.retain(|value| full_domain.binary_search(value).is_ok());
        Ok(self.intern_normalized_shared_sparse_variable_domain(domain, values))
    }

    pub fn restrict_variable_to_shared_sparse_domain(
        &mut self,
        variable: ConstraintVariableId,
        domain_id: SharedVariableDomainId,
    ) -> Result<(), CompiledSelectorProblemError> {
        let variable_domain = self.require_variable(variable)?.domain;
        let Some(shared_domain) = self.shared_variable_domains.get(domain_id.0) else {
            return Err(CompiledSelectorProblemError::UnknownSharedVariableDomain { domain_id });
        };
        let shared_domain_kind = shared_domain.domain;
        if shared_domain_kind != variable_domain {
            return Err(CompiledSelectorProblemError::VariableDomainMismatch {
                variable,
                expected: variable_domain,
                actual: shared_domain_kind,
            });
        }

        let restricted = match self.require_variable(variable)?.values.clone() {
            CompiledVariableDomain::Full(_) => {
                return self.set_variable_domain(
                    variable,
                    CompiledVariableDomain::SharedSparse(domain_id),
                );
            }
            CompiledVariableDomain::Sparse(existing) => {
                let shared_domain_values = &self.shared_variable_domains[domain_id.0].values;
                intersect_sorted_encoded_values(
                    existing.as_slice(),
                    shared_domain_values.as_slice(),
                )
            }
            CompiledVariableDomain::SharedSparse(existing_id) if existing_id == domain_id => {
                return Ok(());
            }
            CompiledVariableDomain::SharedSparse(existing_id) => {
                let replacement = self.intersect_shared_sparse_variable_domains(
                    variable_domain,
                    existing_id,
                    domain_id,
                );
                return self.set_variable_domain(variable, replacement);
            }
        };
        let replacement = self.compiled_sparse_variable_domain(variable_domain, restricted);
        self.set_variable_domain(variable, replacement)
    }

    fn compiled_sparse_variable_domain(
        &mut self,
        domain: VariableDomain,
        values: Vec<BackendValueId>,
    ) -> CompiledVariableDomain {
        if values.len() > SHARED_SPARSE_DOMAIN_THRESHOLD {
            let id = self.intern_normalized_shared_sparse_variable_domain(domain, values);
            CompiledVariableDomain::SharedSparse(id)
        } else {
            CompiledVariableDomain::Sparse(values)
        }
    }

    fn intersect_shared_sparse_variable_domains(
        &mut self,
        domain: VariableDomain,
        left_id: SharedVariableDomainId,
        right_id: SharedVariableDomainId,
    ) -> CompiledVariableDomain {
        if left_id == right_id {
            return CompiledVariableDomain::SharedSparse(left_id);
        }
        let key = SharedVariableDomainIntersectionKey::new(left_id, right_id);
        if let Some(domain) = self.shared_variable_domain_intersections.get(&key) {
            return domain.clone();
        }

        let left = &self.shared_variable_domains[left_id.0].values;
        let right = &self.shared_variable_domains[right_id.0].values;
        let values = intersect_sorted_encoded_values(left, right);
        let intersection = self.compiled_sparse_variable_domain(domain, values);
        self.shared_variable_domain_intersections
            .insert(key, intersection.clone());
        intersection
    }

    fn compiled_variable_domain_is_empty(&self, values: &CompiledVariableDomain) -> bool {
        domain_values(&self.full_domains, &self.shared_variable_domains, values).is_empty()
    }

    fn intern_normalized_shared_sparse_variable_domain(
        &mut self,
        domain: VariableDomain,
        values: Vec<BackendValueId>,
    ) -> SharedVariableDomainId {
        let fingerprint = sparse_variable_domain_fingerprint(domain, values.as_slice());
        if let Some(ids) = self
            .shared_variable_domains_by_fingerprint
            .get(&fingerprint)
        {
            for id in ids {
                let existing = &self.shared_variable_domains[id.0];
                if existing.domain == domain && existing.values == values {
                    return *id;
                }
            }
        }

        let id = SharedVariableDomainId(self.shared_variable_domains.len());
        self.shared_variable_domains
            .push(CompiledSharedVariableDomain { id, domain, values });
        self.shared_variable_domains_by_fingerprint
            .entry(fingerprint)
            .or_default()
            .push(id);
        id
    }
}
