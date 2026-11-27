//! Resuming generators, iterators and async functions runs their bodies where the protocol does.

use super::{CALL_FLAG, assert_reader_split_diverges_with, with_reader_split};
use debundle_e2e_support::*;

/// Opposing control: calling a generator runs nothing, and its body runs
/// when later code resumes the generator object, by `next()`, `for`-`of`,
/// destructuring (declaration, assignment, parameter), spread or `yield*`,
/// also from another statement than the one that created it. A hand-written
/// iterator's `next` runs the same way. Each resumption reaches `second`,
/// whether `run` discards `flag()` or calls what it returns, and whether or
/// not a test keeps `flag` on the source-summary lane.
#[test]
fn resuming_a_generator_or_an_iterator_runs_its_body() {
    const GEN: &str = "function* gen() { second(); yield 0; }";
    const MK: &str = "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }";
    for (definitions, resume, extra) in [
        (GEN, "gen().next();", &["gen"][..]),
        (
            "function* gen(f) { f(); yield 0; }",
            "gen(second).next();",
            &["gen"][..],
        ),
        (
            "function* gen() { counter = 1; yield 0; }",
            "gen().next();",
            &["gen"][..],
        ),
        (GEN, "for (const x of gen()) {}", &["gen"][..]),
        (GEN, "const [x] = gen();", &["gen"][..]),
        (GEN, "let x; [x] = gen();", &["gen"][..]),
        (
            "function* gen() { second(); yield 0; }\nfunction take([x]) { return x; }",
            "take(gen());",
            &["gen", "take"][..],
        ),
        (GEN, "[...gen()];", &["gen"][..]),
        (GEN, "Math.max(...gen());", &["gen"][..]),
        (
            "function* gen() { second(); yield 0; }\nfunction* outer() { yield* gen(); }",
            "outer().next();",
            &["gen", "outer"][..],
        ),
        (
            "const holder = { *items() { second(); yield 0; } };",
            "holder.items().next();",
            &["holder"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nconst box = { it: null };",
            "box.it = gen(); box.it.next();",
            &["gen", "box"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nconst g = gen();",
            "g.next();",
            &["gen", "g"][..],
        ),
        (MK, "for (const x of mk()) {}", &["mk"][..]),
        (MK, "[...mk()];", &["mk"][..]),
        (MK, "const [x] = mk();", &["mk"][..]),
    ] {
        for guard in ["", "if (first()) {} "] {
            for run in ["function run() { flag(); return 0; }", CALL_FLAG] {
                assert_reader_split_diverges_with(
                    &format!(
                        "{definitions}\nfunction flag() {{ {guard}{resume} return noop; }}\n{run}"
                    ),
                    extra,
                );
            }
        }
    }
}

/// Opposing control: a generator result read at top level, and an async
/// generator, whose first `next()` runs the body up to its first `await` or
/// `yield` synchronously. Each reaches `second`.
#[test]
fn a_top_level_generator_result_and_an_async_generator_reach_their_body() {
    for (definitions, extra) in [
        (
            "async function* ag() { second(); yield 0; }\nfunction flag() { ag().next(); return noop; }\nfunction run() { flag(); return 0; }",
            &["ag"][..],
        ),
        (
            "async function* ag() { second(); yield 0; }\nfunction flag() { ag().next(); return noop; }\nfunction run() { return flag()(); }",
            &["ag"][..],
        ),
        (
            "function* gen() { yield second; }\nconst h = gen().next().value;\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["gen", "h"][..],
        ),
        (
            "function* gen() { yield second; }\nconst h = [...gen()][0];\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["gen", "h"][..],
        ),
        (
            "function mk() { return [second]; }\nconst [h] = mk();\nfunction flag() { return h; }\nfunction run() { return flag()(); }",
            &["mk", "h"][..],
        ),
    ] {
        assert_reader_split_diverges_with(definitions, extra);
    }
}

/// Positive anchor: code after an async function's first `await` runs from
/// a job, after the synchronous module body, so it keeps the split.
#[test]
fn an_async_function_after_its_first_await_keeps_the_split() {
    let fixture = with_reader_split(
        "async function a() { await 0; second(); }\nfunction flag() { a(); return noop; }\nfunction run() { flag(); return 0; }",
        &["a"],
        run_fixture,
    );
    assert_preserved_entry_output(&fixture, "0 0\n");
}

/// Positive anchor for the control above: iterating native arrays of
/// effect-free values keeps the split.
#[test]
fn iterating_native_arrays_keeps_the_split() {
    for body in [
        "for (const x of [first]) {}",
        "const [y] = [1];",
        "let z; [z] = [2];",
        "return [...[1, 2]].length;",
    ] {
        let fixture = with_reader_split(
            &format!("function flag() {{ {body} }}\nfunction run() {{ flag(); return 0; }}"),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}
