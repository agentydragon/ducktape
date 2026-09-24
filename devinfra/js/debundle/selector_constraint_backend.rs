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
use selector_ir::{SelectorTargetId, SelectorVariableId, VariableDomain};
use serde::{Deserialize, Serialize};

const SHARED_SPARSE_DOMAIN_THRESHOLD: usize = 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ConstraintVariableId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AllowedTupleConstraintId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AllowedTupleRowsId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SharedVariableDomainId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AllDifferentConstraintId(pub usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BackendValueId(pub i64);

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
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

/// How far compile-time presolve may carry one target's constraints into the
/// domains other constraints see.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PresolveScope {
    /// Propagate freely. A contradiction then surfaces wherever propagation
    /// meets it, as an empty domain or table that no longer says which targets
    /// caused it.
    AcrossTargets,
    /// A constraint prunes a variable's domain only when every target the
    /// constraint is attributed to owns that variable, and `all_different`
    /// propagates nothing. Every constraint keeps its attribution, so the
    /// sidecar can localize an infeasible program to conflicting targets; an
    /// owned variable whose own constraints empty its domain gets an empty
    /// table instead, which localizes to that variable's targets alone.
    WithinTargets,
}

/// The targets each variable belongs to: a target owns its owner variable and
/// its binding variable. A constraint is attributed to every target owning one
/// of its variables; a constraint over unowned variables only is hard.
pub fn targets_by_variable(
    projections: &[TargetProjection],
) -> BTreeMap<ConstraintVariableId, BTreeSet<SelectorTargetId>> {
    let mut targets = BTreeMap::<_, BTreeSet<_>>::new();
    for projection in projections {
        targets
            .entry(projection.owner_variable)
            .or_default()
            .insert(projection.target);
        if let Some(TargetBindingProjection::Variable(binding)) = &projection.binding_projection {
            targets
                .entry(*binding)
                .or_default()
                .insert(projection.target);
        }
    }
    targets
}

