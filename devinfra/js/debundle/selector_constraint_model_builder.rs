//! Backend-neutral lowering from selector IR plus facts into a finite-domain
//! constraint model.
//!
//! This is intentionally a shadow builder: it exposes the model boundary a
//! CP/SAT backend can consume without changing the current Ascent-backed solver
//! path.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use chunk_facts::NodeId;
use selector_constraint_model::{
    BinaryConstraintKind, ConstraintModelError, ConstraintValue, ConstraintVariableId,
    SelectorConstraintModel,
};
use selector_ir::{
    ClaimKind, NodeTerm, OrdinalTerm, OwnerTerm, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorVariableId, StringTerm, VariableDomain,
};

pub fn build_selector_constraint_model(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SelectorConstraintModel, SelectorConstraintModelBuildError> {
    program
        .validate()
        .map_err(SelectorConstraintModelBuildError::InvalidProgram)?;

    let domains = FactDomains::from_program_and_facts(program, facts);
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = SelectorConstraintModel::default();
    let mut variables = Vec::with_capacity(program.variables.len());
    for variable in &program.variables {
        variables.push(model.add_variable(
            variable.id,
            variable.domain,
            domains.values_for(variable.domain),
            variable.debug_name.clone(),
        )?);
    }

    for target in &program.targets {
        let owner_variable = model_variable(&variables, target.owner)?;
        let binding_variable = target_binding_projections
            .binding_variable(target.owner)
            .map(|binding| model_variable(&variables, binding))
            .transpose()?;
        model.add_target_projection(target.id, owner_variable, binding_variable)?;
    }

    for atom in &program.atoms {
        lower_atom_constraint(atom, &domains, &variables, &mut model)?;
    }

    for targets in &program.all_different {
        model.require_target_all_different(targets.clone())?;
    }

    model.validate()?;
    Ok(model)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorConstraintModelBuildError {
    InvalidProgram(SelectorProgramError),
    InvalidModel(ConstraintModelError),
    UnknownSelectorVariable {
        variable: SelectorVariableId,
    },
    UnsupportedAtom {
        atom: String,
    },
    ConstantOnlyAtomUnsatisfied {
        atom: String,
    },
    ConflictingTargetBindingProjection {
        owner: SelectorVariableId,
        existing: TargetBindingProjection,
        actual: TargetBindingProjection,
    },
}

impl fmt::Display for SelectorConstraintModelBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidProgram(err) => write!(f, "invalid selector program: {err}"),
            Self::InvalidModel(err) => write!(f, "invalid selector constraint model: {err}"),
            Self::UnknownSelectorVariable { variable } => {
                write!(f, "selector variable {variable:?} has no model variable")
            }
            Self::UnsupportedAtom { atom } => {
                write!(
                    f,
                    "selector atom is not supported by the constraint model builder: {atom}"
                )
            }
            Self::ConstantOnlyAtomUnsatisfied { atom } => {
                write!(
                    f,
                    "constant-only selector atom has no matching fact: {atom}"
                )
            }
            Self::ConflictingTargetBindingProjection {
                owner,
                existing,
                actual,
            } => write!(
                f,
                "selector owner variable {owner:?} has conflicting target binding projections: {existing:?} vs {actual:?}"
            ),
        }
    }
}

impl Error for SelectorConstraintModelBuildError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidProgram(err) => Some(err),
            Self::InvalidModel(err) => Some(err),
            Self::UnknownSelectorVariable { .. }
            | Self::UnsupportedAtom { .. }
            | Self::ConstantOnlyAtomUnsatisfied { .. }
            | Self::ConflictingTargetBindingProjection { .. } => None,
        }
    }
}

impl From<ConstraintModelError> for SelectorConstraintModelBuildError {
    fn from(err: ConstraintModelError) -> Self {
        Self::InvalidModel(err)
    }
}

