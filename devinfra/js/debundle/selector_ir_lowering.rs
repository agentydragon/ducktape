//! Lower existing debundle selector specs into the global selector IR.
//!
//! Binding selectors, `source_match` selectors, and the current relational
//! selector primitives lower into one joint program.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{ChunkId, StatementKind};
use chunk_facts::{ChunkFacts, NodeId, NodeKind};
use selector_ir::{
    ClaimKind, ClaimOrigin, NodeTerm, OrdinalTerm, OwnerTerm, RelationalPrimitive, SelectorAtom,
    SelectorProgram, SelectorTargetId, SelectorVariableId, StringTerm, VariableDomain,
};
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, ARRAY_ELEMENTS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD,
    CLASS_REST_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD,
    OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
    STRING_LITERAL_REGEX_PREDICATE, hole_name_for, labeled_hole_name_for,
};
use spec::{
    AnonymousStatementSelector, BindingSelector, BindingSourceKind, CrossRefRelation,
    MemberSelectorSpec, SourceMatchIdentifierMode,
};

/// Context shared by every member selector lowered for one logical module.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemberSelectorLoweringContext {
    pub chunk_id: ChunkId,
    pub logical_module: String,
}

impl MemberSelectorLoweringContext {
    pub fn new(chunk_id: ChunkId, logical_module: impl Into<String>) -> Self {
        Self {
            chunk_id,
            logical_module: logical_module.into(),
        }
    }
}

/// Lower one `members[]` selector into a standalone selector program fragment.
pub fn lower_member_selector(
    context: &MemberSelectorLoweringContext,
    export_name: &str,
    selector: &MemberSelectorSpec,
) -> Result<LoweredMemberSelector, SelectorIrLoweringError> {
    let mut builder = MemberSelectorProgramBuilder::new(context.clone());
    let target = builder.lower_member_selector(export_name, selector)?;
    let program = builder.into_program()?;
    Ok(LoweredMemberSelector { target, program })
}

/// Incremental builder for one joint selector program. Targets are scoped by
/// logical module plus export name, while relation anchors resolve against the
/// target set using the selector family's scoping rules.
#[derive(Debug, Clone)]
pub struct MemberSelectorProgramBuilder {
    context: MemberSelectorLoweringContext,
    program: SelectorProgram,
    owners_by_export: BTreeMap<(String, String), SelectorVariableId>,
    global_owner_by_export: BTreeMap<String, Option<SelectorVariableId>>,
    targeted_owners: BTreeMap<SelectorVariableId, String>,
}

impl MemberSelectorProgramBuilder {
    pub fn new(context: MemberSelectorLoweringContext) -> Self {
        Self {
            context,
            program: SelectorProgram::default(),
            owners_by_export: BTreeMap::new(),
            global_owner_by_export: BTreeMap::new(),
            targeted_owners: BTreeMap::new(),
        }
    }

    pub fn lower_member_selector(
        &mut self,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = self.context.logical_module.clone();
        self.lower_member_selector_in_module(logical_module, export_name, selector)
    }

    pub fn lower_member_selector_in_module(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = logical_module.into();
        let target =
            self.declare_member_target_in_module(logical_module.clone(), export_name, selector)?;
        self.lower_member_constraints_in_module(&logical_module, export_name, selector)?;
        Ok(target)
    }

