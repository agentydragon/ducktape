//! Compact compiled selector problem consumed by exact-assignment backends.
//!
//! This is the production boundary between selector/fact lowering and a CP/SAT
//! backend. Values are interned once, variables hold compact full-domain handles
//! or sparse candidate sets, and allowed tuples are stored as interned ids.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::error::Error;
use std::fmt;
use std::hash::{Hash, Hasher};

use analysis::OwnerId;
use selector_ir::{SelectorTargetId, VariableDomain};

const SHARED_SPARSE_DOMAIN_THRESHOLD: usize = 1024;

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

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ConstraintValue {
    Owner(OwnerId),
    String(String),
}

impl ConstraintValue {
    pub fn domain(&self) -> VariableDomain {
        match self {
            Self::Owner(_) => VariableDomain::Owner,
            Self::String(_) => VariableDomain::String,
        }
    }
}

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
    pub owner_variable: ConstraintVariableId,
    pub binding_projection: Option<TargetBindingProjection>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetBindingProjection {
    Variable(ConstraintVariableId),
    Const(String),
}

impl TargetBindingProjection {
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
pub struct CompiledAllowedTupleRows {
    arity: usize,
    values: Vec<BackendValueId>,
}

impl CompiledAllowedTupleRows {
    fn from_rows(arity: usize, rows: Vec<Vec<BackendValueId>>) -> Self {
        debug_assert!(arity > 0);
        let mut values = Vec::with_capacity(rows.len().saturating_mul(arity));
        for row in rows {
            debug_assert_eq!(row.len(), arity);
            values.extend(row);
        }
        Self { arity, values }
    }

    pub fn from_flat_rows(arity: usize, mut values: Vec<BackendValueId>) -> Self {
        debug_assert!(arity > 0);
        sort_dedup_flat_rows(arity, &mut values);
        Self { arity, values }
    }

    fn from_binary_rows(rows: Vec<(BackendValueId, BackendValueId)>) -> Self {
        let mut values = Vec::with_capacity(rows.len().saturating_mul(2));
        for (left, right) in rows {
            values.push(left);
            values.push(right);
        }
        Self { arity: 2, values }
    }

    pub fn len(&self) -> usize {
        debug_assert!(self.arity > 0);
        self.values.len() / self.arity
    }

