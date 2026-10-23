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

fn boundary_domain() -> Vec<ValueOrigin> {
    let mut domain: Vec<_> = (0..=1_024).map(ValueOrigin::Function).collect();
    domain.extend([
        ValueOrigin::Function(u32::MAX),
        ValueOrigin::Class(0),
        ValueOrigin::Class(u32::MAX),
        ValueOrigin::Object(0),
        ValueOrigin::Object(u32::MAX),
        ValueOrigin::Unknown,
    ]);
    domain.sort_unstable();
    domain.dedup();
    domain
}

fn assert_matches_domain(
    values: &Values,
    expected: &BTreeSet<ValueOrigin>,
    domain: &[ValueOrigin],
) {
    let expected_order: Vec<_> = expected.iter().copied().collect();
    assert_eq!(values.iter().collect::<Vec<_>>(), expected_order);
    assert_eq!(
        values.clone().into_iter().collect::<Vec<_>>(),
        expected_order
    );

    for value in domain {
        assert_eq!(
            values.contains(value),
            expected.contains(value),
            "{value:?}"
        );
    }
}

#[test]
fn dense_rank_boundaries_round_trip_in_owned_and_borrowed_iteration() {
    let domain = boundary_domain();
    let origins = Arc::new(OriginTable::from_origins(domain.iter().copied()));
    let selected = [
        ValueOrigin::Function(0),
        ValueOrigin::Function(63),
        ValueOrigin::Function(64),
        ValueOrigin::Function(65),
        ValueOrigin::Function(127),
        ValueOrigin::Function(128),
        ValueOrigin::Function(129),
        ValueOrigin::Function(1_023),
        ValueOrigin::Function(1_024),
        ValueOrigin::Function(u32::MAX),
        ValueOrigin::Class(u32::MAX),
        ValueOrigin::Object(u32::MAX),
        ValueOrigin::Unknown,
    ];

    let expected: BTreeSet<_> = selected.into_iter().collect();
    let mut actual = Values::empty(origins.clone());
    for value in selected.into_iter().rev() {
        actual.insert(value);
    }

    assert_matches_domain(&actual, &expected, &domain);
    for absent in [
        ValueOrigin::Function(1_025),
        ValueOrigin::Class(1),
        ValueOrigin::Object(1),
    ] {
        assert!(!actual.contains(&absent), "unexpected {absent:?}");
    }

    let unknown_only_table = Arc::new(OriginTable::from_origins([]));
    let unknown_only = Values::singleton(unknown_only_table, ValueOrigin::Unknown);
    assert_eq!(
        unknown_only.iter().collect::<Vec<_>>(),
        [ValueOrigin::Unknown]
    );
    assert_eq!(
        unknown_only.into_iter().collect::<Vec<_>>(),
        [ValueOrigin::Unknown]
    );
}

#[test]
fn union_handles_different_rank_lengths_and_reports_new_values() {
    let domain = boundary_domain();
    let origins = Arc::new(OriginTable::from_origins(domain.iter().copied()));
    let low = [ValueOrigin::Function(0), ValueOrigin::Function(63)];
    let high = [
        ValueOrigin::Function(64),
        ValueOrigin::Function(1_023),
        ValueOrigin::Function(1_024),
        ValueOrigin::Function(u32::MAX),
        ValueOrigin::Class(u32::MAX),
        ValueOrigin::Object(u32::MAX),
        ValueOrigin::Unknown,
    ];

    let mut actual = values_from(&origins, low);
    let high_values = values_from(&origins, high);
    let mut expected: BTreeSet<_> = low.into_iter().collect();
    let high_expected: BTreeSet<_> = high.into_iter().collect();

    expected.extend(high_expected.iter().copied());
    assert!(actual.union_with(&high_values));
    assert_matches_domain(&actual, &expected, &domain);
    assert!(!actual.union_with(&high_values));
    assert_matches_domain(&actual, &expected, &domain);

    let mut empty = Values::empty(origins);
    assert!(empty.union_with(&high_values));
    assert_matches_domain(&empty, &high_expected, &domain);
    assert!(!empty.union_with(&high_values));
}

#[test]
fn sparse_high_rank_sets_compose_reuse_and_union_in_any_order() {
    // This table gives Function(12_416) semantic rank 12_416, matching a
    // sparse source whose only occupied word is index 194 in a u64 wordset.
    const HIGH_RANK: u32 = 12_416;
    const INTERMEDIATE_RANK: u32 = 6_400;

    let mut domain: Vec<_> = (0..=HIGH_RANK).map(ValueOrigin::Function).collect();
    domain.extend([
        ValueOrigin::Function(u32::MAX),
        ValueOrigin::Class(u32::MAX),
        ValueOrigin::Object(u32::MAX),
        ValueOrigin::Unknown,
    ]);
    let origins = Arc::new(OriginTable::from_origins(domain.iter().copied()));

    let source_value = ValueOrigin::Function(HIGH_RANK);
    let source = values_from(&origins, [source_value]);
    assert_eq!(source.iter().collect::<Vec<_>>(), [source_value]);

    let sparse_source = values_from(&origins, [ValueOrigin::Function(0), source_value]);
    assert_eq!(sparse_source.occupied_word_indices(), &[0, 194]);

    let low = [
        ValueOrigin::Function(0),
        ValueOrigin::Function(64),
        ValueOrigin::Function(128),
        ValueOrigin::Function(192),
    ];
    let intermediate = ValueOrigin::Function(INTERMEDIATE_RANK);
    let low_values = values_from(&origins, low);

    let mut accumulated = Values::empty(origins.clone());
    let mut accumulated_expected = BTreeSet::new();
    assert!(assert_union_matches_oracle(
        &mut accumulated,
        &mut accumulated_expected,
        &low_values,
    ));
    assert!(assert_union_matches_oracle(
        &mut accumulated,
        &mut accumulated_expected,
        &sparse_source,
    ));
    assert_eq!(
        accumulated.insert(intermediate),
        accumulated_expected.insert(intermediate),
    );
    assert_matches_domain(&accumulated, &accumulated_expected, &domain);

    // Reuse a composed set as the source for a third, initially empty set.
    let mut copied = Values::empty(origins.clone());
    let mut copied_expected = BTreeSet::new();
    assert!(assert_union_matches_oracle(
        &mut copied,
        &mut copied_expected,
        &accumulated,
    ));
    assert!(!assert_union_matches_oracle(
        &mut copied,
        &mut copied_expected,
        &accumulated,
    ));
    assert_matches_domain(&copied, &copied_expected, &domain);

    // Reversing source order produces the same full set and ordered iteration.
    let mut reversed = Values::empty(origins);
    let mut reversed_expected = BTreeSet::new();
    assert!(assert_union_matches_oracle(
        &mut reversed,
        &mut reversed_expected,
        &source,
    ));
    assert!(assert_union_matches_oracle(
        &mut reversed,
        &mut reversed_expected,
        &low_values,
    ));
    assert_eq!(
        reversed.insert(intermediate),
        reversed_expected.insert(intermediate),
    );
    assert_matches_domain(&reversed, &reversed_expected, &domain);
    assert_eq!(reversed, accumulated);
    assert_eq!(reversed_expected, accumulated_expected);
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
