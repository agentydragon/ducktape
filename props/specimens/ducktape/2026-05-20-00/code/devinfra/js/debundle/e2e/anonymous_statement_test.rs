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
// That blocks 4015 of 4106 named horizon bindings in Tana from
// peeling: their only peel proposal is a closure where the
// companions are anonymous statements (decorator applications on
// the class prototype, runtime init calls, bundle preludes). See
// `peelability_test::singleton_blocked_only_by_side_effect_order_to_anonymous_owner_should_be_peelable`
// pin; this test pins the materialization-side fix.
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
// by **AST shape**, not line/column — the Tana dump is prettified
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
            // upstream Tana refactor renamed or removed the leading
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

// Pin source-order interleaving of anonymous-statement members
// and named members within the same logical module.
//
// Within a module's body, statements emit in their original
// chunk source order (Invariant #2 in DESIGN.md). Anonymous
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
// Tana's actual companions are not single-line `console.log`
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
        // Source mirrors the Tana Sentry-prelude shape (lines
        // 1-17 in `static/index-DI2GynTv.js`): a `!`-prefixed
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
