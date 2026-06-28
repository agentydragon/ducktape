//! Compact compiled selector problem consumed by exact-assignment backends.
//!
//! This is the production boundary between selector/fact lowering and a CP/SAT
//! backend. Values are interned once, variables hold compact full-domain handles
//! or sparse candidate sets, and allowed tuples are stored as interned ids.

use std::collections::{BTreeMap, BTreeSet, HashMap};
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BackendValueId(pub i64);

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
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum CompiledVariableDomain {
    Full(VariableDomain),
    Sparse(Vec<BackendValueId>),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledVariable {
    pub id: ConstraintVariableId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<SelectorVariableId>,
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
    pub tuples: Vec<Vec<BackendValueId>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BinaryConstraintKind {
    Equal,
    NotEqual,
    OrdinalBefore,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledBinaryConstraint {
    pub left: ConstraintVariableId,
    pub right: ConstraintVariableId,
    pub kind: BinaryConstraintKind,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledLinearConstraint {
    pub variables: Vec<ConstraintVariableId>,
    pub coefficients: Vec<i64>,
    pub offset: i64,
    pub domain: Vec<i64>,
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
    pub ast_nodes: Vec<BackendValueId>,
    pub strings: Vec<BackendValueId>,
    pub statement_ordinals: Vec<BackendValueId>,
}

impl FullDomainValues {
    pub fn get(&self, domain: VariableDomain) -> &[BackendValueId] {
        match domain {
            VariableDomain::Owner => &self.owners,
            VariableDomain::AstNode => &self.ast_nodes,
            VariableDomain::String => &self.strings,
            VariableDomain::StatementOrdinal => &self.statement_ordinals,
        }
    }

    fn get_mut(&mut self, domain: VariableDomain) -> &mut Vec<BackendValueId> {
        match domain {
            VariableDomain::Owner => &mut self.owners,
            VariableDomain::AstNode => &mut self.ast_nodes,
            VariableDomain::String => &mut self.strings,
            VariableDomain::StatementOrdinal => &mut self.statement_ordinals,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DomainValueDictionary {
    pub owners: Vec<OwnerId>,
    pub ast_nodes: Vec<NodeId>,
    pub strings: Vec<String>,
    pub statement_ordinals: Vec<StatementOrdinal>,
}

impl DomainValueDictionary {
    pub fn total_len(&self) -> usize {
        self.owners.len()
            + self.ast_nodes.len()
            + self.strings.len()
            + self.statement_ordinals.len()
    }

    fn domain_len(&self, domain: VariableDomain) -> usize {
        match domain {
            VariableDomain::Owner => self.owners.len(),
            VariableDomain::AstNode => self.ast_nodes.len(),
            VariableDomain::String => self.strings.len(),
            VariableDomain::StatementOrdinal => self.statement_ordinals.len(),
        }
    }

    pub fn encode(&self, value: &ConstraintValue) -> Option<BackendValueId> {
        let index = match value {
            ConstraintValue::Owner(value) => {
                self.owners.iter().position(|candidate| candidate == value)
            }
            ConstraintValue::AstNode(value) => self
                .ast_nodes
                .iter()
                .position(|candidate| candidate == value),
            ConstraintValue::String(value) => {
                self.strings.iter().position(|candidate| candidate == value)
            }
            ConstraintValue::StatementOrdinal(value) => self
                .statement_ordinals
                .iter()
                .position(|candidate| candidate == value),
        }?;
        Some(BackendValueId(index.try_into().ok()?))
    }

    fn decode(&self, domain: VariableDomain, value: BackendValueId) -> Option<ConstraintValue> {
        let index = backend_value_index(value).ok()?;
        match domain {
            VariableDomain::Owner => self.owners.get(index).copied().map(ConstraintValue::Owner),
            VariableDomain::AstNode => self
                .ast_nodes
                .get(index)
                .copied()
                .map(ConstraintValue::AstNode),
            VariableDomain::String => self
                .strings
                .get(index)
                .cloned()
                .map(ConstraintValue::String),
            VariableDomain::StatementOrdinal => self
                .statement_ordinals
                .get(index)
                .copied()
                .map(ConstraintValue::StatementOrdinal),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledSelectorProblem {
    pub value_dictionary: DomainValueDictionary,
    pub full_domains: FullDomainValues,
    pub variables: Vec<CompiledVariable>,
    pub target_projections: Vec<TargetProjection>,
    pub allowed_tuples: Vec<CompiledAllowedTupleConstraint>,
    pub binary_constraints: Vec<CompiledBinaryConstraint>,
    #[serde(default)]
    pub linear_constraints: Vec<CompiledLinearConstraint>,
    pub all_different: Vec<CompiledAllDifferentConstraint>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub known_unsat: Option<String>,
}

impl CompiledSelectorProblem {
    pub fn variable_domain_values(&self, variable: &CompiledVariable) -> Vec<BackendValueId> {
        match &variable.values {
            CompiledVariableDomain::Full(domain) => self.full_domains.get(*domain).to_vec(),
            CompiledVariableDomain::Sparse(values) => values.clone(),
        }
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

    fn variable_domain_contains(&self, variable: &CompiledVariable, value: BackendValueId) -> bool {
        match &variable.values {
            CompiledVariableDomain::Full(domain) => backend_value_index(value)
                .is_ok_and(|index| index < self.full_domains.get(*domain).len()),
            CompiledVariableDomain::Sparse(values) => values.binary_search(&value).is_ok(),
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
    allowed_tuples: Vec<CompiledAllowedTupleConstraint>,
    binary_constraints: Vec<CompiledBinaryConstraint>,
    linear_constraints: Vec<CompiledLinearConstraint>,
    all_different: Vec<CompiledAllDifferentConstraint>,
    known_unsat: Option<String>,
}

#[derive(Debug, Default)]
struct DomainValueIds {
    owners: HashMap<OwnerId, BackendValueId>,
    ast_nodes: HashMap<NodeId, BackendValueId>,
    strings: HashMap<String, BackendValueId>,
    statement_ordinals: HashMap<StatementOrdinal, BackendValueId>,
}

#[derive(Debug, Clone)]
struct CompiledVariableBuilder {
    id: ConstraintVariableId,
    source: Option<SelectorVariableId>,
    domain: VariableDomain,
    debug_name: Option<String>,
    values: CompiledVariableDomain,
}

impl CompiledSelectorProblemBuilder {
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
            source: Some(source),
            domain,
            debug_name,
            values: CompiledVariableDomain::Full(domain),
        });
        Ok(id)
    }

    pub fn add_internal_integer_variable(
        &mut self,
        debug_name: Option<String>,
        values: impl IntoIterator<Item = BackendValueId>,
    ) -> Result<ConstraintVariableId, CompiledSelectorProblemError> {
        let mut values = values.into_iter().collect::<Vec<_>>();
        values.sort_unstable();
        values.dedup();
        if values.is_empty() {
            self.known_unsat
                .get_or_insert_with(|| "internal integer variable has empty domain".to_string());
            values.push(BackendValueId(0));
        }
        if let Some(value) = values.iter().find(|value| value.0 < 0) {
            return Err(
                CompiledSelectorProblemError::InternalVariableValueOutOfDomain { value: *value },
            );
        }
        let id = ConstraintVariableId(self.variables.len());
        self.variables.push(CompiledVariableBuilder {
            id,
            source: None,
            domain: VariableDomain::StatementOrdinal,
            debug_name,
            values: CompiledVariableDomain::Sparse(values),
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

        let full_domain = self.full_domains.get(domain).to_vec();
        let empty_domain = {
            let variable = self.require_variable_mut(variable)?;
            match &mut variable.values {
                CompiledVariableDomain::Full(_) => {
                    values.retain(|value| full_domain.binary_search(value).is_ok());
                    let empty_domain = values.is_empty();
                    variable.values = CompiledVariableDomain::Sparse(values);
                    empty_domain
                }
                CompiledVariableDomain::Sparse(existing) => {
                    let mut restricted = Vec::new();
                    let mut left = 0;
                    let mut right = 0;
                    while left < existing.len() && right < values.len() {
                        match existing[left].cmp(&values[right]) {
                            std::cmp::Ordering::Less => left += 1,
                            std::cmp::Ordering::Greater => right += 1,
                            std::cmp::Ordering::Equal => {
                                restricted.push(existing[left]);
                                left += 1;
                                right += 1;
                            }
                        }
                    }
                    let empty_domain = restricted.is_empty();
                    *existing = restricted;
                    empty_domain
                }
            }
        };
        if empty_domain {
            self.known_unsat
                .get_or_insert_with(|| "variable restriction has empty domain".to_string());
        }
        Ok(())
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

    pub fn intern_ast_node(
        &mut self,
        value: NodeId,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        if let Some(id) = self.value_ids.ast_nodes.get(&value) {
            return Ok(*id);
        }
        let count = self.value_dictionary.ast_nodes.len();
        let id = backend_value_id(count)?;
        self.value_ids.ast_nodes.insert(value, id);
        self.value_dictionary.ast_nodes.push(value);
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

    pub fn intern_statement_ordinal(
        &mut self,
        value: StatementOrdinal,
    ) -> Result<BackendValueId, CompiledSelectorProblemError> {
        if let Some(id) = self.value_ids.statement_ordinals.get(&value) {
            return Ok(*id);
        }
        let count = self.value_dictionary.statement_ordinals.len();
        let id = backend_value_id(count)?;
        self.value_ids.statement_ordinals.insert(value, id);
        self.value_dictionary.statement_ordinals.push(value);
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
            let mut compiled = Vec::with_capacity(tuple.len());
            let mut row_matches_variable_domains = true;
            for (column, (value_id, expected)) in tuple.into_iter().zip(domains.iter()).enumerate()
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
                compiled.push(value_id);
            }
            if row_matches_variable_domains {
                compiled_tuples.push(compiled);
            }
        }
        compiled_tuples.sort();
        compiled_tuples.dedup();

        if compiled_tuples.is_empty() {
            self.known_unsat
                .get_or_insert_with(|| format!("allowed tuple constraint {id:?} has no rows"));
        }

        self.allowed_tuples.push(CompiledAllowedTupleConstraint {
            id,
            variables,
            tuples: compiled_tuples,
        });
        Ok(id)
    }

    pub fn add_binary_constraint(
        &mut self,
        left: ConstraintVariableId,
        right: ConstraintVariableId,
        kind: BinaryConstraintKind,
    ) -> Result<(), CompiledSelectorProblemError> {
        self.validate_binary_constraint(left, right, kind)?;
        self.binary_constraints
            .push(CompiledBinaryConstraint { left, right, kind });
        Ok(())
    }

    pub fn add_linear_constraint(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        coefficients: Vec<i64>,
        offset: i64,
        domain: Vec<i64>,
    ) -> Result<(), CompiledSelectorProblemError> {
        self.validate_linear_constraint(&variables, &coefficients, &domain)?;
        self.linear_constraints.push(CompiledLinearConstraint {
            variables,
            coefficients,
            offset,
            domain,
        });
        Ok(())
    }

    pub fn variable_domain_values(
        &self,
        variable: ConstraintVariableId,
    ) -> Result<Vec<BackendValueId>, CompiledSelectorProblemError> {
        match &self.require_variable(variable)?.values {
            CompiledVariableDomain::Full(domain) => Ok(self.full_domains.get(*domain).to_vec()),
            CompiledVariableDomain::Sparse(values) => Ok(values.clone()),
        }
    }

    pub fn add_all_different(
        &mut self,
        variables: Vec<ConstraintVariableId>,
        reason: AllDifferentReason,
    ) -> Result<AllDifferentConstraintId, CompiledSelectorProblemError> {
        let id = AllDifferentConstraintId(self.all_different.len());
        self.validate_all_different_constraint(id, &variables, &reason)?;
        self.all_different.push(CompiledAllDifferentConstraint {
            id,
            variables,
            reason,
        });
        Ok(id)
    }

    pub fn require_target_all_different(
        &mut self,
        targets: Vec<SelectorTargetId>,
    ) -> Result<AllDifferentConstraintId, CompiledSelectorProblemError> {
        let variables = targets
            .iter()
            .map(|target| self.target_owner_projection_variable(*target))
            .collect::<Result<Vec<_>, _>>()?;
        self.add_all_different(variables, AllDifferentReason::TargetInjectivity { targets })
    }

    pub fn finish(self) -> Result<CompiledSelectorProblem, CompiledSelectorProblemError> {
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
            value_dictionary: self.value_dictionary,
            full_domains: self.full_domains,
            variables,
            target_projections: self.target_projections,
            allowed_tuples: self.allowed_tuples,
            binary_constraints: self.binary_constraints,
            linear_constraints: self.linear_constraints,
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
            ConstraintValue::AstNode(value) => self.intern_ast_node(value),
            ConstraintValue::String(value) => self.intern_string(&value),
            ConstraintValue::StatementOrdinal(value) => self.intern_statement_ordinal(value),
        }
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
        if self.require_variable(variable)?.source.is_none() {
            return Ok(());
        }
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

    fn encoded_value_matches_variable_domain(
        &self,
        variable: ConstraintVariableId,
        value: BackendValueId,
    ) -> Result<bool, CompiledSelectorProblemError> {
        match &self.require_variable(variable)?.values {
            CompiledVariableDomain::Full(_) => Ok(true),
            CompiledVariableDomain::Sparse(values) => Ok(values.binary_search(&value).is_ok()),
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

    fn validate_binary_constraint(
        &self,
        left: ConstraintVariableId,
        right: ConstraintVariableId,
        kind: BinaryConstraintKind,
    ) -> Result<(), CompiledSelectorProblemError> {
        let left_domain = self.require_variable(left)?.domain;
        let right_domain = self.require_variable(right)?.domain;
        match kind {
            BinaryConstraintKind::Equal | BinaryConstraintKind::NotEqual => {
                if left_domain != right_domain {
                    return Err(CompiledSelectorProblemError::BinaryDomainMismatch {
                        left,
                        right,
                        left_domain,
                        right_domain,
                    });
                }
            }
            BinaryConstraintKind::OrdinalBefore => {
                if left_domain != VariableDomain::StatementOrdinal
                    || right_domain != VariableDomain::StatementOrdinal
                {
                    return Err(CompiledSelectorProblemError::OrdinalBeforeDomainMismatch {
                        left,
                        right,
                    });
                }
            }
        }
        Ok(())
    }

    fn validate_linear_constraint(
        &self,
        variables: &[ConstraintVariableId],
        coefficients: &[i64],
        domain: &[i64],
    ) -> Result<(), CompiledSelectorProblemError> {
        if variables.is_empty() {
            return Err(CompiledSelectorProblemError::DegenerateLinearConstraint);
        }
        if variables.len() != coefficients.len() {
            return Err(CompiledSelectorProblemError::LinearArityMismatch {
                variables: variables.len(),
                coefficients: coefficients.len(),
            });
        }
        if domain.is_empty() || domain.len() % 2 != 0 {
            return Err(CompiledSelectorProblemError::InvalidLinearDomain);
        }
        for interval in domain.chunks_exact(2) {
            if interval[0] > interval[1] {
                return Err(CompiledSelectorProblemError::InvalidLinearDomain);
            }
        }
        for variable in variables {
            let domain = self.require_variable(*variable)?.domain;
            if domain != VariableDomain::StatementOrdinal {
                return Err(CompiledSelectorProblemError::LinearDomainMismatch {
                    variable: *variable,
                    domain,
                });
            }
        }
        Ok(())
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
    DegenerateLinearConstraint,
    LinearArityMismatch {
        variables: usize,
        coefficients: usize,
    },
    InvalidLinearDomain,
    LinearDomainMismatch {
        variable: ConstraintVariableId,
        domain: VariableDomain,
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
    InternalVariableValueOutOfDomain {
        value: BackendValueId,
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
            Self::BinaryDomainMismatch {
                left,
                right,
                left_domain,
                right_domain,
            } => write!(
                f,
                "binary constraint {left:?}/{right:?} has mismatched domains {left_domain:?}/{right_domain:?}"
            ),
            Self::OrdinalBeforeDomainMismatch { left, right } => write!(
                f,
                "ordinal_before constraint {left:?}/{right:?} must reference statement ordinals"
            ),
            Self::DegenerateLinearConstraint => {
                write!(f, "linear constraint has no variables")
            }
            Self::LinearArityMismatch {
                variables,
                coefficients,
            } => write!(
                f,
                "linear constraint has {variables} variables but {coefficients} coefficients"
            ),
            Self::InvalidLinearDomain => {
                write!(f, "linear constraint has an invalid domain")
            }
            Self::LinearDomainMismatch { variable, domain } => write!(
                f,
                "linear constraint variable {variable:?} has {domain:?} domain, expected statement ordinals"
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
            Self::InternalVariableValueOutOfDomain { value } => {
                write!(f, "internal integer variable has invalid value {value:?}")
            }
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
    TargetSupportComplete,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendSolveResult {
    pub status: BackendSolveStatus,
    pub assignment_coverage: BackendAssignmentCoverage,
    pub assignments: Vec<BackendAssignment>,
    pub diagnostic: Option<String>,
    pub solver_response_stats: Option<String>,
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
