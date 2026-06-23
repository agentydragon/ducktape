//! Engine-facing selector IR and fact-store contract for the global selector
//! solver.
//!
//! This module gives `source_match` lowering, relational selector lowering, the
//! Ascent solver, and materialization one shared vocabulary.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use analysis::{ChunkId, OwnerId, StatementOrdinal};
use chunk_facts::{ChunkFacts, NodeId, NodeKind};
use serde::{Deserialize, Serialize};

/// Dense id of a solver variable in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SelectorVariableId(pub usize);

/// Dense id of a materializer claim target in a [`SelectorProgram`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SelectorTargetId(pub usize);

/// The relation domain a solver variable ranges over.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VariableDomain {
    Owner,
    AstNode,
    String,
    StatementOrdinal,
}

/// One variable in the global selector constraint program.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorVariable {
    pub id: SelectorVariableId,
    pub domain: VariableDomain,
    /// Optional authoring/debug name, e.g. `@Button` or `needle.return`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debug_name: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum OwnerTerm {
    Var { id: SelectorVariableId },
    Const { owner: OwnerId },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NodeTerm {
    Var { id: SelectorVariableId },
    Const { node: NodeId },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum StringTerm {
    Var { id: SelectorVariableId },
    Const { value: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum OrdinalTerm {
    Var { id: SelectorVariableId },
    Const { ordinal: StatementOrdinal },
}

/// Materializer-facing shape of a target once the solver has selected an owner.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimKind {
    /// A normal exported member claim. The owner should declare `binding`.
    Binding {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        export_name: Option<String>,
    },
    /// An anonymous top-level statement claim. The owner may declare no binding.
    AnonymousStatement,
    /// One target in a `binding_groups[].source_match` selector.
    BindingGroupMember { export_name: String },
}

/// Where a target came from in the spec/lowering pipeline.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimOrigin {
    MemberSelector,
    BindingGroup { group_index: usize },
    AnonymousStatement { index: usize },
    RelationalSelector { relation: RelationalPrimitive },
    Synthetic,
}

/// Current relational primitives that should become solver atoms rather than
/// late materializer bridge passes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelationalPrimitive {
    CrossRef,
    ReadsMember,
    MemberOfModule,
    PassedToCall,
    MakesDecorateCall,
    IntrinsicAlias,
}

/// One claim the materializer will consume after solving.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorTarget {
    pub id: SelectorTargetId,
    pub chunk_id: ChunkId,
    pub owner: SelectorVariableId,
    /// Spec logical module path/key as authored. Later integration can replace
    /// this with the materializer's module id once lowering has that table.
    pub logical_module: String,
    pub claim: ClaimKind,
    pub origin: ClaimOrigin,
}

/// One conjunctive-query atom in the lowered selector program.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SelectorAtom {
    OwnerKind {
        owner: OwnerTerm,
        statement_kind: StringTerm,
    },
    OwnerStatementOrdinal {
        owner: OwnerTerm,
        ordinal: OrdinalTerm,
    },
    OwnerDeclaresBinding {
        owner: OwnerTerm,
        binding: StringTerm,
    },
    OwnerExportName {
        owner: OwnerTerm,
        export_name: StringTerm,
    },
    OwnerReferencesBinding {
        owner: OwnerTerm,
        binding: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        edge_kind: Option<StringTerm>,
    },
    OwnerReferencesOwner {
        owner: OwnerTerm,
        referenced: OwnerTerm,
    },
    OwnerAliasesOwner {
        owner: OwnerTerm,
        aliased: OwnerTerm,
    },
    AstKind {
        node: NodeTerm,
        node_kind: NodeKind,
    },
    AstChild {
        parent: NodeTerm,
        index: u32,
        child: NodeTerm,
    },
    AstChildCount {
        node: NodeTerm,
        count: u32,
    },
    AstStringLiteral {
        node: NodeTerm,
        value: StringTerm,
    },
    AstNumberLiteral {
        node: NodeTerm,
        value: StringTerm,
    },
    AstBoolLiteral {
        node: NodeTerm,
        value: bool,
    },
    AstIdentifierName {
        node: NodeTerm,
        value: StringTerm,
    },
    AstPropertyName {
        node: NodeTerm,
        value: StringTerm,
    },
    AstOperator {
        node: NodeTerm,
        value: StringTerm,
    },
    AstRegexLiteral {
        node: NodeTerm,
        pattern: StringTerm,
        flags: StringTerm,
    },
    AstTopLevel {
        node: NodeTerm,
        ordinal: OrdinalTerm,
    },
    ReadsMember {
        owner: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        object: Option<StringTerm>,
        member: StringTerm,
    },
    ReadsMemberOfOwner {
        owner: OwnerTerm,
        object: OwnerTerm,
        member: StringTerm,
    },
    ConsumesModuleMember {
        owner: OwnerTerm,
        module: StringTerm,
        member: StringTerm,
    },
    PassedToCall {
        owner: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callee_object: Option<StringTerm>,
        callee_member: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        arg_index: Option<u32>,
    },
    PassedToCallOfOwner {
        owner: OwnerTerm,
        callee_object: OwnerTerm,
        callee_member: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        arg_index: Option<u32>,
    },
    MakesDecorateCall {
        owner: OwnerTerm,
        class_anchor: StringTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<StringTerm>,
    },
    MakesDecorateCallForOwner {
        owner: OwnerTerm,
        class_anchor: OwnerTerm,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<StringTerm>,
    },
    IntrinsicAlias {
        owner: OwnerTerm,
        property: StringTerm,
        referenced_by: OwnerTerm,
    },
    SourceMatchCandidate {
        owner: OwnerTerm,
        selector_key: StringTerm,
    },
    Equal {
        left: SelectorVariableId,
        right: SelectorVariableId,
    },
    NotEqual {
        left: SelectorVariableId,
        right: SelectorVariableId,
    },
}

