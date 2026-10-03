//! Keep an eager initializer with the value it reads, or reject the split.

use debundle_e2e_support::*;

const SOURCE: &str = r#"import { identity } from "./mod_x.js";

var a = identity({ x: { value: "ok" } });
var b = { x: a.x };

function readable() {
  return b.x.value;
}

console.log(readable());
"#;

const EXTRA_CHUNKS: &[(&str, &str)] = &[(
    "static/mod_x",
    "function identity(value) { return value; }\nexport { identity };\n",
)];

fn opts(modules: Vec<LogicalModuleEntry>) -> FixtureOpts<'static> {
    FixtureOpts::new(SOURCE, modules)
        .with_chunk_id("static/app")
        .with_extra_chunks(EXTRA_CHUNKS)
}

#[test]
fn eager_initializer_stays_with_its_value_or_split_is_rejected() {
    assert_entry_output(&run_fixture(opts(vec![])), "ok\n");

    // Inline residual mode leaves `a` in the entry while `b` moves to a
    // dependency. The dependency evaluates first, before the entry initializes
    // `a`, so this eager read cannot be emitted as a valid split.
    let unsafe_split = opts(vec![logical_module(
        "mod_y",
        &[Member::new("b"), Member::new("readable")],
    )])
    .with_unassigned_mode(unassigned_mode_inline());
    run_rejection_fixture(unsafe_split);

    // Co-locating the eager initializer with the value it reads gives ESM a
    // valid evaluation order and preserves the entry's result.
    let safe_split = opts(vec![
        logical_module("mod_x_data", &[Member::new("a"), Member::new("b")]),
        logical_module("mod_y", &[Member::new("readable")]),
    ])
    .with_unassigned_mode(unassigned_mode_inline());
    assert_entry_output(&run_fixture(safe_split), "ok\n");
}
