use debundle_e2e_support::*;

const SOURCE: &str = "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n";

fn check_provider(provider: &str) {
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module("wrapped", &[Member::new("wrapped")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn direct_returned_record() {
    check_provider("export function factory(callback) { return { invoke: callback }; }\n");
}

#[test]
fn namespace_member_alias() {
    check_provider(
        "const api = { factory(callback) { return { invoke: callback }; } };\nconst factory = api.factory;\nexport { factory };\n",
    );
}

#[test]
fn module_initializer_member_alias() {
    check_provider(
        "function load(initialize) { const output = {}; initialize(output); return output; }\nconst api = load((output) => { output.factory = function(callback) { return { invoke: callback }; }; });\nconst factory = api.factory;\nexport { factory };\n",
    );
}

#[test]
fn direct_variable_returned_record() {
    check_provider("export const factory = (callback) => ({ invoke: callback });\n");
}