/// Whole lowered selector program for one chunk/component solve.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorProgram {
    pub variables: Vec<SelectorVariable>,
    pub targets: Vec<SelectorTarget>,
    pub atoms: Vec<SelectorAtom>,
    /// Sets of target ids that must land on distinct owners.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub all_different: Vec<Vec<SelectorTargetId>>,
}

impl SelectorProgram {
    pub fn add_variable(
        &mut self,
        domain: VariableDomain,
        debug_name: Option<String>,
    ) -> SelectorVariableId {
        let id = SelectorVariableId(self.variables.len());
        self.variables.push(SelectorVariable {
            id,
            domain,
            debug_name,
        });
        id
    }

    pub fn add_target(
        &mut self,
        chunk_id: ChunkId,
        owner: SelectorVariableId,
        logical_module: impl Into<String>,
        claim: ClaimKind,
        origin: ClaimOrigin,
    ) -> SelectorTargetId {
        let id = SelectorTargetId(self.targets.len());
        self.targets.push(SelectorTarget {
            id,
            chunk_id,
            owner,
            logical_module: logical_module.into(),
            claim,
            origin,
        });
        id
    }

    pub fn add_atom(&mut self, atom: SelectorAtom) {
        self.atoms.push(atom);
    }

    pub fn require_all_different(&mut self, targets: Vec<SelectorTargetId>) {
        self.all_different.push(targets);
    }

    pub fn validate(&self) -> Result<(), SelectorProgramError> {
        for (idx, variable) in self.variables.iter().enumerate() {
            if variable.id != SelectorVariableId(idx) {
                return Err(SelectorProgramError::NonDenseVariable {
                    expected: SelectorVariableId(idx),
                    actual: variable.id,
                });
            }
        }
        for (idx, target) in self.targets.iter().enumerate() {
            if target.id != SelectorTargetId(idx) {
                return Err(SelectorProgramError::NonDenseTarget {
                    expected: SelectorTargetId(idx),
                    actual: target.id,
                });
            }
            self.require_domain(target.owner, VariableDomain::Owner, "selector target owner")?;
        }
        for atom in &self.atoms {
            self.validate_atom(atom)?;
        }
        for target_set in &self.all_different {
            if target_set.len() < 2 {
                return Err(SelectorProgramError::DegenerateAllDifferent);
            }
            for target in target_set {
                self.require_target(*target)?;
            }
        }
        Ok(())
    }