fn model_variable(
    variables: &[ConstraintVariableId],
    variable: SelectorVariableId,
) -> Result<ConstraintVariableId, SelectorConstraintModelBuildError> {
    variables
        .get(variable.0)
        .copied()
        .ok_or(SelectorConstraintModelBuildError::UnknownSelectorVariable { variable })
}

fn lower_atom_constraint(
    atom: &SelectorAtom,
    domains: &FactDomains,
    variables: &[ConstraintVariableId],
    model: &mut SelectorConstraintModel,
) -> Result<(), SelectorConstraintModelBuildError> {
    match atom {
        SelectorAtom::OwnerKind {
            owner,
            statement_kind,
        } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            statement_kind,
            &domains.owner_kinds,
        ),
        SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => add_owner_ordinal_allowed_tuples(
            model,
            variables,
            owner,
            ordinal,
            &domains.owner_statement_ordinals,
        ),
        SelectorAtom::OwnerDeclaresBinding { owner, binding } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            binding,
            &domains.declared_bindings,
        ),
        SelectorAtom::OwnerExportName { owner, export_name } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            export_name,
            &domains.export_names,
        ),
        SelectorAtom::Equal { left, right } => model
            .add_binary_constraint(
                model_variable(variables, *left)?,
                model_variable(variables, *right)?,
                BinaryConstraintKind::Equal,
            )
            .map_err(Into::into),
        SelectorAtom::NotEqual { left, right } => model
            .add_binary_constraint(
                model_variable(variables, *left)?,
                model_variable(variables, *right)?,
                BinaryConstraintKind::NotEqual,
            )
            .map_err(Into::into),
        SelectorAtom::OrdinalBefore {
            before: OrdinalTerm::Var { id: before },
            after: OrdinalTerm::Var { id: after },
        } => model
            .add_binary_constraint(
                model_variable(variables, *before)?,
                model_variable(variables, *after)?,
                BinaryConstraintKind::OrdinalBefore,
            )
            .map_err(Into::into),
        _ => Err(SelectorConstraintModelBuildError::UnsupportedAtom {
            atom: format!("{atom:?}"),
        }),
    }
}

