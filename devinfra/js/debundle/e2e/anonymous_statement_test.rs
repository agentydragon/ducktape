//! Anonymous-statement selector tests: zero-match, ambiguous-match, round-trip,
//! source-order interleaving, IIFE multiline, and comma-list ordinal offset.

use debundle_e2e_support::*;
use std::fs;

// Round-trip pin for anonymous-statement member support.
//
// Today the spec only addresses named bindings via
// `members[].selector.binding.name`. Anonymous side-effect
// statements (top-level `console.log(...)`, IIFE preludes,
// decorator-application calls like `applyDecorators(C.prototype, …)`)
// have empty `declared_bindings` and no name to reference, so the
// materializer can't emit them as part of any logical module —
// it silently leaves them in residual.
//
// This blocks moving bindings whose atomic unit also contains anonymous
// statements (decorator applications on the class prototype, runtime init
// calls, bundle preludes). This test pins the materialization-side fix.
//
// Spec extension under test: a sibling field on
// `spec::LogicalModule`:
//
// ```yaml
// x_module:
//   members:
//     - selector: { binding: { name: X } }
//   anonymous_statements:
//     - match: 'console.log("a");'
// ```
//
// `match` carries the JS source of the target statement verbatim;
// the resolver parses it as a single `Stmt` and walks the chunk's
// top-level statements looking for exactly one whose AST matches
// (modulo spans). Resolved owners flow into the same
// `selected_ordinals` set the materializer already builds for
// named members.
//
// Per the user constraint, the selector must address statements
// by **AST shape**, not line/column — the the upstream dump is prettified
// and lines aren't stable across re-prettifies.
#[test]
fn round_trip_peels_anon_statement_with_named_member() {
    // Source-order layout:
    //   1. console.log("a")        - anon side-effect (empty declared)
    //   2. var X = (() => "x")()   - var_decl with side-effectful IIFE init
    //   3. const Existing          - named const, stays in residual
    //   4. console.log(Existing)   - anon side-effect, stays in residual
    //
    // The s-edge from owner(X) to owner(console.log("a")) makes the
    // singleton {X} `BlockedResidualDependency`. The proposed peel
    // is the closure {console.log("a"), X}, which the materializer
    // can only emit if the spec carries an anon-statement entry
    // pointing at the leading console.log.
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("a");
var X = (() => "x")();
const Existing = "existing";
console.log(Existing);
export { X, Existing };
"#,
        vec![logical_module_with_anon(
            "x_module",
            &[Member::new("X")],
            &[r#"console.log("a");"#],
        )],
    ));

    // End-to-end behavior: console.log("a") then console.log(Existing).
    // x_module body runs first via ESM import order; residual second.
    assert_entry_output(&fixture, "a\nexisting\n");

    // The peeled module must carry both the leading console.log
    // and the var X declaration, in source order, plus an
    // `export { X }` re-export so entry's `import { X } from "./..."`
    // resolves.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x_module.js",
        &[r#"console.log("a")"#, "var X", "export {", "X"],
        &[],
    );

    // The leading console.log and X's declaration must be gone
    // from residual (otherwise the side-effect runs twice).
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &[],
        &[r#"console.log("a")"#, "var X"],
    );
}

// Regression test (originally RED) for the anonymous-only-module
// side-effect drop: a logical module whose only member is an
// anonymous statement owns no bindings, so the entry used to emit no
// import for it at all — the emitted file existed but was never
// loaded, and its side effects silently vanished while the gate
// accepted the spec. The entry must emit a side-effect-only
// `import "./<module>.js";` for binding-less modules, placed by the
// same shared import ordering as every other entry import.
#[test]
fn anonymous_only_module_side_effects_still_run() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const keep = "k";
console.log("anon-side-effect");
export { keep };
"#,
        vec![logical_module_with_anon(
            "anon_only",
            &[],
            &[r#"console.log("anon-side-effect");"#],
        )],
    ));

    // The module file carries the claimed statement...
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/anon_only.js",
        &[r#"console.log("anon-side-effect")"#],
        &[],
    );
    // ...and entry loads it via a bare side-effect import.
    let entry_src = fs::read_to_string(&fixture.entry_path).expect("read emitted entry");
    assert!(
        entry_src.contains("modules/anon_only.js"),
        "entry must import the binding-less module so its side effects run:\n{entry_src}",
    );
    // End-to-end: the moved side effect actually executes under Node.
    assert_entry_output(&fixture, "anon-side-effect\n");
}

