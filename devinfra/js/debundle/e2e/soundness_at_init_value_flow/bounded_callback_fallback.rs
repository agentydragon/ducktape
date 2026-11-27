//! Bounded callback fallbacks keep pre-await reads and writes and ignore post-await ones.

use super::{in_case, members};
use debundle_e2e_support::*;

#[test]
fn bounded_local_callback_fallback_ignores_post_await_read() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"async function load(count) {
  if (count > 0) return load(count - 1);
  await 0;
  return later;
}
const pending = load(100);
const later = 42;
console.log(later);
"#,
        vec![
            logical_module("callbacks/pending", &binding_members(["load", "pending"])),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn bounded_local_callback_fallback_keeps_pre_await_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"let later = 0;
async function load(count) {
  globalThis.seen = later;
  if (count > 0) return load(count - 1);
  await 0;
}
const pending = load(100);
later = 42;
console.log(globalThis.seen, later);
"#,
            vec![
                logical_module("callbacks/pending", &binding_members(["load", "pending"])),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["callbacks/pending", "values/later"],
        &[],
    );
}

#[test]
fn bounded_unknown_callback_fallback_ignores_post_await_read() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const anchor = {};
function bounce(n) { if (n) return bounce(n - 1); }
async function deferred() { await 0; return later; }
globalThis.registeredBounce = bounce;
globalThis.registeredDeferred = deferred;
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
console.log(later);
export { anchor, bounce, deferred, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/anchor",
                    &binding_members(["anchor", "bounce", "deferred"]),
                    &[
                        "globalThis.registeredBounce = bounce;",
                        "globalThis.registeredDeferred = deferred;",
                        "globalThis.flush = () => {};",
                        "globalThis.flush();",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

/// Anchors for the post-await case above: the host may run a registered
/// callback, and its pre-await read (or a callback it creates before the
/// await) can observe `later`.
#[test]
fn bounded_unknown_callback_fallback_keeps_pre_await_effects() {
    for (name, source, names, anon) in [
        (
            "read before the await",
            r#"function bounce(n) { if (n) return bounce(n - 1); }
async function immediate() { const value = later; await 0; return value; }
globalThis.registeredBounce = bounce;
globalThis.registeredImmediate = immediate;
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
export { bounce, immediate, later };
"#,
            &["bounce", "immediate"][..],
            &[
                "globalThis.registeredBounce = bounce;",
                "globalThis.registeredImmediate = immediate;",
                "globalThis.flush = () => {};",
                "globalThis.flush();",
            ][..],
        ),
        (
            "nested callback created before the await",
            r#"function bounce(n) { if (n) return bounce(n - 1); }
async function publish() {
  globalThis.registeredNested = () => later;
  await 0;
}
globalThis.registeredBounce = bounce;
publish();
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
export { bounce, publish, later };
"#,
            &["bounce", "publish"],
            &[
                "globalThis.registeredBounce = bounce;",
                "publish();",
                "globalThis.flush = () => {};",
                "globalThis.flush();",
            ],
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    source,
                    vec![
                        logical_module_with_anon("value/callbacks", &members(names), anon),
                        logical_module("value/later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["value/callbacks", "value/later"],
                &[],
            );
        });
    }
}

#[test]
fn bounded_callback_fallback_excludes_post_await_binding_write() {
    let source = bounded_callback_write_source("await 0; state = 1;");
    let fixture = run_fixture_with_args(
        bounded_callback_write_split(&source),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "326 0\n");
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    assert!(
        !rows.is_empty(),
        "the emitted order must exercise effect relocation"
    );
    assert!(
        rows.iter().all(|row| {
            row["cells"]
                .as_array()
                .unwrap()
                .iter()
                .all(|cell| cell["cell"]["kind"] != "binding" || cell["cell"]["binding"] != "state")
        }),
        "deferred binding writes must not become modeled initialization conflicts: {rows:#?}"
    );
}

fn bounded_callback_write_source(body: &str) -> String {
    let padding = vec!["() => 3"; 64].join(", ");
    format!(
        "async function deferred() {{ {body} }}\n\
         const result = JSON.stringify([deferred, {padding}]);\n\
         var state = 0;\n\
         console.log(result.length, state);\n"
    )
}

fn bounded_callback_write_split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("value/state", &binding_members(["deferred", "state"])),
            logical_module("callbacks/result", &[Member::new("result")]),
        ],
    )
}

#[test]
fn bounded_callback_fallback_retains_synchronous_and_uncovered_writes() {
    for body in [
        "state = 1; await 0;",
        "state++; await 0;",
        "({ value: state } = { value: 1 }); await 0;",
        "[state] = [1]; await 0;",
        "for (state of [1]) {} await 0;",
        "for (state in { key: 1 }) {} await 0;",
        "await (state = 1);",
        "if (globalThis.suspend) await 0; state = 1;",
        "try { await 0; } catch {} state = 1;",
        "globalThis.saved = () => { state = 1; }; await 0;",
        "globalThis.saved = write; await 0; function write() { state = 1; }",
        "state = await 0;",
        "[(state)] = [1]; await 0;",
        "await 0; [(state)] = [1];",
    ] {
        in_case(body, || {
            let source = bounded_callback_write_source(body);
            expect_atomic_conflict_rejection(
                bounded_callback_write_split(&source),
                &["value/state", "callbacks/result"],
                &[],
            );
        });
    }
}

#[test]
fn bounded_callback_fallback_keeps_deferred_writes_runnable() {
    for body in [
        "await 0; state = 1;",
        "await 0; state++;",
        "await 0; ({ value: state } = { value: 1 });",
        "await 0; [state] = [1];",
        "let value = await Promise.resolve(1); if (!value) return; state = value;",
        "await 0; const write = () => { state = 1; }; write();",
    ] {
        in_case(body, || {
            let source = format!(
                "{}deferred().then(() => console.log(state));\n",
                bounded_callback_write_source(body)
            );
            let fixture = run_fixture(bounded_callback_write_split(&source));
            assert_preserved_entry_output(&fixture, "326 0\n1\n");
        });
    }
}

#[test]
fn bounded_callback_fallback_keeps_deferred_writer_colocated() {
    let source = bounded_callback_write_source("await 0; state = 1;");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("callbacks/writer", &[Member::new("deferred")]),
                logical_module("value/state", &[Member::new("state")]),
            ],
        )
        .with_import_order(&["callbacks/writer", "value/state", "residual"]),
        &["callbacks/writer", "value/state"],
        &["deferred_rebind"],
    );
}
