//! Lower existing debundle selector specs into the global selector IR.
//!
//! Binding selectors and the relational selector primitives lower into one
//! joint program. `source_match` selectors are matched by the shape matcher;
//! only their candidate rows enter the program.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{ChunkId, StatementKind};
use selector_ir::{
    ClaimKind, ClaimOrigin, OwnerTerm, RelationalPrimitive, SelectorAtom, SelectorProgram,
    SelectorProjectedValue, SelectorTargetId, SelectorVariableId, StringTerm, VariableDomain,
};
use spec::{
    AnonymousStatementSelector, BindingSelector, BindingSourceKind, CrossRefRelation,
    MemberSelectorSpec,
};

/// A projected `source_match` candidate: its place and, per reference column,
/// the chunk identifier its template bound there.
pub type ProjectedRow = ((analysis::OwnerId, String), Vec<String>);

/// A projected `source_matches[]` candidate: one place per target binding and,
/// per reference column, the chunk identifier its template bound there.
pub type ProjectedGroupRow = (Vec<(analysis::OwnerId, String)>, Vec<String>);

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
    /// The binding each projected `source_match` export declares, by
    /// (logical module, export name).
    projected_bindings: BTreeMap<(String, String), SelectorVariableId>,
    /// Each projected anonymous statement's owner variable, by logical module
    /// and statement index.
    projected_anonymous_owners: BTreeMap<(String, usize), SelectorVariableId>,
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
            projected_bindings: BTreeMap::new(),
            projected_anonymous_owners: BTreeMap::new(),
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

    pub fn declare_projected_anonymous_statement_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        statement_index: usize,
    ) -> SelectorTargetId {
        let logical_module = logical_module.into();
        let owner = self.program.add_variable(
            VariableDomain::Owner,
            Some(format!(
                "{logical_module}::anonymous_statement.projected.{statement_index}"
            )),
        );
        self.projected_anonymous_owners
            .insert((logical_module.clone(), statement_index), owner);
        self.injective_targeted_owners.insert(owner);
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

    /// The candidate owners of an anonymous statement declared with
    /// [`Self::declare_projected_anonymous_statement_target_in_module`], each
    /// with the value of every `references` variable in that row.
    pub fn lower_projected_anonymous_statement_candidates(
        &mut self,
        logical_module: &str,
        statement_index: usize,
        references: &[SelectorVariableId],
        candidate_rows: Vec<(analysis::OwnerId, Vec<String>)>,
    ) {
        let owner = self.projected_anonymous_owners[&(logical_module.to_string(), statement_index)];
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: [owner]
                .into_iter()
                .chain(references.iter().copied())
                .collect(),
            rows: candidate_rows
                .into_iter()
                .map(|(owner, referenced)| {
                    [SelectorProjectedValue::Owner(owner)]
                        .into_iter()
                        .chain(referenced.into_iter().map(SelectorProjectedValue::String))
                        .collect()
                })
                .collect(),
            reason: format!(
                "{logical_module}::anonymous_statement.source_match.projected.{statement_index}"
            ),
        });
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
        self.lower_selector_atoms(logical_module, owner, selector)
    }

    /// The binding variable of projected `source_match` export `export_name`,
    /// shared by its own candidate table and every template that references
    /// it.
    pub fn projected_binding_variable(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(binding) = self.projected_bindings.get(&key) {
            return *binding;
        }
        let binding = self.program.add_variable(
            VariableDomain::String,
            Some(format!(
                "{logical_module}::source_match.projected_binding.{export_name}"
            )),
        );
        self.projected_bindings.insert(key, binding);
        binding
    }

    /// The binding variable of export `export_name`, pinned by a relational
    /// selector, for templates that reference it: the variable ranges over
    /// the bindings its owner declares.
    pub fn relational_binding_variable(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(binding) = self.projected_bindings.get(&key) {
            return *binding;
        }
        let binding = self.projected_binding_variable(logical_module, export_name);
        let owner = self.owner_for_local_export(logical_module, export_name);
        self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: owner_term(owner),
            binding: string_term(binding),
        });
        binding
    }

    /// Each row is a candidate place and, per `references` variable, the chunk
    /// identifier the template bound at that reference.
    pub fn lower_projected_source_match_candidates(
        &mut self,
        logical_module: &str,
        export_name: &str,
        references: &[SelectorVariableId],
        candidate_rows: Vec<ProjectedRow>,
    ) {
        let owner = self.owner_for_local_export(logical_module, export_name);
        let binding = self.projected_binding_variable(logical_module, export_name);
        self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: owner_term(owner),
            binding: string_term(binding),
        });
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: [owner, binding]
                .into_iter()
                .chain(references.iter().copied())
                .collect(),
            rows: candidate_rows
                .into_iter()
                .map(|((owner, binding), referenced)| {
                    [
                        SelectorProjectedValue::Owner(owner),
                        SelectorProjectedValue::String(binding),
                    ]
                    .into_iter()
                    .chain(referenced.into_iter().map(SelectorProjectedValue::String))
                    .collect()
                })
                .collect(),
            reason: format!("{logical_module}::source_match.projected.{export_name}"),
        });
    }

    /// As [`Self::lower_projected_source_match_candidates`], for every
    /// binding of one `source_matches[]` template: each row holds one place
    /// per target binding, in `exports_by_target` order.
    pub fn lower_projected_source_match_group_candidates(
        &mut self,
        logical_module: &str,
        exports_by_target: &BTreeMap<String, String>,
        references: &[SelectorVariableId],
        candidate_rows: Vec<ProjectedGroupRow>,
    ) {
        let mut variables = Vec::new();
        let injectivity_class = format!(
            "{logical_module}|source_matches.projected|{}",
            exports_by_target
                .keys()
                .cloned()
                .collect::<Vec<_>>()
                .join(",")
        );
        for export_name in exports_by_target.values() {
            let owner = self.owner_for_local_export(logical_module, export_name);
            self.owner_injectivity_classes
                .insert(owner, injectivity_class.clone());
            let binding = self.projected_binding_variable(logical_module, export_name);
            self.program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                owner: owner_term(owner),
                binding: string_term(binding),
            });
            variables.push(owner);
            variables.push(binding);
        }
        variables.extend(references.iter().copied());
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables,
            rows: candidate_rows
                .into_iter()
                .map(|(places, referenced)| {
                    places
                        .into_iter()
                        .flat_map(|(owner, binding)| {
                            [
                                SelectorProjectedValue::Owner(owner),
                                SelectorProjectedValue::String(binding),
                            ]
                        })
                        .chain(referenced.into_iter().map(SelectorProjectedValue::String))
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
        owner: SelectorVariableId,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<(), SelectorIrLoweringError> {
        match selector {
            MemberSelectorSpecRef::Binding(binding) => self.lower_binding_selector(owner, binding),
            MemberSelectorSpecRef::SourceMatch(_) => Err(SelectorIrLoweringError::unsupported(
                "source_match",
                "source_match candidates come from the shape matcher \
                 (lower_projected_source_match_candidates), not selector IR atoms",
            )),
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

fn selector_origin_ref(selector: MemberSelectorSpecRef<'_>) -> ClaimOrigin {
    match relation_for_selector_ref(selector) {
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
    use spec::{CrossRefRelation, CrossRefTarget};

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
}
