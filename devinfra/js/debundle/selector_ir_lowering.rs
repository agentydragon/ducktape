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
    SelectorProgram, SelectorProjectedValue, SelectorSourceMatchProjectionEvent, SelectorTargetId,
    SelectorVariableId, StringTerm, VariableDomain,
};
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, ARRAY_ELEMENTS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, STMT_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
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
    injective_targeted_owners: BTreeSet<SelectorVariableId>,
    owner_injectivity_classes: BTreeMap<SelectorVariableId, String>,
}

#[derive(Debug, Clone, Copy)]
pub enum MemberSelectorSpecRef<'a> {
    Binding(&'a BindingSelector),
    SourceMatch(&'a AnonymousStatementSelector),
    CrossRef(&'a spec::CrossRefTarget),
    ReadsMember(&'a spec::ReadsMemberTarget),
    MemberOfModule(&'a spec::MemberOfModuleTarget),
    PassedToCall(&'a spec::PassedToCallTarget),
    MakesDecorateCall(&'a spec::MakesDecorateCallTarget),
    IntrinsicAlias(&'a spec::IntrinsicAliasTarget),
}

impl<'a> From<&'a MemberSelectorSpec> for MemberSelectorSpecRef<'a> {
    fn from(selector: &'a MemberSelectorSpec) -> Self {
        match selector {
            MemberSelectorSpec::Binding(selector) => Self::Binding(selector),
            MemberSelectorSpec::SourceMatch(selector) => Self::SourceMatch(selector),
            MemberSelectorSpec::CrossRef(selector) => Self::CrossRef(selector),
            MemberSelectorSpec::ReadsMember(selector) => Self::ReadsMember(selector),
            MemberSelectorSpec::MemberOfModule(selector) => Self::MemberOfModule(selector),
            MemberSelectorSpec::PassedToCall(selector) => Self::PassedToCall(selector),
            MemberSelectorSpec::MakesDecorateCall(selector) => Self::MakesDecorateCall(selector),
            MemberSelectorSpec::IntrinsicAlias(selector) => Self::IntrinsicAlias(selector),
        }
    }
}

struct NativeAstFactLowering<'a> {
    facts: &'a ChunkFacts,
    node_vars: &'a BTreeMap<NodeId, SelectorVariableId>,
    skipped_nodes: &'a BTreeSet<NodeId>,
    identifier_mode: SourceMatchIdentifierMode,
    alpha_identifier_vars: &'a BTreeMap<NodeId, SelectorVariableId>,
    exact_identifier_projection_vars: &'a BTreeMap<NodeId, SelectorVariableId>,
    child_list_patterns: &'a BTreeMap<NodeId, NativeChildListPattern>,
    bare_properties: &'a BTreeMap<NodeId, NativeBarePropertySelector>,
    regex_predicates: &'a NativeRegexPredicateIndex,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum OwnerInjectivityClass {
    Owner(SelectorVariableId),
    Shared(String),
}

impl MemberSelectorProgramBuilder {
    pub fn new(context: MemberSelectorLoweringContext) -> Self {
        Self {
            context,
            program: SelectorProgram::default(),
            owners_by_export: BTreeMap::new(),
            global_owner_by_export: BTreeMap::new(),
            targeted_owners: BTreeMap::new(),
            injective_targeted_owners: BTreeSet::new(),
            owner_injectivity_classes: BTreeMap::new(),
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
        let selector = MemberSelectorSpecRef::from(selector);
        let target = self.declare_member_target_in_module_ref(
            logical_module.clone(),
            export_name,
            selector,
        )?;
        self.lower_member_constraints_in_module_ref(&logical_module, export_name, selector)?;
        Ok(target)
    }

    pub fn declare_member_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        self.declare_member_target_in_module_ref(
            logical_module,
            export_name,
            MemberSelectorSpecRef::from(selector),
        )
    }

    pub fn declare_member_target_in_module_ref(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        self.declare_target_in_module_ref(
            logical_module,
            export_name,
            selector,
            ClaimKind::Binding {
                export_name: Some(export_name.to_string()),
            },
        )
    }

    pub fn declare_binding_group_member_target_in_module_ref(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        target_binding: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        self.declare_target_in_module_ref(
            logical_module,
            export_name,
            selector,
            ClaimKind::BindingGroupMember {
                export_name: export_name.to_string(),
                target_binding: target_binding.to_string(),
            },
        )
    }

    fn declare_target_in_module_ref(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
        claim: ClaimKind,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = logical_module.into();
        let owner = self.owner_for_local_export(&logical_module, export_name);
        self.targeted_owners
            .insert(owner, format!("{logical_module}::{export_name}"));
        if !matches!(selector, MemberSelectorSpecRef::Binding(_)) {
            self.injective_targeted_owners.insert(owner);
        }
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
            claim,
            selector_origin_ref(selector),
        ))
    }

    pub fn declare_native_anonymous_statement_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        statement_index: usize,
        selector: &AnonymousStatementSelector,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = logical_module.into();
        let debug_label = format!("anonymous_statement.{statement_index}.source_match");
        let parsed = js_ast::with_swc_globals(|| {
            source_match::ParsedSourceMatchSelector::parse(
                &logical_module,
                "source_match",
                format!("<selector ir source_match in {logical_module}>"),
                selector,
                "source_match",
            )
        })
        .map_err(|error| SelectorIrLoweringError::UnsupportedSourceMatch {
            selector_kind: "source_match",
            reason: format!("logical_module {logical_module} {debug_label}: {error}"),
        })?;
        self.declare_native_anonymous_statement_target_in_module_parsed(
            logical_module,
            statement_index,
            &parsed,
        )
    }

    pub fn declare_native_anonymous_statement_target_in_module_parsed(
        &mut self,
        logical_module: impl Into<String>,
        statement_index: usize,
        parsed: &source_match::ParsedSourceMatchSelector,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let selector = parsed.selector();
        if !native_anonymous_source_match_supported(selector) {
            return Err(SelectorIrLoweringError::unsupported(
                "anonymous_statements.source_match",
                "selector shape is not yet supported by native selector IR",
            ));
        }
        let logical_module = logical_module.into();
        let mut scratch = self.clone();
        let owner = scratch.program.add_variable(
            VariableDomain::Owner,
            Some(format!(
                "{logical_module}::anonymous_statement.{statement_index}"
            )),
        );
        let debug_label = format!("anonymous_statement.{statement_index}.source_match");
        if !scratch.try_lower_native_source_match_parsed(
            &logical_module,
            &debug_label,
            owner,
            parsed,
        )? {
            return Err(SelectorIrLoweringError::unsupported(
                "anonymous_statements.source_match",
                "selector shape is not yet supported by native selector IR",
            ));
        }
        let target = scratch.program.add_target(
            self.context.chunk_id,
            owner,
            logical_module,
            ClaimKind::AnonymousStatement,
            ClaimOrigin::AnonymousStatement {
                index: statement_index,
            },
        );
        *self = scratch;
        Ok(target)
    }

    pub fn declare_projected_anonymous_statement_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        statement_index: usize,
        candidate_owners: Vec<analysis::OwnerId>,
    ) -> SelectorTargetId {
        let logical_module = logical_module.into();
        let owner = self.program.add_variable(
            VariableDomain::Owner,
            Some(format!(
                "{logical_module}::anonymous_statement.projected.{statement_index}"
            )),
        );
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![owner],
            rows: candidate_owners
                .into_iter()
                .map(|owner| vec![SelectorProjectedValue::Owner(owner)])
                .collect(),
            reason: format!(
                "{logical_module}::anonymous_statement.source_match.projected.{statement_index}"
            ),
        });
        self.program.add_target(
            self.context.chunk_id,
            owner,
            logical_module,
            ClaimKind::AnonymousStatement,
            ClaimOrigin::AnonymousStatement {
                index: statement_index,
            },
        )
    }

    pub fn lower_member_constraints_in_module(
        &mut self,
        logical_module: &str,
        export_name: &str,
        selector: &MemberSelectorSpec,
    ) -> Result<(), SelectorIrLoweringError> {
        self.lower_member_constraints_in_module_ref(
            logical_module,
            export_name,
            MemberSelectorSpecRef::from(selector),
        )
    }

    pub fn lower_member_constraints_in_module_ref(
        &mut self,
        logical_module: &str,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<(), SelectorIrLoweringError> {
        let owner = self.owner_for_local_export(logical_module, export_name);
        self.lower_selector_atoms(logical_module, export_name, owner, selector)
    }

    pub fn lower_source_match_constraints_in_module_parsed(
        &mut self,
        logical_module: &str,
        export_name: &str,
        parsed: &source_match::ParsedSourceMatchSelector,
    ) -> Result<(), SelectorIrLoweringError> {
        let owner = self.owner_for_local_export(logical_module, export_name);
        let debug_label = format!("source_match.{export_name}");
        if !self.try_lower_native_source_match_parsed(
            logical_module,
            &debug_label,
            owner,
            parsed,
        )? {
            return Err(SelectorIrLoweringError::UnsupportedSourceMatch {
                selector_kind: "source_match",
                reason: format!(
                    "selector shape is not yet supported by native selector IR in \
                     logical_module {logical_module} for export {export_name}"
                ),
            });
        }
        Ok(())
    }

    pub fn lower_projected_source_match_candidates(
        &mut self,
        logical_module: &str,
        export_name: &str,
        candidate_rows: Vec<(analysis::OwnerId, String)>,
    ) {
        let owner = self.owner_for_local_export(logical_module, export_name);
        let binding = self.program.add_variable(
            VariableDomain::String,
            Some(format!(
                "{logical_module}::source_match.projected_binding.{export_name}"
            )),
        );
        self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: owner_term(owner),
            binding: string_term(binding),
        });
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![owner, binding],
            rows: candidate_rows
                .into_iter()
                .map(|(owner, binding)| {
                    vec![
                        SelectorProjectedValue::Owner(owner),
                        SelectorProjectedValue::String(binding),
                    ]
                })
                .collect(),
            reason: format!("{logical_module}::source_match.projected.{export_name}"),
        });
    }

    pub fn record_source_match_projection_event(
        &mut self,
        event: SelectorSourceMatchProjectionEvent,
    ) {
        self.program.add_source_match_projection_event(event);
    }

    pub fn lower_projected_source_match_group_candidates(
        &mut self,
        logical_module: &str,
        exports_by_target: &BTreeMap<String, String>,
        candidate_rows: Vec<BTreeMap<String, (analysis::OwnerId, String)>>,
    ) {
        let mut variables = Vec::new();
        let mut target_bindings = Vec::new();
        let injectivity_class = format!(
            "{logical_module}|source_matches.projected|{}",
            exports_by_target
                .keys()
                .cloned()
                .collect::<Vec<_>>()
                .join(",")
        );
        for (target_binding, export_name) in exports_by_target {
            let owner = self.owner_for_local_export(logical_module, export_name);
            self.owner_injectivity_classes
                .insert(owner, injectivity_class.clone());
            let binding = self.program.add_variable(
                VariableDomain::String,
                Some(format!(
                    "{logical_module}::source_matches.projected_binding.{target_binding}"
                )),
            );
            self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                owner: owner_term(owner),
                binding: string_term(binding),
            });
            variables.push(owner);
            variables.push(binding);
            target_bindings.push(target_binding.clone());
        }
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables,
            rows: candidate_rows
                .into_iter()
                .map(|row| {
                    target_bindings
                        .iter()
                        .flat_map(|target_binding| {
                            let (owner, binding) = row
                                .get(target_binding)
                                .expect("candidate rows should contain every group target");
                            [
                                SelectorProjectedValue::Owner(*owner),
                                SelectorProjectedValue::String(binding.clone()),
                            ]
                        })
                        .collect::<Vec<_>>()
                })
                .collect(),
            reason: format!("{logical_module}::source_matches.projected"),
        });
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
        let mut unique_target_by_owner_class =
            BTreeMap::<OwnerInjectivityClass, SelectorTargetId>::new();
        for target in &self.program.targets {
            if self.injective_targeted_owners.contains(&target.owner) {
                let class = self
                    .owner_injectivity_classes
                    .get(&target.owner)
                    .cloned()
                    .map(OwnerInjectivityClass::Shared)
                    .unwrap_or(OwnerInjectivityClass::Owner(target.owner));
                unique_target_by_owner_class
                    .entry(class)
                    .or_insert(target.id);
            }
        }
        let all_different_targets = unique_target_by_owner_class
            .into_values()
            .collect::<Vec<_>>();
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
        export_name: &str,
        owner: SelectorVariableId,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<(), SelectorIrLoweringError> {
        match selector {
            MemberSelectorSpecRef::Binding(binding) => self.lower_binding_selector(owner, binding),
            MemberSelectorSpecRef::SourceMatch(selector) => {
                let debug_label = format!("source_match.{export_name}");
                if !self.try_lower_native_source_match(
                    logical_module,
                    &debug_label,
                    owner,
                    selector,
                )? {
                    return Err(SelectorIrLoweringError::UnsupportedSourceMatch {
                        selector_kind: "source_match",
                        reason: format!(
                            "selector shape is not yet supported by native selector IR in \
                             logical_module {logical_module} for export {export_name}"
                        ),
                    });
                }
                Ok(())
            }
            MemberSelectorSpecRef::CrossRef(target) => {
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
            MemberSelectorSpecRef::ReadsMember(target) => {
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
            MemberSelectorSpecRef::MemberOfModule(target) => {
                self.program.add_atom(SelectorAtom::ConsumesModuleMember {
                    owner: owner_term(owner),
                    module: const_str(&target.module),
                    member: const_str(&target.member),
                });
                self.add_kind_atom(owner, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::PassedToCall(target) => {
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
            MemberSelectorSpecRef::MakesDecorateCall(target) => {
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
            MemberSelectorSpecRef::IntrinsicAlias(target) => {
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
        debug_label: &str,
        owner: SelectorVariableId,
        selector: &AnonymousStatementSelector,
    ) -> Result<bool, SelectorIrLoweringError> {
        let parsed = js_ast::with_swc_globals(|| {
            source_match::ParsedSourceMatchSelector::parse(
                logical_module,
                "source_match",
                format!("<selector ir source_match in {logical_module}>"),
                selector,
                "source_match",
            )
        })
        .map_err(|error| SelectorIrLoweringError::UnsupportedSourceMatch {
            selector_kind: "source_match",
            reason: format!("logical_module {logical_module} {debug_label}: {error}"),
        })?;
        self.try_lower_native_source_match_parsed(logical_module, debug_label, owner, &parsed)
    }

    fn try_lower_native_source_match_parsed(
        &mut self,
        logical_module: &str,
        debug_label: &str,
        owner: SelectorVariableId,
        parsed: &source_match::ParsedSourceMatchSelector,
    ) -> Result<bool, SelectorIrLoweringError> {
        let selector = parsed.selector();
        if parsed.body().is_empty() {
            return Ok(false);
        }
        if parsed.body().len() > 1 && selector.target_binding.is_none() {
            return Ok(false);
        }
        let Ok(facts) = chunk_facts::extract_facts_items(parsed.body()) else {
            return Ok(false);
        };
        if parsed.body().len() > 1 && !native_module_stmt_list_hole_roots(&facts).is_empty() {
            return Ok(false);
        }
        let regex_predicates = native_string_literal_regex_predicates(&facts);
        let Some(hole_roots) =
            native_single_node_hole_roots(&facts, &regex_predicates.consumed_nodes)
        else {
            return Ok(false);
        };
        let mut skipped_nodes = native_hole_subtree_nodes(&facts, &hole_roots);
        skipped_nodes.extend(regex_predicates.consumed_nodes.iter().copied());
        let Some(child_list_patterns) = native_child_list_patterns(&facts, &mut skipped_nodes)
        else {
            return Ok(false);
        };
        let bare_properties = if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            native_bare_property_selectors(&facts, &skipped_nodes)
        } else {
            BTreeMap::new()
        };
        if facts.top_level.len() != parsed.body().len() {
            return Ok(false);
        }
        let top_level_roots = facts
            .top_level
            .iter()
            .map(|(root, _ordinal)| *root)
            .collect::<Vec<_>>();
        let alpha_index = (selector.target_binding.is_some()
            || selector.identifiers == SourceMatchIdentifierMode::AlphaAll)
            .then(|| AlphaIdentifierIndex::new(&facts));
        let target_binding_node = match &selector.target_binding {
            Some(target_binding) => {
                let Some(index) = alpha_index.as_ref() else {
                    return Ok(false);
                };
                let Some(target_binding_node) = native_target_binding_node(
                    index,
                    &top_level_roots,
                    &skipped_nodes,
                    target_binding,
                ) else {
                    return Ok(false);
                };
                Some((target_binding.as_str(), target_binding_node))
            }
            None => None,
        };
        let alpha_projected_binding_node = match (&selector.target_binding, selector.identifiers) {
            (None, SourceMatchIdentifierMode::AlphaAll) => {
                let Some(index) = alpha_index.as_ref() else {
                    return Ok(false);
                };
                native_single_declared_binding_node(index, &top_level_roots, &skipped_nodes)
            }
            _ => None,
        };
        let target_root_node = if let Some((_target_binding, target_binding_node)) =
            target_binding_node
        {
            let Some(index) = alpha_index.as_ref() else {
                return Ok(false);
            };
            let Some(root) =
                native_top_level_root_containing_node(index, &top_level_roots, target_binding_node)
            else {
                return Ok(false);
            };
            root
        } else {
            let [root] = top_level_roots.as_slice() else {
                return Ok(false);
            };
            *root
        };
        let Some(target_root_index) = top_level_roots
            .iter()
            .position(|root| *root == target_root_node)
        else {
            return Ok(false);
        };

        let mut node_vars = BTreeMap::<NodeId, SelectorVariableId>::new();
        for (node, _kind) in &facts.node_kind {
            node_vars.insert(
                *node,
                self.program.add_variable(
                    VariableDomain::AstNode,
                    Some(format!("{logical_module}::{debug_label}.node{node}")),
                ),
            );
        }
        let Some(root) = node_vars.get(&target_root_node).copied() else {
            return Ok(false);
        };
        self.program.add_atom(SelectorAtom::OwnerTopLevelRoot {
            owner: owner_term(owner),
            root: node_term(root),
        });
        if top_level_roots.len() > 1 {
            self.lower_native_top_level_window(
                logical_module,
                debug_label,
                &node_vars,
                &top_level_roots,
                target_root_index,
            )?;
        }
        let alpha_identifier_vars = if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            self.lower_alpha_identifier_variables(logical_module, &facts, &skipped_nodes)
        } else {
            BTreeMap::new()
        };
        let mut exact_identifier_projection_vars = BTreeMap::new();
        match (target_binding_node, selector.identifiers) {
            (Some((target_binding, target_binding_node)), SourceMatchIdentifierMode::Exact) => {
                let target_binding_var = self.program.add_variable(
                    VariableDomain::String,
                    Some(format!(
                        "{logical_module}::{debug_label}.target_binding.{target_binding}"
                    )),
                );
                exact_identifier_projection_vars.insert(target_binding_node, target_binding_var);
                self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                    owner: owner_term(owner),
                    binding: string_term(target_binding_var),
                });
            }
            (
                Some((_target_binding, projected_binding_node)),
                SourceMatchIdentifierMode::AlphaAll,
            ) => {
                let projected_binding_var = alpha_identifier_vars
                    .get(&projected_binding_node)
                    .copied()
                    .expect("alpha_all projected binding node should have an identifier variable");
                self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                    owner: owner_term(owner),
                    binding: string_term(projected_binding_var),
                });
            }
            (None, SourceMatchIdentifierMode::AlphaAll) => {
                if let Some(projected_binding_node) = alpha_projected_binding_node {
                    let projected_binding_var = alpha_identifier_vars
                        .get(&projected_binding_node)
                        .copied()
                        .expect(
                            "alpha_all projected binding node should have an identifier variable",
                        );
                    self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                        owner: owner_term(owner),
                        binding: string_term(projected_binding_var),
                    });
                }
            }
            (None, _) => {}
        }
        self.lower_native_ast_facts(NativeAstFactLowering {
            facts: &facts,
            node_vars: &node_vars,
            skipped_nodes: &skipped_nodes,
            identifier_mode: selector.identifiers,
            alpha_identifier_vars: &alpha_identifier_vars,
            exact_identifier_projection_vars: &exact_identifier_projection_vars,
            child_list_patterns: &child_list_patterns,
            bare_properties: &bare_properties,
            regex_predicates: &regex_predicates,
        });
        Ok(true)
    }

    fn lower_native_top_level_fixed_sequence(
        &mut self,
        logical_module: &str,
        variable_label: &str,
        node_vars: &BTreeMap<NodeId, SelectorVariableId>,
        top_level_roots: &[NodeId],
    ) -> Result<Vec<SelectorVariableId>, SelectorIrLoweringError> {
        let mut ordinals = Vec::new();
        for (index, root) in top_level_roots.iter().enumerate() {
            let Some(root) = node_vars.get(root).copied() else {
                continue;
            };
            let ordinal = self.program.add_variable(
                VariableDomain::StatementOrdinal,
                Some(format!(
                    "{logical_module}::{variable_label}.sequence.ordinal.{index}"
                )),
            );
            self.program.add_atom(SelectorAtom::AstTopLevel {
                node: node_term(root),
                ordinal: OrdinalTerm::Var { id: ordinal },
            });
            ordinals.push(ordinal);
        }
        let Some(base_ordinal) = ordinals.first().copied() else {
            return Ok(ordinals);
        };
        for (index, ordinal) in ordinals.iter().enumerate() {
            let offset =
                i32::try_from(index).map_err(|_| SelectorIrLoweringError::Unsupported {
                    selector_kind: "source_match",
                    reason: "multi-statement source_match sequence exceeds solver i32 range",
                })?;
            self.program.add_atom(SelectorAtom::OrdinalOffset {
                base: OrdinalTerm::Var { id: base_ordinal },
                ordinal: OrdinalTerm::Var { id: *ordinal },
                offset,
            });
        }
        Ok(ordinals)
    }

    fn lower_native_top_level_window(
        &mut self,
        logical_module: &str,
        debug_label: &str,
        node_vars: &BTreeMap<NodeId, SelectorVariableId>,
        top_level_roots: &[NodeId],
        target_root_index: usize,
    ) -> Result<(), SelectorIrLoweringError> {
        let target_ordinal = self.program.add_variable(
            VariableDomain::StatementOrdinal,
            Some(format!(
                "{logical_module}::{debug_label}.window.target_ordinal"
            )),
        );
        for (index, root) in top_level_roots.iter().enumerate() {
            let Some(root) = node_vars.get(root).copied() else {
                continue;
            };
            let ordinal = if index == target_root_index {
                target_ordinal
            } else {
                self.program.add_variable(
                    VariableDomain::StatementOrdinal,
                    Some(format!(
                        "{logical_module}::{debug_label}.window.ordinal.{index}"
                    )),
                )
            };
            self.program.add_atom(SelectorAtom::AstTopLevel {
                node: node_term(root),
                ordinal: OrdinalTerm::Var { id: ordinal },
            });
            let offset = i32::try_from(index)
                .and_then(|index| i32::try_from(target_root_index).map(|target| index - target))
                .map_err(|_| SelectorIrLoweringError::Unsupported {
                    selector_kind: "source_match",
                    reason: "multi-statement source_match window exceeds solver i32 range",
                })?;
            self.program.add_atom(SelectorAtom::OrdinalOffset {
                base: OrdinalTerm::Var { id: target_ordinal },
                ordinal: OrdinalTerm::Var { id: ordinal },
                offset,
            });
        }
        Ok(())
    }

    pub fn try_lower_native_source_match_group(
        &mut self,
        logical_module: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<bool, SelectorIrLoweringError> {
        if selector.target_binding.is_some() || exports_by_target.is_empty() {
            return Ok(false);
        }
        let parsed = js_ast::with_swc_globals(|| {
            source_match::ParsedSourceMatchSelector::parse(
                logical_module,
                "source_matches",
                format!("<selector ir binding_group source_match in {logical_module}>"),
                selector,
                "source_matches",
            )
        })
        .map_err(|error| SelectorIrLoweringError::UnsupportedSourceMatch {
            selector_kind: "source_matches",
            reason: format!(
                "logical_module {logical_module} {}: {}",
                grouped_source_matches_debug_label(exports_by_target),
                error
            ),
        })?;
        self.try_lower_native_source_match_group_parsed(logical_module, &parsed, exports_by_target)
    }

    pub fn try_lower_native_source_match_group_parsed(
        &mut self,
        logical_module: &str,
        parsed: &source_match::ParsedSourceMatchSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<bool, SelectorIrLoweringError> {
        let selector = parsed.selector();
        if selector.target_binding.is_some() || exports_by_target.is_empty() {
            return Ok(false);
        }
        if parsed.body().is_empty() {
            return Ok(false);
        }
        let Ok(facts) = chunk_facts::extract_facts_items(parsed.body()) else {
            return Ok(false);
        };
        let module_stmt_list_hole_roots = native_module_stmt_list_hole_roots(&facts);
        let top_level_segments =
            native_pinned_top_level_segments(&facts, &module_stmt_list_hole_roots);
        if top_level_segments.is_empty() {
            return Ok(false);
        }
        let top_level_roots = top_level_segments
            .iter()
            .flatten()
            .copied()
            .collect::<Vec<_>>();
        let regex_predicates = native_string_literal_regex_predicates(&facts);
        let Some(hole_roots) =
            native_single_node_hole_roots(&facts, &regex_predicates.consumed_nodes)
        else {
            return Ok(false);
        };
        let mut skipped_nodes = native_hole_subtree_nodes(&facts, &hole_roots);
        skipped_nodes.extend(native_hole_subtree_nodes(
            &facts,
            &module_stmt_list_hole_roots,
        ));
        skipped_nodes.extend(regex_predicates.consumed_nodes.iter().copied());
        let Some(child_list_patterns) = native_child_list_patterns(&facts, &mut skipped_nodes)
        else {
            return Ok(false);
        };
        let bare_properties = if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            native_bare_property_selectors(&facts, &skipped_nodes)
        } else {
            BTreeMap::new()
        };
        if facts.top_level.len() != parsed.body().len() {
            return Ok(false);
        }
        let group_debug_label = grouped_source_matches_debug_label(exports_by_target);

        let mut target_binding_nodes = BTreeMap::<String, NodeId>::new();
        let index = AlphaIdentifierIndex::new(&facts);
        for target_binding in exports_by_target.keys() {
            let Some(target_binding_node) = native_target_binding_node(
                &index,
                &top_level_roots,
                &skipped_nodes,
                target_binding,
            ) else {
                return Ok(false);
            };
            target_binding_nodes.insert(target_binding.clone(), target_binding_node);
        }

        let mut node_vars = BTreeMap::<NodeId, SelectorVariableId>::new();
        for (node, _kind) in &facts.node_kind {
            node_vars.insert(
                *node,
                self.program.add_variable(
                    VariableDomain::AstNode,
                    Some(format!("{logical_module}::{group_debug_label}.node{node}")),
                ),
            );
        }
        let root_by_target_binding = {
            target_binding_nodes
                .iter()
                .map(|(target_binding, target_binding_node)| {
                    native_top_level_root_containing_node(
                        &index,
                        &top_level_roots,
                        *target_binding_node,
                    )
                    .map(|root| (target_binding.clone(), root))
                })
                .collect::<Option<BTreeMap<_, _>>>()
        };
        let Some(root_by_target_binding) = root_by_target_binding else {
            return Ok(false);
        };
        if top_level_segments.len() > 1 || top_level_roots.len() > 1 {
            let mut previous_segment_last = None;
            for (segment_index, segment_roots) in top_level_segments.iter().enumerate() {
                let ordinals = self.lower_native_top_level_fixed_sequence(
                    logical_module,
                    &format!("{group_debug_label}.segment.{segment_index}"),
                    &node_vars,
                    segment_roots,
                )?;
                let Some(segment_first) = ordinals.first().copied() else {
                    continue;
                };
                if let Some(previous_segment_last) = previous_segment_last {
                    self.program.add_atom(SelectorAtom::OrdinalBefore {
                        before: OrdinalTerm::Var {
                            id: previous_segment_last,
                        },
                        after: OrdinalTerm::Var { id: segment_first },
                    });
                }
                previous_segment_last = ordinals.last().copied();
            }
        }

        let alpha_identifier_vars = if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            self.lower_alpha_identifier_variables(logical_module, &facts, &skipped_nodes)
        } else {
            BTreeMap::new()
        };
        let mut exact_identifier_projection_vars = BTreeMap::new();
        for (target_binding, export_name) in exports_by_target {
            let owner = self.owner_for_local_export(logical_module, export_name);
            let root_node = root_by_target_binding
                .get(target_binding)
                .copied()
                .expect("target binding roots were collected for every export");
            self.owner_injectivity_classes.insert(
                owner,
                binding_group_owner_injectivity_class(
                    logical_module,
                    selector,
                    exports_by_target,
                    root_node,
                ),
            );
            let root = node_vars
                .get(&root_node)
                .copied()
                .expect("target binding root should have a node variable");
            self.program.add_atom(SelectorAtom::OwnerTopLevelRoot {
                owner: owner_term(owner),
                root: node_term(root),
            });
            let target_binding_node = target_binding_nodes
                .get(target_binding)
                .copied()
                .expect("target binding nodes were collected for every export");
            match selector.identifiers {
                SourceMatchIdentifierMode::Exact => {
                    let binding_var = self.program.add_variable(
                        VariableDomain::String,
                        Some(format!(
                            "{logical_module}::{group_debug_label}.target_binding.{target_binding}"
                        )),
                    );
                    exact_identifier_projection_vars.insert(target_binding_node, binding_var);
                    self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                        owner: owner_term(owner),
                        binding: string_term(binding_var),
                    });
                }
                SourceMatchIdentifierMode::AlphaAll => {
                    let binding_var = alpha_identifier_vars
                        .get(&target_binding_node)
                        .copied()
                        .expect("alpha_all target binding node should have an identifier variable");
                    self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                        owner: owner_term(owner),
                        binding: string_term(binding_var),
                    });
                }
            }
        }

        self.lower_native_ast_facts(NativeAstFactLowering {
            facts: &facts,
            node_vars: &node_vars,
            skipped_nodes: &skipped_nodes,
            identifier_mode: selector.identifiers,
            alpha_identifier_vars: &alpha_identifier_vars,
            exact_identifier_projection_vars: &exact_identifier_projection_vars,
            child_list_patterns: &child_list_patterns,
            bare_properties: &bare_properties,
            regex_predicates: &regex_predicates,
        });
        Ok(true)
    }

    fn lower_native_ast_facts(&mut self, lowering: NativeAstFactLowering<'_>) {
        let NativeAstFactLowering {
            facts,
            node_vars,
            skipped_nodes,
            identifier_mode,
            alpha_identifier_vars,
            exact_identifier_projection_vars,
            child_list_patterns,
            bare_properties,
            regex_predicates,
        } = lowering;
        let bare_property_structural_nodes = bare_properties
            .values()
            .flat_map(|property| property.structural_skip_nodes.iter().copied())
            .collect::<BTreeSet<_>>();
        let structurally_skipped_node = |node: &NodeId| {
            skipped_nodes.contains(node)
                || bare_properties.contains_key(node)
                || bare_property_structural_nodes.contains(node)
        };
        let mut child_counts: BTreeMap<NodeId, u32> =
            facts.node_kind.iter().map(|(node, _)| (*node, 0)).collect();
        for (parent, index, _child) in &facts.child {
            let count = child_counts.entry(*parent).or_insert(0);
            *count = (*count).max(index + 1);
        }
        for (node, kind) in &facts.node_kind {
            if skipped_nodes.contains(node) {
                continue;
            }
            let Some(node_var) = node_vars.get(node).copied() else {
                continue;
            };
            if let Some(property) = bare_properties.get(node) {
                let identifier = alpha_identifier_vars
                    .get(&property.identifier_node)
                    .copied()
                    .expect("bare property identifier should have an alpha variable");
                self.program.add_atom(SelectorAtom::AstBareProperty {
                    node: node_term(node_var),
                    key: const_str(&property.key),
                    identifier: string_term(identifier),
                    is_binding: property.is_binding,
                });
                continue;
            }
            if bare_property_structural_nodes.contains(node) {
                continue;
            }
            if let Some(pattern) = regex_predicates.pattern_by_call.get(node) {
                self.program
                    .add_atom(SelectorAtom::AstStringLiteralMatchingRegex {
                        node: node_term(node_var),
                        pattern: const_str(pattern),
                    });
                continue;
            }
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
            if bare_properties.contains_key(parent) {
                continue;
            }
            if regex_predicates.pattern_by_call.contains_key(parent) {
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
            if structurally_skipped_node(class_node) {
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
            if structurally_skipped_node(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstStringLiteral { node, value }
            });
        }
        for (node, value) in &facts.num_lit {
            if structurally_skipped_node(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstNumberLiteral { node, value }
            });
        }
        for (node, value) in &facts.bool_lit {
            if structurally_skipped_node(node) {
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
            if structurally_skipped_node(node) {
                continue;
            }
            match identifier_mode {
                SourceMatchIdentifierMode::Exact => {
                    if let Some(identifier) = exact_identifier_projection_vars.get(node).copied() {
                        if let Some(node) = node_vars.get(node).copied() {
                            self.program.add_atom(SelectorAtom::AstIdentifierName {
                                node: node_term(node),
                                value: string_term(identifier),
                            });
                        }
                    } else {
                        self.add_ast_string_label(node_vars, *node, value, |node, value| {
                            SelectorAtom::AstIdentifierName { node, value }
                        });
                    }
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
            if structurally_skipped_node(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstPropertyName { node, value }
            });
        }
        for (node, value) in &facts.operator {
            if structurally_skipped_node(node) {
                continue;
            }
            self.add_ast_string_label(node_vars, *node, value, |node, value| {
                SelectorAtom::AstOperator { node, value }
            });
        }
        for (node, pattern, flags) in &facts.regex {
            if structurally_skipped_node(node) {
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
        if facts.top_level.is_empty() {
            return BTreeMap::new();
        }
        let index = AlphaIdentifierIndex::new(facts);
        let mut state = AlphaIdentifierState::default();
        let mut result = BTreeMap::new();
        for (root, _ordinal) in &facts.top_level {
            result.extend(self.lower_alpha_identifier_node(
                logical_module,
                &index,
                skipped_nodes,
                &mut state,
                *root,
            ));
        }
        self.add_alpha_identifier_frame_all_different(logical_module, state.current_frame());
        result
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
        match kind {
            Some(NodeKind::FnDecl | NodeKind::ClassDecl) => {
                return self.lower_alpha_declaration_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    node,
                );
            }
            Some(NodeKind::FnExpr | NodeKind::ClassExpr) => {
                return self.lower_alpha_named_expression_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    node,
                );
            }
            Some(NodeKind::VarDecl) => {
                return self.lower_alpha_var_decl_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    node,
                );
            }
            _ => {}
        }
        if let (Some(kind), Some(name)) = (kind, index.ident_name.get(&node)) {
            let identifier = if matches!(kind, NodeKind::BindingIdent | NodeKind::PatAssign) {
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

        let pushed_scope = kind.and_then(alpha_scope_kind);
        if let Some(scope_kind) = pushed_scope {
            state.frames.push(AlphaIdentifierFrame::new(scope_kind));
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
        if pushed_scope.is_some() {
            let frame = state.frames.pop().expect("pushed alpha scope should exist");
            self.add_alpha_identifier_frame_all_different(logical_module, &frame);
        }
        result
    }

    fn lower_alpha_var_decl_identifier_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        let mut result = BTreeMap::new();
        let binding_scope = match index.operator.get(&node).map(String::as_str) {
            Some("var") => AlphaBindingScope::NearestVar,
            _ => AlphaBindingScope::Current,
        };
        for child in index.children_by_parent.get(&node).into_iter().flatten() {
            if index.node_kind.get(child) == Some(&NodeKind::VarDeclarator) {
                result.extend(self.lower_alpha_var_declarator_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    *child,
                    binding_scope,
                ));
            } else {
                result.extend(self.lower_alpha_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    *child,
                ));
            }
        }
        result
    }

    fn lower_alpha_var_declarator_identifier_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
        binding_scope: AlphaBindingScope,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        if skipped_nodes.contains(&node) {
            return BTreeMap::new();
        }
        let mut result = BTreeMap::new();

        for (child_index, child) in index
            .children_by_parent
            .get(&node)
            .into_iter()
            .flatten()
            .copied()
            .enumerate()
        {
            if child_index == 0 {
                result.extend(self.lower_alpha_pattern_binding_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    child,
                    binding_scope,
                ));
            } else {
                result.extend(self.lower_alpha_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    child,
                ));
            }
        }
        result
    }

    fn lower_alpha_pattern_binding_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
        binding_scope: AlphaBindingScope,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        if skipped_nodes.contains(&node) {
            return BTreeMap::new();
        }

        let mut result = BTreeMap::new();
        match index.node_kind.get(&node).copied() {
            Some(NodeKind::BindingIdent) | Some(NodeKind::PatAssign) => {
                if let Some(name) = index.ident_name.get(&node) {
                    let identifier = self.alpha_binding_identifier_in_scope(
                        logical_module,
                        state,
                        name,
                        binding_scope,
                    );
                    result.insert(node, identifier);
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
            }
            Some(NodeKind::AssignPat) => {
                for (child_index, child) in index
                    .children_by_parent
                    .get(&node)
                    .into_iter()
                    .flatten()
                    .copied()
                    .enumerate()
                {
                    if child_index == 0 {
                        result.extend(self.lower_alpha_pattern_binding_node(
                            logical_module,
                            index,
                            skipped_nodes,
                            state,
                            child,
                            binding_scope,
                        ));
                    } else {
                        result.extend(self.lower_alpha_identifier_node(
                            logical_module,
                            index,
                            skipped_nodes,
                            state,
                            child,
                        ));
                    }
                }
            }
            Some(NodeKind::PatKeyValue) => {
                if let Some(value) = child_at(index, node, 1) {
                    result.extend(self.lower_alpha_pattern_binding_node(
                        logical_module,
                        index,
                        skipped_nodes,
                        state,
                        value,
                        binding_scope,
                    ));
                }
            }
            Some(NodeKind::ArrayPat) | Some(NodeKind::ObjectPat) | Some(NodeKind::RestPat) => {
                for child in index.children_by_parent.get(&node).into_iter().flatten() {
                    result.extend(self.lower_alpha_pattern_binding_node(
                        logical_module,
                        index,
                        skipped_nodes,
                        state,
                        *child,
                        binding_scope,
                    ));
                }
            }
            _ => {
                result.extend(self.lower_alpha_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    node,
                ));
            }
        }
        result
    }

    fn lower_alpha_declaration_identifier_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        let mut result = BTreeMap::new();
        for (child_index, child) in index
            .children_by_parent
            .get(&node)
            .into_iter()
            .flatten()
            .copied()
            .enumerate()
        {
            if child_index == 0
                && index.node_kind.get(&child) == Some(&NodeKind::Ident)
                && let Some(name) = index.ident_name.get(&child)
            {
                let identifier = self.alpha_binding_identifier(logical_module, state, name);
                result.insert(child, identifier);
                continue;
            }
            result.extend(self.lower_alpha_identifier_node(
                logical_module,
                index,
                skipped_nodes,
                state,
                child,
            ));
        }
        result
    }

    fn lower_alpha_named_expression_identifier_node(
        &mut self,
        logical_module: &str,
        index: &AlphaIdentifierIndex,
        skipped_nodes: &BTreeSet<NodeId>,
        state: &mut AlphaIdentifierState,
        node: NodeId,
    ) -> BTreeMap<NodeId, SelectorVariableId> {
        let children = index
            .children_by_parent
            .get(&node)
            .cloned()
            .unwrap_or_default();
        let Some(name_child) = children.first().copied().filter(|child| {
            index.node_kind.get(child) == Some(&NodeKind::Ident)
                && index.ident_name.contains_key(child)
        }) else {
            let mut result = BTreeMap::new();
            for child in children {
                result.extend(self.lower_alpha_identifier_node(
                    logical_module,
                    index,
                    skipped_nodes,
                    state,
                    child,
                ));
            }
            return result;
        };

        state.frames.push(AlphaIdentifierFrame::default());
        let mut result = BTreeMap::new();
        let name = index
            .ident_name
            .get(&name_child)
            .expect("checked named expression identifier spelling");
        let identifier = self.alpha_binding_identifier(logical_module, state, name);
        result.insert(name_child, identifier);
        for child in children {
            if child == name_child {
                continue;
            }
            result.extend(self.lower_alpha_identifier_node(
                logical_module,
                index,
                skipped_nodes,
                state,
                child,
            ));
        }
        let frame = state
            .frames
            .pop()
            .expect("pushed named-expression alpha scope should exist");
        self.add_alpha_identifier_frame_all_different(logical_module, &frame);
        result
    }

    fn alpha_binding_identifier(
        &mut self,
        logical_module: &str,
        state: &mut AlphaIdentifierState,
        name: &str,
    ) -> SelectorVariableId {
        self.alpha_binding_identifier_in_scope(
            logical_module,
            state,
            name,
            AlphaBindingScope::Current,
        )
    }

    fn alpha_binding_identifier_in_scope(
        &mut self,
        logical_module: &str,
        state: &mut AlphaIdentifierState,
        name: &str,
        binding_scope: AlphaBindingScope,
    ) -> SelectorVariableId {
        let frame_index = state.binding_frame_index(binding_scope);
        if let Some(existing) = state.frames[frame_index].by_name.get(name).copied() {
            return existing;
        }
        let distinct_from = state.frames[frame_index]
            .by_name
            .values()
            .copied()
            .collect();
        let identifier = self.new_alpha_identifier_variable(logical_module, state, name);
        state.frames[frame_index]
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
        let frame_index = state.binding_frame_index(AlphaBindingScope::NearestVar);
        state.frames[frame_index]
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

    fn add_alpha_identifier_frame_all_different(
        &mut self,
        logical_module: &str,
        frame: &AlphaIdentifierFrame,
    ) {
        let mut variables = frame.by_name.values().copied().collect::<Vec<_>>();
        variables.sort();
        variables.dedup();
        if variables.len() < 2 {
            return;
        }
        self.program.require_variables_all_different(
            variables,
            format!("{logical_module}::source_match.alpha_all.frame"),
        );
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

fn alpha_scope_kind(kind: NodeKind) -> Option<AlphaScopeKind> {
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
    )
    .then_some(AlphaScopeKind::Var)
    .or_else(|| {
        matches!(kind, NodeKind::Block | NodeKind::Catch | NodeKind::Switch)
            .then_some(AlphaScopeKind::Lexical)
    })
}

fn native_target_binding_node(
    index: &AlphaIdentifierIndex,
    top_level_roots: &[NodeId],
    skipped_nodes: &BTreeSet<NodeId>,
    target_binding: &str,
) -> Option<NodeId> {
    if top_level_roots.is_empty() {
        return None;
    }
    let declared_nodes = top_level_roots
        .iter()
        .flat_map(|root| native_declared_binding_nodes(index, skipped_nodes, *root))
        .collect::<Vec<_>>();
    let matches = declared_nodes
        .into_iter()
        .filter(|node| index.ident_name.get(node).map(String::as_str) == Some(target_binding))
        .collect::<Vec<_>>();
    let [node] = matches.as_slice() else {
        return None;
    };
    Some(*node)
}

fn native_top_level_root_containing_node(
    index: &AlphaIdentifierIndex,
    top_level_roots: &[NodeId],
    target: NodeId,
) -> Option<NodeId> {
    let roots = top_level_roots
        .iter()
        .copied()
        .filter(|root| native_node_contains(index, *root, target))
        .collect::<Vec<_>>();
    roots.iter().copied().find(|candidate| {
        !roots
            .iter()
            .any(|other| *other != *candidate && native_node_contains(index, *candidate, *other))
    })
}

fn native_node_contains(index: &AlphaIdentifierIndex, root: NodeId, target: NodeId) -> bool {
    if root == target {
        return true;
    }
    index
        .children_by_parent
        .get(&root)
        .into_iter()
        .flatten()
        .any(|child| native_node_contains(index, *child, target))
}

fn native_module_stmt_list_hole_roots(facts: &ChunkFacts) -> BTreeSet<NodeId> {
    let index = AlphaIdentifierIndex::new(facts);
    facts
        .top_level
        .iter()
        .filter_map(|(root, _ordinal)| {
            if index.node_kind.get(root) != Some(&NodeKind::ExprStmt) {
                return None;
            }
            let is_hole = index.children_by_parent.get(root).is_some_and(|children| {
                let [child] = children.as_slice() else {
                    return false;
                };
                index.node_kind.get(child) == Some(&NodeKind::Ident)
                    && index.ident_name.get(child).is_some_and(|name| {
                        labeled_hole_name_for(name, STMT_LIST_HOLE_KEYWORD).is_some()
                    })
            });
            is_hole.then_some(*root)
        })
        .collect()
}

fn native_pinned_top_level_segments(
    facts: &ChunkFacts,
    module_stmt_list_hole_roots: &BTreeSet<NodeId>,
) -> Vec<Vec<NodeId>> {
    let mut segments = Vec::new();
    let mut current_segment = Vec::new();
    for (root, _ordinal) in &facts.top_level {
        if module_stmt_list_hole_roots.contains(root) {
            if !current_segment.is_empty() {
                segments.push(std::mem::take(&mut current_segment));
            }
            continue;
        }
        current_segment.push(*root);
    }
    if !current_segment.is_empty() {
        segments.push(current_segment);
    }
    segments
}

fn native_single_declared_binding_node(
    index: &AlphaIdentifierIndex,
    top_level_roots: &[NodeId],
    skipped_nodes: &BTreeSet<NodeId>,
) -> Option<NodeId> {
    let [root] = top_level_roots else {
        return None;
    };
    let declared_nodes = native_declared_binding_nodes(index, skipped_nodes, *root);
    let [node] = declared_nodes.as_slice() else {
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
        Some(NodeKind::VarDeclarator) => child_at(index, node, 0)
            .into_iter()
            .flat_map(|pattern| native_pattern_binding_nodes(index, skipped_nodes, pattern))
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
    prop_name: BTreeMap<NodeId, String>,
    operator: BTreeMap<NodeId, String>,
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
            prop_name: facts.prop_name.iter().cloned().collect(),
            operator: facts.operator.iter().cloned().collect(),
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

    fn binding_frame_index(&self, binding_scope: AlphaBindingScope) -> usize {
        match binding_scope {
            AlphaBindingScope::Current => self.frames.len() - 1,
            AlphaBindingScope::NearestVar => self
                .frames
                .iter()
                .rposition(|frame| frame.kind == AlphaScopeKind::Var)
                .unwrap_or(0),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AlphaScopeKind {
    Lexical,
    Var,
}

#[derive(Clone, Copy)]
enum AlphaBindingScope {
    Current,
    NearestVar,
}

struct AlphaIdentifierFrame {
    kind: AlphaScopeKind,
    by_name: BTreeMap<String, SelectorVariableId>,
}

impl AlphaIdentifierFrame {
    fn new(kind: AlphaScopeKind) -> Self {
        Self {
            kind,
            by_name: BTreeMap::new(),
        }
    }
}

impl Default for AlphaIdentifierFrame {
    fn default() -> Self {
        Self::new(AlphaScopeKind::Var)
    }
}

fn native_anonymous_source_match_supported(selector: &AnonymousStatementSelector) -> bool {
    selector.target_binding.is_none()
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

#[derive(Debug, Default)]
struct NativeRegexPredicateIndex {
    pattern_by_call: BTreeMap<NodeId, String>,
    consumed_nodes: BTreeSet<NodeId>,
}

#[derive(Debug, Clone)]
struct NativeBarePropertySelector {
    key: String,
    identifier_node: NodeId,
    is_binding: bool,
    structural_skip_nodes: BTreeSet<NodeId>,
}

fn native_bare_property_selectors(
    facts: &ChunkFacts,
    skipped_nodes: &BTreeSet<NodeId>,
) -> BTreeMap<NodeId, NativeBarePropertySelector> {
    let index = AlphaIdentifierIndex::new(facts);
    let mut result = BTreeMap::new();
    for (node, kind) in &index.node_kind {
        if skipped_nodes.contains(node) {
            continue;
        }
        let property = match kind {
            NodeKind::Shorthand => {
                let Some(name) = index.ident_name.get(node) else {
                    continue;
                };
                if selector_hole_name(name) {
                    continue;
                }
                NativeBarePropertySelector {
                    key: name.clone(),
                    identifier_node: *node,
                    is_binding: false,
                    structural_skip_nodes: BTreeSet::new(),
                }
            }
            NodeKind::KeyValue => {
                let Some(children) = index.children_by_parent.get(node) else {
                    continue;
                };
                let Some((key_node, value_node)) =
                    children.first().copied().zip(children.get(1).copied())
                else {
                    continue;
                };
                if skipped_nodes.contains(&key_node) || skipped_nodes.contains(&value_node) {
                    continue;
                }
                if index.node_kind.get(&value_node) != Some(&NodeKind::Ident) {
                    continue;
                }
                let Some(key) = index.prop_name.get(&key_node) else {
                    continue;
                };
                let Some(identifier) = index.ident_name.get(&value_node) else {
                    continue;
                };
                if selector_hole_name(key) || selector_hole_name(identifier) {
                    continue;
                }
                NativeBarePropertySelector {
                    key: key.clone(),
                    identifier_node: value_node,
                    is_binding: false,
                    structural_skip_nodes: native_bare_property_structural_skip_nodes(
                        &index,
                        &[key_node, value_node],
                    ),
                }
            }
            NodeKind::PatAssign => {
                if index
                    .children_by_parent
                    .get(node)
                    .is_some_and(|children| !children.is_empty())
                {
                    continue;
                }
                let Some(name) = index.ident_name.get(node) else {
                    continue;
                };
                if selector_hole_name(name) {
                    continue;
                }
                NativeBarePropertySelector {
                    key: name.clone(),
                    identifier_node: *node,
                    is_binding: true,
                    structural_skip_nodes: BTreeSet::new(),
                }
            }
            NodeKind::PatKeyValue => {
                let Some(children) = index.children_by_parent.get(node) else {
                    continue;
                };
                let Some((key_node, value_node)) =
                    children.first().copied().zip(children.get(1).copied())
                else {
                    continue;
                };
                if skipped_nodes.contains(&key_node) || skipped_nodes.contains(&value_node) {
                    continue;
                }
                if index.node_kind.get(&value_node) != Some(&NodeKind::BindingIdent) {
                    continue;
                }
                let Some(key) = index.prop_name.get(&key_node) else {
                    continue;
                };
                let Some(identifier) = index.ident_name.get(&value_node) else {
                    continue;
                };
                if selector_hole_name(key) || selector_hole_name(identifier) {
                    continue;
                }
                NativeBarePropertySelector {
                    key: key.clone(),
                    identifier_node: value_node,
                    is_binding: true,
                    structural_skip_nodes: native_bare_property_structural_skip_nodes(
                        &index,
                        &[key_node, value_node],
                    ),
                }
            }
            _ => continue,
        };
        result.insert(*node, property);
    }
    result
}

fn native_bare_property_structural_skip_nodes(
    index: &AlphaIdentifierIndex,
    roots: &[NodeId],
) -> BTreeSet<NodeId> {
    let mut skipped = BTreeSet::new();
    let mut stack = roots.to_vec();
    while let Some(node) = stack.pop() {
        if !skipped.insert(node) {
            continue;
        }
        if let Some(children) = index.children_by_parent.get(&node) {
            stack.extend(children.iter().copied());
        }
    }
    skipped
}

fn native_string_literal_regex_predicates(facts: &ChunkFacts) -> NativeRegexPredicateIndex {
    let node_kind: BTreeMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let ident_name: BTreeMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(node, name)| (*node, name.as_str()))
        .collect();
    let str_lit: BTreeMap<NodeId, &str> = facts
        .str_lit
        .iter()
        .map(|(node, value)| (*node, value.as_str()))
        .collect();
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

    let mut result = NativeRegexPredicateIndex::default();
    for (node, kind) in &node_kind {
        if *kind != NodeKind::Call {
            continue;
        }
        let Some(children) = children_by_parent.get(node) else {
            continue;
        };
        let [(_, callee), (_, arg)] = children.as_slice() else {
            continue;
        };
        if node_kind.get(callee) != Some(&NodeKind::Ident)
            || ident_name.get(callee).copied() != Some(STRING_LITERAL_REGEX_PREDICATE)
            || node_kind.get(arg) != Some(&NodeKind::StrLit)
        {
            continue;
        }
        let Some(pattern) = str_lit.get(arg).copied() else {
            continue;
        };
        result.pattern_by_call.insert(*node, pattern.to_string());
        result.consumed_nodes.insert(*callee);
        result.consumed_nodes.insert(*arg);
    }
    result
}

fn selector_hole_name(name: &str) -> bool {
    name == STRING_LITERAL_REGEX_PREDICATE
        || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        || hole_name_for(name, EXPR_HOLE_KEYWORD).is_some()
        || hole_name_for(name, STMT_HOLE_KEYWORD).is_some()
        || [
            STMT_LIST_HOLE_KEYWORD,
            CASE_REST_HOLE_KEYWORD,
            DECLARATORS_HOLE_KEYWORD,
            ARGS_HOLE_KEYWORD,
            ARRAY_ELEMENTS_HOLE_KEYWORD,
        ]
        .iter()
        .any(|keyword| labeled_hole_name_for(name, keyword).is_some())
}

fn native_single_node_hole_roots(
    facts: &ChunkFacts,
    consumed_nodes: &BTreeSet<NodeId>,
) -> Option<BTreeSet<NodeId>> {
    let node_kind: BTreeMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let mut parent_by_child = BTreeMap::<NodeId, (NodeId, u32)>::new();
    let mut child_counts = BTreeMap::<NodeId, u32>::new();
    for (parent, index, child) in &facts.child {
        parent_by_child.insert(*child, (*parent, *index));
        *child_counts.entry(*parent).or_insert(0) += 1;
    }

    let mut roots = BTreeSet::new();
    for (node, name) in &facts.ident_name {
        if consumed_nodes.contains(node) {
            continue;
        }
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
    for (node, name) in &facts.prop_name {
        if consumed_nodes.contains(node) {
            continue;
        }
        if selector_hole_name(name) && !native_child_list_hole_name(name) {
            if native_contextual_anything_prop_child_list_carrier(
                *node,
                name,
                &node_kind,
                &parent_by_child,
                &child_counts,
            ) {
                continue;
            }
            return None;
        }
    }
    Some(roots)
}

fn native_contextual_anything_prop_child_list_carrier(
    node: NodeId,
    name: &str,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    parent_by_child: &BTreeMap<NodeId, (NodeId, u32)>,
    child_counts: &BTreeMap<NodeId, u32>,
) -> bool {
    if hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_none() {
        return false;
    }
    let Some((parent, index)) = parent_by_child.get(&node).copied() else {
        return false;
    };
    index == 0
        && node_kind.get(&parent) == Some(&NodeKind::ClassProp)
        && child_counts.get(&parent).copied().unwrap_or(0) == 1
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

    if kind == Some(NodeKind::BindingIdent) && hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
    {
        return Some(HoleClassification::Supported { root: node });
    }

    if hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        && (kind == Some(NodeKind::Shorthand)
            || (kind == Some(NodeKind::PatAssign)
                && child_counts.get(&node).copied().unwrap_or(0) == 0))
    {
        return Some(HoleClassification::NotHole);
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
        DECLARATORS_HOLE_KEYWORD,
        CASE_REST_HOLE_KEYWORD,
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
    let prop_name: BTreeMap<NodeId, &str> = facts
        .prop_name
        .iter()
        .map(|(node, name)| (*node, name.as_str()))
        .collect();
    let child_list_idents = ident_name
        .iter()
        .filter_map(|(node, name)| {
            if !skipped_nodes.contains(node) && native_child_list_hole_name(name) {
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
            if !skipped_nodes.contains(node) && native_child_list_hole_name(name) {
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
        for (index, child) in children.iter().enumerate() {
            if let Some(ident) = native_child_list_carrier_ident(
                parent_kind,
                *child,
                &node_kind,
                &children_by_parent,
                &ident_name,
                &prop_name,
            ) {
                hole_positions.insert(index);
                valid_child_list_nodes.insert(ident);
                collect_native_child_list_subtree(*child, &children_by_parent, skipped_nodes);
            }
        }
        if hole_positions.is_empty() {
            if child_list_hole_nodes.is_empty()
                && let Some(declarator) =
                    single_declarator_segment(parent_kind, &children, &node_kind)
            {
                patterns.insert(
                    *parent,
                    NativeChildListPattern {
                        start_index,
                        segments: vec![vec![declarator]],
                        anchored_left: false,
                        anchored_right: false,
                    },
                );
            }
            continue;
        }

        let (segments, anchored_left, anchored_right) =
            child_list_segments(&children, &hole_positions);
        if segments.is_empty() && !native_all_hole_child_list_pattern_supported(parent_kind) {
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

    if child_list_hole_nodes.is_subset(&valid_child_list_nodes) {
        Some(patterns)
    } else {
        None
    }
}

fn native_all_hole_child_list_pattern_supported(parent_kind: NodeKind) -> bool {
    matches!(
        parent_kind,
        NodeKind::Block
            | NodeKind::SwitchCase
            | NodeKind::Array
            | NodeKind::VarDecl
            | NodeKind::Class
            | NodeKind::Object
            | NodeKind::ObjectPat
            | NodeKind::Call
            | NodeKind::New
            | NodeKind::OptCall
            | NodeKind::Switch
    )
}

fn single_declarator_segment(
    parent_kind: NodeKind,
    children: &[NodeId],
    node_kind: &BTreeMap<NodeId, NodeKind>,
) -> Option<NodeId> {
    if parent_kind != NodeKind::VarDecl {
        return None;
    }
    let [declarator] = children else {
        return None;
    };
    if node_kind.get(declarator) == Some(&NodeKind::VarDeclarator) {
        Some(*declarator)
    } else {
        None
    }
}

fn native_child_list_start_index(parent_kind: NodeKind) -> Option<u32> {
    match parent_kind {
        NodeKind::Block
        | NodeKind::SwitchCase
        | NodeKind::Array
        | NodeKind::VarDecl
        | NodeKind::Class
        | NodeKind::Object
        | NodeKind::ObjectPat => Some(0),
        NodeKind::Call | NodeKind::New | NodeKind::OptCall => Some(1),
        NodeKind::Switch => Some(1),
        _ => None,
    }
}

fn native_child_list_carrier_ident(
    parent_kind: NodeKind,
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ident_name: &BTreeMap<NodeId, &str>,
    prop_name: &BTreeMap<NodeId, &str>,
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
        NodeKind::VarDecl => {
            declarators_carrier_ident(node, node_kind, children_by_parent, ident_name)
        }
        NodeKind::Class => class_rest_carrier_key(node, node_kind, children_by_parent, prop_name),
        NodeKind::Switch => {
            case_rest_carrier_ident(node, node_kind, children_by_parent, ident_name)
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
                && ident_name
                    .get(&node)
                    .is_some_and(|name| hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
            {
                Some(node)
            } else {
                None
            }
        }
        NodeKind::ObjectPat => {
            if node_kind.get(&node) == Some(&NodeKind::PatAssign)
                && children_by_parent.get(&node).is_none_or(Vec::is_empty)
                && ident_name
                    .get(&node)
                    .is_some_and(|name| hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
            {
                Some(node)
            } else {
                None
            }
        }
        _ => None,
    }
}

fn declarators_carrier_ident(
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ident_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    if node_kind.get(&node) != Some(&NodeKind::VarDeclarator) {
        return None;
    }
    let (_, binding) = children_by_parent.get(&node)?.first()?;
    if node_kind.get(binding) == Some(&NodeKind::BindingIdent)
        && ident_name.get(binding).is_some_and(|name| {
            labeled_hole_name_for(name, DECLARATORS_HOLE_KEYWORD).is_some()
                || hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some()
        })
    {
        Some(*binding)
    } else {
        None
    }
}

fn class_rest_carrier_key(
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    prop_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    if node_kind.get(&node) != Some(&NodeKind::ClassProp) {
        return None;
    }
    let [(_, key)] = children_by_parent.get(&node)?.as_slice() else {
        return None;
    };
    if prop_name
        .get(key)
        .is_some_and(|name| hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
    {
        Some(*key)
    } else {
        None
    }
}

fn case_rest_carrier_ident(
    node: NodeId,
    node_kind: &BTreeMap<NodeId, NodeKind>,
    children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ident_name: &BTreeMap<NodeId, &str>,
) -> Option<NodeId> {
    if node_kind.get(&node) != Some(&NodeKind::SwitchCase) {
        return None;
    }
    let [(_, test)] = children_by_parent.get(&node)?.as_slice() else {
        return None;
    };
    if node_kind.get(test) == Some(&NodeKind::Ident)
        && ident_name
            .get(test)
            .is_some_and(|name| labeled_hole_name_for(name, CASE_REST_HOLE_KEYWORD).is_some())
    {
        Some(*test)
    } else {
        None
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

fn selector_origin_ref(selector: MemberSelectorSpecRef<'_>) -> ClaimOrigin {
    match relation_for_selector_ref(selector) {
        Some(relation) => ClaimOrigin::RelationalSelector { relation },
        None => ClaimOrigin::MemberSelector,
    }
}

fn binding_group_owner_injectivity_class(
    logical_module: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
    root: NodeId,
) -> String {
    let target_bindings = exports_by_target
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "{logical_module}|source_matches|{}|{target_bindings}|root:{root}",
        selector.match_source
    )
}

fn grouped_source_matches_debug_label(exports_by_target: &BTreeMap<String, String>) -> String {
    let target_bindings = exports_by_target
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(",");
    format!("source_matches.{target_bindings}")
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
    UnsupportedSourceMatch {
        selector_kind: &'static str,
        reason: String,
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
            Self::UnsupportedSourceMatch {
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

fn relation_for_selector_ref(selector: MemberSelectorSpecRef<'_>) -> Option<RelationalPrimitive> {
    match selector {
        MemberSelectorSpecRef::CrossRef(_) => Some(RelationalPrimitive::CrossRef),
        MemberSelectorSpecRef::ReadsMember(_) => Some(RelationalPrimitive::ReadsMember),
        MemberSelectorSpecRef::MemberOfModule(_) => Some(RelationalPrimitive::MemberOfModule),
        MemberSelectorSpecRef::PassedToCall(_) => Some(RelationalPrimitive::PassedToCall),
        MemberSelectorSpecRef::MakesDecorateCall(_) => Some(RelationalPrimitive::MakesDecorateCall),
        MemberSelectorSpecRef::IntrinsicAlias(_) => Some(RelationalPrimitive::IntrinsicAlias),
        MemberSelectorSpecRef::Binding(_) | MemberSelectorSpecRef::SourceMatch(_) => None,
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
            lowered
                .program
                .atoms
                .iter()
                .any(|atom| matches!(atom, SelectorAtom::AstSuperClass { .. }))
        );
    }

    #[test]
    fn exact_source_match_sparse_child_indices_use_index_arity() {
        let selector = AnonymousStatementSelector::exact("!(function () {})();");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        let fn_expr_node = lowered
            .program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                SelectorAtom::AstKind {
                    node: NodeTerm::Var { id },
                    node_kind: NodeKind::FnExpr,
                } => Some(*id),
                _ => None,
            })
            .expect("anonymous function expression should be lowered");
        let fn_expr_child_count = lowered
            .program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                SelectorAtom::AstChildCount {
                    node: NodeTerm::Var { id },
                    count,
                } if *id == fn_expr_node => Some(*count),
                _ => None,
            })
            .expect("function expression should carry an arity constraint");
        assert_eq!(fn_expr_child_count, 2);
    }

    #[test]
    fn multi_statement_source_match_lowers_to_native_window_constraints() {
        let mut selector = AnonymousStatementSelector::exact(
            "function helper(value) { return value + 1; }\nconst selected = makeThing();",
        );
        selector.target_binding = Some("selected".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        let offsets = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OrdinalOffset { offset, .. } => Some(*offset),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(offsets, vec![-1, 0]);
        assert_eq!(
            lowered
                .program
                .atoms
                .iter()
                .filter(|atom| matches!(atom, SelectorAtom::AstTopLevel { .. }))
                .count(),
            2
        );
    }

    #[test]
    fn exact_source_match_string_literal_regex_predicate_lowers_natively() {
        let selector = AnonymousStatementSelector::exact(
            r#"const readableStyle = STR_LITERAL_MATCHING_RE("^WidgetShell-[0-9]+$");"#,
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            lowered.program.atoms.iter().any(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstStringLiteralMatchingRegex {
                        pattern: StringTerm::Const { value },
                        ..
                    } if value == "^WidgetShell-[0-9]+$"
                )
            }),
            "expected a native regex string-literal atom: {:#?}",
            lowered.program.atoms
        );
        assert!(
            !lowered.program.atoms.iter().any(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstIdentifierName {
                        value: StringTerm::Const { value },
                        ..
                    } if value == STRING_LITERAL_REGEX_PREDICATE
                )
            }),
            "predicate callee should be consumed, not matched as a real identifier"
        );
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
        assert_eq!(lowered.program.all_different_variables.len(), 1);
        assert_eq!(
            lowered.program.all_different_variables[0].variables,
            vec![identifier_vars[0], identifier_vars[1], identifier_vars[2]]
        );
        assert_eq!(
            lowered.program.all_different_variables[0].label,
            "runtime/widgets::source_match.alpha_all.frame"
        );
    }

    #[test]
    fn alpha_all_source_match_keeps_property_names_exact() {
        let mut selector =
            AnonymousStatementSelector::exact("function a() { return object.stableName; }");
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
        let mut selector = AnonymousStatementSelector::exact("function a() { return b; }");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("a".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn alpha_all_target_binding_in_multideclarator_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact(
            r#"const first = build("left"), second = build("right");"#,
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("second".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Selected",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        let target_owner = lowered.program.targets[lowered.target.0].owner;
        let projected_bindings = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Var { id: binding },
                } if *id == target_owner => Some(*binding),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(
            projected_bindings.len(),
            1,
            "target owner should project exactly one declared binding variable",
        );
    }

    #[test]
    fn alpha_all_grouped_source_matches_lowers_to_one_shared_root() {
        let mut group_selector = AnonymousStatementSelector::exact(
            r#"const first = build("a"), second = build(first);"#,
        );
        group_selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let mut first_selector = group_selector.clone();
        first_selector.target_binding = Some("first".to_string());
        let mut second_selector = group_selector.clone();
        second_selector.target_binding = Some("second".to_string());

        let mut builder = MemberSelectorProgramBuilder::new(context());
        let first_target = builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "First",
                &MemberSelectorSpec::SourceMatch(first_selector),
            )
            .unwrap();
        let second_target = builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "Second",
                &MemberSelectorSpec::SourceMatch(second_selector),
            )
            .unwrap();
        assert!(
            builder
                .try_lower_native_source_match_group(
                    "runtime/widgets",
                    &group_selector,
                    &BTreeMap::from([
                        ("first".to_string(), "First".to_string()),
                        ("second".to_string(), "Second".to_string()),
                    ]),
                )
                .unwrap()
        );
        let program = builder.into_program().unwrap();
        let first_owner = program.targets[first_target.0].owner;
        let second_owner = program.targets[second_target.0].owner;
        let shared_roots = program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OwnerTopLevelRoot {
                    owner: OwnerTerm::Var { id },
                    root: NodeTerm::Var { id: root },
                } if *id == first_owner || *id == second_owner => Some(*root),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(shared_roots.len(), 2);
        assert_eq!(shared_roots[0], shared_roots[1]);
        assert!(
            program.all_different.is_empty(),
            "same-root binding group exports must not be forced to distinct owners"
        );

        let projected_bindings = program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Var { id: binding },
                } if *id == first_owner || *id == second_owner => Some(*binding),
                _ => None,
            })
            .collect::<BTreeSet<_>>();
        assert_eq!(projected_bindings.len(), 2);
    }

    #[test]
    fn multi_statement_grouped_source_matches_lowers_to_native_sequence() {
        let mut group_selector = AnonymousStatementSelector::exact(
            r#"const first = makeFirst();
function second() {
    return first.value;
}"#,
        );
        group_selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let mut first_selector = group_selector.clone();
        first_selector.target_binding = Some("first".to_string());
        let mut second_selector = group_selector.clone();
        second_selector.target_binding = Some("second".to_string());

        let mut builder = MemberSelectorProgramBuilder::new(context());
        let first_target = builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "First",
                &MemberSelectorSpec::SourceMatch(first_selector),
            )
            .unwrap();
        let second_target = builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "Second",
                &MemberSelectorSpec::SourceMatch(second_selector),
            )
            .unwrap();
        assert!(
            builder
                .try_lower_native_source_match_group(
                    "runtime/widgets",
                    &group_selector,
                    &BTreeMap::from([
                        ("first".to_string(), "First".to_string()),
                        ("second".to_string(), "Second".to_string()),
                    ]),
                )
                .unwrap()
        );
        let program = builder.into_program().unwrap();
        let first_owner = program.targets[first_target.0].owner;
        let second_owner = program.targets[second_target.0].owner;
        let roots = program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OwnerTopLevelRoot {
                    owner: OwnerTerm::Var { id },
                    root: NodeTerm::Var { id: root },
                } if *id == first_owner || *id == second_owner => Some(*root),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(roots.len(), 2);
        assert_ne!(roots[0], roots[1]);
        assert_eq!(
            program
                .atoms
                .iter()
                .filter(|atom| matches!(atom, SelectorAtom::AstTopLevel { .. }))
                .count(),
            2
        );
        let offsets = program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OrdinalOffset { offset, .. } => Some(*offset),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(offsets, vec![0, 1]);
    }

    #[test]
    fn grouped_source_matches_with_outer_stmt_list_holes_lowers_to_native_sequence() {
        let mut group_selector = AnonymousStatementSelector::exact(
            r#"STMT_LIST_HEAD;
const first = makeFirst();
function second() {
    return first.value;
}
STMT_LIST_TAIL;"#,
        );
        group_selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let mut first_selector = group_selector.clone();
        first_selector.target_binding = Some("first".to_string());
        let mut second_selector = group_selector.clone();
        second_selector.target_binding = Some("second".to_string());

        let mut builder = MemberSelectorProgramBuilder::new(context());
        builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "First",
                &MemberSelectorSpec::SourceMatch(first_selector),
            )
            .unwrap();
        builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "Second",
                &MemberSelectorSpec::SourceMatch(second_selector),
            )
            .unwrap();
        assert!(
            builder
                .try_lower_native_source_match_group(
                    "runtime/widgets",
                    &group_selector,
                    &BTreeMap::from([
                        ("first".to_string(), "First".to_string()),
                        ("second".to_string(), "Second".to_string()),
                    ]),
                )
                .unwrap()
        );
        let program = builder.into_program().unwrap();
        assert_eq!(
            program
                .atoms
                .iter()
                .filter(|atom| matches!(atom, SelectorAtom::AstTopLevel { .. }))
                .count(),
            2,
            "only pinned top-level statements should get top-level ordinal constraints"
        );
        assert!(
            !program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value.starts_with(STMT_LIST_HOLE_KEYWORD)
            )),
            "module-level STMT_LIST labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn grouped_source_matches_with_internal_stmt_list_hole_lowers_to_native_sequence() {
        let mut group_selector = AnonymousStatementSelector::exact(
            r#"const first = makeFirst();
STMT_LIST_MIDDLE;
function second() {
    return first.value;
}"#,
        );
        group_selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let mut first_selector = group_selector.clone();
        first_selector.target_binding = Some("first".to_string());
        let mut second_selector = group_selector.clone();
        second_selector.target_binding = Some("second".to_string());

        let mut builder = MemberSelectorProgramBuilder::new(context());
        builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "First",
                &MemberSelectorSpec::SourceMatch(first_selector),
            )
            .unwrap();
        builder
            .declare_member_target_in_module(
                "runtime/widgets",
                "Second",
                &MemberSelectorSpec::SourceMatch(second_selector),
            )
            .unwrap();

        assert!(
            builder
                .try_lower_native_source_match_group(
                    "runtime/widgets",
                    &group_selector,
                    &BTreeMap::from([
                        ("first".to_string(), "First".to_string()),
                        ("second".to_string(), "Second".to_string()),
                    ]),
                )
                .unwrap(),
            "internal top-level STMT_LIST should lower to an ordinal-before constraint"
        );
        let program = builder.into_program().unwrap();
        assert!(program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OrdinalBefore {
                before: OrdinalTerm::Var { .. },
                after: OrdinalTerm::Var { .. },
            }
        )));
    }

    #[test]
    fn alpha_all_var_decl_without_target_binding_lowers_natively() {
        let mut selector =
            AnonymousStatementSelector::exact(r#"const readable = { kind: "selected" };"#);
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn alpha_all_single_declarator_source_match_lowers_natively() {
        let mut selector =
            AnonymousStatementSelector::exact(r#"const readable = { kind: "selected" };"#);
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                anchored_left: false,
                anchored_right: false,
                segments,
                ..
            } if segments.len() == 1
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
    fn source_match_with_all_stmt_list_lowers_natively_without_block_arity() {
        let selector = AnonymousStatementSelector::exact("function readable() { STMT_LIST_BODY; }");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
        let block_nodes = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstKind {
                    node: NodeTerm::Var { id },
                    node_kind,
                } if *node_kind == NodeKind::Block => Some(*id),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(!block_nodes.is_empty());
        for block_node in block_nodes {
            assert!(
                !lowered.program.atoms.iter().any(|atom| matches!(
                    atom,
                    SelectorAtom::AstChildCount {
                        node: NodeTerm::Var { id },
                        ..
                    } if *id == block_node
                )),
                "bare STMT_LIST block selector must not constrain block child count"
            );
        }
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
    fn exact_source_match_with_all_argument_run_hole_lowers_natively_without_call_arity() {
        let selector =
            AnonymousStatementSelector::exact(r#"const selectedValue = joinParts(ARGS);"#);
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ARGS"
            )),
            "ARGS is hole syntax, not an identifier constraint"
        );
        let call_nodes = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstKind {
                    node: NodeTerm::Var { id },
                    node_kind,
                } if *node_kind == NodeKind::Call => Some(*id),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(!call_nodes.is_empty());
        for call_node in call_nodes {
            assert!(
                !lowered.program.atoms.iter().any(|atom| matches!(
                    atom,
                    SelectorAtom::AstChildCount {
                        node: NodeTerm::Var { id },
                        ..
                    } if *id == call_node
                )),
                "bare ARGS call selector must not constrain call child count"
            );
        }
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
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstChildListPattern {
                start_index: 1,
                anchored_left: false,
                anchored_right: true,
                segments,
                ..
            } if segments.len() == 1
        )));
    }

    #[test]
    fn source_match_with_anything_binding_params_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact(
            "function readable(ANYTHING, ANYTHING) { return 1; }",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ANYTHING"
            )),
            "ANYTHING binding params are hole syntax, not identifier constraints"
        );
        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn source_match_with_anything_object_pattern_value_lowers_natively() {
        let selector =
            AnonymousStatementSelector::exact("const { stable: ANYTHING } = readInput();");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ANYTHING"
            )),
            "ANYTHING pattern values are hole syntax, not identifier constraints"
        );
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstPropertyName {
                value: StringTerm::Const { value },
                ..
            } if value == "stable"
        )));
    }

    #[test]
    fn alpha_all_source_match_bare_object_property_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact("function a(x) { return { x: x }; }");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstBareProperty {
                key: StringTerm::Const { value },
                identifier: StringTerm::Var { .. },
                is_binding: false,
                ..
            } if value == "x"
        )));
    }

    #[test]
    fn alpha_all_source_match_bare_object_pattern_property_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact(
            "function a(input) { const { stable } = input; return stable; }",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstBareProperty {
                key: StringTerm::Const { value },
                identifier: StringTerm::Var { .. },
                is_binding: true,
                ..
            } if value == "stable"
        )));
    }

    #[test]
    fn alpha_all_source_match_sequence_expression_lowers_natively() {
        let mut selector = AnonymousStatementSelector::exact(
            "function a(target, decorators) { return (applyDecorators(target, decorators), target); }",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstKind {
                node_kind: NodeKind::Seq,
                ..
            }
        )));
    }

    #[test]
    fn alpha_all_source_match_assign_property_lowers_natively() {
        let mut selector =
            AnonymousStatementSelector::exact("const selected = { stable = fallbackValue };");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstKind {
                node_kind: NodeKind::AssignProp,
                ..
            }
        )));
    }

    #[test]
    fn alpha_all_source_match_nested_scope_lowers_natively() {
        let mut selector =
            AnonymousStatementSelector::exact("function a(xs) { return xs.map((x) => x.id); }");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        let identifier_var_count = lowered
            .program
            .atoms
            .iter()
            .filter(|atom| {
                matches!(
                    atom,
                    SelectorAtom::AstIdentifierName {
                        value: StringTerm::Var { .. },
                        ..
                    }
                )
            })
            .count();
        assert!(identifier_var_count >= 4);
    }

    #[test]
    fn alpha_all_arrow_param_shadowing_uses_inner_binding_for_inner_refs() {
        let mut selector = AnonymousStatementSelector::exact(
            "function readable(x) { return items.map((x) => x); }",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        assert_eq!(identifier_vars.len(), 5);
        let occurrence_counts = identifier_occurrence_counts(&identifier_vars);
        assert_eq!(
            occurrence_counts
                .values()
                .copied()
                .filter(|count| *count == 2)
                .count(),
            1,
            "the arrow parameter and body reference should share one alpha binding: {occurrence_counts:?}"
        );
        assert!(
            !occurrence_counts.values().any(|count| *count > 2),
            "the inner arrow binding must not merge with the outer parameter: {occurrence_counts:?}"
        );
    }

    #[test]
    fn alpha_all_const_destructuring_in_sibling_blocks_uses_block_scoped_bindings() {
        let mut selector = AnonymousStatementSelector::exact(
            r#"function readable(input) {
  { const { value } = input.left; collect(value); }
  { const { value } = input.right; collect(value); }
}"#,
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        let occurrence_counts = identifier_occurrence_counts(&identifier_vars);
        assert_eq!(
            occurrence_counts
                .values()
                .filter(|count| **count == 1)
                .count(),
            3,
            "expected sibling block shorthand bindings to remain independent in the alpha model: {occurrence_counts:?}"
        );
        assert!(
            !occurrence_counts.values().any(|count| *count == 4),
            "sibling block destructuring bindings must not collapse into one alpha variable: {occurrence_counts:?}"
        );
    }

    #[test]
    fn alpha_all_var_decl_inside_catch_binds_enclosing_var_scope() {
        let mut selector = AnonymousStatementSelector::exact(
            r#"function readable() {
  try {
    risky();
  } catch (err) {
    var hoisted = err;
  }
  return hoisted;
}"#,
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        let occurrence_counts = identifier_occurrence_counts(&identifier_vars);
        assert_eq!(
            occurrence_counts
                .values()
                .copied()
                .filter(|count| *count == 2)
                .count(),
            2,
            "catch param uses and catch-body var/return uses should each pair: {occurrence_counts:?}"
        );
        assert!(
            !occurrence_counts.values().any(|count| *count > 2),
            "catch-local names must not merge with function-scope var names: {occurrence_counts:?}"
        );
    }

    #[test]
    fn alpha_all_switch_let_uses_switch_lexical_scope() {
        let mut selector = AnonymousStatementSelector::exact(
            r#"function readable(input) {
  const value = "outer";
  switch (input.kind) {
    case "left":
      const value = input.left;
      return value;
  }
  return value;
}"#,
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        let occurrence_counts = identifier_occurrence_counts(&identifier_vars);
        assert_eq!(
            occurrence_counts
                .values()
                .copied()
                .filter(|count| *count == 2)
                .count(),
            2,
            "outer and switch-local `value` bindings should each pair with their own return: {occurrence_counts:?}"
        );
        assert!(
            !occurrence_counts.values().any(|count| *count == 4),
            "switch-local lexical bindings must not collapse with the enclosing block binding: {occurrence_counts:?}"
        );
    }

    #[test]
    fn alpha_all_var_decl_inside_switch_binds_enclosing_var_scope() {
        let mut selector = AnonymousStatementSelector::exact(
            r#"function readable(input) {
  switch (input.kind) {
    case "left":
      var hoisted = input.left;
      return hoisted;
  }
  return hoisted;
}"#,
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        let occurrence_counts = identifier_occurrence_counts(&identifier_vars);
        assert!(
            occurrence_counts.values().any(|count| *count == 3),
            "switch-body var binding should pair with both in-switch and after-switch uses: {occurrence_counts:?}"
        );
    }

    #[test]
    fn alpha_all_named_function_expression_name_is_private_to_function_body() {
        let mut selector = AnonymousStatementSelector::exact(
            "const holder = function inner() { return inner; }; const leak = inner;",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("holder".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        assert_eq!(identifier_vars.len(), 5);
        let function_name = identifier_vars[1];
        let body_reference = identifier_vars[2];
        let outside_reference = identifier_vars[4];
        assert_eq!(function_name, body_reference);
        assert_ne!(function_name, outside_reference);
    }

    #[test]
    fn alpha_all_named_class_expression_name_is_private_to_class_body() {
        let mut selector = AnonymousStatementSelector::exact(
            "const holder = class Inner { method() { return Inner; } }; const leak = Inner;",
        );
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("holder".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let identifier_vars = alpha_identifier_vars_in_source_order(&lowered.program);
        assert_eq!(identifier_vars.len(), 5);
        let class_name = identifier_vars[1];
        let method_reference = identifier_vars[2];
        let outside_reference = identifier_vars[4];
        assert_eq!(class_name, method_reference);
        assert_ne!(class_name, outside_reference);
    }

    #[test]
    fn alpha_all_class_source_match_with_target_binding_stays_native() {
        let mut selector =
            AnonymousStatementSelector::exact("class A { method() { return \"selected\"; } }");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        selector.target_binding = Some("A".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn alpha_all_function_without_target_binding_stays_native() {
        let mut selector =
            AnonymousStatementSelector::exact("function a() { return \"selected\"; }");
        selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id },
                binding: StringTerm::Var { .. },
            } if *id == target_owner
        )));
    }

    #[test]
    fn misplaced_argument_run_hole_fails_closed() {
        let selector = AnonymousStatementSelector::exact("const a = ARGS;");
        let error = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap_err();

        match error {
            SelectorIrLoweringError::Unsupported {
                selector_kind: "source_match",
                reason: "selector shape is not yet supported by native selector IR",
            } => {}
            SelectorIrLoweringError::UnsupportedSourceMatch {
                selector_kind: "source_match",
                reason,
            } => {
                assert!(
                    reason.contains("logical_module runtime/widgets for export Widget"),
                    "{reason}"
                );
            }
            error => panic!("unexpected error: {error}"),
        }
    }

    #[test]
    fn source_match_with_leading_trailing_object_prop_holes_lowers_to_open_pattern() {
        let selector =
            AnonymousStatementSelector::exact("const actual = { ANYTHING, stable: 1, ANYTHING };");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
    fn source_match_with_open_object_pattern_props_lowers_to_native_child_list_pattern() {
        let selector =
            AnonymousStatementSelector::exact("const { ANYTHING, stable, ANYTHING } = input;");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                } if value == "ANYTHING"
            )),
            "ANYTHING object-pattern shorthands are run-hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn source_match_with_all_hole_object_does_not_pin_object_arity() {
        let selector = AnonymousStatementSelector::exact("const actual = { ANYTHING };");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ANYTHING"
            )),
            "a lone ANYTHING shorthand is hole syntax, not an identifier constraint"
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
                "an all-hole object selector must not constrain object child count"
            );
            assert!(
                !lowered.program.atoms.iter().any(|atom| matches!(
                    atom,
                    SelectorAtom::AstChildListPattern {
                        parent: NodeTerm::Var { id },
                        ..
                    } if *id == object_node
                )),
                "an all-hole object list is represented by omitting its child atoms"
            );
        }
    }

    #[test]
    fn source_match_with_anything_object_props_hole_lowers_natively() {
        let selector =
            AnonymousStatementSelector::exact("const actual = { stable: 1, ANYTHING, tail: 2 };");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                } if value == "ANYTHING"
            )),
            "ANYTHING object-property shorthand is run-hole syntax, not an identifier constraint"
        );
    }

    #[test]
    fn source_match_with_anything_object_pattern_props_hole_lowers_natively() {
        let selector =
            AnonymousStatementSelector::exact("const { stable, ANYTHING, tail } = actual;");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                } if value == "ANYTHING"
            )),
            "ANYTHING object-pattern shorthand is run-hole syntax, not an identifier constraint"
        );
    }

    #[test]
    fn source_match_with_declarators_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "const DECLARATORS_BEFORE = null, picked = make(), DECLARATORS_AFTER = null;",
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                } if value.starts_with(DECLARATORS_HOLE_KEYWORD)
            )),
            "DECLARATORS labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn source_match_with_anything_declarators_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "const ANYTHING = null, picked = make(), ANYTHING = null;",
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                } if value == "ANYTHING"
            )),
            "ANYTHING declarators are run-hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn source_match_with_all_hole_class_body_lowers_natively_without_class_arity() {
        let selector = AnonymousStatementSelector::exact("class Widget { ANYTHING; }");
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstPropertyName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ANYTHING"
            )),
            "a lone ANYTHING class field is hole syntax, not a property-name constraint"
        );
        let class_nodes = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstKind {
                    node: NodeTerm::Var { id },
                    node_kind,
                } if *node_kind == NodeKind::Class => Some(*id),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(!class_nodes.is_empty());
        for class_node in class_nodes {
            assert!(
                !lowered.program.atoms.iter().any(|atom| matches!(
                    atom,
                    SelectorAtom::AstChildCount {
                        node: NodeTerm::Var { id },
                        ..
                    } if *id == class_node
                )),
                "an all-hole class selector must not constrain class child count"
            );
        }
    }

    #[test]
    fn source_match_with_anything_class_rest_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            "class Widget { ANYTHING; render() { return 1; } ANYTHING; }",
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
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
                SelectorAtom::AstPropertyName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "ANYTHING"
            )),
            "ANYTHING class members are run-hole syntax, not property-name constraints"
        );
    }

    #[test]
    fn source_match_with_case_rest_lowers_to_native_child_list_pattern() {
        let selector = AnonymousStatementSelector::exact(
            r#"function readable(kind) {
  switch (kind) {
    case CASE_REST_BEFORE:
    case "go":
      return 42;
    case CASE_REST_AFTER:
  }
}"#,
        );
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector),
        )
        .unwrap();
        let switch_pattern_parent = lowered.program.atoms.iter().find_map(|atom| match atom {
            SelectorAtom::AstChildListPattern {
                parent: NodeTerm::Var { id },
                start_index: 1,
                anchored_left: false,
                anchored_right: false,
                segments,
            } if segments.len() == 1 && segments[0].len() == 1 => Some(*id),
            _ => None,
        });
        let switch_pattern_parent =
            switch_pattern_parent.expect("CASE_REST should lower to a switch child-list pattern");
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstKind {
                node: NodeTerm::Var { id },
                node_kind,
            } if *id == switch_pattern_parent && *node_kind == NodeKind::Switch
        )));
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value.starts_with(CASE_REST_HOLE_KEYWORD)
            )),
            "CASE_REST labels are hole syntax, not identifier constraints"
        );
    }

    #[test]
    fn exact_source_match_target_binding_projects_binding_variable() {
        let mut selector = AnonymousStatementSelector::exact("const a = 1, b = 2;");
        selector.target_binding = Some("b".to_string());
        let lowered = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(selector.clone()),
        )
        .unwrap();

        let target_owner = lowered.program.targets[lowered.target.0].owner;
        let projected_bindings = lowered
            .program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Var { id: binding },
                } if *id == target_owner => Some(*binding),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(projected_bindings.len(), 1);
        let projected_binding = projected_bindings[0];
        assert!(lowered.program.atoms.iter().any(|atom| matches!(
            atom,
            SelectorAtom::AstIdentifierName {
                value: StringTerm::Var { id },
                ..
            } if *id == projected_binding
        )));
        assert!(
            !lowered.program.atoms.iter().any(|atom| matches!(
                atom,
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Const { value },
                    ..
                } if value == "b"
            )),
            "target_binding's selector-local spelling should project from the matched node"
        );
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
        assert!(
            program.all_different.is_empty(),
            "plain binding targets are not owner-injective by themselves"
        );
        assert!(matches!(
            program.atoms.iter().find(|atom| matches!(atom, SelectorAtom::OwnerReferencesOwner { .. })),
            Some(SelectorAtom::OwnerReferencesOwner {
                owner: OwnerTerm::Var { id: owner_id },
                referenced: OwnerTerm::Var { id: referenced_id },
            }) if *owner_id == delegator_owner && *referenced_id == anchor_owner
        ));
    }

    #[test]
    fn relational_targets_remain_owner_injective_without_binding_anchors() {
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
        let first = builder
            .lower_member_selector(
                "First",
                &MemberSelectorSpec::CrossRef(CrossRefTarget {
                    relation: CrossRefRelation::References,
                    anchor: "Anchor".to_string(),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();
        let second = builder
            .lower_member_selector(
                "Second",
                &MemberSelectorSpec::CrossRef(CrossRefTarget {
                    relation: CrossRefRelation::References,
                    anchor: "Anchor".to_string(),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();

        let program = builder.into_program().unwrap();

        assert_eq!(program.all_different, vec![vec![first, second]]);
        assert!(
            !program
                .all_different
                .iter()
                .flatten()
                .any(|target| *target == anchor),
            "plain binding anchors should not participate in target owner injectivity"
        );
    }

    fn alpha_identifier_vars_in_source_order(program: &SelectorProgram) -> Vec<SelectorVariableId> {
        program
            .atoms
            .iter()
            .filter_map(|atom| match atom {
                SelectorAtom::AstIdentifierName {
                    value: StringTerm::Var { id },
                    ..
                } => Some(*id),
                _ => None,
            })
            .collect()
    }

    fn identifier_occurrence_counts(
        identifier_vars: &[SelectorVariableId],
    ) -> BTreeMap<SelectorVariableId, usize> {
        let mut counts = BTreeMap::new();
        for identifier in identifier_vars {
            *counts.entry(*identifier).or_default() += 1;
        }
        counts
    }
}
