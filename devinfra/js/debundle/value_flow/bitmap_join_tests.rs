use super::values::{BitmapJoinMemoEntry, BitmapJoinResult};
use super::*;

fn bitmap(values: impl IntoIterator<Item = u32>) -> Arc<RoaringBitmap> {
    Arc::new(values.into_iter().collect())
}

fn values(bitmap: Arc<RoaringBitmap>, unknown: bool, origins: &Arc<OriginTable>) -> Values {
    Values {
        bitmap,
        unknown,
        origins: origins.clone(),
    }
}

fn mask_bitmap(mask: u32, width: u32) -> Arc<RoaringBitmap> {
    bitmap((0..width).filter(|bit| mask & (1 << bit) != 0))
}

#[test]
fn guarded_non_function_projection_preserves_unknown_and_proxy_values() {
    let function = Value::Alloc(Kind::Function, 1);
    let class = Value::Alloc(Kind::Class, 2);
    let proxy = Value::Alloc(Kind::Proxy, 3);
    let object = Value::Alloc(Kind::Object, 4);
    let origins = Arc::new(OriginTable::from_origins([function, class, proxy, object]));
    let mut values = Values::empty(origins);
    for value in [function, class, proxy, object, Value::Top] {
        values.insert(value);
    }

    let filtered = values.without_function_class();
    assert!(!filtered.contains(&function));
    assert!(!filtered.contains(&class));
    assert!(filtered.contains(&proxy));
    assert!(filtered.contains(&object));
    assert!(filtered.contains(&Value::Top));

    let unchanged = Values::singleton(values.origins.clone(), object);
    let original_bitmap = unchanged.bitmap.clone();
    let filtered_unchanged = unchanged.without_function_class();
    assert!(
        Arc::ptr_eq(&filtered_unchanged.bitmap, &original_bitmap),
        "a disjoint filter should preserve bitmap sharing",
    );
}

#[test]
fn cached_joins_match_uncached_bitmap_and_unknown_algebra_exhaustively() {
    let origins = Arc::new(OriginTable::from_origins([]));
    for width in 0..=4 {
        let masks = 0..(1 << width);
        for left_mask in masks.clone() {
            for right_mask in masks.clone() {
                for left_unknown in [false, true] {
                    for right_unknown in [false, true] {
                        for second_unknown in [false, true] {
                            let left_bitmap = mask_bitmap(left_mask, width);
                            let right_bitmap = mask_bitmap(right_mask, width);
                            let left = values(left_bitmap.clone(), left_unknown, &origins);
                            let second_left = values(left_bitmap.clone(), second_unknown, &origins);
                            let right = values(right_bitmap, right_unknown, &origins);

                            let mut expected = left.clone();
                            let expected_changed = expected.union_with(&right);
                            let mut second_expected = second_left.clone();
                            let second_expected_changed = second_expected.union_with(&right);

                            let mut actual = left;
                            let mut second_actual = second_left;
                            let mut scratch = FlowMergeScratch::default();
                            let actual_changed = merge_values_with_scratch(
                                &mut actual,
                                right.clone(),
                                &mut scratch,
                                |_| {},
                            );
                            let second_changed = merge_values_with_scratch(
                                &mut second_actual,
                                right.clone(),
                                &mut scratch,
                                |_| {},
                            );
                            assert_eq!(actual_changed, expected_changed);
                            assert_eq!(actual, expected);
                            assert_eq!(second_changed, second_expected_changed);
                            assert_eq!(second_actual, second_expected);
                            assert!(actual.shares_bitmap(&second_actual));

                            // Metrics-on uses the same cached join path;
                            // observation cannot alter values or changes.
                            let mut observed = values(left_bitmap.clone(), left_unknown, &origins);
                            let mut second_observed = values(left_bitmap, second_unknown, &origins);
                            let mut observed_scratch = FlowMergeScratch::default();
                            let mut operations = Vec::new();
                            let observed_changed = merge_values_with_scratch(
                                &mut observed,
                                right.clone(),
                                &mut observed_scratch,
                                |operation| operations.push(operation),
                            );
                            let second_observed_changed = merge_values_with_scratch(
                                &mut second_observed,
                                right,
                                &mut observed_scratch,
                                |operation| operations.push(operation),
                            );
                            assert!(operations.iter().any(|operation| matches!(
                                operation,
                                work_metrics::Operation::BitmapJoinMemoHit
                            )));
                            assert_eq!(observed_changed, expected_changed);
                            assert_eq!(observed, expected);
                            assert_eq!(second_observed_changed, second_expected_changed);
                            assert_eq!(second_observed, second_expected);
                            assert!(observed.shares_bitmap(&second_observed));
                        }
                    }
                }
            }
        }
    }
}