// Pin the zero-match error path for anonymous-statement selectors.
//
// When an `anonymous_statements[].match` source doesn't match any
// top-level statement in the chunk, the materializer must reject
// the spec with a diagnostic that includes:
//
//   1. The logical module's id (so the author knows which entry
//      is broken).
//   2. The selector source verbatim (so the author can spot what
//      changed).
//   3. A clear "did not match" framing so the author knows the
//      remediation is "find the new shape" or "remove the entry."
//
// Mirrors the validator's "cycle = reject" philosophy: a stale
// anonymous-statement selector becomes loud at validation time
// rather than silently skipping the co-move.
#[test]
fn rejects_anonymous_statement_match_with_no_top_level_match() {
    let opts = FixtureOpts::new(
        r#"console.log("a");
var X = (() => "x")();
const Existing = "existing";
console.log(Existing);
export { X, Existing };
"#,
        vec![logical_module_with_anon(
            "x_module",
            &[Member::new("X")],
            // Selector that doesn't appear in the chunk: the author's
            // upstream an upstream refactor renamed or removed the leading
            // console.log, but the spec still claims it.
            &[r#"console.log("nope");"#],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            // Names the offending logical module so the author
            // knows which spec entry to fix.
            "static/app::x_module",
            // "did not match" framing.
            "did not match",
            // The selector source verbatim so the author can see
            // what's stale.
            r#"console.log("nope")"#,
        ],
    );
}

// Pin the ambiguous-match error path for anonymous-statement
// selectors.
//
// Anonymous-statement selectors are unique-by-design: each
// `match` source must address exactly one top-level statement.
// When the chunk contains two structurally-identical top-level
// statements (e.g. two `console.log("dup")` calls) and the
// selector matches both, the materializer must reject the spec
// with a diagnostic that includes:
//
//   1. The logical module's id.
//   2. The selector source verbatim.
//   3. The matching statement ordinals so the author can refine
//      by writing two distinct selectors (probably with
//      surrounding context) or accept that the chunk genuinely
//      contains indistinguishable duplicates.
//
// Mirrors the validator's "cycle = reject" philosophy: the
// resolver never picks one match silently, even if the chunk's
// source order is well-defined — the spec author has to make
// the choice explicit.
#[test]
fn rejects_anonymous_statement_match_with_multiple_top_level_matches() {
    // Two source-order positions both produce
    // `ExprStmt(Call(console.log, ["dup"]))`. EqIgnoreSpan treats
    // them as equal — there's no way to disambiguate from the
    // selector source alone.
    let opts = FixtureOpts::new(
        r#"console.log("dup");
var X = (() => "x")();
console.log("dup");
const Existing = "existing";
console.log(Existing);
export { X, Existing };
"#,
        vec![logical_module_with_anon(
            "x_module",
            &[Member::new("X")],
            &[r#"console.log("dup");"#],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            // Names the offending logical module.
            "static/app::x_module",
            // "ambiguous" framing.
            "ambiguous",
            // Selector source verbatim.
            r#"console.log("dup")"#,
        ],
    );
}

#[test]
fn keep_going_reports_anonymous_statement_failures_and_duplicate_claims_together() {
    let opts = FixtureOpts::new(
        r#"console.log("dup");
const marker = 1;
console.log("dup");
export { marker };
"#,
        vec![
            logical_module_with_anon("diagnostics/missing", &[], &[r#"console.log("missing");"#]),
            logical_module_with_anon("diagnostics/ambiguous", &[], &[r#"console.log("dup");"#]),
            logical_module("owners/marker", &[Member::new("marker")]),
            logical_module(
                "duplicates/marker",
                &[Member::renamed("markerAgain", "marker")],
            ),
        ],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "Anonymous statement selector diagnostic report: 2 unresolved selector(s) found",
        "diagnostics/missing",
        "did not match any top-level statement group",
        r#"console.log("missing")"#,
        "diagnostics/ambiguous",
        "ambiguous",
        r#"console.log("dup")"#,
        "Duplicate binding claim report: 1 duplicate claim(s) found",
        "\"marker\"",
        "owners/marker",
        "duplicates/marker",
        "as `markerAgain`",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
}

// Pin source-order interleaving of anonymous-statement members
// and named members within the same logical module.
//
// Within a module's body, statements emit in their original
// chunk source order (Invariant #2 in docs/design.md). Anonymous
// statements claimed by the module must interleave naturally
// with named members — there's no separate "anon section" or
// reordering pass.
//
// This guards against a regression where the materializer
// reorders anon and named owners (e.g. emitting all named
// members first, then anons at the end). For decorator-style
// companions like `Ww([Z], $g.prototype, "invites", 2);`, that
// reordering would put the decorator application BEFORE the
// class declaration in the emitted module — violating ESM
// evaluation order and causing
// `ReferenceError: Cannot access $g before initialization`.
#[test]
fn anon_statements_emit_in_chunk_source_order_alongside_named_members() {
    // Source-order layout interleaves three anon side-effects
    // with two named consts:
    //   1. console.log("before")
    //   2. const A = 1
    //   3. console.log("between")
    //   4. const B = 2
    //   5. console.log("after")
    //   6. const Existing = "existing"          (residual)
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("before");
const A = 1;
console.log("between");
const B = 2;
console.log("after");
const Existing = "existing";
export { A, B, Existing };
"#,
        vec![logical_module_with_anon(
            "ab_module",
            &[Member::new("A"), Member::new("B")],
            &[
                r#"console.log("before");"#,
                r#"console.log("between");"#,
                r#"console.log("after");"#,
            ],
        )],
    ));

    // Emitted ab_module body must preserve source order:
    //   console.log("before")  →  const A = 1  →  console.log("between")
    //   →  const B = 2  →  console.log("after")
    //
    // Verified by checking the relative byte offset of each
    // landmark in the read-back source — string-search positions
    // are a stable enough proxy for emission order in this
    // fixture (each substring is unique).
    let ab_src = fs::read_to_string(fixture.out_root.join("static/app/modules/ab_module.js"))
        .expect("read ab_module.js");
    let landmarks = [
        r#"console.log("before")"#,
        "const A = 1",
        r#"console.log("between")"#,
        "const B = 2",
        r#"console.log("after")"#,
    ];
    let positions: Vec<usize> = landmarks
        .iter()
        .map(|needle| {
            ab_src
                .find(needle)
                .unwrap_or_else(|| panic!("ab_module.js missing {needle:?}; got:\n{ab_src}"))
        })
        .collect();
    let mut sorted = positions.clone();
    sorted.sort();
    assert_eq!(
        positions, sorted,
        "ab_module.js statements not in source order: {positions:?} vs sorted {sorted:?}\n{ab_src}",
    );

    // End-to-end behavior: console.log statements run in source
    // order alongside the const initializations.
    assert_entry_output(&fixture, "before\nbetween\nafter\n");
}

// Pin matching of multi-line IIFE anonymous statements.
//
// the upstream actual companions are not single-line `console.log`
// calls; they include multi-line IIFE preludes like the Sentry
// debug-id wrapper:
//
// ```js
// !(function () {
//   try {
//     var e = "undefined" != typeof window ? window : globalThis,
//       n = new e.Error().stack;
//     n && (e._sentryDebugIds = e._sentryDebugIds || {},
//           e._sentryDebugIds[n] = "...");
//   } catch (e) {}
// })();
// ```
//
// This test pins:
//   * the `match` source can be a multi-line block scalar
//   * the parser accepts the wrapped IIFE as a single
//     ExpressionStatement
//   * `EqIgnoreSpan` ignores whitespace differences (the YAML
//     block scalar's indentation is stripped, the chunk's
//     formatting is whatever the prettifier produced) and
//     compares structurally
//   * the resolver finds exactly one match
//
// Without this guard, an upstream re-prettify that touched the
// IIFE's indentation would silently desynchronize selectors
// from chunk source.
#[test]
fn multi_line_iife_anon_statement_matches_modulo_whitespace() {
    let fixture = run_fixture(FixtureOpts::new(
        // Source mirrors the an upstream Sentry-prelude shape (lines
        // 1-17 in `static/index-EXAMPLE.js`): a `!`-prefixed
        // IIFE expression statement that walks `globalThis` to
        // attach a debug id.
        r#"!(function () {
  try {
    var e =
        "undefined" != typeof window
          ? window
          : "undefined" != typeof globalThis
            ? globalThis
            : {},
      n = new e.Error().stack;
    n && ((e._sentryDebugIds = e._sentryDebugIds || {}), (e._sentryDebugIds[n] = "test-debug-id"));
  } catch (e) {}
})();
var X = (() => "x")();
const Existing = "existing";
console.log(Existing);
export { X, Existing };
"#,
        vec![logical_module_with_anon(
            "x_module",
            &[Member::new("X")],
            // Selector source mirrors the IIFE shape but with
            // different indentation than the chunk source — the
            // resolver's `EqIgnoreSpan` comparison must look
            // through that.
            &[r#"!(function () {
    try {
        var e =
                "undefined" != typeof window
                    ? window
                    : "undefined" != typeof globalThis
                        ? globalThis
                        : {},
            n = new e.Error().stack;
        n && ((e._sentryDebugIds = e._sentryDebugIds || {}), (e._sentryDebugIds[n] = "test-debug-id"));
    } catch (e) {}
})();"#],
        )],
    ));

    // The peeled module carries the IIFE alongside the var X
    // declaration.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x_module.js",
        &["_sentryDebugIds", "var X"],
        &[],
    );

    // Residual no longer carries the IIFE.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &[],
        &["_sentryDebugIds", "var X"],
    );

    // End-to-end: the IIFE's try/catch swallows any error from
    // touching `globalThis`, so the module loads cleanly. Then
    // `console.log(Existing)` runs from residual.
    assert_entry_output(&fixture, "existing\n");
}

// Pin the body-index → statement-ordinal conversion when an
// earlier comma-list var-decl in the chunk shifts the count.
//
// `facts::top_level_item_views` splits a top-level
// `var a = …, b = …;` into two post-split owners with
// consecutive `StatementOrdinal` values. A subsequent anonymous
// statement at pre-split body index N therefore lives at
// post-split statement ordinal N + (number of extra splits in
// body[..N]).
//
// Without the conversion, the ChunkFactorization's destination override
// (which keys off `statement_ordinal`) targets the wrong owner
// node — the materializer still emits the right body item into
// the right module, but the realizability check sees a stale
// module dep graph and the spec gets rejected with a fake
// cycle. This test is the regression pin: a comma-list before
// a peeled anon side-effect, with a named member also peeled,
// and the round-trip must complete without a cycle error.
#[test]
fn anon_statement_after_comma_list_resolves_correct_owner() {
    // body[0] = `var a = 1, b = 2;` — 2-decl comma-list (post-split positions 0, 1)
    // body[1] = `console.log("between");`            — anon (post-split position 2)
    // body[2] = `var X = (() => "x")();`             — named (post-split position 3)
    // body[3] = `const Existing = "existing";`       — named (post-split position 4)
    // body[4] = `console.log(Existing);`             — anon (post-split position 5)
    //
    // Pre-split body index of `console.log("between")` is 1.
    // Post-split statement_ordinal is 2 (because body[0]'s
    // comma-list adds +1 to the count).
    //
    // If the conversion is wrong, factorization.rs would override
    // owner with `statement_ordinal == 1` (which is `b`'s owner)
    // instead of the anon owner — `b` would be claimed by
    // x_module while the anon stays in residual, and either:
    //   (a) the module dep graph would have a fake cycle, or
    //   (b) the named-member assertion would catch a non-X
    //       binding in x_module's exports.
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = 1, b = 2;
console.log("between");
var X = (() => "x")();
const Existing = "existing";
console.log(Existing);
export { a, b, X, Existing };
"#,
        vec![logical_module_with_anon(
            "x_module",
            &[Member::new("X")],
            &[r#"console.log("between");"#],
        )],
    ));

    // x_module owns X and the anon `console.log("between");`.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x_module.js",
        &[r#"console.log("between")"#, "var X", "export {", "X"],
        // Comma-list var-decls (a, b) must NOT have been swept
        // into x_module by a stale conversion.
        &["var a", "var b", " a = ", " b = "],
    );

    // Residual still emits a, b, Existing, console.log(Existing).
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["a", "b", "Existing"],
        &[r#"console.log("between")"#, "var X"],
    );

    // End-to-end: prints "between" then "existing".
    assert_entry_output(&fixture, "between\nexisting\n");
}

#[test]
fn alpha_anonymous_statement_selector_survives_identifier_drift() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function applyMetadata(args, target, prop) {
  console.log(`${prop}:${target.constructor.name}:${args.length}`);
}
const RuntimeToken = Symbol("runtime-token");
class RuntimeSubject {}
applyMetadata([RuntimeToken], RuntimeSubject.prototype, "statusFlag");
const Existing = "existing";
console.log(Existing);
export { RuntimeSubject, Existing };
"#,
        vec![logical_module_with_anon_alpha(
            "decorated_runtime",
            &[
                Member::new("applyMetadata"),
                Member::new("RuntimeToken"),
                Member::new("RuntimeSubject"),
            ],
            r#"decorate([token], Subject.prototype, "statusFlag");"#,
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/decorated_runtime.js",
        &[
            "function applyMetadata",
            "const RuntimeToken",
            "class RuntimeSubject",
            "applyMetadata([",
            "RuntimeToken",
            "RuntimeSubject.prototype",
            r#""statusFlag""#,
        ],
        &["const Existing"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const Existing"],
        &["RuntimeSubject.prototype", r#""statusFlag""#],
    );
    assert_entry_output(&fixture, "statusFlag:RuntimeSubject:1\nexisting\n");
}

#[test]
fn alpha_anonymous_statement_target_statement_uses_context_but_claims_only_target() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeLeft = "left",
  runtimeRight = "right";
console.log(`${runtimeLeft}:${runtimeRight}`);
const Existing = "existing";
console.log(Existing);
export { runtimeLeft, runtimeRight, Existing };
"#,
        vec![logical_module_with_anon_alpha_target_statement(
            "selected_pair",
            &[Member::new("runtimeLeft"), Member::new("runtimeRight")],
            r#"const selectedLeft = "left",
  selectedRight = "right";
console.log(`${selectedLeft}:${selectedRight}`);"#,
            1,
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_pair.js",
        &[
            "const runtimeLeft",
            "const runtimeRight",
            "console.log(`${runtimeLeft}:${runtimeRight}`)",
        ],
        &["const Existing"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const Existing"],
        &["runtimeLeft}:${runtimeRight"],
    );
    assert_entry_output(&fixture, "left:right\nexisting\n");
}

#[test]
fn alpha_anonymous_statement_target_statements_claims_multiple_targets_from_one_selector() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeContext = "context";
console.log("first selected");
console.log("second selected");
const Existing = "existing";
console.log(Existing);
export { runtimeContext, Existing };
"#,
        vec![logical_module_with_anon_alpha_target_statements(
            "selected_logs",
            &[],
            r#"const selectorContext = "context";
console.log("first selected");
console.log("second selected");"#,
            &[1, 2],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_logs.js",
        &[
            r#"console.log("first selected")"#,
            r#"console.log("second selected")"#,
        ],
        &["runtimeContext", "Existing"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const runtimeContext", "const Existing"],
        &["first selected", "second selected"],
    );
    assert_entry_output(&fixture, "first selected\nsecond selected\nexisting\n");
}

#[test]
fn alpha_anonymous_statement_target_statements_match_assignment_targets_from_context() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let runtimeRecord, runtimeReplay;
true && (runtimeReplay = "replay");
false || (runtimeRecord = "record");
console.log(`${runtimeReplay}:${runtimeRecord}`);
export { runtimeRecord, runtimeReplay };
"#,
        vec![logical_module_with_anon_alpha_target_statements(
            "bridge_slots",
            &[Member::new("runtimeRecord"), Member::new("runtimeReplay")],
            r#"let selectedRecord, selectedReplay;
true && (selectedReplay = "replay");
false || (selectedRecord = "record");"#,
            &[1, 2],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/bridge_slots.js",
        &[
            "let runtimeRecord",
            "runtimeReplay = \"replay\"",
            "runtimeRecord = \"record\"",
        ],
        &["selectedRecord", "selectedReplay"],
    );
    assert_entry_output(&fixture, "replay:record\n");
}

#[test]
fn binding_group_target_statements_claims_assignments_from_same_template() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let runtimeRecord, runtimeReplay;
true && (runtimeReplay = "replay");
false || (runtimeRecord = "record");
console.log(`${runtimeReplay}:${runtimeRecord}`);
export { runtimeRecord, runtimeReplay };
"#,
        vec![logical_module_with_binding_groups(
            "bridge_slots",
            &[],
            &[BindingGroup::source_alpha(
                r#"let recordSlot, replaySlot;
true && (replaySlot = "replay");
false || (recordSlot = "record");"#,
                &[
                    ("recordSlot", "recordBridge"),
                    ("replaySlot", "replayBridge"),
                ],
            )
            .with_target_statements(&[1, 2])],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/bridge_slots.js",
        &[
            "let recordBridge",
            "replayBridge = \"replay\"",
            "recordBridge = \"record\"",
        ],
        &["recordSlot", "replaySlot"],
    );
    assert_entry_output(&fixture, "replay:record\n");
}

#[test]
fn binding_group_claims_decorated_class_and_decorator_statements_from_one_selector() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function applyPropertyDecorator(markers, target, property) {
  target[`_${property}`] = markers.length;
}
class RuntimeModel {
  report() {
    return `${this._state}:${this._title}`;
  }
}
applyPropertyDecorator(["observable"], RuntimeModel.prototype, "state");
applyPropertyDecorator(["observable"], RuntimeModel.prototype, "title");
console.log(new RuntimeModel().report());
export { RuntimeModel };
"#,
        vec![logical_module_with_binding_groups(
            "decorated_model",
            &[],
            &[BindingGroup::source_alpha_adopt_names(
                r#"class DecoratedModel {
  CLASS_REST;
  report() {
    STMT_LIST;
  }
  CLASS_REST;
}
decorate(["observable"], DecoratedModel.prototype, "state");
decorate(["observable"], DecoratedModel.prototype, "title");"#,
                &["DecoratedModel"],
            )
            .with_target_statements(&[1, 2])],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/decorated_model.js",
        &[
            "class DecoratedModel",
            "applyPropertyDecorator([",
            "DecoratedModel.prototype",
            r#""state""#,
            r#""title""#,
        ],
        &["class RuntimeModel", "function applyPropertyDecorator"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["function applyPropertyDecorator"],
        &["RuntimeModel.prototype", "DecoratedModel.prototype"],
    );
    assert_entry_output(&fixture, "1:1\n");
}

#[test]
fn binding_group_target_statements_supports_stmt_list_context_gap() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let runtimeRecord, runtimeReplay;
const ignoredContext = "setup";
true && (runtimeReplay = "replay");
false || (runtimeRecord = "record");
console.log(`${runtimeReplay}:${runtimeRecord}`);
export { runtimeRecord, runtimeReplay };
"#,
        vec![logical_module_with_binding_groups(
            "bridge_slots",
            &[],
            &[BindingGroup::source_alpha(
                r#"let recordSlot, replaySlot;
STMT_LIST;
true && (replaySlot = "replay");
false || (recordSlot = "record");"#,
                &[
                    ("recordSlot", "recordBridge"),
                    ("replaySlot", "replayBridge"),
                ],
            )
            .with_target_statements(&[2, 3])],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/bridge_slots.js",
        &[
            "let recordBridge",
            "replayBridge = \"replay\"",
            "recordBridge = \"record\"",
        ],
        &["ignoredContext", "STMT_LIST"],
    );
    assert_entry_output(&fixture, "replay:record\n");
}

#[test]
fn binding_group_target_statements_still_rejects_ambiguous_ranges() {
    let opts = FixtureOpts::new(
        r#"let firstRecord, firstReplay;
true && (firstReplay = "replay");
false || (firstRecord = "record");
let secondRecord, secondReplay;
true && (secondReplay = "replay");
false || (secondRecord = "record");
export { firstRecord, firstReplay, secondRecord, secondReplay };
"#,
        vec![logical_module_with_binding_groups(
            "bridge_slots",
            &[],
            &[BindingGroup::source_alpha(
                r#"let recordSlot, replaySlot;
true && (replaySlot = "replay");
false || (recordSlot = "record");"#,
                &[
                    ("recordSlot", "recordBridge"),
                    ("replaySlot", "replayBridge"),
                ],
            )
            .with_target_statements(&[1, 2])],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::bridge_slots",
            "binding_groups[].source_match",
            "ambiguous",
            "target_binding `recordSlot`",
        ],
    );
}

#[test]
fn alpha_anonymous_statement_target_statements_all_supports_top_level_stmt_list_hole() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log("before selected");
const skippedContext = "skip";
console.log("after selected");
const Existing = "existing";
console.log(Existing);
export { skippedContext, Existing };
"#,
        vec![logical_module_with_anon_alpha_target_statements_all(
            "selected_logs",
            &[],
            r#"console.log("before selected");
STMT_LIST;
console.log("after selected");"#,
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_logs.js",
        &[
            r#"console.log("before selected")"#,
            r#"console.log("after selected")"#,
        ],
        &["skippedContext", "Existing", "STMT_LIST"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const skippedContext", "const Existing"],
        &["before selected", "after selected"],
    );
    assert_entry_output(&fixture, "before selected\nafter selected\nexisting\n");
}

#[test]
fn alpha_anonymous_statement_selector_keeps_member_properties_significant() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const selectedValue = "selected";
const existingValue = "existing";
console.log(globalThis.primaryService?.enabled ? selectedValue : "off");
console.log(globalThis.secondaryService?.enabled ? existingValue : "off");
export { selectedValue, existingValue };
"#,
        vec![
            logical_module_with_anon_alpha(
                "primary_probe",
                &[Member::new("selectedValue")],
                r#"console.log(globalThis.primaryService?.enabled ? replacementValue : "off");"#,
            ),
            logical_module_with_anon_alpha(
                "secondary_probe",
                &[Member::new("existingValue")],
                r#"console.log(globalThis.secondaryService?.enabled ? replacementValue : "off");"#,
            ),
        ],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/primary_probe.js",
        &["selectedValue", "primaryService"],
        &["secondaryService", "existingValue"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/secondary_probe.js",
        &["secondaryService", "existingValue"],
        &["primaryService", "selectedValue"],
    );
    assert_entry_output(&fixture, "off\noff\n");
}

#[test]
fn alpha_anonymous_statement_selector_supports_explicit_string_literal_wildcards() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const selectedValue = 1;
const existingValue = 2;
(function () {
  const target = globalThis;
  target.auditMarkers = target.auditMarkers || {};
  target.auditMarkers["primary-slot"] = "runtime-generated-primary";
})();
(function () {
  const target = globalThis;
  target.auditMarkers = target.auditMarkers || {};
  target.auditMarkers["secondary-slot"] = "runtime-generated-secondary";
})();
export { selectedValue, existingValue };
"#,
        vec![logical_module_with_anon_alpha_string_wildcards(
            "primary_marker",
            &[Member::new("selectedValue")],
            r#"(function () {
  const target = globalThis;
  target.auditMarkers = target.auditMarkers || {};
  target.auditMarkers["primary-slot"] = "<generated-id>";
})();"#,
            &["<generated-id>"],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/primary_marker.js",
        &["selectedValue", "primary-slot", "runtime-generated-primary"],
        &[
            "<generated-id>",
            "secondary-slot",
            "runtime-generated-secondary",
        ],
    );
    assert_entry_output(&fixture, "");
}

#[test]
fn member_source_match_variable_declarator_survives_binding_name_drift() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeBinding = { kind: "selected", enabled: true },
  siblingBinding = { kind: "other", enabled: false };
const Existing = "existing";
console.log(runtimeBinding.kind, siblingBinding.kind, Existing);
export { runtimeBinding, siblingBinding, Existing };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha(
                "selectedConfig",
                r#"const oldBinding = { kind: "selected", enabled: true };"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_config.js",
        &["const selectedConfig", r#""selected""#],
        &["siblingBinding", "const Existing"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["siblingBinding", "const Existing"],
        &["const runtimeBinding"],
    );
    assert_entry_output(&fixture, "selected other existing\n");
}

#[test]
fn source_match_timing_env_reports_member_selector_resolution() {
    let fixture = run_fixture_with_env(
        FixtureOpts::new(
            r#"function runtimeFormat(value) {
  return value.trim().toUpperCase();
}
console.log(runtimeFormat(" ok "));
export { runtimeFormat };
"#,
            vec![logical_module(
                "format",
                &[Member::source_alpha(
                    "formatValue",
                    r#"function formatValue(value) {
  return value.trim().toUpperCase();
}"#,
                )],
            )],
        ),
        &[("DUCKTAPE_SOURCE_MATCH_TIMINGS", "1")],
    );

    assert_entry_output(&fixture, "OK\n");
    for required in [
        "[debundle source_match]",
        "request=static/app::format",
        "kind=members[].selector.source_match export=`formatValue`",
        "selector_key=",
        "body_key=",
        "body_indices=[0]",
        "binding=runtimeFormat",
        "selector=function formatValue(value) { return value.trim().toUpperCase(); }",
    ] {
        assert!(
            fixture.stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{}",
            fixture.stderr,
        );
    }
}

#[test]
fn exact_native_source_match_skips_legacy_resolver_timing() {
    let fixture = run_fixture_with_env(
        FixtureOpts::new(
            r#"function runtimeFormat(value) {
  return value.trim().toUpperCase();
}
console.log(runtimeFormat(" ok "));
export { runtimeFormat };
"#,
            vec![logical_module(
                "format",
                &[Member::source_exact_target(
                    "formatValue",
                    "runtimeFormat",
                    r#"function runtimeFormat(value) {
  return value.trim().toUpperCase();
}"#,
                )],
            )],
        ),
        &[("DUCKTAPE_SOURCE_MATCH_TIMINGS", "1")],
    );

    assert_entry_output(&fixture, "OK\n");
    assert!(
        !fixture.stderr.contains("[debundle source_match]"),
        "native exact source_match should not call the legacy resolver\nstderr:\n{}",
        fixture.stderr,
    );
}

#[test]
fn source_match_timing_preview_can_be_disabled() {
    let fixture = run_fixture_with_env(
        FixtureOpts::new(
            r#"function runtimeNormalize(value) {
  return value.trim().toLowerCase();
}
console.log(runtimeNormalize(" OK "));
export { runtimeNormalize };
"#,
            vec![logical_module(
                "normalize",
                &[Member::source_alpha(
                    "normalizeValue",
                    r#"function normalizeValue(value) {
  return value.trim().toLowerCase();
}"#,
                )],
            )],
        ),
        &[
            ("DUCKTAPE_SOURCE_MATCH_TIMINGS", "1"),
            ("DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW", "0"),
        ],
    );

    assert_entry_output(&fixture, "ok\n");
    for required in [
        "[debundle source_match]",
        "request=static/app::normalize",
        "selector_key=",
        "body_key=",
        "body_indices=[0]",
        "binding=runtimeNormalize",
    ] {
        assert!(
            fixture.stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{}",
            fixture.stderr,
        );
    }
    assert!(
        !fixture.stderr.contains("function normalizeValue"),
        "timing preview should be hidden when DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW=0\nstderr:\n{}",
        fixture.stderr,
    );
}