    pub fn arity(&self) -> usize {
        self.arity
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn cell_count(&self) -> usize {
        self.values.len()
    }

    pub fn values(&self) -> &[BackendValueId] {
        &self.values
    }

    pub fn iter(&self) -> impl Iterator<Item = &[BackendValueId]> {
        debug_assert!(self.arity > 0);
        self.values.chunks_exact(self.arity)
    }

    pub fn contains(&self, row: &[BackendValueId]) -> bool {
        self.iter().any(|candidate| candidate == row)
    }

    fn fingerprint(&self) -> AllowedTupleRowsFingerprint {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.arity.hash(&mut hasher);
        self.values.hash(&mut hasher);
        AllowedTupleRowsFingerprint {
            arity: self.arity,
            cell_count: self.values.len(),
            hash: hasher.finish(),
        }
    }
}

fn sort_dedup_flat_rows(arity: usize, values: &mut Vec<BackendValueId>) {
    debug_assert!(arity > 0);
    if values.is_empty() {
        return;
    }
    debug_assert_eq!(values.len() % arity, 0);
    let row_count = values.len() / arity;
    let mut row_indices = (0..row_count).collect::<Vec<_>>();
    row_indices.sort_unstable_by(|left, right| {
        let left_start = left * arity;
        let right_start = right * arity;
        values[left_start..left_start + arity].cmp(&values[right_start..right_start + arity])
    });

    let mut deduped = Vec::with_capacity(values.len());
    for row_index in row_indices {
        let row_start = row_index * arity;
        let row = &values[row_start..row_start + arity];
        if deduped.len() >= arity && &deduped[deduped.len() - arity..] == row {
            continue;
        }
        deduped.extend_from_slice(row);
    }
    *values = deduped;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct AllowedTupleRowsFingerprint {
    arity: usize,
    cell_count: usize,
    hash: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledAllDifferentConstraint {
    pub id: AllDifferentConstraintId,
    pub variables: Vec<ConstraintVariableId>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FullDomainValues {
    pub owners: Vec<BackendValueId>,
    pub strings: Vec<BackendValueId>,
}

impl FullDomainValues {
    pub fn get(&self, domain: VariableDomain) -> &[BackendValueId] {
        match domain {
            VariableDomain::Owner => &self.owners,
            VariableDomain::String => &self.strings,
        }
    }

    fn get_mut(&mut self, domain: VariableDomain) -> &mut Vec<BackendValueId> {
        match domain {
            VariableDomain::Owner => &mut self.owners,
            VariableDomain::String => &mut self.strings,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DomainValueDictionary {
    pub owners: Vec<OwnerId>,
    pub strings: Vec<String>,
}

impl DomainValueDictionary {
    fn domain_len(&self, domain: VariableDomain) -> usize {
        match domain {
            VariableDomain::Owner => self.owners.len(),
            VariableDomain::String => self.strings.len(),
        }
    }

    pub fn encode(&self, value: &ConstraintValue) -> Option<BackendValueId> {
        let index = match value {
            ConstraintValue::Owner(value) => {
                self.owners.iter().position(|candidate| candidate == value)
            }
            ConstraintValue::String(value) => {
                self.strings.iter().position(|candidate| candidate == value)
            }
        }?;
        Some(BackendValueId(index.try_into().ok()?))
    }

    fn decode(&self, domain: VariableDomain, value: BackendValueId) -> Option<ConstraintValue> {
        let index = backend_value_index(value).ok()?;
        match domain {
            VariableDomain::Owner => self.owners.get(index).copied().map(ConstraintValue::Owner),
            VariableDomain::String => self
                .strings
                .get(index)
                .cloned()
                .map(ConstraintValue::String),
        }
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
    pub fn variable_domain_values(&self, variable: &CompiledVariable) -> Vec<BackendValueId> {
        match &variable.values {
            CompiledVariableDomain::Full(domain) => self.full_domains.get(*domain).to_vec(),
            CompiledVariableDomain::Sparse(values) => values.clone(),
            CompiledVariableDomain::SharedSparse(id) => {
                self.shared_variable_domains[id.0].values.clone()
            }
        }
    }

    pub fn variable_domain_value_count(&self, variable: &CompiledVariable) -> usize {
        match &variable.values {
            CompiledVariableDomain::Full(domain) => self.full_domains.get(*domain).len(),
            CompiledVariableDomain::Sparse(values) => values.len(),
            CompiledVariableDomain::SharedSparse(id) => {
                self.shared_variable_domains[id.0].values.len()
            }
        }
    }

    pub fn shared_variable_domain(
        &self,
        id: SharedVariableDomainId,
    ) -> Option<&CompiledSharedVariableDomain> {
        self.shared_variable_domains.get(id.0)
    }

    pub fn decode_assignment(
        &self,
        assignment: &BackendAssignment,
    ) -> Result<BTreeMap<ConstraintVariableId, ConstraintValue>, BackendAssignmentError> {
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
    ) -> Option<ConstraintValue> {
        self.value_dictionary.decode(domain, value)
    }
}

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
    owners: HashMap<OwnerId, BackendValueId>,
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

impl CompiledSelectorProblemBuilder {
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
        values: impl IntoIterator<Item = ConstraintValue>,
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
        owner_variable: ConstraintVariableId,
        binding_projection: Option<TargetBindingProjection>,
    ) -> Result<(), CompiledSelectorProblemError> {
        self.require_domain(owner_variable, VariableDomain::Owner)?;
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
            owner_variable,
            binding_projection,
        });
        Ok(())
    }

    pub fn add_allowed_tuples(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<ConstraintValue>>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let domains = self.validate_allowed_tuple_variables(&variables)?;
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
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
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
        Ok(id)
    }

    pub fn intern_encoded_allowed_binary_row_set(
        &mut self,
        variables: [ConstraintVariableId; 2],
        domains: [VariableDomain; 2],
        tuples: Vec<(BackendValueId, BackendValueId)>,
    ) -> Result<AllowedTupleRowsId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
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
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
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
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
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

    pub fn intern_owner(
        &mut self,
        value: OwnerId,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        if let Some(id) = self.value_ids.owners.get(&value) {
            return Ok(*id);
        }
        let count = self.value_dictionary.owners.len();
        let id = backend_value_id(count)?;
        self.value_ids.owners.insert(value, id);
        self.value_dictionary.owners.push(value);
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
        match &self.require_variable(variable)?.values {
            CompiledVariableDomain::Full(domain) => Ok(self.full_domains.get(*domain).to_vec()),
            CompiledVariableDomain::Sparse(values) => Ok(values.clone()),
            CompiledVariableDomain::SharedSparse(id) => {
                Ok(self.shared_variable_domains[id.0].values.clone())
            }
        }
    }

    pub fn simplify_allowed_tuples_against_current_domains(
        &mut self,
    ) -> Result<(), CompiledSelectorProblemError> {
        while self.simplify_allowed_tuple_constraints_once()? && !self.known_unsat {}
        Ok(())
    }

    pub fn require_target_all_different(
        &mut self,
        targets: Vec<SelectorTargetId>,
    ) -> Result<Option<AllDifferentConstraintId>, CompiledSelectorProblemError> {
        let variables = targets
            .iter()
            .map(|target| self.target_owner_projection_variable(*target))
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
        value: ConstraintValue,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        match value {
            ConstraintValue::Owner(value) => self.intern_owner(value),
            ConstraintValue::String(value) => self.intern_string(&value),
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

    fn simplify_allowed_tuple_constraints_once(
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
            let id = AllowedTupleConstraintId(self.allowed_tuples.len());
            self.allowed_tuples.push(CompiledAllowedTupleConstraint {
                id,
                variables,
                row_set,
            });
        }
        Ok(changed)
    }

    fn simplified_allowed_tuple_constraint(
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

    fn can_restrict_variable_to_encoded_values(
        &self,
        variable: ConstraintVariableId,
        values: &[BackendValueId],
    ) -> Result<bool, CompiledSelectorProblemError> {
        let domain = self.require_variable(variable)?.domain;
        for value in values {
            if value.0 < 0 {
                return Ok(false);
            }
            let Ok(index) = usize::try_from(value.0) else {
                return Ok(false);
            };
            if index >= self.value_dictionary.domain_len(domain) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn restrict_variable_to_encoded_values_changed(
        &mut self,
        variable: ConstraintVariableId,
        values: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<bool, CompiledSelectorProblemError> {
        let before = self.variable_domain_values(variable)?;
        self.restrict_variable_to_encoded_values(variable, values)?;
        Ok(self.variable_domain_values(variable)? != before)
    }

    fn compiled_variable_domain_is_empty(&self, values: &CompiledVariableDomain) -> bool {
        match values {
            CompiledVariableDomain::Full(domain) => self.full_domains.get(*domain).is_empty(),
            CompiledVariableDomain::Sparse(values) => values.is_empty(),
            CompiledVariableDomain::SharedSparse(id) => {
                self.shared_variable_domains[id.0].values.is_empty()
            }
        }
    }

    fn simplify_all_different_variables(
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

    fn validate_allowed_tuple_variables(
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

    fn validate_encoded_value_domain(
        &self,
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if value.0 < 0 {
            return Err(CompiledSelectorProblemError::EncodedTupleValueOutOfDomain {
                id,
                tuple_index,
                variable,
                domain,
                value,
            });
        }
        let Ok(index) = usize::try_from(value.0) else {
            return Err(CompiledSelectorProblemError::EncodedTupleValueOutOfDomain {
                id,
                tuple_index,
                variable,
                domain,
                value,
            });
        };
        if index >= self.value_dictionary.domain_len(domain) {
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

    fn validate_encoded_domain_value(
        &self,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if value.0 < 0 {
            return Err(
                CompiledSelectorProblemError::EncodedSharedDomainValueOutOfDomain { domain, value },
            );
        }
        let Ok(index) = usize::try_from(value.0) else {
            return Err(
                CompiledSelectorProblemError::EncodedSharedDomainValueOutOfDomain { domain, value },
            );
        };
        if index >= self.value_dictionary.domain_len(domain) {
            return Err(
                CompiledSelectorProblemError::EncodedSharedDomainValueOutOfDomain { domain, value },
            );
        }
        Ok(())
    }

    fn encoded_value_matches_variable_domain(
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

    fn validate_encoded_variable_domain_value(
        &self,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    ) -> Result<(), CompiledSelectorProblemError> {
        if value.0 < 0 {
            return Err(
                CompiledSelectorProblemError::EncodedVariableDomainValueOutOfDomain {
                    variable,
                    domain,
                    value,
                },
            );
        }
        let Ok(index) = usize::try_from(value.0) else {
            return Err(
                CompiledSelectorProblemError::EncodedVariableDomainValueOutOfDomain {
                    variable,
                    domain,
                    value,
                },
            );
        };
        if index >= self.value_dictionary.domain_len(domain) {
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

    fn ensure_full_domain_contains(&mut self, domain: VariableDomain, value: BackendValueId) {
        let values = self.full_domains.get_mut(domain);
        let Ok(index) = usize::try_from(value.0) else {
            return;
        };
        if index < values.len() {
            return;
        }
        if index == values.len() {
            values.push(value);
        }
    }

    fn validate_all_different_constraint(
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

    fn target_owner_projection_variable(
        &self,
        target: SelectorTargetId,
    ) -> Result<ConstraintVariableId, CompiledSelectorProblemError> {
        self.target_projections
            .iter()
            .find_map(|projection| {
                (projection.target == target).then_some(projection.owner_variable)
            })
            .ok_or(CompiledSelectorProblemError::UnknownTargetProjection { target })
    }

    fn require_domain(
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

    fn require_variable(
        &self,
        variable: ConstraintVariableId,
    ) -> Result<&CompiledVariableBuilder, CompiledSelectorProblemError> {
        self.variables
            .get(variable.0)
            .ok_or(CompiledSelectorProblemError::UnknownVariable { variable })
    }

    fn require_variable_mut(
        &mut self,
        variable: ConstraintVariableId,
    ) -> Result<&mut CompiledVariableBuilder, CompiledSelectorProblemError> {
        self.variables
            .get_mut(variable.0)
            .ok_or(CompiledSelectorProblemError::UnknownVariable { variable })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompiledSelectorProblemError {
    UnknownVariable {
        variable: ConstraintVariableId,
    },
    UnknownSharedVariableDomain {
        domain_id: SharedVariableDomainId,
    },
    VariableDomainMismatch {
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    DomainValueMismatch {
        expected: VariableDomain,
        actual: VariableDomain,
    },
    DuplicateTargetProjection {
        target: SelectorTargetId,
    },
    UnknownTargetProjection {
        target: SelectorTargetId,
    },
    EmptyAllowedTupleVariables {
        id: AllowedTupleConstraintId,
    },
    DuplicateTupleVariable {
        id: AllowedTupleConstraintId,
        variable: ConstraintVariableId,
    },
    UnknownAllowedTupleRowSet {
        row_set: AllowedTupleRowsId,
    },
    TupleArityMismatch {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        expected: usize,
        actual: usize,
    },
    TupleDomainMismatch {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    EncodedTupleValueOutOfDomain {
        id: AllowedTupleConstraintId,
        tuple_index: usize,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    },
    EncodedVariableDomainValueOutOfDomain {
        variable: ConstraintVariableId,
        domain: VariableDomain,
        value: BackendValueId,
    },
    EncodedSharedDomainValueOutOfDomain {
        domain: VariableDomain,
        value: BackendValueId,
    },
    DegenerateAllDifferent {
        id: AllDifferentConstraintId,
    },
    DuplicateAllDifferentVariable {
        id: AllDifferentConstraintId,
        variable: ConstraintVariableId,
    },
    AllDifferentDomainMismatch {
        id: AllDifferentConstraintId,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    TooManyValues {
        count: usize,
    },
}

impl fmt::Display for CompiledSelectorProblemError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownVariable { variable } => {
                write!(f, "constraint references unknown variable {variable:?}")
            }
            Self::UnknownSharedVariableDomain { domain_id } => {
                write!(
                    f,
                    "constraint references unknown shared domain {domain_id:?}"
                )
            }
            Self::VariableDomainMismatch {
                variable,
                expected,
                actual,
            } => write!(
                f,
                "variable {variable:?} expected {expected:?} domain, found {actual:?}"
            ),
            Self::DomainValueMismatch { expected, actual } => {
                write!(f, "expected {expected:?} value, found {actual:?}")
            }
            Self::DuplicateTargetProjection { target } => {
                write!(f, "target {target:?} has more than one projection")
            }
            Self::UnknownTargetProjection { target } => {
                write!(f, "target {target:?} has no projection")
            }
            Self::EmptyAllowedTupleVariables { id } => {
                write!(f, "allowed tuple constraint {id:?} has no variables")
            }
            Self::DuplicateTupleVariable { id, variable } => write!(
                f,
                "allowed tuple constraint {id:?} references variable {variable:?} more than once"
            ),
            Self::UnknownAllowedTupleRowSet { row_set } => {
                write!(f, "allowed tuple row set {row_set:?} does not exist")
            }
            Self::TupleArityMismatch {
                id,
                tuple_index,
                expected,
                actual,
            } => write!(
                f,
                "allowed tuple constraint {id:?} row {tuple_index} has arity {actual}, expected {expected}"
            ),
            Self::TupleDomainMismatch {
                id,
                tuple_index,
                variable,
                expected,
                actual,
            } => write!(
                f,
                "allowed tuple constraint {id:?} row {tuple_index} variable {variable:?} expected {expected:?}, found {actual:?}"
            ),
            Self::EncodedTupleValueOutOfDomain {
                id,
                tuple_index,
                variable,
                domain,
                value,
            } => write!(
                f,
                "allowed tuple constraint {id:?} row {tuple_index} variable {variable:?} has encoded value {value:?} outside {domain:?} domain"
            ),
            Self::EncodedVariableDomainValueOutOfDomain {
                variable,
                domain,
                value,
            } => write!(
                f,
                "variable {variable:?} restriction contains encoded value {value:?} outside {domain:?} domain"
            ),
            Self::EncodedSharedDomainValueOutOfDomain { domain, value } => write!(
                f,
                "shared {domain:?} domain contains encoded value {value:?} outside its dictionary"
            ),
            Self::DegenerateAllDifferent { id } => {
                write!(
                    f,
                    "all_different constraint {id:?} has fewer than two variables"
                )
            }
            Self::DuplicateAllDifferentVariable { id, variable } => write!(
                f,
                "all_different constraint {id:?} references variable {variable:?} more than once"
            ),
            Self::AllDifferentDomainMismatch {
                id,
                expected,
                actual,
            } => write!(
                f,
                "all_different constraint {id:?} mixes {expected:?} and {actual:?} domains"
            ),
            Self::TooManyValues { count } => {
                write!(f, "compiled selector problem has too many values: {count}")
            }
        }
    }
}

impl Error for CompiledSelectorProblemError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendAssignment {
    pub values: Vec<BackendVariableAssignment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendVariableAssignment {
    pub variable: ConstraintVariableId,
    pub value: BackendValueId,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendSolveStatus {
    Unsatisfiable,
    Satisfiable,
    Ambiguous,
    Unknown,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum BackendAssignmentCoverage {
    #[default]
    Sample,
    /// Every value each projected variable can take appears in some assignment.
    TargetSupportComplete,
    /// As `TargetSupportComplete`, except that some ambiguous variable stopped at
    /// `MAX_ALTERNATIVES_PER_VARIABLE` values without proving it has no more.
    TargetSupportCapped,
}

/// Distinct values a backend lists per ambiguous projected variable before it reports
/// `BackendAssignmentCoverage::TargetSupportCapped`: the listing bound of an
/// ambiguous selector outcome.
pub const MAX_ALTERNATIVES_PER_VARIABLE: u32 = selector_outcome::MAX_LISTED_CANDIDATES as u32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendSolveResult {
    pub status: BackendSolveStatus,
    pub assignment_coverage: BackendAssignmentCoverage,
    pub assignments: Vec<BackendAssignment>,
    pub diagnostic: Option<String>,
    /// Only with [`BackendSolveStatus::Unknown`]: the projected variables
    /// proven to take one value before the backend stopped; each takes the
    /// value it has in every assignment. Every other variable is undecided.
    pub fixed_variables: BTreeSet<ConstraintVariableId>,
}

pub trait SelectorProblemBackend {
    type Error;

    fn solve(&self, problem: &CompiledSelectorProblem) -> Result<BackendSolveResult, Self::Error>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendAssignmentError {
    UnknownVariable {
        variable: ConstraintVariableId,
    },
    UnknownValue {
        value: BackendValueId,
    },
    ValueOutsideDomain {
        variable: ConstraintVariableId,
        value: BackendValueId,
    },
    DuplicateVariable {
        variable: ConstraintVariableId,
    },
    NegativeValue {
        value: BackendValueId,
    },
    ValueIndexOutOfRange {
        value: BackendValueId,
    },
}

impl fmt::Display for BackendAssignmentError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownVariable { variable } => {
                write!(f, "assignment references unknown variable {variable:?}")
            }
            Self::UnknownValue { value } => {
                write!(f, "assignment references unknown value {value:?}")
            }
            Self::ValueOutsideDomain { variable, value } => {
                write!(
                    f,
                    "assignment value {value:?} is outside variable {variable:?} domain"
                )
            }
            Self::DuplicateVariable { variable } => {
                write!(
                    f,
                    "assignment includes variable {variable:?} more than once"
                )
            }
            Self::NegativeValue { value } => {
                write!(f, "assignment value id {value:?} is negative")
            }
            Self::ValueIndexOutOfRange { value } => {
                write!(f, "assignment value id {value:?} does not fit in usize")
            }
        }
    }
}

impl Error for BackendAssignmentError {}

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
