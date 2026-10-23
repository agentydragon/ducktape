use std::{collections::BTreeSet, sync::Arc};

use super::{
    ArrowExpr, ChunkValueFlow, Decl, ModuleItem, Stmt, ValueOrigin, Visit, VisitWith,
    value_flow::{OriginTable, Values},
};

const DOMAIN: [ValueOrigin; 10] = [
    ValueOrigin::Function(0),
    ValueOrigin::Function(7),
    ValueOrigin::Function(u32::MAX),
    ValueOrigin::Class(0),
    ValueOrigin::Class(19),
    ValueOrigin::Class(u32::MAX),
    ValueOrigin::Object(0),
    ValueOrigin::Object(23),
    ValueOrigin::Object(u32::MAX),
    ValueOrigin::Unknown,
];

const ABSENT: [ValueOrigin; 3] = [
    ValueOrigin::Function(6),
    ValueOrigin::Class(18),
    ValueOrigin::Object(22),
];

fn origin_table() -> Arc<OriginTable> {
    Arc::new(OriginTable::from_origins(DOMAIN))
}

fn values_from(
    origins: &Arc<OriginTable>,
    values: impl IntoIterator<Item = ValueOrigin>,
) -> Values {
    let mut result = Values::empty(origins.clone());
    result.extend(values);
    result
}

fn assert_matches_oracle(values: &Values, expected: &BTreeSet<ValueOrigin>) {
    let actual_order: Vec<_> = values.iter().collect();
    let expected_order: Vec<_> = expected.iter().copied().collect();
    assert_eq!(actual_order, expected_order);

    for value in DOMAIN.into_iter().chain(ABSENT) {
        assert_eq!(
            values.contains(&value),
            expected.contains(&value),
            "{value:?}"
        );
    }
}

fn assert_union_matches_oracle(
    actual: &mut Values,
    expected: &mut BTreeSet<ValueOrigin>,
    right: &Values,
) -> bool {
    let old_size = expected.len();
    expected.extend(right.iter());
    let expected_changed = expected.len() != old_size;

    let actual_changed = actual.union_with(right);
    assert_eq!(actual_changed, expected_changed);
    assert_matches_oracle(actual, expected);
    actual_changed
}

#[test]
fn values_operations_match_btree_set_for_all_origins_and_input_orders() {
    let origins = origin_table();
    let sequences = [
        DOMAIN.to_vec(),
        DOMAIN.into_iter().rev().collect(),
        vec![
            ValueOrigin::Unknown,
            ValueOrigin::Function(7),
            ValueOrigin::Unknown,
            ValueOrigin::Object(23),
            ValueOrigin::Function(7),
            ValueOrigin::Class(19),
            ValueOrigin::Object(0),
            ValueOrigin::Class(0),
            ValueOrigin::Function(0),
            ValueOrigin::Function(u32::MAX),
        ],
    ];

    for input in sequences {
        let expected: BTreeSet<_> = input.iter().copied().collect();
        let collected = values_from(&origins, input.iter().copied());
        assert_matches_oracle(&collected, &expected);

        let mut inserted = Values::empty(origins.clone());
        let mut inserted_expected = BTreeSet::new();
        for value in &input {
            assert_eq!(
                inserted.insert(*value),
                inserted_expected.insert(*value),
                "insert return value for {value:?} in {input:?}",
            );
            assert_matches_oracle(&inserted, &inserted_expected);
        }

        let mut extended = Values::empty(origins.clone());
        let mut extended_expected = BTreeSet::new();
        extended.extend(input.iter().copied());
        extended_expected.extend(input.iter().copied());
        assert_matches_oracle(&extended, &extended_expected);

        let midpoint = input.len() / 2;
        let left_input = &input[..midpoint];
        let right_input = &input[midpoint..];
        let mut union = values_from(&origins, left_input.iter().copied());
        let mut union_expected = left_input.iter().copied().collect::<BTreeSet<_>>();
        let right = values_from(&origins, right_input.iter().copied());
        assert_union_matches_oracle(&mut union, &mut union_expected, &right);

        // Union is idempotent and independent of the order in which the
        // partitions are merged.
        let mut reverse_union = values_from(&origins, right_input.iter().copied());
        let reverse_right = values_from(&origins, left_input.iter().copied());
        let mut reverse_expected = right_input.iter().copied().collect::<BTreeSet<_>>();
        assert_union_matches_oracle(&mut reverse_union, &mut reverse_expected, &reverse_right);
        assert_eq!(reverse_expected, union_expected);
        let same = union.clone();
        assert!(!assert_union_matches_oracle(
            &mut union,
            &mut union_expected,
            &same,
        ));
    }
}