    pub fn declare_member_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = logical_module.into();
        let owner = self.owner_for_local_export(&logical_module, export_name);
        self.targeted_owners
            .insert(owner, format!("{logical_module}::{export_name}"));
        self.global_owner_by_export
            .entry(export_name.to_string())
            .and_modify(|slot| {
                if *slot != Some(owner) {
                    *slot = None;
                }
            })
            .or_insert(Some(owner));
        Ok(self.program.add_target(
            self.context.chunk_id,
            owner,
            logical_module.clone(),
            ClaimKind::Binding {
                export_name: Some(export_name.to_string()),
            },
            selector_origin(selector),
        ))
    }

    pub fn lower_member_constraints_in_module(
        &mut self,
        logical_module: &str,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<(), SelectorIrLoweringError> {
        let owner = self.owner_for_local_export(logical_module, export_name);
        self.lower_selector_atoms(logical_module, owner, selector)
    }

    pub fn into_program(self) -> Result<SelectorProgram, SelectorIrLoweringError> {
        for ((logical_module, export_name), owner) in &self.owners_by_export {
            if !self.targeted_owners.contains_key(owner) {
                return Err(SelectorIrLoweringError::DanglingAnchor {
                    logical_module: logical_module.clone(),
                    export_name: export_name.clone(),
                });
            }
        }
        self.program.validate()?;
        Ok(self.program)
    }

    fn owner_for_local_export(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(owner) = self.owners_by_export.get(&key) {
            return *owner;
        }
        let owner = self.program.add_variable(
            VariableDomain::Owner,
            Some(format!("{logical_module}::@{export_name}")),
        );
        self.owners_by_export.insert(key, owner);
        owner
    }

    fn owner_for_global_export(
        &mut self,
        export_name: &str,
    ) -> Result<SelectorVariableId, SelectorIrLoweringError> {
        match self
            .global_owner_by_export
            .get(export_name)
            .copied()
            .flatten()
        {
            Some(owner) => Ok(owner),
            None if self.global_owner_by_export.contains_key(export_name) => {
                Err(SelectorIrLoweringError::AmbiguousAnchor {
                    export_name: export_name.to_string(),
                })
            }
            None => {
                let owner = self
                    .program
                    .add_variable(VariableDomain::Owner, Some(format!("@{export_name}")));
                self.owners_by_export
                    .insert(("<global>".to_string(), export_name.to_string()), owner);
                Ok(owner)
            }
        }
    }

    fn lower_selector_atoms(
        &mut self,
        logical_module: &str,
        owner: SelectorVariableId,
        selector: &MemberSelectorSpec,
    ) -> Result<(), SelectorIrLoweringError> {
        match selector {
            MemberSelectorSpec::Binding(binding) => self.lower_binding_selector(owner, binding),
            MemberSelectorSpec::SourceMatch(selector) => {
                if !self.try_lower_native_exact_source_match(logical_module, owner, selector)? {
                    self.program.add_atom(SelectorAtom::SourceMatchCandidate {
                        owner: owner_term(owner),
                        selector_key: const_str(&source_match::selector_key(selector)),
                    });
                }
                Ok(())
            }
            MemberSelectorSpec::CrossRef(target) => {
                let anchor = self.owner_for_global_export(&target.anchor)?;
                match target.relation {
                    CrossRefRelation::References => {
                        self.program.add_atom(SelectorAtom::OwnerReferencesOwner {
                            owner: owner_term(owner),
                            referenced: owner_term(anchor),
                        });
                    }
                    CrossRefRelation::Aliases => {
                        self.program.add_atom(SelectorAtom::OwnerAliasesOwner {
                            owner: owner_term(owner),
                            aliased: owner_term(anchor),
                        });
                    }
                }
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpec::ReadsMember(target) => {
                if let Some(object) = &target.object {
                    let object = self.owner_for_global_export(object)?;
                    self.program.add_atom(SelectorAtom::ReadsMemberOfOwner {
                        owner: owner_term(owner),
                        object: owner_term(object),
                        member: const_str(&target.member),
                    });
                } else {
                    self.program.add_atom(SelectorAtom::ReadsMember {
                        owner: owner_term(owner),
                        object: None,
                        member: const_str(&target.member),
                    });
                }
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpec::MemberOfModule(target) => {
                self.program.add_atom(SelectorAtom::ConsumesModuleMember {
                    owner: owner_term(owner),
                    module: const_str(&target.module),
                    member: const_str(&target.member),
                });
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpec::PassedToCall(target) => {
                let arg_index = optional_index(target.arg_index)?;
                if let Some(object) = &target.object {
                    let object = self.owner_for_global_export(object)?;
                    self.program.add_atom(SelectorAtom::PassedToCallOfOwner {
                        owner: owner_term(owner),
                        callee_object: owner_term(object),
                        callee_member: const_str(&target.callee_member),
                        arg_index,
                    });
                } else {
                    self.program.add_atom(SelectorAtom::PassedToCall {
                        owner: owner_term(owner),
                        callee_object: None,
                        callee_member: const_str(&target.callee_member),
                        arg_index,
                    });
                }
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpec::MakesDecorateCall(target) => {
                let class_anchor = self.owner_for_global_export(&target.class)?;
                self.program
                    .add_atom(SelectorAtom::MakesDecorateCallForOwner {
                        owner: owner_term(owner),
                        class_anchor: owner_term(class_anchor),
                        member: target.member.as_deref().map(const_str),
                    });
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpec::IntrinsicAlias(target) => {
                let referenced_by =
                    self.owner_for_local_export(logical_module, &target.referenced_by);
                self.program.add_atom(SelectorAtom::IntrinsicAlias {
                    owner: owner_term(owner),
                    property: const_str(&target.property),
                    referenced_by: owner_term(referenced_by),
                });
                Ok(())
            }
        }
    }

    fn lower_binding_selector(
        &mut self,
        owner: SelectorVariableId,
        selector: &BindingSelector,
    ) -> Result<(), SelectorIrLoweringError> {
        if selector.kind == Some(BindingSourceKind::ImportSpecifier) {
            return Err(SelectorIrLoweringError::unsupported(
                "binding",
                "import_specifier binding selectors need import-owner fact modeling",
            ));
        }

        self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: const_str(&selector.name),
        });
        self.add_kind_atom(owner, selector.kind);
        Ok(())
    }

    fn try_lower_native_exact_source_match(
        &mut self,
        logical_module: &str,
        owner: SelectorVariableId,
        selector: &AnonymousStatementSelector,
    ) -> Result<bool, SelectorIrLoweringError> {
        if selector.identifiers != SourceMatchIdentifierMode::Exact
            || selector.target_statement.is_some()
            || selector.target_statements.is_some()
        {
            return Ok(false);
        }
        let Ok(parsed) = js_ast::with_swc_globals(|| {
            js_ast::parse_js_module_ast(
                &format!("<selector ir source_match in {logical_module}>"),
                &selector.match_source,
            )
        }) else {
            return Ok(false);
        };
        if parsed.body.len() != 1 {
            return Ok(false);
        }
        let Ok(facts) =
            chunk_facts::extract_facts_needle(&parsed.body, &selector.wildcard_string_literals)
        else {
            return Ok(false);
        };
        let Some(hole_roots) = native_single_node_hole_roots(&facts) else {
            return Ok(false);
        };
        let skipped_nodes = native_hole_subtree_nodes(&facts, &hole_roots);
        let [(root_node, _ordinal)] = facts.top_level.as_slice() else {
            return Ok(false);
        };

        let ordinal = self.program.add_variable(
            VariableDomain::StatementOrdinal,
            Some(format!("{logical_module}::source_match.ordinal")),
        );
        let mut node_vars = BTreeMap::<NodeId, SelectorVariableId>::new();
        for (node, _kind) in &facts.node_kind {
            node_vars.insert(
                *node,
                self.program.add_variable(
                    VariableDomain::AstNode,
                    Some(format!("{logical_module}::source_match.node{node}")),
                ),
            );
        }
        let Some(root) = node_vars.get(root_node).copied() else {
            return Ok(false);
        };
        self.program.add_atom(SelectorAtom::OwnerStatementOrdinal {
            owner: owner_term(owner),
            ordinal: ordinal_term(ordinal),
        });
        self.program.add_atom(SelectorAtom::AstTopLevel {
            node: node_term(root),
            ordinal: ordinal_term(ordinal),
        });
        if let Some(target_binding) = &selector.target_binding {
            self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                owner: owner_term(owner),
                binding: const_str(target_binding),
            });
        }
        self.lower_native_ast_facts(logical_module, &facts, &node_vars, &skipped_nodes);
        Ok(true)
    }

    fn lower_native_ast_facts(
        &mut self,
        logical_module: &str,
        facts: &ChunkFacts,
        node_vars: &BTreeMap<NodeId, SelectorVariableId>,
        skipped_nodes: &BTreeSet<NodeId>,
    ) {
        let mut wildcard_string_vars = BTreeMap::<String, SelectorVariableId>::new();
        for (_node, token) in &facts.str_wildcard {
            if skipped_nodes.contains(_node) {
                continue;
            }
            wildcard_string_vars
                .entry(token.clone())
                .or_insert_with(|| {
                    self.program.add_variable(
                        VariableDomain::String,
                        Some(format!("{logical_module}::source_match.string.{token}")),
                    )
                });
        }
        let wildcard_string_by_node: BTreeMap<NodeId, SelectorVariableId> = facts
            .str_wildcard
            .iter()
            .filter(|(node, _token)| !skipped_nodes.contains(node))
            .filter_map(|(node, token)| wildcard_string_vars.get(token).map(|var| (*node, *var)))
            .collect();
        let mut child_counts: BTreeMap<NodeId, u32> =
            facts.node_kind.iter().map(|(node, _)| (*node, 0)).collect();
        for (parent, _index, _child) in &facts.child {
            *child_counts.entry(*parent).or_insert(0) += 1;
        }
        for (node, kind) in &facts.node_kind {
            if skipped_nodes.contains(node) {
                continue;
            }
            let Some(node_var) = node_vars.get(node).copied() else {
                continue;
            };
            self.program.add_atom(SelectorAtom::AstKind {
                node: node_term(node_var),
                node_kind: *kind,
            });
            self.program.add_atom(SelectorAtom::AstChildCount {
                node: node_term(node_var),
                count: child_counts.get(node).copied().unwrap_or(0),
            });
        }
        for (parent, index, child) in &facts.child {
            if skipped_nodes.contains(parent) {
                continue;
            }
            let (Some(parent), Some(child)) = (node_vars.get(parent), node_vars.get(child)) else {
                continue;
            };
            self.program.add_atom(SelectorAtom::AstChild {
                parent: node_term(*parent),
                index: *index,
                child: node_term(*child),
            });
        }
        for (class_node, super_class) in &facts.super_class {
            if skipped_nodes.contains(class_node) {
                continue;
            }
            let (Some(class_node), Some(super_class)) =
                (node_vars.get(class_node), node_vars.get(super_class))
            else {
                continue;
            };
            self.program.add_atom(SelectorAtom::AstSuperClass {
                class_node: node_term(*class_node),
                super_class: node_term(*super_class),
            });
        }
        for (node, value) in &facts.str_lit {
            if skipped_nodes.contains(node) {
                continue;
            }
            if let Some(wildcard_string_var) = wildcard_string_by_node.get(node).copied() {
                if let Some(node) = node_vars.get(node).copied() {
                    self.program.add_atom(SelectorAtom::AstStringLiteral {
                        node: node_term(node),
                        value: string_term(wildcard_string_var),
                    });
                }
            } else {
                self.add_ast_string_label(node_vars, *node, value, |node, value| {
                    SelectorAtom::AstStringLiteral { node, value }
                });
            }
        }
        for (node, value) in &facts.num_lit {
            if skipped_nodes.contains(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstNumberLiteral { node, value }
            });
        }
        for (node, value) in &facts.bool_lit {
            if skipped_nodes.contains(node) {
                continue;
            }
            if let Some(node) = node_vars.get(node).copied() {
                self.program.add_atom(SelectorAtom::AstBoolLiteral {
                    node: node_term(node),
                    value: *value,
                });
            }
        }
        for (node, value) in &facts.ident_name {
            if skipped_nodes.contains(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstIdentifierName { node, value }
            });
        }
        for (node, value) in &facts.prop_name {
            if skipped_nodes.contains(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstPropertyName { node, value }
            });
        }
        for (node, value) in &facts.operator {
            if skipped_nodes.contains(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstOperator { node, value }
            });
        }
        for (node, pattern, flags) in &facts.regex {
            if skipped_nodes.contains(node) {
                continue;
            }
            if let Some(node) = node_vars.get(node).copied() {
                self.program.add_atom(SelectorAtom::AstRegexLiteral {
                    node: node_term(node),
                    pattern: const_str(pattern),
                    flags: const_str(flags),
                });
            }
        }
    }

    fn add_ast_string_label(
        &mut self,
        node_vars: &BTreeMap<NodeId, SelectorVariableId>,
        node: NodeId,
        value: &str,
        make_atom: fn(NodeTerm, StringTerm) -> SelectorAtom,
    ) {
        if let Some(node) = node_vars.get(&node).copied() {
            self.program
                .add_atom(make_atom(node_term(node), const_str(value)));
        }
    }

    fn add_kind_atom(&mut self, owner: SelectorVariableId, kind: Option<BindingSourceKind>) {
        if let Some(kind) = kind {
            self.program.add_atom(SelectorAtom::OwnerKind {
                owner: owner_term(owner),
                statement_kind: const_str(statement_kind_str_for_spec(kind)),
            });
        }
    }
}

