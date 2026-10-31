//! A callback stored through a member chain stays reachable by later calls.
use debundle_e2e_support::*;

/// Unsplit, the stored callback reads `later` before `later = 42`; the split
/// would run that assignment first, so it must be refused.
fn assert_stored_callback_keeps_early_read(setup: &str, call: &str, names: &[&'static str]) {
    let source = format!(
        "let later = 0;\nlet observed = -1;\n{setup}\nconst result = {call};\nlater = 42;\nconsole.log(observed);\n"
    );
    let mut registry = vec![Member::new("observed"), Member::new("result")];
    registry.extend(names.iter().map(|name| Member::new(name)));
    let steps = if names.is_empty() {
        vec![setup]
    } else {
        vec![]
    };
    assert_preserved_entry_output(&run_fixture(FixtureOpts::new(&source, vec![])), "0\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon("registry", &registry, &steps),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn replaced_array_prototype_method_runs_on_a_later_call() {
    assert_stored_callback_keeps_early_read(
        "Array.prototype.forEach = function () { observed = later; };",
        "[{ id: 'a' }].forEach(() => 0)",
        &[],
    );
}

#[test]
fn nested_tracked_receiver_keeps_the_stored_callback() {
    assert_stored_callback_keeps_early_read(
        "const holder = { inner: {} }; holder.inner.run = () => { observed = later; };",
        "holder.inner.run()",
        &["holder"],
    );
}
