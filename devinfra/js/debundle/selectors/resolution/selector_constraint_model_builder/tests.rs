use super::*;
use selector_constraint_backend::{AllowedTupleConstraintId, BackendValueId};
use selector_ir::{ClaimKind, SelectorFact, SelectorProjectedValue};
use selector_test_fixtures::{
    broad_specific_targets, declared_binding, member_read, module_member_use, source_unit_fact,
};
use source_identity::SourceUnitId;
use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq, Eq)]
struct AllowedTupleConstraint {
    id: AllowedTupleConstraintId,
    variables: Vec<ConstraintVariableId>,
    tuples: Vec<Vec<SelectorProjectedValue>>,
}

fn source_unit(value: usize) -> SelectorProjectedValue {
    SelectorProjectedValue::SourceUnit(SourceUnitId(value))
}

fn string(value: &str) -> SelectorProjectedValue {
    SelectorProjectedValue::String(value.to_string())
}

fn source_unit_reference(source_unit: usize, binding: &str) -> SelectorFact {
    SelectorFact::SourceUnitReferencesBinding {
        source_unit: SourceUnitId(source_unit),
        binding: binding.to_string(),
    }
}

fn decorate_call(callee: &str, class_anchor: &str, member: Option<&str>) -> SelectorFact {
    SelectorFact::DecorateCallUse {
        callee: callee.to_string(),
        class_anchor: class_anchor.to_string(),
        member: member.map(str::to_string),
    }
}

fn intrinsic_alias(binding: &str, property: &str) -> SelectorFact {
    SelectorFact::IntrinsicAliasUse {
        binding: binding.to_string(),
        property: property.to_string(),
    }
}

fn fact_store(facts: Vec<SelectorFact>) -> SelectorFactStore {
    SelectorFactStore { facts }
}

fn allowed_tuples_for(
    model: &CompiledSelectorProblem,
    variables: &[ConstraintVariableId],
) -> AllowedTupleConstraint {
    let constraint = model
        .allowed_tuples
        .iter()
        .find(|constraint| constraint.variables == variables)
        .unwrap();
    AllowedTupleConstraint {
        id: constraint.id,
        variables: constraint.variables.clone(),
        tuples: model
            .allowed_tuple_rows(constraint)
            .iter()
            .map(|tuple| decode_tuple(model, &constraint.variables, tuple))
            .collect(),
    }
}

fn satisfying_tuples_for(
    model: &CompiledSelectorProblem,
    variables: &[ConstraintVariableId],
) -> Vec<Vec<SelectorProjectedValue>> {
    let mut rows = BTreeSet::new();
    let mut assignment = BTreeMap::new();
    collect_satisfying_tuples(model, variables, 0, &mut assignment, &mut rows);
    rows.into_iter().collect()
}

fn collect_satisfying_tuples(
    model: &CompiledSelectorProblem,
    variables: &[ConstraintVariableId],
    variable_index: usize,
    assignment: &mut BTreeMap<ConstraintVariableId, BackendValueId>,
    rows: &mut BTreeSet<Vec<SelectorProjectedValue>>,
) {
    let Some(variable) = model.variables.get(variable_index) else {
        if model_constraints_satisfied(model, assignment)
            && let Some(row) = variables
                .iter()
                .map(|variable| {
                    assignment
                        .get(variable)
                        .map(|value| decode_variable_value(model, *variable, *value))
                })
                .collect::<Option<Vec<_>>>()
        {
            rows.insert(row);
        }
        return;
    };

    for value in model.variable_domain_values(variable) {
        assignment.insert(variable.id, value);
        collect_satisfying_tuples(model, variables, variable_index + 1, assignment, rows);
    }
    assignment.remove(&variable.id);
}

fn model_constraints_satisfied(
    model: &CompiledSelectorProblem,
    assignment: &BTreeMap<ConstraintVariableId, BackendValueId>,
) -> bool {
    model.allowed_tuples.iter().all(|constraint| {
        constraint
            .variables
            .iter()
            .map(|variable| assignment.get(variable).copied())
            .collect::<Option<Vec<_>>>()
            .is_some_and(|row| model.allowed_tuple_rows(constraint).contains(&row))
    }) && model.all_different.iter().all(|constraint| {
        let mut seen = BTreeSet::new();
        constraint.variables.iter().all(|variable| {
            assignment
                .get(variable)
                .is_some_and(|value| seen.insert(*value))
        })
    })
}

