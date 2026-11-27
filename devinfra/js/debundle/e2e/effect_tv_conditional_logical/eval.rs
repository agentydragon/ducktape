//! Direct `eval` is refused at admission; indirect `eval` and `Function` keep the split.

use super::{CALL_FLAG, with_reader_split};
use debundle_e2e_support::*;

/// Opposing control: a direct `eval` runs string code that can read and
/// write any binding visible by name, which no summary sees: refused at
/// admission (A1) at any depth.
#[test]
fn direct_eval_at_any_depth_is_refused_at_admission() {
    for definitions in [
        "function flag() { let f = noop; eval('f = second'); return f; }",
        "function flag() { return eval('second'); }",
        "function flag() { eval('counter = 1'); return noop; }",
        "function flag() { eval('second()'); return noop; }",
        "function flag() { let f = noop; const set = () => eval('f = second'); set(); return f; }",
    ] {
        with_reader_split(&format!("{definitions}\n{CALL_FLAG}"), &[], |opts| {
            expect_rejection_containing_all(opts, &["a1_eval", "admission_overrides"])
        });
    }
}

/// Positive anchor: in an ES module, an indirect `eval`, `Function` and
/// `new Function` run in global scope, where no module binding is
/// visible (`typeof second` is `"undefined"`): an ordinary opaque call
/// that keeps the split.
#[test]
fn indirect_eval_and_function_in_a_module_keep_the_split() {
    for call in [
        "(0, eval)('typeof second')",
        "Function('return typeof second')()",
        "new Function('return typeof second')()",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {call}; return noop; }}\nexport {{ first }};\n{CALL_FLAG}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}