fn add_owner_string_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    facts: &BTreeSet<(OwnerId, String)>,
) -> Result<(), SelectorConstraintModelBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = string {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_owner, fact_string)| {
                owner_term_matches(owner, *fact_owner) && string_term_matches(string, fact_string)
            })
            .then_some(())
            .ok_or_else(
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/string fact {owner:?} {string:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_owner, fact_string)| {
            owner_term_matches(owner, *fact_owner) && string_term_matches(string, fact_string)
        })
        .map(|(fact_owner, fact_string)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(owner, OwnerTerm::Var { .. }) {
                tuple.push(ConstraintValue::Owner(*fact_owner));
            }
            if matches!(string, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_string.clone()));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_ordinal_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(OwnerId, StatementOrdinal)>,
) -> Result<(), SelectorConstraintModelBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let OrdinalTerm::Var { id } = ordinal {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_owner, fact_ordinal)| {
                owner_term_matches(owner, *fact_owner)
                    && ordinal_term_matches(ordinal, *fact_ordinal)
            })
            .then_some(())
            .ok_or_else(
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/ordinal fact {owner:?} {ordinal:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_owner, fact_ordinal)| {
            owner_term_matches(owner, *fact_owner) && ordinal_term_matches(ordinal, *fact_ordinal)
        })
        .map(|(fact_owner, fact_ordinal)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(owner, OwnerTerm::Var { .. }) {
                tuple.push(ConstraintValue::Owner(*fact_owner));
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(ConstraintValue::StatementOrdinal(*fact_ordinal));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_allowed_tuple_set(
    model: &mut SelectorConstraintModel,
    variables: Vec<ConstraintVariableId>,
    tuples: BTreeSet<Vec<ConstraintValue>>,
) -> Result<(), SelectorConstraintModelBuildError> {
    model
        .add_allowed_tuples(variables, tuples.into_iter().collect())
        .map(|_| ())
        .map_err(Into::into)
}

fn owner_term_matches(term: &OwnerTerm, owner: OwnerId) -> bool {
    match term {
        OwnerTerm::Var { .. } => true,
        OwnerTerm::Const {
            owner: expected_owner,
        } => *expected_owner == owner,
    }
}

fn string_term_matches(term: &StringTerm, value: &str) -> bool {
    match term {
        StringTerm::Var { .. } => true,
        StringTerm::Const {
            value: expected_value,
        } => expected_value == value,
    }
}

fn ordinal_term_matches(term: &OrdinalTerm, ordinal: StatementOrdinal) -> bool {
    match term {
        OrdinalTerm::Var { .. } => true,
        OrdinalTerm::Const {
            ordinal: expected_ordinal,
        } => *expected_ordinal == ordinal,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetBindingProjection {
    Const(String),
    Var(SelectorVariableId),
}

#[derive(Debug, Default)]
struct TargetBindingProjections {
    by_owner: BTreeMap<SelectorVariableId, TargetBindingProjection>,
}

impl TargetBindingProjections {
    fn from_program(program: &SelectorProgram) -> Result<Self, SelectorConstraintModelBuildError> {
        let mut projections = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Const { value },
                } => projections.insert(*owner, TargetBindingProjection::Const(value.clone()))?,
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Var { id: binding },
                } => projections.insert(*owner, TargetBindingProjection::Var(*binding))?,
                _ => {}
            }
        }
        Ok(projections)
    }

    fn insert(
        &mut self,
        owner: SelectorVariableId,
        binding: TargetBindingProjection,
    ) -> Result<(), SelectorConstraintModelBuildError> {
        match self.by_owner.get(&owner) {
            Some(existing) if existing != &binding => Err(
                SelectorConstraintModelBuildError::ConflictingTargetBindingProjection {
                    owner,
                    existing: existing.clone(),
                    actual: binding,
                },
            ),
            Some(_) => Ok(()),
            None => {
                self.by_owner.insert(owner, binding);
                Ok(())
            }
        }
    }

    fn binding_variable(&self, owner: SelectorVariableId) -> Option<SelectorVariableId> {
        match self.by_owner.get(&owner) {
            Some(TargetBindingProjection::Var(binding)) => Some(*binding),
            Some(TargetBindingProjection::Const(_)) | None => None,
        }
    }
}

#[derive(Debug, Default)]
struct FactDomains {
    owners: BTreeSet<OwnerId>,
    nodes: BTreeSet<NodeId>,
    strings: BTreeSet<String>,
    ordinals: BTreeSet<StatementOrdinal>,
    owner_kinds: BTreeSet<(OwnerId, String)>,
    owner_statement_ordinals: BTreeSet<(OwnerId, StatementOrdinal)>,
    declared_bindings: BTreeSet<(OwnerId, String)>,
    export_names: BTreeSet<(OwnerId, String)>,
}

impl FactDomains {
    fn from_program_and_facts(program: &SelectorProgram, facts: &SelectorFactStore) -> Self {
        let mut domains = Self::default();
        domains.add_facts(facts);
        domains.add_program_constants(program);
        domains
    }

    fn values_for(&self, domain: VariableDomain) -> Vec<ConstraintValue> {
        match domain {
            VariableDomain::Owner => self
                .owners
                .iter()
                .copied()
                .map(ConstraintValue::Owner)
                .collect(),
            VariableDomain::AstNode => self
                .nodes
                .iter()
                .copied()
                .map(ConstraintValue::AstNode)
                .collect(),
            VariableDomain::String => self
                .strings
                .iter()
                .cloned()
                .map(ConstraintValue::String)
                .collect(),
            VariableDomain::StatementOrdinal => self
                .ordinals
                .iter()
                .copied()
                .map(ConstraintValue::StatementOrdinal)
                .collect(),
        }
    }