    fn validate_atom(&self, atom: &SelectorAtom) -> Result<(), SelectorProgramError> {
        match atom {
            SelectorAtom::OwnerKind {
                owner,
                statement_kind,
            } => {
                self.validate_owner_term(owner, "owner_kind.owner")?;
                self.validate_string_term(statement_kind, "owner_kind.statement_kind")
            }
            SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => {
                self.validate_owner_term(owner, "owner_statement_ordinal.owner")?;
                self.validate_ordinal_term(ordinal, "owner_statement_ordinal.ordinal")
            }
            SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
                self.validate_owner_term(owner, "owner_declares_binding.owner")?;
                self.validate_string_term(binding, "owner_declares_binding.binding")
            }
            SelectorAtom::OwnerExportName { owner, export_name } => {
                self.validate_owner_term(owner, "owner_export_name.owner")?;
                self.validate_string_term(export_name, "owner_export_name.export_name")
            }
            SelectorAtom::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => {
                self.validate_owner_term(owner, "owner_references_binding.owner")?;
                self.validate_string_term(binding, "owner_references_binding.binding")?;
                if let Some(edge_kind) = edge_kind {
                    self.validate_string_term(edge_kind, "owner_references_binding.edge_kind")?;
                }
                Ok(())
            }
            SelectorAtom::OwnerReferencesOwner { owner, referenced } => {
                self.validate_owner_term(owner, "owner_references_owner.owner")?;
                self.validate_owner_term(referenced, "owner_references_owner.referenced")
            }
            SelectorAtom::OwnerAliasesOwner { owner, aliased } => {
                self.validate_owner_term(owner, "owner_aliases_owner.owner")?;
                self.validate_owner_term(aliased, "owner_aliases_owner.aliased")
            }
            SelectorAtom::AstKind { node, .. } => self.validate_node_term(node, "ast_kind.node"),
            SelectorAtom::AstChild { parent, child, .. } => {
                self.validate_node_term(parent, "ast_child.parent")?;
                self.validate_node_term(child, "ast_child.child")
            }
            SelectorAtom::AstChildCount { node, .. } => {
                self.validate_node_term(node, "ast_child_count.node")
            }
            SelectorAtom::AstStringLiteral { node, value }
            | SelectorAtom::AstNumberLiteral { node, value }
            | SelectorAtom::AstIdentifierName { node, value }
            | SelectorAtom::AstPropertyName { node, value }
            | SelectorAtom::AstOperator { node, value } => {
                self.validate_node_term(node, "ast_label.node")?;
                self.validate_string_term(value, "ast_label.value")
            }
            SelectorAtom::AstBoolLiteral { node, .. } => {
                self.validate_node_term(node, "ast_bool_literal.node")
            }
            SelectorAtom::AstRegexLiteral {
                node,
                pattern,
                flags,
            } => {
                self.validate_node_term(node, "ast_regex_literal.node")?;
                self.validate_string_term(pattern, "ast_regex_literal.pattern")?;
                self.validate_string_term(flags, "ast_regex_literal.flags")
            }
            SelectorAtom::AstTopLevel { node, ordinal } => {
                self.validate_node_term(node, "ast_top_level.node")?;
                self.validate_ordinal_term(ordinal, "ast_top_level.ordinal")
            }
            SelectorAtom::ReadsMember {
                owner,
                object,
                member,
            } => {
                self.validate_owner_term(owner, "reads_member.owner")?;
                if let Some(object) = object {
                    self.validate_string_term(object, "reads_member.object")?;
                }
                self.validate_string_term(member, "reads_member.member")
            }
            SelectorAtom::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => {
                self.validate_owner_term(owner, "reads_member_of_owner.owner")?;
                self.validate_owner_term(object, "reads_member_of_owner.object")?;
                self.validate_string_term(member, "reads_member_of_owner.member")
            }
            SelectorAtom::ConsumesModuleMember {
                owner,
                module,
                member,
            } => {
                self.validate_owner_term(owner, "consumes_module_member.owner")?;
                self.validate_string_term(module, "consumes_module_member.module")?;
                self.validate_string_term(member, "consumes_module_member.member")
            }
            SelectorAtom::PassedToCall {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.validate_owner_term(owner, "passed_to_call.owner")?;
                if let Some(callee_object) = callee_object {
                    self.validate_string_term(callee_object, "passed_to_call.callee_object")?;
                }
                self.validate_string_term(callee_member, "passed_to_call.callee_member")
            }
            SelectorAtom::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.validate_owner_term(owner, "passed_to_call_of_owner.owner")?;
                self.validate_owner_term(callee_object, "passed_to_call_of_owner.callee_object")?;
                self.validate_string_term(callee_member, "passed_to_call_of_owner.callee_member")
            }
            SelectorAtom::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => {
                self.validate_owner_term(owner, "makes_decorate_call.owner")?;
                self.validate_string_term(class_anchor, "makes_decorate_call.class_anchor")?;
                if let Some(member) = member {
                    self.validate_string_term(member, "makes_decorate_call.member")?;
                }
                Ok(())
            }
            SelectorAtom::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => {
                self.validate_owner_term(owner, "makes_decorate_call_for_owner.owner")?;
                self.validate_owner_term(
                    class_anchor,
                    "makes_decorate_call_for_owner.class_anchor",
                )?;
                if let Some(member) = member {
                    self.validate_string_term(member, "makes_decorate_call_for_owner.member")?;
                }
                Ok(())
            }
            SelectorAtom::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => {
                self.validate_owner_term(owner, "intrinsic_alias.owner")?;
                self.validate_string_term(property, "intrinsic_alias.property")?;
                self.validate_owner_term(referenced_by, "intrinsic_alias.referenced_by")
            }
            SelectorAtom::SourceMatchCandidate {
                owner,
                selector_key,
            } => {
                self.validate_owner_term(owner, "source_match_candidate.owner")?;
                self.validate_string_term(selector_key, "source_match_candidate.selector_key")
            }
            SelectorAtom::Equal { left, right } | SelectorAtom::NotEqual { left, right } => {
                let left_domain = self.require_variable(*left, "equality.left")?;
                let right_domain = self.require_variable(*right, "equality.right")?;
                if left_domain != right_domain {
                    return Err(SelectorProgramError::DomainMismatch {
                        context: "equality",
                        expected: left_domain,
                        actual: right_domain,
                    });
                }
                Ok(())
            }
        }
    }

    fn validate_owner_term(
        &self,
        term: &OwnerTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let OwnerTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::Owner, context)?;
        }
        Ok(())
    }

    fn validate_node_term(
        &self,
        term: &NodeTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let NodeTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::AstNode, context)?;
        }
        Ok(())
    }

    fn validate_string_term(
        &self,
        term: &StringTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let StringTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::String, context)?;
        }
        Ok(())
    }

    fn validate_ordinal_term(
        &self,
        term: &OrdinalTerm,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        if let OrdinalTerm::Var { id } = term {
            self.require_domain(*id, VariableDomain::StatementOrdinal, context)?;
        }
        Ok(())
    }

    fn require_domain(
        &self,
        id: SelectorVariableId,
        expected: VariableDomain,
        context: &'static str,
    ) -> Result<(), SelectorProgramError> {
        let actual = self.require_variable(id, context)?;
        if actual != expected {
            return Err(SelectorProgramError::DomainMismatch {
                context,
                expected,
                actual,
            });
        }
        Ok(())
    }

    fn require_variable(
        &self,
        id: SelectorVariableId,
        context: &'static str,
    ) -> Result<VariableDomain, SelectorProgramError> {
        self.variables
            .get(id.0)
            .map(|v| v.domain)
            .ok_or(SelectorProgramError::UnknownVariable { context, id })
    }

    fn require_target(&self, id: SelectorTargetId) -> Result<(), SelectorProgramError> {
        if self.targets.get(id.0).is_none() {
            return Err(SelectorProgramError::UnknownTarget { id });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorProgramError {
    NonDenseVariable {
        expected: SelectorVariableId,
        actual: SelectorVariableId,
    },
    NonDenseTarget {
        expected: SelectorTargetId,
        actual: SelectorTargetId,
    },
    UnknownVariable {
        context: &'static str,
        id: SelectorVariableId,
    },
    UnknownTarget {
        id: SelectorTargetId,
    },
    DomainMismatch {
        context: &'static str,
        expected: VariableDomain,
        actual: VariableDomain,
    },
    DegenerateAllDifferent,
}

impl fmt::Display for SelectorProgramError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonDenseVariable { expected, actual } => {
                write!(
                    f,
                    "selector variable ids must be dense: expected {expected:?}, found {actual:?}"
                )
            }
            Self::NonDenseTarget { expected, actual } => {
                write!(
                    f,
                    "selector target ids must be dense: expected {expected:?}, found {actual:?}"
                )
            }
            Self::UnknownVariable { context, id } => {
                write!(f, "{context} references unknown selector variable {id:?}")
            }
            Self::UnknownTarget { id } => {
                write!(f, "all_different references unknown selector target {id:?}")
            }
            Self::DomainMismatch {
                context,
                expected,
                actual,
            } => {
                write!(
                    f,
                    "{context} expected {expected:?} variable, found {actual:?}"
                )
            }
            Self::DegenerateAllDifferent => {
                write!(f, "all_different requires at least two targets")
            }
        }
    }
}

