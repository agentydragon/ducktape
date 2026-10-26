//! Declarative inputs shared by compiler and solver tests; no expected outcomes.

use analysis::{SourceUnitId, StatementOrdinal};
use selector_ir::{
    ClaimKind, SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram, SelectorTargetId,
    SourceUnitTerm, StringTerm, VariableDomain,
};

pub fn source_unit_fact(source_unit: usize, ordinal: usize, statement_kind: &str) -> SelectorFact {
    SelectorFact::SourceUnit {
        source_unit: SourceUnitId(source_unit),
        statement_ordinal: StatementOrdinal(ordinal),
        statement_kind: statement_kind.to_string(),
    }
}

pub fn declared_binding(source_unit: usize, binding: &str) -> SelectorFact {
    SelectorFact::DeclaredBinding {
        source_unit: SourceUnitId(source_unit),
        binding: binding.to_string(),
    }
}

pub fn member_read(ordinal: usize, object: Option<&str>, member: &str) -> SelectorFact {
    SelectorFact::MemberRead {
        statement_ordinal: StatementOrdinal(ordinal),
        object: object.map(str::to_string),
        member: member.to_string(),
    }
}

pub fn module_member_use(ordinal: usize, module: &str, member: &str) -> SelectorFact {
    SelectorFact::ModuleMemberUse {
        statement_ordinal: StatementOrdinal(ordinal),
        module: module.to_string(),
        member: member.to_string(),
    }
}

pub fn call_argument_use(
    argument: &str,
    callee_object: Option<&str>,
    callee_member: &str,
    arg_index: usize,
) -> SelectorFact {
    SelectorFact::CallArgumentUse {
        argument: argument.to_string(),
        callee_object: callee_object.map(str::to_string),
        callee_member: callee_member.to_string(),
        arg_index,
    }
}

pub fn broad_specific_targets() -> (
    SelectorProgram,
    SelectorFactStore,
    SelectorTargetId,
    SelectorTargetId,
) {
    let mut program = SelectorProgram::default();
    let broad_source_unit =
        program.add_variable(VariableDomain::SourceUnit, Some("broad".to_string()));
    let strict_source_unit =
        program.add_variable(VariableDomain::SourceUnit, Some("strict".to_string()));
    let broad_target = program.add_target(
        broad_source_unit,
        "module",
        ClaimKind::Binding {
            export_name: Some("Broad".to_string()),
        },
    );
    let strict_target = program.add_target(
        strict_source_unit,
        "module",
        ClaimKind::Binding {
            export_name: Some("Strict".to_string()),
        },
    );
    program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
        source_unit: SourceUnitTerm::Var {
            id: broad_source_unit,
        },
        binding: StringTerm::Const {
            value: "shared".to_string(),
        },
    });
    program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
        source_unit: SourceUnitTerm::Var {
            id: strict_source_unit,
        },
        binding: StringTerm::Const {
            value: "specific".to_string(),
        },
    });
    program.require_all_different(vec![broad_target, strict_target]);

    let facts = SelectorFactStore {
        facts: vec![
            source_unit_fact(10, 0, "var"),
            source_unit_fact(20, 1, "var"),
            declared_binding(10, "shared"),
            declared_binding(20, "shared"),
            declared_binding(20, "specific"),
        ],
    };

    (program, facts, broad_target, strict_target)
}