pub fn constraint_targets(
    targets_by_variable: &BTreeMap<ConstraintVariableId, BTreeSet<SelectorTargetId>>,
    variables: &[ConstraintVariableId],
) -> BTreeSet<SelectorTargetId> {
    variables
        .iter()
        .filter_map(|variable| targets_by_variable.get(variable))
        .flatten()
        .copied()
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum CompiledVariableDomain {
    Full(VariableDomain),
    Sparse(Vec<BackendValueId>),
    SharedSparse(SharedVariableDomainId),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledVariable {
    pub id: ConstraintVariableId,
    pub source: SelectorVariableId,
    pub domain: VariableDomain,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debug_name: Option<String>,
    pub values: CompiledVariableDomain,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TargetProjection {
    pub target: SelectorTargetId,
    pub owner_variable: ConstraintVariableId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_projection: Option<TargetBindingProjection>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledAllowedTupleConstraint {
    pub id: AllowedTupleConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub row_set: AllowedTupleRowsId,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledAllowedTupleRowSet {
    pub id: AllowedTupleRowsId,
    pub rows: CompiledAllowedTupleRows,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledSharedVariableDomain {
    pub id: SharedVariableDomainId,
    pub domain: VariableDomain,
    pub values: Vec<BackendValueId>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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

    fn from_flat_rows(arity: usize, mut values: Vec<BackendValueId>) -> Self {
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

    fn from_unary_values(values: Vec<BackendValueId>) -> Self {
        Self { arity: 1, values }
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AllDifferentReason {
    TargetInjectivity { targets: Vec<SelectorTargetId> },
    SelectorSemantics { label: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledAllDifferentConstraint {
    pub id: AllDifferentConstraintId,
    pub variables: Vec<ConstraintVariableId>,
    pub reason: AllDifferentReason,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DomainValueDictionary {
    pub owners: Vec<OwnerId>,
    pub strings: Vec<String>,
}

impl DomainValueDictionary {
    pub fn total_len(&self) -> usize {
        self.owners.len() + self.strings.len()
    }

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledSelectorProblem {
    pub presolve_scope: PresolveScope,
    pub value_dictionary: DomainValueDictionary,
    pub full_domains: FullDomainValues,
    #[serde(default)]
    pub shared_variable_domains: Vec<CompiledSharedVariableDomain>,
    pub variables: Vec<CompiledVariable>,
    pub target_projections: Vec<TargetProjection>,
    #[serde(default)]
    pub allowed_tuple_row_sets: Vec<CompiledAllowedTupleRowSet>,
    pub allowed_tuples: Vec<CompiledAllowedTupleConstraint>,
    pub all_different: Vec<CompiledAllDifferentConstraint>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub known_unsat: Option<String>,
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

#[derive(Debug)]
pub struct CompiledSelectorProblemBuilder {
    presolve_scope: PresolveScope,
    targets_by_variable: BTreeMap<ConstraintVariableId, BTreeSet<SelectorTargetId>>,
    /// `WithinTargets` only: owned variables whose own constraints left no
    /// value. Their domain keeps its last non-empty value and `finish` adds an
    /// empty table over each.
    exhausted_variables: BTreeSet<ConstraintVariableId>,
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
    known_unsat: Option<String>,
}

#[derive(Debug, Default)]
struct DomainValueIds {
    owners: HashMap<OwnerId, BackendValueId>,
    strings: HashMap<String, BackendValueId>,
}

#[derive(Debug, Clone)]
struct CompiledVariableBuilder {
    id: ConstraintVariableId,
    source: SelectorVariableId,
    domain: VariableDomain,
    debug_name: Option<String>,
    values: CompiledVariableDomain,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct SharedVariableDomainFingerprint {
    domain: u8,
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
    pub fn new(presolve_scope: PresolveScope) -> Self {
        Self {
            presolve_scope,
            targets_by_variable: BTreeMap::new(),
            exhausted_variables: BTreeSet::new(),
            value_ids: DomainValueIds::default(),
            value_dictionary: DomainValueDictionary::default(),
            full_domains: FullDomainValues::default(),
            variables: Vec::new(),
            target_projections: Vec::new(),
            shared_variable_domains: Vec::new(),
            shared_variable_domains_by_fingerprint: HashMap::new(),
            shared_variable_domain_intersections: HashMap::new(),
            allowed_tuple_row_sets: Vec::new(),
            allowed_tuple_row_sets_by_fingerprint: HashMap::new(),
            allowed_tuples: Vec::new(),
            all_different: Vec::new(),
            known_unsat: None,
        }
    }

    pub fn known_unsat_reason(&self) -> Option<&str> {
        self.known_unsat.as_deref()
    }

    /// Whether presolve may narrow `variable` from a constraint over
    /// `constraint_variables` (see [`PresolveScope`]).
    pub fn may_prune(
        &self,
        constraint_variables: &[ConstraintVariableId],
        variable: ConstraintVariableId,
    ) -> bool {
        match self.presolve_scope {
            PresolveScope::AcrossTargets => true,
            PresolveScope::WithinTargets => {
                let owners = self.targets_by_variable.get(&variable);
                constraint_targets(&self.targets_by_variable, constraint_variables)
                    .iter()
                    .all(|target| owners.is_some_and(|owners| owners.contains(target)))
            }
        }
    }

    /// Records that a table over `id` has no rows. Across targets that proves
    /// the program unsatisfiable; within targets the empty table stays in the
    /// problem for the sidecar to attribute.
    fn record_empty_table(&mut self, id: AllowedTupleConstraintId) {
        if self.presolve_scope == PresolveScope::AcrossTargets {
            self.known_unsat
                .get_or_insert_with(|| format!("allowed tuple constraint {id:?} has no rows"));
        }
    }

    /// Installs a narrowed domain for `variable`, handling the empty case.
    fn set_variable_domain(
        &mut self,
        variable: ConstraintVariableId,
        values: CompiledVariableDomain,
    ) -> Result<(), CompiledSelectorProblemError> {
        if !self.compiled_variable_domain_is_empty(&values) {
            self.require_variable_mut(variable)?.values = values;
            return Ok(());
        }
        if self.presolve_scope == PresolveScope::WithinTargets
            && self.targets_by_variable.contains_key(&variable)
        {
            self.exhausted_variables.insert(variable);
            return Ok(());
        }
        self.require_variable_mut(variable)?.values = values;
        let reason = self.variable_empty_domain_reason(variable);
        self.known_unsat.get_or_insert(reason);
        Ok(())
    }

    fn variable_empty_domain_reason(&self, variable: ConstraintVariableId) -> String {
        match self.variables.get(variable.0) {
            Some(variable) => format!(
                "variable restriction has empty domain for {:?} ({:?}, debug_name={})",
                variable.id,
                variable.domain,
                variable.debug_name.as_deref().unwrap_or("<none>")
            ),
            None => format!("variable restriction has empty domain for unknown {variable:?}"),
        }
    }

    fn variable_debug_summary(&self, variable: ConstraintVariableId) -> String {
        match self.variables.get(variable.0) {
            Some(variable) => format!(
                "{:?} ({:?}, source={:?}, debug_name={})",
                variable.id,
                variable.domain,
                variable.source,
                variable.debug_name.as_deref().unwrap_or("<none>")
            ),
            None => format!("unknown {variable:?}"),
        }
    }

    fn variables_debug_summary(&self, variables: &[ConstraintVariableId]) -> String {
        variables
            .iter()
            .copied()
            .map(|variable| self.variable_debug_summary(variable))
            .collect::<Vec<_>>()
            .join(", ")
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
        source: SelectorVariableId,
        domain: VariableDomain,
        debug_name: Option<String>,
    ) -> Result<ConstraintVariableId, CompiledSelectorProblemError> {
        let id = ConstraintVariableId(self.variables.len());
        self.variables.push(CompiledVariableBuilder {
            id,
            source,
            domain,
            debug_name,
            values: CompiledVariableDomain::Full(domain),
        });
        Ok(id)
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
        let projection = TargetProjection {
            target,
            owner_variable,
            binding_projection,
        };
        for (variable, targets) in targets_by_variable(std::slice::from_ref(&projection)) {
            self.targets_by_variable
                .entry(variable)
                .or_default()
                .extend(targets);
        }
        self.target_projections.push(projection);
        Ok(())
    }

    pub fn add_allowed_tuples(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<ConstraintValue>>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let domains = self.validate_allowed_tuple_variables(&variables)?;
        let mut compiled_tuples = Vec::with_capacity(tuples.len());
        for (tuple_index, tuple) in tuples.into_iter().enumerate() {
            if tuple.len() != variables.len() {
                return Err(CompiledSelectorProblemError::TupleArityMismatch {
                    id: AllowedTupleConstraintId(self.allowed_tuples.len()),
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
                        id: AllowedTupleConstraintId(self.allowed_tuples.len()),
                        tuple_index,
                        variable: variables[column],
                        expected: *expected,
                        actual,
                    });
                }
                compiled.push(self.intern_value(value)?);
            }
            compiled_tuples.push(compiled);
        }
        self.add_encoded_allowed_tuples_with_domains(variables, domains, compiled_tuples)
    }

    pub fn add_encoded_allowed_tuples(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<BackendValueId>>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let domains = self.validate_allowed_tuple_variables(&variables)?;
        self.add_encoded_allowed_tuples_with_domains(variables, domains, tuples)
    }

    pub fn add_encoded_allowed_binary_tuples(
        &mut self,
        variables: [ConstraintVariableId; 2],
        tuples: Vec<(BackendValueId, BackendValueId)>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        let variables = variables.to_vec();
        let domains = self.validate_allowed_tuple_variables(&variables)?;
        let [left_domain, right_domain]: [VariableDomain; 2] = domains
            .try_into()
            .expect("binary tuple domains have arity 2");
        let mut compiled_tuples = Vec::with_capacity(tuples.len());
        for (tuple_index, (left, right)) in tuples.into_iter().enumerate() {
            self.validate_encoded_value_domain(id, tuple_index, variables[0], left_domain, left)?;
            if !self.encoded_value_matches_variable_domain(variables[0], left)? {
                continue;
            }
            self.ensure_full_domain_contains(left_domain, left);
            self.validate_encoded_value_domain(id, tuple_index, variables[1], right_domain, right)?;
            if !self.encoded_value_matches_variable_domain(variables[1], right)? {
                continue;
            }
            self.ensure_full_domain_contains(right_domain, right);
            compiled_tuples.push((left, right));
        }
        compiled_tuples.sort_unstable();
        compiled_tuples.dedup();

        if compiled_tuples.is_empty() {
            self.record_empty_table(id);
        }

        let row_set = self
            .intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_binary_rows(compiled_tuples));
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
        Ok(id)
    }

    pub fn add_encoded_allowed_unary_tuples(
        &mut self,
        variable: ConstraintVariableId,
        values: Vec<BackendValueId>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let domain = self.require_variable(variable)?.domain;
        let row_set = self.intern_encoded_allowed_unary_row_set(variable, domain, values)?;
        self.add_encoded_allowed_row_set(vec![variable], row_set)
    }

    pub fn intern_encoded_allowed_unary_row_set(
        &mut self,
        variable: ConstraintVariableId,
        domain: VariableDomain,
        values: Vec<BackendValueId>,
    ) -> Result<AllowedTupleRowsId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        let mut values = values;
        values.sort_unstable();
        values.dedup();
        for (tuple_index, value) in values.iter().copied().enumerate() {
            self.validate_encoded_value_domain(id, tuple_index, variable, domain, value)?;
        }

        let full_domain = self.full_domains.get(domain);
        values.retain(|value| full_domain.binary_search(value).is_ok());
        Ok(self.intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_unary_values(values)))
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

    pub fn intern_encoded_allowed_row_set_for_variables(
        &mut self,
        variables: &[ConstraintVariableId],
        rows: Vec<Vec<BackendValueId>>,
    ) -> Result<AllowedTupleRowsId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        let domains = self.validate_allowed_tuple_variables(variables)?;
        let mut compiled_rows = Vec::with_capacity(rows.len());
        for (tuple_index, row) in rows.into_iter().enumerate() {
            if row.len() != variables.len() {
                return Err(CompiledSelectorProblemError::TupleArityMismatch {
                    id,
                    tuple_index,
                    expected: variables.len(),
                    actual: row.len(),
                });
            }
            for (column, (value, domain)) in row.iter().copied().zip(domains.iter()).enumerate() {
                self.validate_encoded_value_domain(
                    id,
                    tuple_index,
                    variables[column],
                    *domain,
                    value,
                )?;
                self.ensure_full_domain_contains(*domain, value);
            }
            compiled_rows.push(row);
        }
        compiled_rows.sort();
        compiled_rows.dedup();
        Ok(
            self.intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_rows(
                variables.len(),
                compiled_rows,
            )),
        )
    }

    pub fn intern_flat_encoded_allowed_row_set_for_variables(
        &mut self,
        variables: &[ConstraintVariableId],
        values: Vec<BackendValueId>,
    ) -> Result<AllowedTupleRowsId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        let domains = self.validate_allowed_tuple_variables(variables)?;
        let arity = variables.len();
        if !values.len().is_multiple_of(arity) {
            return Err(CompiledSelectorProblemError::TupleArityMismatch {
                id,
                tuple_index: values.len() / arity,
                expected: arity,
                actual: values.len() % arity,
            });
        }

        for (cell_index, value) in values.iter().copied().enumerate() {
            let tuple_index = cell_index / arity;
            let column = cell_index % arity;
            let domain = domains[column];
            self.validate_encoded_value_domain(id, tuple_index, variables[column], domain, value)?;
            self.ensure_full_domain_contains(domain, value);
        }

        Ok(self.intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_flat_rows(arity, values)))
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
        if rows.is_empty() {
            self.record_empty_table(id);
        }
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
        Ok(id)
    }

    pub fn allowed_tuple_row_set(
        &self,
        row_set: AllowedTupleRowsId,
    ) -> Result<&CompiledAllowedTupleRows, CompiledSelectorProblemError> {
        self.allowed_tuple_row_sets
            .get(row_set.0)
            .map(|row_set| &row_set.rows)
            .ok_or(CompiledSelectorProblemError::UnknownAllowedTupleRowSet { row_set })
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

    fn add_encoded_allowed_tuples_with_domains(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        domains: Vec<VariableDomain>,
        tuples: Vec<Vec<BackendValueId>>,
    ) -> Result<AllowedTupleConstraintId, CompiledSelectorProblemError> {
        let id = AllowedTupleConstraintId(self.allowed_tuples.len());
        if variables.is_empty() {
            return Err(CompiledSelectorProblemError::EmptyAllowedTupleVariables { id });
        }

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
            let mut row_matches_variable_domains = true;
            for (column, (value_id, expected)) in
                tuple.iter().copied().zip(domains.iter()).enumerate()
            {
                self.validate_encoded_value_domain(
                    id,
                    tuple_index,
                    variables[column],
                    *expected,
                    value_id,
                )?;
                if !self.encoded_value_matches_variable_domain(variables[column], value_id)? {
                    row_matches_variable_domains = false;
                    break;
                }
                self.ensure_full_domain_contains(*expected, value_id);
            }
            if row_matches_variable_domains {
                compiled_tuples.push(tuple);
            }
        }
        compiled_tuples.sort();
        compiled_tuples.dedup();

        if compiled_tuples.is_empty() {
            self.record_empty_table(id);
        }

        let arity = variables.len();
        let row_set = self
            .intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_rows(arity, compiled_tuples));
        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            row_set,
        });
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
        while self.simplify_allowed_tuple_constraints_once()? && self.known_unsat.is_none() {}
        Ok(())
    }

    pub fn add_all_different(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        reason: AllDifferentReason,
    ) -> Result<Option<AllDifferentConstraintId>, CompiledSelectorProblemError> {
        let id = AllDifferentConstraintId(self.all_different.len());
        self.validate_all_different_constraint(id, &variables, &reason)?;
        let variables = self.simplify_all_different_entries(variables, |variable| *variable)?;
        if variables.len() < 2 {
            return Ok(None);
        }
        self.all_different.push(CompiledAllDifferentConstraint {
            id,
            variables,
            reason,
        });
        Ok(Some(id))
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
        self.validate_all_different_constraint(
            id,
            &variables,
            &AllDifferentReason::TargetInjectivity {
                targets: targets.clone(),
            },
        )?;
        let entries = variables.into_iter().zip(targets).collect::<Vec<_>>();
        let entries =
            self.simplify_all_different_entries(entries, |(variable, _target)| *variable)?;
        if entries.len() < 2 {
            return Ok(None);
        }
        let (variables, targets): (Vec<_>, Vec<_>) = entries.into_iter().unzip();
        self.all_different.push(CompiledAllDifferentConstraint {
            id,
            variables,
            reason: AllDifferentReason::TargetInjectivity { targets },
        });
        Ok(Some(id))
    }

    pub fn finish(mut self) -> Result<CompiledSelectorProblem, CompiledSelectorProblemError> {
        for variable in std::mem::take(&mut self.exhausted_variables) {
            let row_set = self
                .intern_allowed_tuple_rows(CompiledAllowedTupleRows::from_unary_values(Vec::new()));
            let id = AllowedTupleConstraintId(self.allowed_tuples.len());
            self.allowed_tuples.push(CompiledAllowedTupleConstraint {
                id,
                variables: vec![variable],
                row_set,
            });
        }
        let variables: Vec<CompiledVariable> = self
            .variables
            .iter()
            .map(|variable| CompiledVariable {
                id: variable.id,
                source: variable.source,
                domain: variable.domain,
                debug_name: variable.debug_name.clone(),
                values: variable.values.clone(),
            })
            .collect();

        Ok(CompiledSelectorProblem {
            presolve_scope: self.presolve_scope,
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
        if kept_rows.is_empty() && self.presolve_scope == PresolveScope::WithinTargets {
            return Ok(Some((
                variables.to_vec(),
                CompiledAllowedTupleRows::from_flat_rows(arity, Vec::new()),
                rows.cell_count() != 0,
            )));
        }
        if kept_rows.is_empty() {
            if self.known_unsat.is_none() {
                let variables = self.variables_debug_summary(variables);
                self.known_unsat = Some(format!(
                    "allowed tuple constraint over [{variables}] has no rows after domain pruning"
                ));
            }
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
            let can_restrict = self.may_prune(variables, variables[column])
                && self.can_restrict_variable_to_encoded_values(variables[column], values)?;
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

    fn simplify_all_different_entries<T>(
        &mut self,
        entries: Vec<T>,
        variable_for_entry: impl Fn(&T) -> ConstraintVariableId,
    ) -> Result<Vec<T>, CompiledSelectorProblemError> {
        if self.presolve_scope == PresolveScope::WithinTargets {
            return Ok(entries);
        }
        let mut entries = entries;
        let mut fixed_values = BTreeSet::new();
        loop {
            let mut next_entries = Vec::new();
            let mut learned_fixed_value = false;
            for entry in entries {
                let variable = variable_for_entry(&entry);
                let mut values = self.variable_domain_values(variable)?;
                if !fixed_values.is_empty() {
                    values.retain(|value| !fixed_values.contains(value));
                    self.restrict_variable_to_encoded_values(variable, values.iter().copied())?;
                }
                match values.as_slice() {
                    [] => {
                        let reason = self.variable_empty_domain_reason(variable);
                        self.known_unsat.get_or_insert(reason);
                    }
                    [value] => {
                        if !fixed_values.insert(*value) {
                            self.known_unsat.get_or_insert_with(|| {
                                format!(
                                    "all_different has duplicate fixed value {value:?} for variable {variable:?}"
                                )
                            });
                        }
                        learned_fixed_value = true;
                    }
                    _ => next_entries.push(entry),
                }
            }
            entries = next_entries;
            if !learned_fixed_value || self.known_unsat.is_some() {
                break;
            }
        }
        Ok(entries)
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
        reason: &AllDifferentReason,
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

        if let AllDifferentReason::TargetInjectivity { targets } = reason {
            let projected_variables = targets
                .iter()
                .map(|target| self.target_owner_projection_variable(*target))
                .collect::<Result<Vec<_>, _>>()?;
            if projected_variables != variables {
                return Err(
                    CompiledSelectorProblemError::TargetInjectivityProjectionMismatch { id },
                );
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
    TargetInjectivityProjectionMismatch {
        id: AllDifferentConstraintId,
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
            Self::TargetInjectivityProjectionMismatch { id } => write!(
                f,
                "target-injectivity all_different constraint {id:?} does not match target projections"
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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
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
    pub solver_response_stats: Option<String>,
    /// Unsatisfiable cores found after the plain solve was infeasible. The
    /// status and assignments then cover only the targets outside them.
    pub conflicts: Vec<Vec<SelectorTargetId>>,
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
    let domain_tag = variable_domain_fingerprint_tag(domain);
    domain_tag.hash(&mut hasher);
    values.hash(&mut hasher);
    SharedVariableDomainFingerprint {
        domain: domain_tag,
        len: values.len(),
        hash: hasher.finish(),
    }
}

fn variable_domain_fingerprint_tag(domain: VariableDomain) -> u8 {
    match domain {
        VariableDomain::Owner => 0,
        VariableDomain::String => 2,
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