impl Error for SelectorProgramError {}

/// Solver EDB row. These are the stable serialization boundary between program
/// analysis and the fixed Ascent rule library.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SelectorFact {
    Owner {
        chunk_id: ChunkId,
        owner: OwnerId,
        statement_ordinal: StatementOrdinal,
        statement_kind: String,
    },
    DeclaredBinding {
        chunk_id: ChunkId,
        owner: OwnerId,
        binding: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        export_name: Option<String>,
    },
    OwnerReferencesBinding {
        chunk_id: ChunkId,
        owner: OwnerId,
        binding: String,
        edge_kind: String,
    },
    AstKind {
        chunk_id: ChunkId,
        node: NodeId,
        node_kind: NodeKind,
    },
    AstChild {
        chunk_id: ChunkId,
        parent: NodeId,
        index: u32,
        child: NodeId,
    },
    AstStringLiteral {
        chunk_id: ChunkId,
        node: NodeId,
        value: String,
    },
    AstStringWildcard {
        chunk_id: ChunkId,
        node: NodeId,
        token: String,
    },
    AstNumberLiteral {
        chunk_id: ChunkId,
        node: NodeId,
        value: String,
    },
    AstBoolLiteral {
        chunk_id: ChunkId,
        node: NodeId,
        value: bool,
    },
    AstIdentifierName {
        chunk_id: ChunkId,
        node: NodeId,
        value: String,
    },
    AstPropertyName {
        chunk_id: ChunkId,
        node: NodeId,
        value: String,
    },
    AstOperator {
        chunk_id: ChunkId,
        node: NodeId,
        value: String,
    },
    AstRegexLiteral {
        chunk_id: ChunkId,
        node: NodeId,
        pattern: String,
        flags: String,
    },
    AstSuperClass {
        chunk_id: ChunkId,
        class_node: NodeId,
        super_class: NodeId,
    },
    AstTopLevel {
        chunk_id: ChunkId,
        node: NodeId,
        statement_ordinal: StatementOrdinal,
    },
    MemberRead {
        chunk_id: ChunkId,
        statement_ordinal: StatementOrdinal,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        object: Option<String>,
        member: String,
    },
    ModuleMemberUse {
        chunk_id: ChunkId,
        statement_ordinal: StatementOrdinal,
        module: String,
        member: String,
    },
    CallArgumentUse {
        chunk_id: ChunkId,
        argument: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callee_object: Option<String>,
        callee_member: String,
        arg_index: usize,
    },
    DecorateCallUse {
        chunk_id: ChunkId,
        callee: String,
        class_anchor: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        member: Option<String>,
    },
    IntrinsicAliasUse {
        chunk_id: ChunkId,
        binding: String,
        property: String,
    },
    SourceMatchCandidate {
        chunk_id: ChunkId,
        selector_key: String,
        statement_ordinal: StatementOrdinal,
        binding: String,
    },
}

