use super::*;

#[test]
fn tuple_values_join_domains_even_when_ids_were_interned_out_of_order() {
    let mut model = CompiledSelectorProblemBuilder::default();
    let ids: Vec<_> = ["a", "b", "c"]
        .into_iter()
        .map(|value| model.intern_string(value).unwrap())
        .collect();
    let variable = model.add_variable(VariableDomain::String, None);
    for value in ["c", "a", "b"] {
        model
            .add_allowed_tuples(
                vec![variable],
                vec![vec![SelectorProjectedValue::String(value.to_string())]],
            )
            .unwrap();
    }
    assert_eq!(model.variable_domain_values(variable).unwrap(), ids);
}

#[test]
fn encoded_rows_fill_holes_without_duplicates() {
    let mut model = CompiledSelectorProblemBuilder::default();
    let ids: Vec<_> = ["a", "b", "c"]
        .into_iter()
        .map(|value| model.intern_string(value).unwrap())
        .collect();
    model
        .add_full_domain_values(
            VariableDomain::String,
            ["a", "c"].map(|value| SelectorProjectedValue::String(value.to_string())),
        )
        .unwrap();
    let left = model.add_variable(VariableDomain::String, None);
    let right = model.add_variable(VariableDomain::String, None);
    model
        .intern_encoded_allowed_binary_row_set(
            [left, right],
            [VariableDomain::String; 2],
            vec![(ids[1], ids[2]), (ids[1], ids[0])],
        )
        .unwrap();
    assert_eq!(model.variable_domain_values(left).unwrap(), ids);
}
