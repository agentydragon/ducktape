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
    ClaimKind, ClaimOrigin, NodeTerm, OwnerTerm, RelationalPrimitive, SelectorAtom,
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

struct NativeAstFactLowering<'a> {
    logical_module: &'a str,
    facts: &'a ChunkFacts,
    node_vars: &'a BTreeMap<NodeId, SelectorVariableId>,
    skipped_nodes: &'a BTreeSet<NodeId>,
    identifier_mode: SourceMatchIdentifierMode,
    alpha_identifier_vars: &'a BTreeMap<NodeId, SelectorVariableId>,
    child_list_patterns: &'a BTreeMap<NodeId, NativeChildListPattern>,
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

    pub fn into_program(mut self) -> Result<SelectorProgram, SelectorIrLoweringError> {
        for ((logical_module, export_name), owner) in &self.owners_by_export {
            if !self.targeted_owners.contains_key(owner) {
                return Err(SelectorIrLoweringError::DanglingAnchor {
                    logical_module: logical_module.clone(),
                    export_name: export_name.clone(),
                });
            }
        }
        let mut unique_target_by_owner = BTreeMap::<SelectorVariableId, SelectorTargetId>::new();
        for target in &self.program.targets {
            if self.targeted_owners.contains_key(&target.owner) {
                unique_target_by_owner
                    .entry(target.owner)
                    .or_insert(target.id);
            }
        }
        let all_different_targets = unique_target_by_owner.into_values().collect::<Vec<_>>();
        if all_different_targets.len() > 1 {
            self.program.require_all_different(all_different_targets);
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
                if !self.try_lower_native_source_match(logical_module, owner, selector)? {
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

    fn try_lower_native_source_match(
        &mut self,
        logical_module: &str,
        owner: SelectorVariableId,
        selector: &AnonymousStatementSelector,
    ) -> Result<bool, SelectorIrLoweringError> {
        if selector.target_statement.is_some() || selector.target_statements.is_some() {
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
        let mut skipped_nodes = native_hole_subtree_nodes(&facts, &hole_roots);
        let Some(child_list_patterns) = native_child_list_patterns(&facts, &mut skipped_nodes)
        else {
            return Ok(false);
        };
        let [(root_node, _ordinal)] = facts.top_level.as_slice() else {
            return Ok(false);
        };
        let alpha_target_binding_node = match (&selector.target_binding, selector.identifiers) {
            (Some(target_binding), SourceMatchIdentifierMode::AlphaAll) => {
                let Some(target_binding_node) =
                    native_target_binding_node(&facts, &skipped_nodes, target_binding)
                else {
                    return Ok(false);
                };
                Some(target_binding_node)
            }
            _ => None,
        };

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
        self.program.add_atom(SelectorAtom::OwnerTopLevelRoot {
            owner: owner_term(owner),
            root: node_term(root),
        });
        let alpha_identifier_vars = if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            self.lower_alpha_identifier_variables(logical_module, &facts, &skipped_nodes)
        } else {
            BTreeMap::new()
        };
        match (&selector.target_binding, selector.identifiers) {
            (Some(target_binding), SourceMatchIdentifierMode::Exact) => {
                self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                    owner: owner_term(owner),
                    binding: const_str(target_binding),
                });
            }
            (Some(_target_binding), SourceMatchIdentifierMode::AlphaAll) => {
                let target_binding_node = alpha_target_binding_node
                    .expect("alpha_all target_binding native node was checked before lowering");
                let target_binding_var = alpha_identifier_vars
                    .get(&target_binding_node)
                    .copied()
                    .expect("alpha_all target_binding node should have an identifier variable");
                self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                    owner: owner_term(owner),
                    binding: string_term(target_binding_var),
                });
            }
            (None, _) => {}
        }
        self.lower_native_ast_facts(NativeAstFactLowering {
            logical_module,
            facts: &facts,
            node_vars: &node_vars,
            skipped_nodes: &skipped_nodes,
            identifier_mode: selector.identifiers,
            alpha_identifier_vars: &alpha_identifier_vars,
            child_list_patterns: &child_list_patterns,
        });
        Ok(true)
    }

    fn lower_native_ast_facts(&mut self, lowering: NativeAstFactLowering<'_>) {
        let NativeAstFactLowering {
            logical_module,
            facts,
            node_vars,
            skipped_nodes,
            identifier_mode,
            alpha_identifier_vars,
            child_list_patterns,
        } = lowering;
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
            if !child_list_patterns.contains_key(node) {
                self.program.add_atom(SelectorAtom::AstChildCount {
                    node: node_term(node_var),
                    count: child_counts.get(node).copied().unwrap_or(0),
                });
            }
        }
        for (parent, index, child) in &facts.child {
            if skipped_nodes.contains(parent) {
                continue;
            }
            if child_list_patterns
                .get(parent)
                .is_some_and(|pattern| *index >= pattern.start_index)
            {
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
        for (parent, pattern) in child_list_patterns {
            let Some(parent) = node_vars.get(parent).copied() else {
                continue;
            };
            let segments = pattern
                .segments
                .iter()
                .map(|segment| {
                    segment
                        .iter()
                        .filter_map(|node| node_vars.get(node).copied())
                        .map(node_term)
                        .collect::<Vec<_>>()
                })
                .filter(|segment| !segment.is_empty())
                .collect::<Vec<_>>();
            if segments.is_empty() {
                continue;
            }
            self.program.add_atom(SelectorAtom::AstChildListPattern {
                parent: node_term(parent),
                start_index: pattern.start_index,
                segments,
                anchored_left: pattern.anchored_left,
                anchored_right: pattern.anchored_right,
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
            match identifier_mode {
                SourceMatchIdentifierMode::Exact => {
                    self.add_ast_string_label(node_vars, *node, value, |node, value| {
                        SelectorAtom::AstIdentifierName { node, value }
                    });
                }
                SourceMatchIdentifierMode::AlphaAll => {
                    let (Some(node), Some(identifier)) = (
                        node_vars.get(node).copied(),
                        alpha_identifier_vars.get(node).copied(),
                    ) else {
                        continue;
                    };
                    self.program.add_atom(SelectorAtom::AstIdentifierName {
                        node: node_term(node),
                        value: string_term(identifier),
                    });
                }
            }
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

    fn lower_alpha_identifier_variables(
        &mut self,
        logical_module: &str,
        facts: &ChunkFacts,
        skipped_nodes: &BTreeSet<NodeId>,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        let [(root, _ordinal)] = facts.top_level.as_slice() else {
            return BTreeMap::new();
        };
        let index = AlphaIdentifierIndex::new(facts);
        let mut state = AlphaIdentifierState::default();
        self.lower_alpha_identifier_node(logical_module, &index, skipped_nodes, &mut state, *root)
    }

    fn lower_alpha_identifier_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        if skipped_nodes.contains(&node) {
            return BTreeMap::new();
        }

        let mut result = BTreeMap::new();
        let kind = index.node_kind.get(&node).copied();
        if let (Some(kind), Some(name)) = (kind, index.ident_name.get(&node)) {
            let identifier = if kind == NodeKind::BindingIdent {
                self.alpha_binding_identifier(logical_module, state, name)
            } else {
                self.alpha_reference_identifier(logical_module, state, name)
            };
            result.insert(node, identifier);
        }

        if kind == Some(NodeKind::Class)
            && let Some(super_class) = index.super_class_by_class.get(&node).copied()
        {
            result.extend(self.lower_alpha_identifier_node(
                logical_module,
                index,
                skipped_nodes,
                state,
                super_class,
            ));
        }

        let pushes_scope = kind.is_some_and(introduces_alpha_scope);
        if pushes_scope {
            state.frames.push(AlphaIdentifierFrame::default());
        }
        for child in index.children_by_parent.get(&node).into_iter().flatten() {
            result.extend(self.lower_alpha_identifier_node(
                logical_module,
                index,
                skipped_nodes,
                state,
                *child,
            ));
        }
        if pushes_scope {
            state.frames.pop();
        }
        result
    }

    fn alpha_binding_identifier(
        &mut self,
        logical_module: &str,
        state: &mut AlphaIdentifierState,
        name: &str,
    ) -> SelectorVariableId {
        if let Some(existing) = state.current_frame().by_name.get(name).copied() {
            return existing;
        }
        let distinct_from = state.current_frame().by_name.values().copied().collect();
        let identifier = self.new_alpha_identifier_variable(logical_module, state, name);
        state
            .current_frame_mut()
            .by_name
            .insert(name.to_string(), identifier);
        self.add_alpha_identifier_inequalities(state, identifier, distinct_from);
        identifier
    }

    fn alpha_reference_identifier(
        &mut self,
        logical_module: &str,
        state: &mut AlphaIdentifierState,
        name: &str,
    ) -> SelectorVariableId {
        for frame in state.frames.iter().rev() {
            if let Some(existing) = frame.by_name.get(name).copied() {
                return existing;
            }
        }
        let distinct_from = state
            .frames
            .iter()
            .flat_map(|frame| frame.by_name.values().copied())
            .collect();
        let identifier = self.new_alpha_identifier_variable(logical_module, state, name);
        state
            .current_frame_mut()
            .by_name
            .insert(name.to_string(), identifier);
        self.add_alpha_identifier_inequalities(state, identifier, distinct_from);
        identifier
    }

    fn new_alpha_identifier_variable(
        &mut self,
        logical_module: &str,
        state: &mut AlphaIdentifierState,
        name: &str,
    ) -> SelectorVariableId {
        let index = state.next_variable_index;
        state.next_variable_index += 1;
        self.program.add_variable(
            VariableDomain::String,
            Some(format!(
                "{logical_module}::source_match.ident.{name}.{index}"
            )),
        )
    }

    fn add_alpha_identifier_inequalities(
        &mut self,
        state: &mut AlphaIdentifierState,
        identifier: SelectorVariableId,
        distinct_from: Vec<SelectorVariableId>,
    ) {
        for other in distinct_from {
            if identifier == other {
                continue;
            }
            let pair = if identifier < other {
                (identifier, other)
            } else {
                (other, identifier)
            };
            if state.not_equal_pairs.insert(pair) {
                self.program.add_atom(SelectorAtom::NotEqual {
                    left: pair.0,
                    right: pair.1,
                });
            }
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

fn introduces_alpha_scope(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::Function
            | NodeKind::AsyncFunction
            | NodeKind::GeneratorFunction
            | NodeKind::AsyncGeneratorFunction
            | NodeKind::Arrow
            | NodeKind::AsyncArrow
            | NodeKind::Constructor
            | NodeKind::Setter
            | NodeKind::Catch
    )
}

fn native_target_binding_node(
    facts: &ChunkFacts,
    skipped_nodes: &BTreeSet<NodeId>,
    target_binding: &str,
) -> Option<NodeId> {
    let [(root, _ordinal)] = facts.top_level.as_slice() else {
        return None;
    };
    let index = AlphaIdentifierIndex::new(facts);
    let declared_nodes = native_declared_binding_nodes(&index, skipped_nodes, *root);
    let matches = declared_nodes
        .into_iter()
        .filter(|node| index.ident_name.get(node).map(String::as_str) == Some(target_binding))
        .collect::<Vec<_>>();
    let [node] = matches.as_slice() else {
        return None;
    };
    Some(*node)
}

fn native_declared_binding_nodes(
    index: &AlphaIdentifierIndex,
    skipped_nodes: &BTreeSet<NodeId>,
    node: NodeId,
) -> Vec<NodeId> {
    if skipped_nodes.contains(&node) {
        return Vec::new();
    }
    match index.node_kind.get(&node).copied() {
        Some(NodeKind::ExportDecl) => child_at(index, node, 0)
            .into_iter()
            .flat_map(|child| native_declared_binding_nodes(index, skipped_nodes, child))
            .collect(),
        Some(NodeKind::Import) => index
            .children_by_parent
            .get(&node)
            .into_iter()
            .flatten()
            .copied()
            .filter(|child| {
                !skipped_nodes.contains(child)
                    && index.node_kind.get(child) == Some(&NodeKind::ImportSpecifier)
                    && index.ident_name.contains_key(child)
            })
            .collect(),
        Some(NodeKind::FnDecl) | Some(NodeKind::ClassDecl) => child_at(index, node, 0)
            .filter(|child| {
                !skipped_nodes.contains(child)
                    && index.node_kind.get(child) == Some(&NodeKind::Ident)
                    && index.ident_name.contains_key(child)
            })
            .into_iter()
            .collect(),
        Some(NodeKind::VarDecl) => index
            .children_by_parent
            .get(&node)
            .into_iter()
            .flatten()
            .copied()
            .filter(|child| {
                !skipped_nodes.contains(child)
                    && index.node_kind.get(child) == Some(&NodeKind::VarDeclarator)
            })
            .flat_map(|declarator| {
                child_at(index, declarator, 0)
                    .into_iter()
                    .flat_map(|pattern| native_pattern_binding_nodes(index, skipped_nodes, pattern))
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn native_pattern_binding_nodes(
    index: &AlphaIdentifierIndex,
    skipped_nodes: &BTreeSet<NodeId>,
    node: NodeId,
) -> Vec<NodeId> {
    if skipped_nodes.contains(&node) {
        return Vec::new();
    }
    match index.node_kind.get(&node).copied() {
        Some(NodeKind::BindingIdent) | Some(NodeKind::PatAssign)
            if index.ident_name.contains_key(&node) =>
        {
            vec![node]
        }
        Some(NodeKind::AssignPat) => child_at(index, node, 0)
            .into_iter()
            .flat_map(|child| native_pattern_binding_nodes(index, skipped_nodes, child))
            .collect(),
        Some(NodeKind::PatKeyValue) => child_at(index, node, 1)
            .into_iter()
            .flat_map(|child| native_pattern_binding_nodes(index, skipped_nodes, child))
            .collect(),
        Some(NodeKind::ArrayPat) | Some(NodeKind::ObjectPat) | Some(NodeKind::RestPat) => index
            .children_by_parent
            .get(&node)
            .into_iter()
            .flatten()
            .copied()
            .flat_map(|child| native_pattern_binding_nodes(index, skipped_nodes, child))
            .collect(),
        _ => Vec::new(),
    }
}

fn child_at(index: &AlphaIdentifierIndex, parent: NodeId, child_index: u32) -> Option<NodeId> {
    index
        .children_by_parent
        .get(&parent)?
        .get(child_index as usize)
        .copied()
}

struct AlphaIdentifierIndex {
    node_kind: BTreeMap<NodeId, NodeKind>,
    ident_name: BTreeMap<NodeId, String>,
    children_by_parent: BTreeMap<NodeId, Vec<NodeId>>,
    super_class_by_class: BTreeMap<NodeId, NodeId>,
}

impl AlphaIdentifierIndex {
    fn new(facts: &ChunkFacts) -> Self {
        let mut children_by_parent = BTreeMap::<NodeId, Vec<(u32, NodeId)>>::new();
        for (parent, index, child) in &facts.child {
            children_by_parent
                .entry(*parent)
                .or_default()
                .push((*index, *child));
        }
        let children_by_parent = children_by_parent
            .into_iter()
            .map(|(parent, mut children)| {
                children.sort();
                (
                    parent,
                    children.into_iter().map(|(_index, child)| child).collect(),
                )
            })
            .collect();

        Self {
            node_kind: facts.node_kind.iter().copied().collect(),
            ident_name: facts.ident_name.iter().cloned().collect(),
            children_by_parent,
            super_class_by_class: facts.super_class.iter().copied().collect(),
        }
    }
}

struct AlphaIdentifierState {
    frames: Vec<AlphaIdentifierFrame>,
    not_equal_pairs: BTreeSet<(SelectorVariableId, SelectorVariableId)>,
    next_variable_index: usize,
}

impl Default for AlphaIdentifierState {
    fn default() -> Self {
        Self {
            frames: vec![AlphaIdentifierFrame::default()],
            not_equal_pairs: BTreeSet::new(),
            next_variable_index: 0,
        }
    }
}

impl AlphaIdentifierState {
    fn current_frame(&self) -> &AlphaIdentifierFrame {
        self.frames
            .last()
            .expect("alpha identifier lowering always keeps a root frame")
    }

    fn current_frame_mut(&mut self) -> &mut AlphaIdentifierFrame {
        self.frames
            .last_mut()
            .expect("alpha identifier lowering always keeps a root frame")
    }
}

#[derive(Default)]
struct AlphaIdentifierFrame {
    by_name: BTreeMap<String, SelectorVariableId>,
}

fn owner_term(owner: SelectorVariableId) -> OwnerTerm {
    OwnerTerm::Var { id: owner }
}

fn node_term(node: SelectorVariableId) -> NodeTerm {
    NodeTerm::Var { id: node }
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

#[derive(Debug)]
struct NativeChildListPattern {
    start_index: u32,
    segments: Vec<Vec<NodeId>>,
    anchored_left: bool,
    anchored_right: bool,
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
        if selector_hole_name(name)
            && labeled_hole_name_for(name, OBJECT_PROPS_HOLE_KEYWORD).is_none()
        {
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

    if native_child_list_hole_name(name) {
        return Some(HoleClassification::NotHole);
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

fn native_child_list_hole_name(name: &str) -> bool {
    [
        STMT_LIST_HOLE_KEYWORD,
        ARGS_HOLE_KEYWORD,
        OBJECT_PROPS_HOLE_KEYWORD,
        ARRAY_ELEMENTS_HOLE_KEYWORD,
    ]
    .iter()
    .any(|keyword| labeled_hole_name_for(name, keyword).is_some())
}

fn native_child_list_patterns(
    facts: &ChunkFacts,
    skipped_nodes: &mut BTreeSet<NodeId>,
) -> Option<BTreeMap<NodeId, NativeChildListPattern>> {
    let node_kind: BTreeMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let ident_name: BTreeMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(node, name)| (*node, name.as_str()))
        .collect();
    let child_list_idents = ident_name
        .iter()
        .filter_map(|(node, name)| {
            if native_child_list_hole_name(name) {
                Some(*node)
            } else {
                None
            }
        })
        .collect::<BTreeSet<_>>();
    let child_list_prop_nodes = facts
        .prop_name
        .iter()
        .filter_map(|(node, name)| {
            if native_child_list_hole_name(name) {
                Some(*node)
            } else {
                None
            }
        })
        .collect::<BTreeSet<_>>();
    let child_list_hole_nodes = child_list_idents
        .union(&child_list_prop_nodes)
        .copied()
        .collect::<BTreeSet<_>>();
    if child_list_hole_nodes.is_empty() {
        return Some(BTreeMap::new());
    }

    let mut children_by_parent = BTreeMap::<NodeId, Vec<(u32, NodeId)>>::new();
    for (parent, index, child) in &facts.child {
        children_by_parent
            .entry(*parent)
            .or_default()
            .push((*index, *child));
    }
    for children in children_by_parent.values_mut() {
        children.sort_by_key(|(index, _child)| *index);
    }

    let mut patterns = BTreeMap::new();
    let mut valid_child_list_nodes = BTreeSet::new();
    for (parent, children_with_indices) in &children_by_parent {
        let Some(parent_kind) = node_kind.get(parent).copied() else {
            continue;
        };
        let Some(start_index) = native_child_list_start_index(parent_kind) else {
            continue;
        };

        let children = children_with_indices
            .iter()
            .filter(|(index, _child)| *index >= start_index)
            .map(|(_index, child)| *child)
            .collect::<Vec<_>>();
        let mut hole_positions = BTreeSet::new();
        let mut allow_all_hole_pattern = false;
        for (index, child) in children.iter().enumerate() {
            if let Some(ident) = native_child_list_carrier_ident(
                parent_kind,
                *child,
                &node_kind,
                &children_by_parent,
                &ident_name,
            ) {
                hole_positions.insert(index);
                valid_child_list_nodes.insert(ident);
                if matches!(parent_kind, NodeKind::Object | NodeKind::ObjectPat) {
                    allow_all_hole_pattern = true;
                }
                collect_native_child_list_subtree(*child, &children_by_parent, skipped_nodes);
            }
        }
        if hole_positions.is_empty() {
            continue;
        }

        let (segments, anchored_left, anchored_right) =
            child_list_segments(&children, &hole_positions);
        if segments.is_empty() && !allow_all_hole_pattern {
            return None;
        }
        patterns.insert(
            *parent,
            NativeChildListPattern {
                start_index,
                segments,
                anchored_left,
                anchored_right,
            },
        );
    }

    if valid_child_list_nodes == child_list_hole_nodes {
        Some(patterns)
    } else {
        None
    }
}

fn native_child_list_start_index(parent_kind: NodeKind) -> Option<u32> {
    match parent_kind {
        NodeKind::Block | NodeKind::SwitchCase | NodeKind::Array => Some(0),
        NodeKind::Call | NodeKind::New | NodeKind::OptCall => Some(1),
        _ => None,
    }
}

fn native_child_list_carrier_ident(
    parent_kind: NodeKind,
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ident_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    match parent_kind {
        NodeKind::Block | NodeKind::SwitchCase => {
            if node_kind.get(&node) != Some(&NodeKind::ExprStmt) {
                return None;
            }
            let [(_, ident)] = children_by_parent.get(&node)?.as_slice() else {
                return None;
            };
            native_ident_hole(*ident, STMT_LIST_HOLE_KEYWORD, node_kind, ident_name)
        }
        NodeKind::Call | NodeKind::New | NodeKind::OptCall => {
            native_ident_hole(node, ARGS_HOLE_KEYWORD, node_kind, ident_name)
        }
        NodeKind::Array => {
            native_ident_hole(node, ARRAY_ELEMENTS_HOLE_KEYWORD, node_kind, ident_name)
        }
        NodeKind::Object | NodeKind::ObjectPat => {
            object_props_carrier_ident(parent_kind, node, node_kind, children_by_parent, ident_name)
        }
        _ => None,
    }
}

fn native_ident_hole(
    node: NodeId,
    keyword: &str,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    ident_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    if node_kind.get(&node) == Some(&NodeKind::Ident)
        && ident_name
            .get(&node)
            .is_some_and(|name| labeled_hole_name_for(name, keyword).is_some())
    {
        Some(node)
    } else {
        None
    }
}

fn object_props_carrier_ident(
    parent_kind: NodeKind,
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ident_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    match parent_kind {
        NodeKind::Object => {
            if node_kind.get(&node) == Some(&NodeKind::Shorthand)
                && ident_name.get(&node).is_some_and(|name| {
                    labeled_hole_name_for(name, OBJECT_PROPS_HOLE_KEYWORD).is_some()
                })
            {
                Some(node)
            } else {
                None
            }
        }
        NodeKind::ObjectPat => {
            if node_kind.get(&node) == Some(&NodeKind::PatAssign)
                && children_by_parent.get(&node).is_none_or(Vec::is_empty)
                && ident_name.get(&node).is_some_and(|name| {
                    labeled_hole_name_for(name, OBJECT_PROPS_HOLE_KEYWORD).is_some()
                })
            {
                Some(node)
            } else {
                None
            }
        }
        _ => None,
    }
}

fn collect_native_child_list_subtree(
    node: NodeId,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    skipped_nodes: &mut BTreeSet<NodeId>,
) {
    if skipped_nodes.insert(node)
        && let Some(children) = children_by_parent.get(&node)
    {
        for (_index, child) in children {
            collect_native_child_list_subtree(*child, children_by_parent, skipped_nodes);
        }
    }
}

fn child_list_segments(
    children: &[NodeId],
    hole_positions: &BTreeSet<usize>,
) -> (Vec<Vec<NodeId>>, bool, bool) {
    let mut segments = Vec::new();
    let mut index = 0;
    while index < children.len() {
        if hole_positions.contains(&index) {
            index += 1;
            continue;
        }
        let start = index;
        while index < children.len() && !hole_positions.contains(&index) {
            index += 1;
        }
        segments.push(children[start..index].to_vec());
    }
    let anchored_left = !children.is_empty() && !hole_positions.contains(&0);
    let anchored_right = !children.is_empty() && !hole_positions.contains(&(children.len() - 1));
    (segments, anchored_left, anchored_right)
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

    fn assert_source_match_oracle_only(
        lowered: &LoweredMemberSelector,
        selector: &AnonymousStatementSelector,
    ) {
        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert_eq!(lowered.program.atoms.len(), 1);
        assert!(matches!(
            &lowered.program.atoms[0],
            SelectorAtom::SourceMatchCandidate {
                owner: OwnerTerm::Var { id },
                selector_key: StringTerm::Const { value },
            } if *id == target_owner && value == &source_match::selector_key(selector)
        ));
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
                .any(|atom| matches!(atom, SelectorAtom::OwnerTopLevelRoot { .. }))
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
    fn alpha_all_source_match_lowers_identifier_names_to_query_variables() {
        let mut selector = AnonymousStatementSelector::exact("const a = b, c = a;");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
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
        let identifier_vars = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Var { id },
                    ..
                } => Some(*id),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(identifier_vars.len(), 4);
        assert_eq!(identifier_vars[0], identifier_vars[3]);
        assert_ne!(identifier_vars[0], identifier_vars[1]);
        assert_ne!(identifier_vars[0], identifier_vars[2]);
        assert_ne!(identifier_vars[1], identifier_vars[2]);

        let not_equal_pairs = lowered
            .program
            .atoms
            .iter()
            .filter(|atom| matches!(atom, SelectorAtom::NotEqual { .. }))
            .count();
        assert_eq!(not_equal_pairs, 3);
    }

    #[test]
    fn alpha_all_source_match_keeps_property_names_exact() {
        let mut selector = AnonymousStatementSelector::exact("const a = object.stableName;");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstPropertyName {
                value: StringTerm::Const { value },
                ..
            } if value == "stableName"
        )));
    }

    #[test]
    fn alpha_all_source_match_with_target_binding_projects_binding_variable() {
        let mut selector = AnonymousStatementSelector::exact("const a = b;");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("a".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn source_match_with_stmt_list_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "function readable() { head(); STMT_LIST_BODY; tail(); }",
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                anchored_left: true,
                anchored_right: true,
                segments,
                ..
            } if segments.len() == 2
        )));
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "STMT_LIST_BODY"
            )),
            "STMT_LIST labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn exact_source_match_with_argument_run_holes_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            r#"const selectedValue = joinParts("stable", ARGS_BEFORE, importantValue, ARGS_AFTER);"#,
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
        let (segments, anchored_left, anchored_right) = lowered
            .program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                SelectorAtom::AstChildListPattern {
                    start_index: 1,
                    segments,
                    anchored_left,
                    anchored_right,
                    ..
                } => Some((segments, *anchored_left, *anchored_right)),
                _ => None,
            })
            .expect("argument holes should lower to a child-list pattern");
        assert_eq!(segments.len(), 2);
        assert_eq!(segments[0].len(), 1);
        assert_eq!(segments[1].len(), 1);
        assert!(anchored_left);
        assert!(!anchored_right);
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ARGS_BEFORE" || value == "ARGS_AFTER"
            )),
            "ARGS labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn exact_source_match_with_array_element_run_hole_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            r#"const readable = ["black", ARRAY_ELEMENTS, "white"];"#,
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
        let (segments, anchored_left, anchored_right) = lowered
            .program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                SelectorAtom::AstChildListPattern {
                    start_index: 0,
                    segments,
                    anchored_left,
                    anchored_right,
                    ..
                } if segments.len() == 2 => Some((segments, *anchored_left, *anchored_right)),
                _ => None,
            })
            .expect("array-element holes should lower to a child-list pattern");
        assert_eq!(segments[0].len(), 1);
        assert_eq!(segments[1].len(), 1);
        assert!(anchored_left);
        assert!(anchored_right);
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ARRAY_ELEMENTS"
            )),
            "ARRAY_ELEMENTS labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn alpha_all_source_match_with_argument_run_hole_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact("const a = foo(ARGS, b);");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern { start_index: 1, .. }
        )));
    }

    #[test]
    fn misplaced_argument_run_hole_stays_oracle_only() {
        let selector = AnonymousStatementSelector::exact("const a = ARGS;");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap();

        assert_source_match_oracle_only(&lowered, &selector);
    }

    #[test]
    fn source_match_with_object_props_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "const actual = { head: 1, OBJECT_PROPS_MID, tail: 2 };",
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                start_index: 0,
                anchored_left: true,
                anchored_right: true,
                segments,
                ..
            } if segments.len() == 2
                && segments[0].len() == 1
                && segments[1].len() == 1
        )));
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "OBJECT_PROPS_MID"
            )),
            "OBJECT_PROPS labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn source_match_with_object_props_before_after_lowers_to_open_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "const actual = { OBJECT_PROPS_BEFORE, stable: 1, OBJECT_PROPS_AFTER };",
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                start_index: 0,
                anchored_left: false,
                anchored_right: false,
                segments,
                ..
            } if segments.len() == 1 && segments[0].len() == 1
        )));
    }

    #[test]
    fn source_match_with_object_pattern_props_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "const { OBJECT_PROPS_BEFORE, stable, OBJECT_PROPS_AFTER } = input;",
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                start_index: 0,
                anchored_left: false,
                anchored_right: false,
                segments,
                ..
            } if segments.len() == 1 && segments[0].len() == 1
        )));
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "OBJECT_PROPS_BEFORE" || value == "OBJECT_PROPS_AFTER"
            )),
            "OBJECT_PROPS pattern labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn source_match_with_bare_object_props_hole_does_not_pin_object_arity() {
        let selector = AnonymousStatementSelector::exact("const actual = { OBJECT_PROPS };");
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
            !lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstChildListPattern { .. })),
            "all-hole object lists are represented by omitting arity and child atoms"
        );
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "OBJECT_PROPS"
            )),
            "bare OBJECT_PROPS is hole syntax, not an identifier constraint"
        );
        let object_nodes = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstKind {
                    node: NodeTerm::Var { id },
                    node_kind,
                } if node_kind.as_tag() == "Object" => Some(*id),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(!object_nodes.is_empty());
        for object_node in object_nodes {
            assert!(
                !lowered.program.atoms.iter().any(|atom| matches!(
                    atom,
                    SelectorAtom::AstChildCount {
                        node: NodeTerm::Var { id },
                        ..
                    } if *id == object_node
                )),
                "bare OBJECT_PROPS object selector must not constrain object child count"
            );
        }
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
                .any(|atom| matches!(atom, SelectorAtom::OwnerTopLevelRoot { .. }))
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
        assert_eq!(program.all_different, vec![vec![anchor, delegator]]);
        assert!(matches!(
            program.atoms.iter().find(|atom| matches!(atom, SelectorAtom::OwnerReferencesOwner { .. })),
            Some(SelectorAtom::OwnerReferencesOwner {
                owner: OwnerTerm::Var { id: owner_id },
                referenced: OwnerTerm::Var { id: referenced_id },
            }) if *owner_id == delegator_owner && *referenced_id == anchor_owner
        ));
    }
}