#[test]
fn join_outcomes_preserve_pointer_sharing_and_reuse_shared_union_results() {
    let origins = Arc::new(OriginTable::from_origins([]));

    let shared = bitmap([1, 2]);
    let mut same = values(shared.clone(), false, &origins);
    assert!(!merge_values_with_scratch(
        &mut same,
        values(shared.clone(), false, &origins),
        &mut FlowMergeScratch::default(),
        |_| {},
    ));
    assert!(same.shares_bitmap(&values(shared.clone(), false, &origins)));

    let old = bitmap([1, 2]);
    let mut included = values(old.clone(), false, &origins);
    let incoming_subset = values(bitmap([2]), false, &origins);
    assert!(!merge_values_with_scratch(
        &mut included,
        incoming_subset,
        &mut FlowMergeScratch::default(),
        |_| {},
    ));
    assert!(Arc::ptr_eq(&included.bitmap, &old));

    let incoming = values(bitmap([1, 2, 3]), false, &origins);
    let incoming_bitmap = incoming.bitmap.clone();
    let mut adopted = values(bitmap([1]), false, &origins);
    assert!(merge_values_with_scratch(
        &mut adopted,
        incoming,
        &mut FlowMergeScratch::default(),
        |_| {},
    ));
    assert!(Arc::ptr_eq(&adopted.bitmap, &incoming_bitmap));

    let old_bitmap = bitmap([1]);
    let old_witness = Arc::downgrade(&old_bitmap);
    let mut first = values(old_bitmap.clone(), false, &origins);
    let mut second = values(old_bitmap.clone(), false, &origins);
    drop(old_bitmap);
    let right = values(bitmap([2]), true, &origins);
    let work = Rc::new(RefCell::new(FlowWorkStats::default()));
    let mut scratch = FlowMergeScratch::with_work(work.clone());
    let mut operations = Vec::new();
    assert!(merge_values_with_scratch(
        &mut first,
        right.clone(),
        &mut scratch,
        |operation| operations.push(operation),
    ));
    assert!(merge_values_with_scratch(
        &mut second,
        right,
        &mut scratch,
        |operation| operations.push(operation),
    ));
    assert!(first.shares_bitmap(&second));
    assert!(first.unknown && second.unknown);
    assert_eq!(work.borrow().bitmap_join_memo_hits, 1);
    assert_eq!(work.borrow().bitmap_subset_checks, 2);
    assert_eq!(work.borrow().bitmap_copy_on_write_unions, 1);
    assert_eq!(
        operations
            .iter()
            .filter(|operation| matches!(operation, work_metrics::Operation::BitmapJoinMemoHit))
            .count(),
        1
    );
    assert_eq!(
        operations
            .iter()
            .filter(|operation| matches!(
                operation,
                work_metrics::Operation::BitmapCopyOnWriteUnion
            ))
            .count(),
        1,
        "cached union reuse is not counted as a second COW union"
    );
    drop(second);
    assert!(
        old_witness.upgrade().is_none(),
        "memo must not pin old bitmap payloads"
    );
}

#[test]
fn in_place_cow_invalidates_weak_pair_witnesses_before_bitmap_mutation() {
    let origins = Arc::new(OriginTable::from_origins([]));
    let old_bitmap = bitmap([1]);
    let mut first = values(old_bitmap.clone(), false, &origins);
    let mut second = values(old_bitmap.clone(), false, &origins);
    drop(old_bitmap);
    let right = values(bitmap([2]), false, &origins);
    let other = values(bitmap([4]), false, &origins);
    let work = Rc::new(RefCell::new(FlowWorkStats::default()));
    let mut scratch = FlowMergeScratch::with_work(work.clone());

    assert!(merge_values_with_scratch(
        &mut first,
        right.clone(),
        &mut scratch,
        |_| {},
    ));
    drop(first);
    // The old bitmap now has one strong owner. A different join mutates it
    // in place; Arc::make_mut must dissociate the prior weak witness.
    assert!(merge_values_with_scratch(
        &mut second,
        other,
        &mut scratch,
        |_| {},
    ));
    assert!(merge_values_with_scratch(
        &mut second,
        right,
        &mut scratch,
        |_| {},
    ));
    assert_eq!(second.bitmap.iter().collect::<Vec<_>>(), [1, 2, 4]);
    assert_eq!(work.borrow().bitmap_join_memo_hits, 0);
}

#[test]
fn dead_weak_witness_rejects_an_address_only_aba_match() {
    let mut scratch = FlowMergeScratch::default();
    let old = bitmap([1]);
    let stale_witness = Arc::downgrade(&old);
    drop(old);
    let current = bitmap([1]);
    let from = bitmap([2]);
    scratch.entries[0] = Some(BitmapJoinMemoEntry {
        // Simulate allocator address reuse: the address matches, but the
        // weak witness belongs to the dead prior allocation.
        into_address: Arc::as_ptr(&current) as usize,
        from_address: Arc::as_ptr(&from) as usize,
        into: stale_witness,
        from: Arc::downgrade(&from),
        result: BitmapJoinResult::SubsetSkip,
    });
    assert!(scratch.lookup(&current, &from).is_none());
}

#[test]
fn parsed_property_write_fanout_reuses_shared_bitmap_join_pairs() {
    let mut source = String::from("function left() {}\nfunction right() {}\n");
    for index in 0..12 {
        source.push_str(&format!(
            "const object{index} = {{}};\nobject{index}.p = left;\nobject{index}.p = right;\n"
        ));
    }
    let parsed = raw_js_test_support::parse(&source);
    let views = analysis_domain::item_views::top_level_item_views(&parsed.body);
    let (flow, work) = ChunkValueFlow::analyze_with_stats(&views);
    assert_eq!(work.bitmap_join_memo_hits, 12);
    assert_eq!(work.bitmap_join_memo_misses, 12);
    assert_eq!(work.bitmap_subset_checks, 24);
    assert_eq!(work.bitmap_copy_on_write_unions, 12);

    let id = |name: &str| (name.into(), swc_common::SyntaxContext::empty());
    let left = flow.values_of_binding(&id("left"));
    let right = flow.values_of_binding(&id("right"));
    for index in 0..12 {
        let object = flow.values_of_binding(&id(&format!("object{index}")));
        let [Value::Alloc(Kind::Object, object)] = object.iter().collect::<Vec<_>>()[..] else {
            panic!("object allocation should be singleton");
        };
        let property = flow
            .values_of_property(object, "p")
            .expect("property write result");
        assert!(left.iter().all(|value| property.contains(&value)));
        assert!(right.iter().all(|value| property.contains(&value)));
    }
}