#[test]
fn member_source_match_target_binding_uses_multideclarator_context() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeLocalPart = "primary",
  runtimeDomain = "example.test",
  runtimeAddress = `${runtimeLocalPart}@${runtimeDomain}`;
const duplicateLocalPart = "primary";
console.log(runtimeLocalPart, duplicateLocalPart, runtimeAddress);
export { runtimeLocalPart, runtimeDomain, runtimeAddress, duplicateLocalPart };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha_target(
                "selectedLocalPart",
                "localPart",
                r#"const localPart = "primary",
  domain = "example.test",
  address = `${localPart}@${domain}`;"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_config.js",
        &["const selectedLocalPart", r#""primary""#],
        &["runtimeDomain", "runtimeAddress", "duplicateLocalPart"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["runtimeDomain", "runtimeAddress", "duplicateLocalPart"],
        &["runtimeLocalPart"],
    );
    assert_entry_output(&fixture, "primary primary primary@example.test\n");
}

#[test]
fn binding_group_source_match_extracts_multiple_bindings_from_multideclarator() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let runtimeA = 100,
  runtimeB = null,
  runtimeC = `${runtimeA}:${runtimeB === null}:bar`;
const Existing = "existing";
console.log(runtimeA, runtimeB === null, runtimeC, Existing);
export { runtimeA, runtimeB, runtimeC, Existing };
"#,
        vec![logical_module_with_binding_groups(
            "selected_values",
            &[],
            &[BindingGroup::source_alpha(
                r#"let a = 100,
  b = null,
  c = `${a}:${b === null}:bar`;"#,
                &[("a", "NameA"), ("b", "NameB"), ("c", "NameC")],
            )],
        )],
    ));

    let mut exports =
        list_module_exports(&fixture.out_root, "static/app/modules/selected_values.js");
    exports.sort();
    assert_eq!(exports, vec!["NameA", "NameB", "NameC"]);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_values.js",
        &["NameA", "NameB", "NameC", "100", "bar"],
        &[
            "let runtimeA",
            "let runtimeB",
            "let runtimeC",
            "const Existing",
        ],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["Existing"],
        &["runtimeA = 100", "runtimeB = null", "runtimeC ="],
    );
    assert_entry_output(&fixture, "100 true 100:true:bar existing\n");
}

