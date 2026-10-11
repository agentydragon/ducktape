//! Many plain objects and repeated writes exercise escape reachability at
//! scale. The source is synthetic; the output must still preserve the value.

use debundle_e2e_support::*;

#[test]
fn repeated_writes_do_not_repeat_candidate_graph_searches() {
    const COUNT: usize = 2_200;
    let mut source = String::new();
    for index in 0..COUNT {
        source.push_str(&format!(
            "const foo_{index} = {{ child: {{}}, value: {index} }};\n"
        ));
    }
    for _ in 0..COUNT {
        source.push_str("foo_0.value = 1;\n");
    }
    source.push_str(&format!("console.log(foo_{}.value);\n", COUNT - 1));

    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module("foo", &[Member::new("foo_2199")])],
    ));
    assert_preserved_entry_output(&fixture, &format!("{}\n", COUNT - 1));
}