fn owner_term(owner: SelectorVariableId) -> OwnerTerm {
    OwnerTerm::Var { id: owner }
}

fn node_term(node: SelectorVariableId) -> NodeTerm {
    NodeTerm::Var { id: node }
}

fn ordinal_term(ordinal: SelectorVariableId) -> OrdinalTerm {
    OrdinalTerm::Var { id: ordinal }
}

fn string_term(string: SelectorVariableId) -> StringTerm {
    StringTerm::Var { id: string }
}

fn const_str(value: &str) -> StringTerm {
    StringTerm::Const {
        value: value.to_string(),
    }
}

fn optional_index(index: Option<usize>) -> Result<Option<u32>, SelectorIrLoweringError> {
    index
        .map(|index| {
            u32::try_from(index).map_err(|_| SelectorIrLoweringError::Unsupported {
                selector_kind: "passed_to_call",
                reason: "arg_index exceeds solver u32 range",
            })
        })
        .transpose()
}

fn selector_hole_name(name: &str) -> bool {
    name == STRING_LITERAL_REGEX_PREDICATE
        || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        || hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
        || hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
        || [
            STMT_LIST_HOLE_KEYWORD,
            CLASS_REST_HOLE_KEYWORD,
            CASE_REST_HOLE_KEYWORD,
            DECLARATORS_HOLE_KEYWORD,
            ARGS_HOLE_KEYWORD,
            OBJECT_PROPS_HOLE_KEYWORD,
            ARRAY_ELEMENTS_HOLE_KEYWORD,
        ]
        .iter()
        .any(|keyword| labeled_hole_name_for(name, keyword).is_some())
}