#[test]
fn member_source_match_target_binding_can_select_single_declarator_from_comma_list() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const siblingBinding = { kind: "other", enabled: false },
  runtimeBinding = { kind: "selected", enabled: true };
const Existing = "existing";
console.log(runtimeBinding.kind, siblingBinding.kind, Existing);
export { siblingBinding, runtimeBinding, Existing };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha_target(
                "selectedConfig",
                "config",
                r#"const config = { kind: "selected", enabled: true };"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_config.js",
        &["const selectedConfig", r#""selected""#],
        &["siblingBinding", "const Existing"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["siblingBinding", "const Existing"],
        &["runtimeBinding"],
    );
    assert_entry_output(&fixture, "selected other existing\n");
}

#[test]
fn member_source_match_target_binding_context_selects_single_declarator_from_comma_list() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const helperBinding = { kind: "helper" },
  runtimeBinding = { kind: "selected", enabled: true },
  trailingBinding = { kind: "trailing" };
function runtimeReader() {
  return runtimeBinding.kind;
}
console.log(runtimeReader(), helperBinding.kind, trailingBinding.kind);
export { helperBinding, runtimeBinding, trailingBinding, runtimeReader };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha_target(
                "selectedConfig",
                "config",
                r#"const config = { kind: "selected", enabled: true };
function readConfig() {
  return config.kind;
}"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_config.js",
        &["const selectedConfig", r#""selected""#],
        &["helperBinding", "trailingBinding", "runtimeReader"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["helperBinding", "trailingBinding", "runtimeReader"],
        &["runtimeBinding"],
    );
    assert_entry_output(&fixture, "selected helper trailing\n");
}

#[test]
fn binding_group_source_match_still_rejects_ambiguous_multideclarator_matches() {
    let opts = FixtureOpts::new(
        r#"let firstA = 100,
  firstB = null,
  firstC = `${firstA}:${firstB === null}:bar`;
let secondA = 100,
  secondB = null,
  secondC = `${secondA}:${secondB === null}:bar`;
export { firstA, firstB, firstC, secondA, secondB, secondC };
"#,
        vec![logical_module_with_binding_groups(
            "selected_values",
            &[],
            &[BindingGroup::source_alpha(
                r#"let a = 100,
  b = null,
  c = `${a}:${b === null}:bar`;"#,
                &[("a", "NameA"), ("b", "NameB"), ("c", "NameC")],
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_values",
            "NameA",
            "ambiguous",
            "target_binding",
            "a",
        ],
    );
}

#[test]
fn member_source_match_target_binding_uses_following_statement_context() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const traceContexts = ["load"];
const selectedRegistry = {};
for (const context of traceContexts)
  selectedRegistry[context] = () => context;
const duplicateRegistry = {};
console.log(selectedRegistry !== duplicateRegistry, Object.keys(duplicateRegistry).length);
export { traceContexts, selectedRegistry, duplicateRegistry };
"#,
        vec![logical_module(
            "selected_registry",
            &[Member::source_alpha_target(
                "traceCommandRegistry",
                "registry",
                r#"const registry = {};
for (const context of traceContexts)
  registry[context] = () => context;"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/selected_registry.js",
        &["const traceCommandRegistry = {}"],
        &["duplicateRegistry"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["duplicateRegistry"],
        &["const selectedRegistry"],
    );
    assert_entry_output(&fixture, "true 0\n");
}

#[test]
fn member_source_match_target_binding_uses_adjacent_function_consumers_as_context() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const runtimeRecordSessions = new Map([["main", "record"]]);
function closeRecordSessions() {
  return runtimeRecordSessions.clear();
}
function getRecordSession(name) {
  return runtimeRecordSessions.get(name);
}
const runtimeReplaySessions = new Map([["main", "replay"]]);
function closeReplaySessions() {
  return runtimeRecordSessions.clear();
}
function getReplaySession(name) {
  return runtimeReplaySessions.get(name);
}
console.log(getRecordSession("main"), getReplaySession("main"));
export { runtimeRecordSessions, runtimeReplaySessions };
"#,
        vec![logical_module(
            "sessions",
            &[Member::source_alpha_target(
                "recordSessions",
                "sessions",
                r#"const sessions = new Map(EXPR);
function closeSessions() {
  return sessions.clear();
}
function getSession(name) {
  return sessions.get(name);
}"#,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/sessions.js",
        &["const recordSessions = new Map", "\"record\""],
        &["runtimeReplaySessions"],
    );
    assert_entry_output(&fixture, "record replay\n");
}

#[test]
fn member_source_match_variable_declarator_still_rejects_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"const firstRuntime = { kind: "selected", enabled: true },
  secondRuntime = { kind: "selected", enabled: true };
export { firstRuntime, secondRuntime };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha(
                "selectedConfig",
                r#"const oldBinding = { kind: "selected", enabled: true };"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_config",
            "selectedConfig",
            "ambiguous",
            r#"const oldBinding = { kind: "selected", enabled: true }"#,
        ],
    );
}

#[test]
fn member_source_match_target_binding_still_rejects_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"const firstLocalPart = "primary",
  firstDomain = "example.test",
  firstAddress = `${firstLocalPart}@${firstDomain}`;
const secondLocalPart = "primary",
  secondDomain = "example.test",
  secondAddress = `${secondLocalPart}@${secondDomain}`;
export { firstLocalPart, firstDomain, firstAddress, secondLocalPart, secondDomain, secondAddress };
"#,
        vec![logical_module(
            "selected_config",
            &[Member::source_alpha_target(
                "selectedLocalPart",
                "localPart",
                r#"const localPart = "primary",
  domain = "example.test",
  address = `${localPart}@${domain}`;"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_config",
            "selectedLocalPart",
            "ambiguous",
            "target_binding",
            "localPart",
        ],
    );
}

#[test]
fn member_source_match_target_binding_context_still_rejects_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"const traceContexts = ["load"];
const firstRegistry = {};
for (const context of traceContexts)
  firstRegistry[context] = () => context;
const secondRegistry = {};
for (const context of traceContexts)
  secondRegistry[context] = () => context;
export { firstRegistry, secondRegistry };
"#,
        vec![logical_module(
            "selected_registry",
            &[Member::source_alpha_target(
                "traceCommandRegistry",
                "registry",
                r#"const registry = {};
for (const context of traceContexts)
  registry[context] = () => context;"#,
            )],
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::selected_registry",
            "traceCommandRegistry",
            "ambiguous",
            "target_binding",
            "registry",
        ],
    );
}

#[test]
fn alpha_anonymous_statement_selector_still_rejects_ambiguous_matches() {
    let opts = FixtureOpts::new(
        r#"function decorate(args, target, prop) {
  console.log(`${prop}:${target.constructor.name}:${args.length}`);
}
const FirstToken = Symbol("first");
const SecondToken = Symbol("second");
class FirstSubject {}
class SecondSubject {}
decorate([FirstToken], FirstSubject.prototype, "statusFlag");
decorate([SecondToken], SecondSubject.prototype, "statusFlag");
export { FirstSubject, SecondSubject };
"#,
        vec![logical_module_with_anon_alpha(
            "first_subject",
            &[
                Member::new("decorate"),
                Member::new("FirstToken"),
                Member::new("FirstSubject"),
            ],
            r#"applyMetadata([token], Subject.prototype, "statusFlag");"#,
        )],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "static/app::first_subject",
            "ambiguous",
            r#"applyMetadata([token], Subject.prototype, "statusFlag")"#,
        ],
    );
}
