//! A small behavioral witness for the alias/property matrix used by the memory benchmark.

use debundle_e2e_support::*;

#[test]
fn alias_property_matrix_preserves_entry_output() {
    let source = r#"
        const object0 = {};
        const object1 = {};
        const object2 = {};
        const object3 = {};
        let receiver = object0;
        receiver = object1;
        receiver = object2;
        receiver = object3;
        receiver.p0 = () => 0;
        receiver.p1 = () => 1;
        receiver.p2 = () => 2;
        receiver.p3 = () => 3;
        function observe() { return [receiver.p0, receiver.p1, receiver.p2, receiver.p3]; }
        console.log(receiver.p0(), receiver.p3());
    "#;
    let fixture = run_fixture(
        FixtureOpts::new(source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0 3\n");
}