fn native_single_node_hole_roots(facts: &ChunkFacts) -> Option<BTreeSet<NodeId>> {
    let node_kind: BTreeMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let mut parent_by_child = BTreeMap::<NodeId, (NodeId, u32)>::new();
    let mut child_counts = BTreeMap::<NodeId, u32>::new();
    for (parent, index, child) in &facts.child {
        parent_by_child.insert(*child, (*parent, *index));
        *child_counts.entry(*parent).or_insert(0) += 1;
    }

    let mut roots = BTreeSet::new();
    for (node, name) in &facts.ident_name {
        match classify_native_single_node_hole(
            *node,
            name,
            &node_kind,
            &parent_by_child,
            &child_counts,
        )? {
            HoleClassification::NotHole => {}
            HoleClassification::Supported { root } => {
                roots.insert(root);
            }
        }
    }
    for (_node, name) in &facts.prop_name {
        if selector_hole_name(name) {
            return None;
        }
    }
    Some(roots)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HoleClassification {
    NotHole,
    Supported { root: NodeId },
}

fn classify_native_single_node_hole(
    node: NodeId,
    name: &str,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    parent_by_child: &BTreeMap<NodeId, (NodeId, u32)>,
    child_counts: &BTreeMap<NodeId, u32>,
) -> Option<HoleClassification> {
    let kind = node_kind.get(&node).copied();
    if kind == Some(NodeKind::Ident)
        && (hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
            || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
        && let Some(parent) =
            expr_stmt_carrier_parent(node, node_kind, parent_by_child, child_counts)
    {
        return Some(HoleClassification::Supported { root: parent });
    }

    if kind == Some(NodeKind::Ident)
        && (hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
            || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
    {
        return Some(HoleClassification::Supported { root: node });
    }

    if selector_hole_name(name) {
        return None;
    }
    Some(HoleClassification::NotHole)
}

fn expr_stmt_carrier_parent(
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    parent_by_child: &BTreeMap<NodeId, (NodeId, u32)>,
    child_counts: &BTreeMap<NodeId, u32>,
) -> Option<NodeId> {
    let (parent, index) = parent_by_child.get(&node).copied()?;
    if index == 0
        && node_kind.get(&parent) == Some(&NodeKind::ExprStmt)
        && child_counts.get(&parent).copied().unwrap_or(0) == 1
    {
        Some(parent)
    } else {
        None
    }
}

fn native_hole_subtree_nodes(
    facts: &ChunkFacts,
    hole_roots: &BTreeSet<NodeId>,
) -> BTreeSet<NodeId> {
    let mut children_by_parent = BTreeMap::<NodeId, Vec<NodeId>>::new();
    for (parent, _index, child) in &facts.child {
        children_by_parent.entry(*parent).or_default().push(*child);
    }

    let mut skipped = hole_roots.clone();
    let mut stack: Vec<NodeId> = hole_roots.iter().copied().collect();
    while let Some(node) = stack.pop() {
        if let Some(children) = children_by_parent.get(&node) {
            for child in children {
                if skipped.insert(*child) {
                    stack.push(*child);
                }
            }
        }
    }
    skipped
}

fn selector_origin(selector: &MemberSelectorSpec) -> ClaimOrigin {
    match relation_for_selector(selector) {
        Some(relation) => ClaimOrigin::RelationalSelector { relation },
        None => ClaimOrigin::MemberSelector,
    }
}

fn statement_kind_str_for_spec(kind: BindingSourceKind) -> &'static str {
    let statement_kind = match kind {
        BindingSourceKind::VariableDeclarator => StatementKind::VarDecl,
        BindingSourceKind::FunctionDeclaration => StatementKind::FnDecl,
        BindingSourceKind::ClassDeclaration => StatementKind::ClassDecl,
        BindingSourceKind::ImportSpecifier => StatementKind::Import,
    };
    statement_kind.into()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoweredMemberSelector {
    pub target: SelectorTargetId,
    pub program: SelectorProgram,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorIrLoweringError {
    Unsupported {
        selector_kind: &'static str,
        reason: &'static str,
    },
    DanglingAnchor {
        logical_module: String,
        export_name: String,
    },
    AmbiguousAnchor {
        export_name: String,
    },
    InvalidProgram(selector_ir::SelectorProgramError),
}

impl SelectorIrLoweringError {
    fn unsupported(selector_kind: &'static str, reason: &'static str) -> Self {
        Self::Unsupported {
            selector_kind,
            reason,
        }
    }
}

impl fmt::Display for SelectorIrLoweringError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported {
                selector_kind,
                reason,
            } => write!(
                f,
                "unsupported selector IR lowering for {selector_kind}: {reason}"
            ),
            Self::DanglingAnchor {
                logical_module,
                export_name,
            } => write!(
                f,
                "cross_ref/reads_member/passed_to_call/makes_decorate_call/intrinsic_alias \
                 selector anchor `@{export_name}` does not name a lowered member in module \
                 {logical_module}"
            ),
            Self::AmbiguousAnchor { export_name } => write!(
                f,
                "cross_ref/reads_member/passed_to_call/makes_decorate_call/intrinsic_alias \
                 selector anchor `@{export_name}` is ambiguous across lowered members"
            ),
            Self::InvalidProgram(error) => write!(f, "invalid selector IR program: {error}"),
        }
    }
}

impl Error for SelectorIrLoweringError {}

impl From<selector_ir::SelectorProgramError> for SelectorIrLoweringError {
    fn from(error: selector_ir::SelectorProgramError) -> Self {
        Self::InvalidProgram(error)
    }
}

fn relation_for_selector(selector: &MemberSelectorSpec) -> Option<RelationalPrimitive> {
    match selector {
        MemberSelectorSpec::CrossRef(_) => Some(RelationalPrimitive::CrossRef),
        MemberSelectorSpec::ReadsMember(_) => Some(RelationalPrimitive::ReadsMember),
        MemberSelectorSpec::MemberOfModule(_) => Some(RelationalPrimitive::MemberOfModule),
        MemberSelectorSpec::PassedToCall(_) => Some(RelationalPrimitive::PassedToCall),
        MemberSelectorSpec::MakesDecorateCall(_) => Some(RelationalPrimitive::MakesDecorateCall),
        MemberSelectorSpec::IntrinsicAlias(_) => Some(RelationalPrimitive::IntrinsicAlias),
        MemberSelectorSpec::Binding(_) | MemberSelectorSpec::SourceMatch(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use selector_ir::{SelectorAtom, StringTerm};
    use spec::{AnonymousStatementSelector, CrossRefRelation, CrossRefTarget};

    fn context() -> MemberSelectorLoweringContext {
        MemberSelectorLoweringContext::new(ChunkId(3), "runtime/widgets")
    }

    #[test]
    fn lowers_binding_name_selector() {
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::Binding(BindingSelector {
                name: "a".to_string(),
                kind: None,
            }),
        )
        .unwrap();

        assert_eq!(lowered.target, SelectorTargetId(0));
        assert_eq!(lowered.program.targets[0].logical_module, "runtime/widgets");
        assert_eq!(lowered.program.atoms.len(), 1);
        assert!(matches!(
            &lowered.program.atoms[0],
            SelectorAtom::OwnerDeclaresBinding {
                binding: StringTerm::Const { value },
                ..
            } if value == "a"
        ));
    }

    #[test]
    fn lowers_binding_kind_constraint() {
        let lowered = lower_member_selector(
            &context(),
            "WidgetFactory",
            &MemberSelectorSpec::Binding(BindingSelector {
                name: "f".to_string(),
                kind: Some(BindingSourceKind::FunctionDeclaration),
            }),
        )
        .unwrap();

        assert_eq!(lowered.program.atoms.len(), 2);
        assert!(matches!(
            &lowered.program.atoms[1],
            SelectorAtom::OwnerKind {
                statement_kind: StringTerm::Const { value },
                ..
            } if value == "fn_decl"
        ));
    }

    #[test]
    fn import_specifier_binding_fails_closed_for_now() {
        let error = lower_member_selector(
            &context(),
            "ImportedWidget",
            &MemberSelectorSpec::Binding(BindingSelector {
                name: "a".to_string(),
                kind: Some(BindingSourceKind::ImportSpecifier),
            }),
        )
        .unwrap_err();

        assert_eq!(
            error,
            SelectorIrLoweringError::Unsupported {
                selector_kind: "binding",
                reason: "import_specifier binding selectors need import-owner fact modeling",
            }
        );
    }

    #[test]
    fn exact_source_match_lowers_to_native_ast_constraints() {
        let selector = AnonymousStatementSelector::exact("const a = 1;");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap();

        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::OwnerStatementOrdinal { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstTopLevel { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstKind { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstChildCount { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstChild { .. }))
        );
    }

    #[test]
    fn exact_source_match_with_superclass_lowers_to_native_ast_constraint() {
        let selector = AnonymousStatementSelector::exact("class Widget extends BaseWidget {}");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstSuperClass { .. }))
        );
    }

    #[test]
    fn exact_source_match_wildcard_strings_lower_to_shared_string_var() {
        let mut selector =
            AnonymousStatementSelector::exact("const pair = [\"__VALUE__\", \"__VALUE__\"];");
        selector
            .wildcard_string_literals
            .insert("__VALUE__".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        let mut string_vars = lowered.program.atoms.iter().filter_map(|atom| match atom {
            SelectorAtom::AstStringLiteral {
                value: StringTerm::Var { id },
                ..
            } => Some(*id),
            _ => None,
        });
        let first = string_vars.next().expect("first wildcard string atom");
        let second = string_vars.next().expect("second wildcard string atom");
        assert_eq!(first, second);
        assert!(string_vars.next().is_none());
        assert!(matches!(
            lowered.program.variables[first.0].domain,
            VariableDomain::String
        ));
    }

    #[test]
    fn exact_source_match_with_expr_holes_lowers_natively() {
        let selector =
            AnonymousStatementSelector::exact("const readable = Math.max(EXPR_VALUE, EXPR_VALUE);");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        assert!(
            !lowered.program.atoms.iter().any(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstIdentifierName {
                        value: StringTerm::Const { value },
                        ..
                    } if value == "EXPR_VALUE"
                )
            }),
            "expression hole labels should not become identifier constraints"
        );
        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::Equal { .. })),
            "repeated expression hole labels are cosmetic, not equality constraints"
        );
    }

    #[test]
    fn exact_source_match_with_stmt_hole_lowers_natively() {
        let selector = AnonymousStatementSelector::exact(
            "function readable(flag) { if (flag) { STMT_BODY; } }",
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        assert!(
            !lowered.program.atoms.iter().any(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstIdentifierName {
                        value: StringTerm::Const { value },
                        ..
                    } if value == "STMT_BODY"
                )
            }),
            "statement hole labels should not become identifier constraints"
        );
        assert!(
            !lowered.program.atoms.iter().any(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstKind {
                        node_kind: NodeKind::ExprStmt,
                        ..
                    }
                )
            }),
            "statement holes should not constrain the expression-statement carrier shape"
        );
    }

    #[test]
    fn source_match_with_list_hole_syntax_stays_oracle_only() {
        let selector = AnonymousStatementSelector::exact(
            "function readable() { head(); STMT_LIST_BODY; tail(); }",
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert_eq!(lowered.program.atoms.len(), 1);
        assert!(matches!(
            &lowered.program.atoms[0],
            SelectorAtom::SourceMatchCandidate {
                owner: OwnerTerm::Var { id },
                selector_key: StringTerm::Const { value },
            } if *id == target_owner && value == &source_match::selector_key(&selector)
        ));
    }

    #[test]
    fn exact_source_match_target_binding_adds_owner_binding_constraint() {
        let mut selector = AnonymousStatementSelector::exact("const a = 1, b = 2;");
        selector.target_binding = Some("b".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::SourceMatchCandidate { .. }))
        );
        assert!(matches!(
            lowered.program.atoms.iter().find(|atom| {
                matches!(atom, SelectorAtom::OwnerDeclaresBinding { .. })
            }),
            Some(SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Const { value },
            }) if *id == target_owner && value == "b"
        ));
        assert!(
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstTopLevel { .. }))
        );
    }

    #[test]
    fn joint_builder_reuses_anchor_owner_variable() {
        let mut builder = MemberSelectorProgramBuilder::new(context());
        let anchor = builder
            .lower_member_selector(
                "Anchor",
                &MemberSelectorSpec::Binding(BindingSelector {
                    name: "a".to_string(),
                    kind: None,
                }),
            )
            .unwrap();
        let delegator = builder
            .lower_member_selector(
                "Delegator",
                &MemberSelectorSpec::CrossRef(CrossRefTarget {
                    relation: CrossRefRelation::References,
                    anchor: "Anchor".to_string(),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();

        let program = builder.into_program().unwrap();
        let anchor_owner = program.targets[anchor.0].owner;
        let delegator_owner = program.targets[delegator.0].owner;

        assert_eq!(program.variables.len(), 2);
        assert!(matches!(
            program.atoms.iter().find(|atom| matches!(atom, SelectorAtom::OwnerReferencesOwner { .. })),
            Some(SelectorAtom::OwnerReferencesOwner {
                owner: OwnerTerm::Var { id: owner_id },
                referenced: OwnerTerm::Var { id: referenced_id },
            }) if *owner_id == delegator_owner && *referenced_id == anchor_owner
        ));
    }
}