impl SelectorFact {
    pub fn relation(&self) -> &'static str {
        match self {
            Self::Owner { .. } => "owner",
            Self::DeclaredBinding { .. } => "declared_binding",
            Self::OwnerReferencesBinding { .. } => "owner_references_binding",
            Self::AstKind { .. } => "ast_kind",
            Self::AstChild { .. } => "ast_child",
            Self::AstStringLiteral { .. } => "ast_string_literal",
            Self::AstStringWildcard { .. } => "ast_string_wildcard",
            Self::AstNumberLiteral { .. } => "ast_number_literal",
            Self::AstBoolLiteral { .. } => "ast_bool_literal",
            Self::AstIdentifierName { .. } => "ast_identifier_name",
            Self::AstPropertyName { .. } => "ast_property_name",
            Self::AstOperator { .. } => "ast_operator",
            Self::AstRegexLiteral { .. } => "ast_regex_literal",
            Self::AstSuperClass { .. } => "ast_super_class",
            Self::AstTopLevel { .. } => "ast_top_level",
            Self::MemberRead { .. } => "member_read",
            Self::ModuleMemberUse { .. } => "module_member_use",
            Self::CallArgumentUse { .. } => "call_argument_use",
            Self::DecorateCallUse { .. } => "decorate_call_use",
            Self::IntrinsicAliasUse { .. } => "intrinsic_alias_use",
            Self::SourceMatchCandidate { .. } => "source_match_candidate",
        }
    }
}