    fn add_facts(&mut self, facts: &SelectorFactStore) {
        for fact in &facts.facts {
            match fact {
                SelectorFact::Owner {
                    owner,
                    statement_ordinal,
                    statement_kind,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_ordinal(*statement_ordinal);
                    self.add_string(statement_kind);
                    self.owner_kinds.insert((*owner, statement_kind.clone()));
                    self.owner_statement_ordinals
                        .insert((*owner, *statement_ordinal));
                }
                SelectorFact::DeclaredBinding {
                    owner,
                    binding,
                    export_name,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_string(binding);
                    self.declared_bindings.insert((*owner, binding.clone()));
                    if let Some(export_name) = export_name {
                        self.add_string(export_name);
                        self.export_names.insert((*owner, export_name.clone()));
                    }
                }
                SelectorFact::OwnerReferencesBinding {
                    owner,
                    binding,
                    edge_kind,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_string(binding);
                    self.add_string(edge_kind);
                }
                SelectorFact::AstKind { node, .. }
                | SelectorFact::AstStringLiteral { node, .. }
                | SelectorFact::AstStringWildcard { node, .. }
                | SelectorFact::AstNumberLiteral { node, .. }
                | SelectorFact::AstBoolLiteral { node, .. }
                | SelectorFact::AstIdentifierName { node, .. }
                | SelectorFact::AstPropertyName { node, .. }
                | SelectorFact::AstBareProperty { node, .. }
                | SelectorFact::AstOperator { node, .. }
                | SelectorFact::AstRegexLiteral { node, .. }
                | SelectorFact::AstTopLevel { node, .. } => self.add_node(*node),
                SelectorFact::AstChild { parent, child, .. } => {
                    self.add_node(*parent);
                    self.add_node(*child);
                }
                SelectorFact::AstSuperClass {
                    class_node,
                    super_class,
                    ..
                } => {
                    self.add_node(*class_node);
                    self.add_node(*super_class);
                }
                SelectorFact::MemberRead {
                    statement_ordinal,
                    object,
                    member,
                    ..
                } => {
                    self.add_ordinal(*statement_ordinal);
                    if let Some(object) = object {
                        self.add_string(object);
                    }
                    self.add_string(member);
                }
                SelectorFact::ModuleMemberUse {
                    statement_ordinal,
                    module,
                    member,
                    ..
                } => {
                    self.add_ordinal(*statement_ordinal);
                    self.add_string(module);
                    self.add_string(member);
                }
                SelectorFact::CallArgumentUse {
                    argument,
                    callee_object,
                    callee_member,
                    ..
                } => {
                    self.add_string(argument);
                    if let Some(callee_object) = callee_object {
                        self.add_string(callee_object);
                    }
                    self.add_string(callee_member);
                }
                SelectorFact::DecorateCallUse {
                    callee,
                    class_anchor,
                    member,
                    ..
                } => {
                    self.add_string(callee);
                    self.add_string(class_anchor);
                    if let Some(member) = member {
                        self.add_string(member);
                    }
                }
                SelectorFact::IntrinsicAliasUse {
                    binding, property, ..
                } => {
                    self.add_string(binding);
                    self.add_string(property);
                }
            }

            self.add_fact_strings(fact);
            self.add_fact_ordinals(fact);
        }
    }

    fn add_fact_strings(&mut self, fact: &SelectorFact) {
        match fact {
            SelectorFact::AstStringLiteral { value, .. }
            | SelectorFact::AstStringWildcard { token: value, .. }
            | SelectorFact::AstNumberLiteral { value, .. }
            | SelectorFact::AstIdentifierName { value, .. }
            | SelectorFact::AstPropertyName { value, .. }
            | SelectorFact::AstOperator { value, .. } => self.add_string(value),
            SelectorFact::AstBareProperty {
                key, identifier, ..
            } => {
                self.add_string(key);
                self.add_string(identifier);
            }
            SelectorFact::AstRegexLiteral { pattern, flags, .. } => {
                self.add_string(pattern);
                self.add_string(flags);
            }
            _ => {}
        }
    }