fn decode_tuple(
    model: &CompiledSelectorProblem,
    variables: &[ConstraintVariableId],
    values: &[BackendValueId],
) -> Vec<SelectorProjectedValue> {
    variables
        .iter()
        .zip(values.iter())
        .map(|(variable, value)| decode_variable_value(model, *variable, *value))
        .collect()
}

fn decode_variable_value(
    model: &CompiledSelectorProblem,
    variable: ConstraintVariableId,
    value: BackendValueId,
) -> SelectorProjectedValue {
    let variable = &model.variables[variable.0];
    model
        .decode_value(variable.domain, value)
        .expect("test fixture assigns values from the variable domain")
}

fn decoded_variable_domain(
    model: &CompiledSelectorProblem,
    variable: ConstraintVariableId,
) -> Vec<SelectorProjectedValue> {
    model
        .variable_domain_values(&model.variables[variable.0])
        .iter()
        .map(|value| decode_variable_value(model, variable, *value))
        .collect()
}

#[test]
fn duplicate_variables_in_allowed_tuple_atoms_are_merged() {
    let mut program = SelectorProgram::default();
    let source_unit_var =
        program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
    program.add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
        source_unit: SourceUnitTerm::Var {
            id: source_unit_var,
        },
        referenced: SourceUnitTerm::Var {
            id: source_unit_var,
        },
    });

    let facts = fact_store(vec![
        source_unit_fact(10, 0, "var"),
        source_unit_fact(20, 1, "var"),
        source_unit_fact(30, 2, "var"),
        declared_binding(10, "a"),
        declared_binding(20, "b"),
        declared_binding(30, "c"),
        source_unit_reference(10, "a"),
        source_unit_reference(10, "b"),
        source_unit_reference(20, "b"),
        source_unit_reference(30, "a"),
    ]);

    let model = compile_selector_problem(&program, &facts).unwrap();

    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(0)),
        vec![source_unit(10), source_unit(20)]
    );
}

#[test]
fn single_row_projected_allowed_tuples_restricts_domains_without_table() {
    let mut program = SelectorProgram::default();
    let source_unit_var =
        program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
    let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
    program.add_atom(SelectorAtom::ProjectedAllowedTuples {
        variables: vec![source_unit_var, binding_var],
        rows: vec![vec![
            SelectorProjectedValue::SourceUnit(SourceUnitId(7)),
            SelectorProjectedValue::String("actual".to_string()),
        ]],
    });

    let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

    assert_eq!(model.allowed_tuples, vec![]);
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(0)),
        vec![source_unit(7)]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(1)),
        vec![string("actual")]
    );
}

#[test]
fn multi_row_projected_allowed_tuples_preserves_correlation() {
    let mut program = SelectorProgram::default();
    let source_unit_var =
        program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
    let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
    program.add_atom(SelectorAtom::ProjectedAllowedTuples {
        variables: vec![source_unit_var, binding_var],
        rows: vec![
            vec![
                SelectorProjectedValue::SourceUnit(SourceUnitId(7)),
                SelectorProjectedValue::String("actual".to_string()),
            ],
            vec![
                SelectorProjectedValue::SourceUnit(SourceUnitId(8)),
                SelectorProjectedValue::String("other".to_string()),
            ],
        ],
    });

    let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(0)),
        vec![source_unit(7), source_unit(8)]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(1)),
        vec![string("actual"), string("other")]
    );
    assert_eq!(
        satisfying_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
        vec![
            vec![source_unit(7), string("actual")],
            vec![source_unit(8), string("other")]
        ]
    );
}