/// Append-only EDB relation store for one solver invocation.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelectorFactStore {
    pub facts: Vec<SelectorFact>,
}

impl SelectorFactStore {
    pub fn push(&mut self, fact: SelectorFact) {
        self.facts.push(fact);
    }

    pub fn len(&self) -> usize {
        self.facts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.facts.is_empty()
    }

    pub fn counts_by_relation(&self) -> BTreeMap<&'static str, usize> {
        let mut counts = BTreeMap::new();
        for fact in &self.facts {
            *counts.entry(fact.relation()).or_insert(0) += 1;
        }
        counts
    }

    /// Import AST facts already extracted by `chunk_facts`. Bridge/use-site facts
    /// and owner-graph facts are pushed separately because they need joins with
    /// imports, owner ids, or materializer-specific binding metadata.
    pub fn extend_chunk_facts(&mut self, chunk_id: ChunkId, facts: &ChunkFacts) {
        self.facts.extend(
            facts
                .node_kind
                .iter()
                .map(|(node, node_kind)| SelectorFact::AstKind {
                    chunk_id,
                    node: *node,
                    node_kind: *node_kind,
                }),
        );
        self.facts
            .extend(
                facts
                    .child
                    .iter()
                    .map(|(parent, index, child)| SelectorFact::AstChild {
                        chunk_id,
                        parent: *parent,
                        index: *index,
                        child: *child,
                    }),
            );
        self.facts.extend(facts.str_lit.iter().map(|(node, value)| {
            SelectorFact::AstStringLiteral {
                chunk_id,
                node: *node,
                value: value.clone(),
            }
        }));
        self.facts
            .extend(facts.str_wildcard.iter().map(|(node, token)| {
                SelectorFact::AstStringWildcard {
                    chunk_id,
                    node: *node,
                    token: token.clone(),
                }
            }));
        self.facts.extend(facts.num_lit.iter().map(|(node, value)| {
            SelectorFact::AstNumberLiteral {
                chunk_id,
                node: *node,
                value: value.clone(),
            }
        }));
        self.facts
            .extend(
                facts
                    .bool_lit
                    .iter()
                    .map(|(node, value)| SelectorFact::AstBoolLiteral {
                        chunk_id,
                        node: *node,
                        value: *value,
                    }),
            );
        self.facts
            .extend(
                facts
                    .ident_name
                    .iter()
                    .map(|(node, value)| SelectorFact::AstIdentifierName {
                        chunk_id,
                        node: *node,
                        value: value.clone(),
                    }),
            );
        self.facts
            .extend(
                facts
                    .prop_name
                    .iter()
                    .map(|(node, value)| SelectorFact::AstPropertyName {
                        chunk_id,
                        node: *node,
                        value: value.clone(),
                    }),
            );
        self.facts.extend(
            facts
                .operator
                .iter()
                .map(|(node, value)| SelectorFact::AstOperator {
                    chunk_id,
                    node: *node,
                    value: value.clone(),
                }),
        );
        self.facts
            .extend(facts.regex.iter().map(|(node, pattern, flags)| {
                SelectorFact::AstRegexLiteral {
                    chunk_id,
                    node: *node,
                    pattern: pattern.clone(),
                    flags: flags.clone(),
                }
            }));
        self.facts
            .extend(facts.super_class.iter().map(|(class_node, super_class)| {
                SelectorFact::AstSuperClass {
                    chunk_id,
                    class_node: *class_node,
                    super_class: *super_class,
                }
            }));
        self.facts
            .extend(facts.top_level.iter().map(|(node, statement_ordinal)| {
                SelectorFact::AstTopLevel {
                    chunk_id,
                    node: *node,
                    statement_ordinal: StatementOrdinal(*statement_ordinal),
                }
            }));
    }
}

/// Solver output projected to materializer targets.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SolverResult {
    pub claims: Vec<SolverClaim>,
}