#[test]
fn union_reports_whether_the_oracle_gains_any_values() {
    let origins = origin_table();
    let mut actual = Values::empty(origins.clone());
    let mut expected = BTreeSet::new();
    let empty = Values::empty(origins.clone());

    assert!(!assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &empty,
    ));

    let initial = values_from(&origins, [ValueOrigin::Unknown, ValueOrigin::Function(7)]);
    assert!(assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &initial,
    ));
    assert!(!assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &empty,
    ));

    let subset = values_from(&origins, [ValueOrigin::Unknown]);
    assert!(!assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &subset,
    ));

    let mixed = values_from(&origins, [ValueOrigin::Function(7), ValueOrigin::Class(19)]);
    assert!(assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &mixed,
    ));

    let same = actual.clone();
    assert!(!assert_union_matches_oracle(
        &mut actual,
        &mut expected,
        &same,
    ));
}

#[test]
fn generated_value_sequences_match_btree_set_after_each_operation() {
    // A tiny fixed-seed generator covers repeated insertions, bulk extension,
    // and unions without making the test depend on a random-number crate.
    let origins = origin_table();
    for seed in 0_u32..16 {
        let mut state = seed.wrapping_add(1);
        let mut actual = Values::empty(origins.clone());
        let mut expected = BTreeSet::new();

        for step in 0..96 {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let value = DOMAIN[(state as usize) % DOMAIN.len()];
            match (state >> 29) % 3 {
                0 => {
                    assert_eq!(actual.insert(value), expected.insert(value));
                }
                1 => {
                    let batch = [value, DOMAIN[(step + seed as usize) % DOMAIN.len()]];
                    actual.extend(batch);
                    expected.extend(batch);
                }
                _ => {
                    let batch = [value, DOMAIN[(step * 3 + seed as usize) % DOMAIN.len()]];
                    let batch_values = values_from(&origins, batch);
                    assert_union_matches_oracle(&mut actual, &mut expected, &batch_values);
                }
            }
            assert_matches_oracle(&actual, &expected);
        }
    }
}

#[derive(Default)]
struct ArrowOrigins(BTreeSet<ValueOrigin>);

impl Visit for ArrowOrigins {
    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.0.insert(ValueOrigin::Function(arrow.span.lo.0));
        arrow.visit_children_with(self);
    }
}

#[test]
fn deep_logical_and_conditional_unions_append_each_origin_once() {
    let leaf_count = 1_024;
    let mut source = String::from("const result = (() => {})");
    for _ in 1..leaf_count {
        source.push_str(" || (true ? (() => {}) : null)");
    }
    source.push(';');

    let module = raw_js_test_support::parse(&source);
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = &module.body[0] else {
        panic!("expected a variable declaration");
    };
    let expression = declaration.decls[0]
        .init
        .as_deref()
        .expect("expected an initializer");

    let mut expected = ArrowOrigins::default();
    expression.visit_with(&mut expected);
    assert_eq!(expected.0.len(), leaf_count);

    let (actual, appended_items) =
        ChunkValueFlow::default().evaluate_value_expr_for_test(expression);
    assert_eq!(appended_items, leaf_count);
    assert_eq!(actual.iter().collect::<BTreeSet<_>>(), expected.0,);
}
