//! Lower existing debundle selector specs into the global selector IR.
//!
//! Binding selectors, `source_match` selectors, and the current relational
//! selector primitives lower into one joint program.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use analysis::{ChunkId, StatementKind};
use selector_ir::{
    ClaimKind, ClaimOrigin, OwnerTerm, RelationalPrimitive, SelectorAtom, SelectorProgram,
    SelectorTargetId, SelectorVariableId, StringTerm, VariableDomain,
};
use spec::{BindingSelector, BindingSourceKind, CrossRefRelation, MemberSelectorSpec};

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
                self.program.add_atom(SelectorAtom::SourceMatchCandidate {
                    owner: owner_term(owner),
                    selector_key: const_str(&source_match::selector_key(selector)),
                });
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
    fn lowers_source_match_candidate_constraint() {
        let selector = AnonymousStatementSelector::exact("const a = 1;");
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
