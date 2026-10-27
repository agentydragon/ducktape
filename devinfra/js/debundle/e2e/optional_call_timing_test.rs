//! Optional calls retain ordinary opaque-callee callback effects.

use debundle_e2e_support::*;

const EAGER_PROVIDER: &str = "export function register(callback) { return callback(); }\nexport const api = { register };\nexport function get() { return api; }\n";

fn assert_caught_tdz_and_rejected_split(expression: &str, provider: &str) {
    let statement =
        format!("try {{ observed = {expression}; }} catch (error) {{ observed = error.name; }}");
    let source = format!(
        "import {{ register, api, get }} from './peer.js';\nlet observed;\n{statement}\nconst later = 42;\nconsole.log(observed);\n"
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &[Member::new("observed"), Member::new("later")],
                &[&statement],
            )],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "effects/observed",
                    &[Member::new("observed")],
                    &[&statement],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
        &["effects/observed", "values/later"],
        &[],
    );
}

#[test]
fn optional_eager_calls_preserve_caught_tdz_and_reject_reordering() {
    for call in [
        "register?.(() => later)",
        "api?.register(() => later)",
        "api.register?.(() => later)",
        "register?.(...[() => later])",
        "get()?.register(() => later)",
    ] {
        assert_caught_tdz_and_rejected_split(call, EAGER_PROVIDER);
    }
}

#[test]
fn optional_proxy_lookup_and_returned_callback_protocols_remain_conservative() {
    let provider = "export function register(callback) { return () => callback(); }\nexport const api = new Proxy({}, { get() { return callback => callback(); } });\nexport function get() { return api; }\n";
    for call in ["api.register?.(() => later)", "register?.(() => later)?.()"] {
        assert_caught_tdz_and_rejected_split(call, provider);
    }
}

#[test]
fn storing_provider_keeps_runtime_behavior_with_conservative_optional_timing() {
    let provider = "const callbacks = new Set;\nexport function register(callback) { callbacks.add(callback); }\nexport function emit() { return [...callbacks].map(callback => callback()); }\n";
    let source = "import { register, emit } from './peer.js';\nconst registration = register?.(() => later);\nconst later = 42;\nconsole.log(emit()[0]);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "all",
                &[Member::new("registration"), Member::new("later")],
            )],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/registration", &[Member::new("registration")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
        &["callbacks/registration", "values/later"],
        &[],
    );
}

#[test]
fn nullish_optional_call_still_skips_argument_evaluation_at_runtime() {
    let fixture = run_fixture(FixtureOpts::new(
        "let calls = 0;\nconst callback = null;\nconst result = callback?.((calls++, () => later));\nconst later = 42;\nconsole.log(calls, result, later);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("calls"),
                Member::new("callback"),
                Member::new("result"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0 undefined 42\n");
}