#[test]
fn target_injectivity_prunes_fixed_values_from_broad_targets() {
    let (program, facts, broad_target, strict_target) = broad_specific_targets();

    let model = compile_selector_problem(&program, &facts).unwrap();

    let source_unit_domain = |target| {
        let projection = model
            .target_projections
            .iter()
            .find(|projection| projection.target == target)
            .unwrap();
        decoded_variable_domain(&model, projection.source_unit_variable)
    };
    // The strict target can only be source unit 20, so injectivity removes source unit 20
    // from the broad target's domain and nothing is left to be all-different.
    assert_eq!(source_unit_domain(broad_target), vec![source_unit(10)]);
    assert_eq!(source_unit_domain(strict_target), vec![source_unit(20)]);
    assert!(model.all_different.is_empty());
}

#[test]
fn target_binding_projection_preserves_binding_variable() {
    let mut program = SelectorProgram::default();
    let source_unit_var =
        program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
    let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
    let target = program.add_target(
        source_unit_var,
        "module",
        ClaimKind::Binding { export_name: None },
    );
    program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
        source_unit: SourceUnitTerm::Var {
            id: source_unit_var,
        },
        binding: StringTerm::Var { id: binding_var },
    });

    let facts = fact_store(vec![
        source_unit_fact(7, 0, "var"),
        source_unit_fact(8, 1, "var"),
        declared_binding(7, "actual"),
        declared_binding(8, "other"),
    ]);

    let model = compile_selector_problem(&program, &facts).unwrap();

    assert_eq!(model.target_projections.len(), 1);
    assert_eq!(model.target_projections[0].target, target);
    assert_eq!(
        model.target_projections[0].source_unit_variable,
        ConstraintVariableId(0)
    );
    assert_eq!(
        model.target_projections[0].binding_projection,
        Some(TargetBindingProjection::Variable(ConstraintVariableId(1)))
    );
    assert_eq!(
        allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
        AllowedTupleConstraint {
            id: AllowedTupleConstraintId(0),
            variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
            tuples: vec![
                vec![source_unit(7), string("actual")],
                vec![source_unit(8), string("other")],
            ],
        }
    );
}

