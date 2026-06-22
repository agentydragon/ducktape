//! Lower existing debundle selector specs into the global selector IR.
//!
//! This is the first G2 entrypoint. It intentionally starts with the selector
//! kind whose semantics are already a direct owner-graph predicate
//! (`members[].selector.binding`) and reports the remaining selector kinds as
//! explicit unsupported lowerings, so compare-mode wiring can fail closed while
//! source_match and relational lowering are added incrementally.

use std::error::Error;
use std::fmt;

use analysis::{ChunkId, StatementKind};
use selector_ir::{
    ClaimKind, ClaimOrigin, OwnerTerm, RelationalPrimitive, SelectorAtom, SelectorProgram,
    SelectorTargetId, StringTerm, VariableDomain,
};
use spec::{BindingSelector, BindingSourceKind, MemberSelectorSpec};

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
/// Later compare-mode integration can merge these fragments into one connected
/// component program before solving.
pub fn lower_member_selector(
    context: &MemberSelectorLoweringContext,
    export_name: &str,
    selector: &MemberSelectorSpec,
) -> Result<LoweredMemberSelector, SelectorIrLoweringError> {
    match selector {
        MemberSelectorSpec::Binding(binding) => {
            lower_binding_selector(context, export_name, binding)
        }
        MemberSelectorSpec::SourceMatch(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "source_match requires AST-pattern lowering and differential parity",
        )),
        MemberSelectorSpec::CrossRef(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "cross_ref lowering belongs with relational primitive fold-in",
        )),
        MemberSelectorSpec::ReadsMember(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "reads_member lowering belongs with relational primitive fold-in",
        )),
        MemberSelectorSpec::MemberOfModule(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "member_of_module lowering belongs with relational primitive fold-in",
        )),
        MemberSelectorSpec::PassedToCall(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "passed_to_call lowering belongs with relational primitive fold-in",
        )),
        MemberSelectorSpec::MakesDecorateCall(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "makes_decorate_call lowering belongs with relational primitive fold-in",
        )),
        MemberSelectorSpec::IntrinsicAlias(_) => Err(SelectorIrLoweringError::unsupported(
            selector.selector_kind_label(),
            "intrinsic_alias lowering belongs with relational primitive fold-in",
        )),
    }
}

fn lower_binding_selector(
    context: &MemberSelectorLoweringContext,
    export_name: &str,
    selector: &BindingSelector,
) -> Result<LoweredMemberSelector, SelectorIrLoweringError> {
    if selector.kind == Some(BindingSourceKind::ImportSpecifier) {
        return Err(SelectorIrLoweringError::unsupported(
            "binding",
            "import_specifier binding selectors need import-owner fact modeling",
        ));
    }

    let mut program = SelectorProgram::default();
    let owner = program.add_variable(VariableDomain::Owner, Some(format!("@{export_name}")));
    let target = program.add_target(
        context.chunk_id,
        owner,
        context.logical_module.clone(),
        ClaimKind::Binding {
            export_name: Some(export_name.to_string()),
        },
        ClaimOrigin::MemberSelector,
    );
    program.add_atom(SelectorAtom::OwnerDeclaresBinding {
        owner: OwnerTerm::Var { id: owner },
        binding: StringTerm::Const {
            value: selector.name.clone(),
        },
    });
    if let Some(kind) = selector.kind {
        program.add_atom(SelectorAtom::OwnerKind {
            owner: OwnerTerm::Var { id: owner },
            statement_kind: StringTerm::Const {
                value: statement_kind_str_for_spec(kind).to_string(),
            },
        });
    }
    program.validate()?;

    Ok(LoweredMemberSelector { target, program })
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

#[allow(dead_code)]
fn relation_for_unsupported_primitive(
    selector: &MemberSelectorSpec,
) -> Option<RelationalPrimitive> {
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
    use spec::AnonymousStatementSelector;

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
    fn source_match_fails_closed_until_ast_lowering_lands() {
        let error = lower_member_selector(
            &context(),
            "Widget",
            &MemberSelectorSpec::SourceMatch(AnonymousStatementSelector::exact("const a = 1;")),
        )
        .unwrap_err();

        assert_eq!(
            error,
            SelectorIrLoweringError::Unsupported {
                selector_kind: "source_match",
                reason: "source_match requires AST-pattern lowering and differential parity",
            }
        );
    }
}
