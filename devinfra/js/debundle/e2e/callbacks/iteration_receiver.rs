//! AR2: an opaque call on a primitive currently loses the enclosing iteration's
//! receiver proof. Keep the conservative refusal and its behavioral controls
//! until a capability proof can replace that fallback.

use debundle_e2e_support::*;

const HOST: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(value) { console.log(value); }\n",
)];

fn source(iteration: &str) -> String {
    format!(
        "import {{ host }} from './peer.js';\n\
         const entries = [() => later];\n\
         {iteration}\n\
         const first = entries[0];\n\
         const later = 42;\n\
         console.log(first());\n"
    )
}

fn split<'a>(source: &'a str, peers: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module("later", &[Member::new("later")])],
    )
    .with_extra_chunks(peers)
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn primitive_only_host_call_keeps_the_current_conservative_refusal() {
    let source = source("entries.forEach(() => host(1));");
    let inline = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(&HOST)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&inline, "1\n42\n");
    // This split is semantically valid: host receives neither the array nor
    // its stored callback. AR2 should turn this into an emitted-output check
    // when the receiver's confinement is proved, without suppressing effects
    // or claiming normal completion for the opaque call.
    expect_atomic_conflict_rejection(split(&source, &HOST), &["later"], &[]);
}

#[test]
fn removing_the_opaque_call_allows_the_same_extraction() {
    let source = source("entries.forEach(() => 0);");
    let fixture = run_fixture(split(&source, &HOST));
    assert_preserved_entry_output(&fixture, "42\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/later.js",
        &["later"],
        &[],
    );
}

#[test]
fn publishing_the_receiver_really_can_observe_the_future_binding() {
    let source = source("entries.forEach((_value, _index, array) => host(array));");
    let peer = [(
        "static/peer",
        "export function host(array) {\n\
           try { console.log(array[0]()); }\n\
           catch (error) { console.log(error.name); }\n\
         }\n",
    )];
    let inline = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(&peer)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&inline, "ReferenceError\n42\n");
    expect_atomic_conflict_rejection(split(&source, &peer), &["later"], &[]);
}