#[test]
fn relational_atoms_lower_to_allowed_tuple_constraints() {
    let mut program = SelectorProgram::default();
    let referenced_source_unit = program.add_variable(
        VariableDomain::SourceUnit,
        Some("referenced_source_unit".to_string()),
    );
    let reader_source_unit = program.add_variable(
        VariableDomain::SourceUnit,
        Some("reader_source_unit".to_string()),
    );
    let object_source_unit = program.add_variable(
        VariableDomain::SourceUnit,
        Some("object_source_unit".to_string()),
    );
    let class_source_unit = program.add_variable(
        VariableDomain::SourceUnit,
        Some("class_source_unit".to_string()),
    );
    let intrinsic_referencer = program.add_variable(
        VariableDomain::SourceUnit,
        Some("intrinsic_referencer".to_string()),
    );
    let read_member = program.add_variable(VariableDomain::String, Some("read_member".to_string()));
    let module_consumer = program.add_variable(
        VariableDomain::SourceUnit,
        Some("module_consumer".to_string()),
    );

    program.add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
        source_unit: SourceUnitTerm::Const {
            source_unit: SourceUnitId(20),
        },
        referenced: SourceUnitTerm::Var {
            id: referenced_source_unit,
        },
    });
    program.add_atom(SelectorAtom::ReadsMember {
        source_unit: SourceUnitTerm::Var {
            id: reader_source_unit,
        },
        member: StringTerm::Var { id: read_member },
    });
    program.add_atom(SelectorAtom::ReadsMemberOfSourceUnit {
        source_unit: SourceUnitTerm::Const {
            source_unit: SourceUnitId(40),
        },
        object: SourceUnitTerm::Var {
            id: object_source_unit,
        },
        member: StringTerm::Const {
            value: "value".to_string(),
        },
    });
    program.add_atom(SelectorAtom::ConsumesModuleMember {
        source_unit: SourceUnitTerm::Var {
            id: module_consumer,
        },
        module: StringTerm::Const {
            value: "./accessors".to_string(),
        },
        member: StringTerm::Const {
            value: "Widget".to_string(),
        },
    });
    program.add_atom(SelectorAtom::MakesDecorateCallForSourceUnit {
        source_unit: SourceUnitTerm::Const {
            source_unit: SourceUnitId(60),
        },
        class_anchor: SourceUnitTerm::Var {
            id: class_source_unit,
        },
        member: Some(StringTerm::Const {
            value: "field".to_string(),
        }),
    });
    program.add_atom(SelectorAtom::IntrinsicAlias {
        source_unit: SourceUnitTerm::Const {
            source_unit: SourceUnitId(80),
        },
        property: StringTerm::Const {
            value: "defineProperty".to_string(),
        },
        referenced_by: SourceUnitTerm::Var {
            id: intrinsic_referencer,
        },
    });

    let facts = fact_store(vec![
        source_unit_fact(10, 0, "function"),
        declared_binding(10, "target"),
        source_unit_fact(20, 1, "function"),
        declared_binding(20, "referrer"),
        source_unit_reference(20, "target"),
        source_unit_fact(40, 3, "function"),
        declared_binding(40, "reader"),
        member_read(3, None, "size"),
        member_read(3, Some("objectBinding"), "value"),
        source_unit_fact(50, 4, "var_decl"),
        declared_binding(50, "objectBinding"),
        source_unit_fact(60, 5, "function"),
        declared_binding(60, "decorate"),
        source_unit_fact(65, 55, "function"),
        declared_binding(65, "moduleConsumer"),
        module_member_use(55, "./accessors", "Widget"),
        source_unit_fact(66, 56, "function"),
        declared_binding(66, "otherModuleConsumer"),
        module_member_use(56, "./accessors", "Other"),
        // Same member of a different module: not a consumer of `./accessors`.
        source_unit_fact(67, 57, "function"),
        declared_binding(67, "otherSourceConsumer"),
        module_member_use(57, "./other", "Widget"),
        source_unit_fact(70, 6, "class"),
        declared_binding(70, "Class"),
        decorate_call("decorate", "Class", Some("field")),
        source_unit_fact(80, 7, "var_decl"),
        declared_binding(80, "define"),
        intrinsic_alias("define", "defineProperty"),
        source_unit_fact(90, 8, "function"),
        declared_binding(90, "aliasUser"),
        source_unit_reference(90, "define"),
    ]);

    let model = compile_selector_problem(&program, &facts).unwrap();

    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(0)),
        vec![source_unit(10)]
    );
    assert_eq!(
        satisfying_tuples_for(&model, &[ConstraintVariableId(1), ConstraintVariableId(5)]),
        vec![
            vec![source_unit(40), string("size")],
            vec![source_unit(40), string("value")],
        ]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(2)),
        vec![source_unit(50)]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(3)),
        vec![source_unit(70)]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(4)),
        vec![source_unit(90)]
    );
    assert_eq!(
        decoded_variable_domain(&model, ConstraintVariableId(6)),
        vec![source_unit(65)]
    );
}

#[test]
fn unsupported_atoms_fail_closed() {
    let mut program = SelectorProgram::default();
    let source_unit_var =
        program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
    let member_var = program.add_variable(VariableDomain::String, Some("member".to_string()));
    program.add_atom(SelectorAtom::PassedToCall {
        source_unit: SourceUnitTerm::Var {
            id: source_unit_var,
        },
        callee_member: StringTerm::Var { id: member_var },
        arg_index: None,
    });

    let facts = fact_store(vec![source_unit_fact(10, 0, "var")]);

    let err = compile_selector_problem(&program, &facts).unwrap_err();
    assert!(matches!(
        err,
        CompiledSelectorProblemBuildError::UnsupportedAtom { .. }
    ));
}

#[test]
fn unsatisfied_constant_only_atoms_fail_closed() {
    let mut program = SelectorProgram::default();
    program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
        source_unit: SourceUnitTerm::Const {
            source_unit: SourceUnitId(10),
        },
        binding: StringTerm::Const {
            value: "missing".to_string(),
        },
    });

    let facts = fact_store(vec![source_unit_fact(10, 0, "var")]);

    let err = compile_selector_problem(&program, &facts).unwrap_err();
    assert!(matches!(
        err,
        CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied { .. }
    ));
}