impl SolverResult {
    pub fn outcome_for(&self, target: SelectorTargetId) -> Option<&ClaimOutcome> {
        self.claims
            .iter()
            .find(|claim| claim.target == target)
            .map(|claim| &claim.outcome)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SolverClaim {
    pub target: SelectorTargetId,
    pub outcome: ClaimOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimOutcome {
    Unique {
        claim: ResolvedClaim,
    },
    NoMatch,
    Ambiguous {
        candidates: Vec<ResolvedClaim>,
    },
    Duplicate {
        owner: OwnerId,
        conflicting_targets: Vec<SelectorTargetId>,
    },
    Unsupported {
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedClaim {
    pub chunk_id: ChunkId,
    pub owner: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub provenance: Vec<ProvenanceFact>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProvenanceFact {
    pub relation: String,
    pub summary: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn const_str(value: &str) -> StringTerm {
        StringTerm::Const {
            value: value.to_string(),
        }
    }

    #[test]
    fn validates_owner_target_and_atom_domains() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("@Widget".to_string()));
        let node = program.add_variable(VariableDomain::AstNode, Some("needle.root".to_string()));
        let member = program.add_target(
            ChunkId(0),
            owner,
            "runtime/widgets",
            ClaimKind::Binding {
                export_name: Some("Widget".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: const_str("a"),
        });
        program.add_atom(SelectorAtom::AstKind {
            node: NodeTerm::Var { id: node },
            node_kind: NodeKind::FnDecl,
        });
        program.require_all_different(vec![member, member]);

        assert_eq!(program.validate(), Ok(()));
    }

    #[test]
    fn rejects_non_owner_target_variable() {
        let mut program = SelectorProgram::default();
        let node = program.add_variable(VariableDomain::AstNode, None);
        program.add_target(
            ChunkId(0),
            node,
            "runtime/widgets",
            ClaimKind::AnonymousStatement,
            ClaimOrigin::AnonymousStatement { index: 0 },
        );

        assert_eq!(
            program.validate(),
            Err(SelectorProgramError::DomainMismatch {
                context: "selector target owner",
                expected: VariableDomain::Owner,
                actual: VariableDomain::AstNode,
            })
        );
    }

    #[test]
    fn imports_chunk_facts_into_contract_rows() {
        let mut chunk = ChunkFacts::default();
        chunk.node_kind.push((1, NodeKind::FnDecl));
        chunk.child.push((1, 0, 2));
        chunk.ident_name.push((2, "helper".to_string()));
        chunk.str_lit.push((3, "stable".to_string()));
        chunk.top_level.push((1, 4));

        let mut store = SelectorFactStore::default();
        store.extend_chunk_facts(ChunkId(7), &chunk);

        let counts = store.counts_by_relation();
        assert_eq!(counts["ast_kind"], 1);
        assert_eq!(counts["ast_child"], 1);
        assert_eq!(counts["ast_identifier_name"], 1);
        assert_eq!(counts["ast_string_literal"], 1);
        assert_eq!(counts["ast_top_level"], 1);
        assert!(store.facts.contains(&SelectorFact::AstTopLevel {
            chunk_id: ChunkId(7),
            node: 1,
            statement_ordinal: StatementOrdinal(4),
        }));
    }

    #[test]
    fn serde_round_trips_program_and_result() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("@Class".to_string()));
        let target = program.add_target(
            ChunkId(2),
            owner,
            "runtime/classes",
            ClaimKind::Binding {
                export_name: Some("Class".to_string()),
            },
            ClaimOrigin::RelationalSelector {
                relation: RelationalPrimitive::MakesDecorateCall,
            },
        );
        program.add_atom(SelectorAtom::MakesDecorateCall {
            owner: OwnerTerm::Var { id: owner },
            class_anchor: const_str("C"),
            member: Some(const_str("ready")),
        });
        program.validate().unwrap();

        let json = serde_json::to_string(&program).unwrap();
        let decoded: SelectorProgram = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded, program);

        let result = SolverResult {
            claims: vec![SolverClaim {
                target,
                outcome: ClaimOutcome::Unique {
                    claim: ResolvedClaim {
                        chunk_id: ChunkId(2),
                        owner: OwnerId(9),
                        statement_ordinal: StatementOrdinal(9),
                        binding: Some("a".to_string()),
                        provenance: vec![ProvenanceFact {
                            relation: "makes_decorate_call".to_string(),
                            summary: "decorates C.ready".to_string(),
                        }],
                    },
                },
            }],
        };
        let json = serde_json::to_string(&result).unwrap();
        let decoded: SolverResult = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.outcome_for(target), result.outcome_for(target));
    }
}