    fn add_fact_ordinals(&mut self, fact: &SelectorFact) {
        if let SelectorFact::AstTopLevel {
            statement_ordinal, ..
        } = fact
        {
            self.add_ordinal(*statement_ordinal);
        }
    }

    fn add_program_constants(&mut self, program: &SelectorProgram) {
        for target in &program.targets {
            match &target.claim {
                ClaimKind::Binding {
                    export_name: Some(export_name),
                }
                | ClaimKind::BindingGroupMember { export_name } => self.add_string(export_name),
                ClaimKind::Binding { export_name: None } | ClaimKind::AnonymousStatement => {}
            }
        }

        for atom in &program.atoms {
            self.add_atom_constants(atom);
        }
    }

    fn add_atom_constants(&mut self, atom: &SelectorAtom) {
        match atom {
            SelectorAtom::OwnerKind {
                owner,
                statement_kind,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(statement_kind);
            }
            SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => {
                self.add_owner_term(owner);
                self.add_ordinal_term(ordinal);
            }
            SelectorAtom::OwnerTopLevelRoot { owner, root } => {
                self.add_owner_term(owner);
                self.add_node_term(root);
            }
            SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
                self.add_owner_term(owner);
                self.add_string_term(binding);
            }
            SelectorAtom::OwnerExportName { owner, export_name } => {
                self.add_owner_term(owner);
                self.add_string_term(export_name);
            }
            SelectorAtom::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(binding);
                if let Some(edge_kind) = edge_kind {
                    self.add_string_term(edge_kind);
                }
            }
            SelectorAtom::OwnerReferencesOwner { owner, referenced }
            | SelectorAtom::OwnerAliasesOwner {
                owner,
                aliased: referenced,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(referenced);
            }
            SelectorAtom::AstKind { node, .. }
            | SelectorAtom::AstChildCount { node, .. }
            | SelectorAtom::AstBoolLiteral { node, .. } => self.add_node_term(node),
            SelectorAtom::AstChild { parent, child, .. } => {
                self.add_node_term(parent);
                self.add_node_term(child);
            }
            SelectorAtom::AstChildListPattern {
                parent, segments, ..
            } => {
                self.add_node_term(parent);
                for segment in segments {
                    for child in segment {
                        self.add_node_term(child);
                    }
                }
            }
            SelectorAtom::AstSuperClass {
                class_node,
                super_class,
            } => {
                self.add_node_term(class_node);
                self.add_node_term(super_class);
            }
            SelectorAtom::AstStringLiteral { node, value }
            | SelectorAtom::AstStringLiteralMatchingRegex {
                node,
                pattern: value,
            }
            | SelectorAtom::AstNumberLiteral { node, value }
            | SelectorAtom::AstIdentifierName { node, value }
            | SelectorAtom::AstPropertyName { node, value }
            | SelectorAtom::AstOperator { node, value } => {
                self.add_node_term(node);
                self.add_string_term(value);
            }
            SelectorAtom::AstBareProperty {
                node,
                key,
                identifier,
                ..
            } => {
                self.add_node_term(node);
                self.add_string_term(key);
                self.add_string_term(identifier);
            }
            SelectorAtom::AstRegexLiteral {
                node,
                pattern,
                flags,
            } => {
                self.add_node_term(node);
                self.add_string_term(pattern);
                self.add_string_term(flags);
            }
            SelectorAtom::AstTopLevel { node, ordinal } => {
                self.add_node_term(node);
                self.add_ordinal_term(ordinal);
            }
            SelectorAtom::OrdinalOffset { base, ordinal, .. }
            | SelectorAtom::OrdinalBefore {
                before: base,
                after: ordinal,
            } => {
                self.add_ordinal_term(base);
                self.add_ordinal_term(ordinal);
            }
            SelectorAtom::ReadsMember {
                owner,
                object,
                member,
            } => {
                self.add_owner_term(owner);
                if let Some(object) = object {
                    self.add_string_term(object);
                }
                self.add_string_term(member);
            }
            SelectorAtom::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(object);
                self.add_string_term(member);
            }
            SelectorAtom::ConsumesModuleMember {
                owner,
                module,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(module);
                self.add_string_term(member);
            }
            SelectorAtom::PassedToCall {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.add_owner_term(owner);
                if let Some(callee_object) = callee_object {
                    self.add_string_term(callee_object);
                }
                self.add_string_term(callee_member);
            }
            SelectorAtom::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(callee_object);
                self.add_string_term(callee_member);
            }
            SelectorAtom::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(class_anchor);
                if let Some(member) = member {
                    self.add_string_term(member);
                }
            }
            SelectorAtom::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(class_anchor);
                if let Some(member) = member {
                    self.add_string_term(member);
                }
            }
            SelectorAtom::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(property);
                self.add_owner_term(referenced_by);
            }
            SelectorAtom::Equal { .. } | SelectorAtom::NotEqual { .. } => {}
        }
    }

    fn add_owner_term(&mut self, term: &OwnerTerm) {
        if let OwnerTerm::Const { owner } = term {
            self.add_owner(*owner);
        }
    }

    fn add_node_term(&mut self, term: &NodeTerm) {
        if let NodeTerm::Const { node } = term {
            self.add_node(*node);
        }
    }

    fn add_string_term(&mut self, term: &StringTerm) {
        if let StringTerm::Const { value } = term {
            self.add_string(value);
        }
    }

    fn add_ordinal_term(&mut self, term: &OrdinalTerm) {
        if let OrdinalTerm::Const { ordinal } = term {
            self.add_ordinal(*ordinal);
        }
    }

    fn add_owner(&mut self, owner: OwnerId) {
        self.owners.insert(owner);
    }

    fn add_node(&mut self, node: NodeId) {
        self.nodes.insert(node);
    }

    fn add_string(&mut self, value: &str) {
        self.strings.insert(value.to_string());
    }

    fn add_ordinal(&mut self, ordinal: StatementOrdinal) {
        self.ordinals.insert(ordinal);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{ChunkId, OwnerId, StatementOrdinal};
    use chunk_facts::NodeKind;
    use selector_constraint_model::{
        AllDifferentConstraint, AllDifferentConstraintId, AllDifferentReason,
        AllowedTupleConstraint, AllowedTupleConstraintId, BinaryConstraint, BinaryConstraintKind,
        ConstraintValue,
    };
    use selector_ir::{ClaimOrigin, SelectorTargetId};

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    fn string(value: &str) -> ConstraintValue {
        ConstraintValue::String(value.to_string())
    }

    fn ordinal(value: usize) -> ConstraintValue {
        ConstraintValue::StatementOrdinal(StatementOrdinal(value))
    }

    fn owner_fact(owner: usize, ordinal: usize, statement_kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: statement_kind.to_string(),
        }
    }

    fn declared_binding(owner: usize, binding: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            binding: binding.to_string(),
            export_name: None,
        }
    }

    fn ast_kind(node: u32, node_kind: NodeKind) -> SelectorFact {
        SelectorFact::AstKind {
            chunk_id: ChunkId(0),
            node,
            node_kind,
        }
    }

    fn fact_store(facts: Vec<SelectorFact>) -> SelectorFactStore {
        SelectorFactStore { facts }
    }

    fn allowed_tuples_for<'a>(
        model: &'a SelectorConstraintModel,
        variables: &[ConstraintVariableId],
    ) -> &'a AllowedTupleConstraint {
        model
            .allowed_tuples
            .iter()
            .find(|constraint| constraint.variables == variables)
            .unwrap()
    }

    #[test]
    fn target_injectivity_preserves_broad_specific_shape() {
        let mut program = SelectorProgram::default();
        let broad_owner = program.add_variable(VariableDomain::Owner, Some("broad".to_string()));
        let strict_owner = program.add_variable(VariableDomain::Owner, Some("strict".to_string()));
        let broad_target = program.add_target(
            ChunkId(0),
            broad_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Broad".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        let strict_target = program.add_target(
            ChunkId(0),
            strict_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Strict".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: broad_owner },
            binding: StringTerm::Const {
                value: "shared".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: strict_owner },
            binding: StringTerm::Const {
                value: "specific".to_string(),
            },
        });
        program.require_all_different(vec![broad_target, strict_target]);

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            owner_fact(20, 1, "var"),
            declared_binding(10, "shared"),
            declared_binding(20, "shared"),
            declared_binding(20, "specific"),
        ]);

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 2);
        assert_eq!(model.target_projections[0].target, broad_target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(model.target_projections[0].binding_variable, None);
        assert_eq!(model.target_projections[1].target, strict_target);
        assert_eq!(
            model.target_projections[1].owner_variable,
            ConstraintVariableId(1)
        );
        assert_eq!(model.target_projections[1].binding_variable, None);
        assert_eq!(model.binary_constraints, Vec::<BinaryConstraint>::new());
        assert_eq!(
            model.all_different,
            vec![AllDifferentConstraint {
                id: AllDifferentConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                reason: AllDifferentReason::TargetInjectivity {
                    targets: vec![SelectorTargetId(0), SelectorTargetId(1)],
                },
            }]
        );

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![owner(10)], vec![owner(20)]],
            }
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(1),
                variables: vec![ConstraintVariableId(1)],
                tuples: vec![vec![owner(20)]],
            }
        );
    }

    #[test]
    fn target_binding_projection_preserves_binding_variable() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner_var,
            "module",
            ClaimKind::Binding { export_name: None },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner_var },
            binding: StringTerm::Var { id: binding_var },
        });

        let facts = fact_store(vec![
            owner_fact(7, 0, "var"),
            owner_fact(8, 1, "var"),
            declared_binding(7, "actual"),
            declared_binding(8, "other"),
        ]);

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 1);
        assert_eq!(model.target_projections[0].target, target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            model.target_projections[0].binding_variable,
            Some(ConstraintVariableId(1))
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                tuples: vec![
                    vec![owner(7), string("actual")],
                    vec![owner(8), string("other")],
                ],
            }
        );
    }

    #[test]
    fn explicit_binary_atoms_stay_binary_constraints() {
        let mut program = SelectorProgram::default();
        let left = program.add_variable(VariableDomain::StatementOrdinal, Some("left".to_string()));
        let right =
            program.add_variable(VariableDomain::StatementOrdinal, Some("right".to_string()));
        program.add_atom(SelectorAtom::OrdinalBefore {
            before: OrdinalTerm::Var { id: left },
            after: OrdinalTerm::Var { id: right },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var"), owner_fact(20, 1, "var")]);

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.allowed_tuples, Vec::<AllowedTupleConstraint>::new());
        assert_eq!(
            model.binary_constraints,
            vec![BinaryConstraint {
                left: ConstraintVariableId(0),
                right: ConstraintVariableId(1),
                kind: BinaryConstraintKind::OrdinalBefore,
            }]
        );
        assert_eq!(model.variables[0].values, vec![ordinal(0), ordinal(1)]);
    }

    #[test]
    fn unsupported_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let root_var = program.add_variable(VariableDomain::AstNode, Some("root".to_string()));
        program.add_atom(SelectorAtom::OwnerTopLevelRoot {
            owner: OwnerTerm::Var { id: owner_var },
            root: NodeTerm::Var { id: root_var },
        });

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            ast_kind(99, NodeKind::FnDecl),
        ]);

        let err = build_selector_constraint_model(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            SelectorConstraintModelBuildError::UnsupportedAtom { .. }
        ));
    }

    #[test]
    fn unsatisfied_constant_only_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Const { owner: OwnerId(10) },
            binding: StringTerm::Const {
                value: "missing".to_string(),
            },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var")]);

        let err = build_selector_constraint_model(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied { .. }
        ));
    }
}
