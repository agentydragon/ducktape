use std::collections::{BTreeMap, BTreeSet};

use super::*;
use crate::*;
use swc_common::{FileName, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

fn parse(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("test.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Es(Default::default()),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer).parse_module().unwrap()
}

// --- Redundant `purity: pure` hint detection ---------------------------

/// Run `analyze_chunk` with the given hints applied, then
/// return the list of `(binding_name, reason)` pairs the analyzer
/// reports as redundant. Used by the test cases below to pin the
/// "the analyzer would have figured this out on its own" verdict
/// without depending on the wrapping warning-print format.
fn redundant_hints(src: &str, hints: &[&str]) -> Vec<(String, RedundantPurityReason)> {
    let module = parse(src);
    let declared_pure: BTreeSet<String> = hints.iter().map(|s| (*s).to_string()).collect();
    analyze_redundant_hints(&module, &declared_pure)
}

/// Wrapper that constructs a `ChunkFactAnalysis` and returns its
/// `redundant_purity_hints` list as `(name, reason)` pairs.
fn analyze_redundant_hints(
    module: &Module,
    declared_pure: &BTreeSet<String>,
) -> Vec<(String, RedundantPurityReason)> {
    analyze_chunk(
        module,
        &AnalysisHints::from_declared_pure(declared_pure),
        None,
        |_| None,
    )
    .redundant_purity_hints
    .into_iter()
    .map(|h| (h.binding_name, h.reason))
    .collect()
}

#[test]
fn redundant_hint_on_pure_function_is_reported() {
    // `f` is a chunk-local arrow whose body classifies pure on
    // its own (returns a primitive literal). The hint is a no-op
    // — the analyzer reaches the same verdict without it.
    let got = redundant_hints("const f = (x) => x + 1;", &["f"]);
    assert_eq!(
        got,
        vec![("f".to_string(), RedundantPurityReason::InferredPureFunction)]
    );
}

#[test]
fn load_bearing_hint_on_impure_function_is_not_reported() {
    // `f` body writes to `globalThis`, which the analyzer flags
    // as impure regardless of any hint. The hint here is a real
    // author-trust assertion ("trust me, despite this body, the
    // callsite is safe") and must NOT be reported as redundant.
    let got = redundant_hints(
        "function f() { globalThis.touched = true; return 1; }",
        &["f"],
    );
    assert!(
        got.is_empty(),
        "load-bearing hint on truly-impure body must not be flagged redundant; got {got:?}",
    );
}

#[test]
fn redundant_hint_on_plain_data_binding_is_reported() {
    // `purity: pure` callsite hints are intended for callable
    // bindings; when the hint sits on a chunk-local `const`
    // plain-object literal it's a no-op (the binding admits as
    // `PlainData`, member reads classify pure on their own, and
    // the binding isn't called). Report it so the spec author
    // can drop the misplaced hint.
    let got = redundant_hints(r#"const TA = { FOO: "bar" };"#, &["TA"]);
    assert_eq!(
        got,
        vec![(
            "TA".to_string(),
            RedundantPurityReason::InferredPlainDataBinding
        )]
    );
}

#[test]
fn hint_on_unknown_binding_is_not_reported() {
    // Hint name doesn't bind anywhere in the chunk (typo,
    // import-only, vendor binding). The analyzer can't infer
    // anything about it, so reporting it as "redundant" would
    // be wrong — the hint is in effect for the bound name from
    // wherever it actually comes from, and silently flagging
    // it would lead the author to delete a load-bearing hint.
    let got = redundant_hints("const X = 1;", &["unknownName"]);
    assert!(
        got.is_empty(),
        "hint on a name not bound at chunk top must not be flagged; got {got:?}",
    );
}

#[test]
fn hint_chain_keeps_only_genuinely_redundant_entries() {
    // `a` calls `b`; `b`'s body is itself pure (returns
    // `x + 1`). Per-hint independent removal:
    // - Drop `a`'s hint → analyzer probes `a` with declared_pure
    //   = {b}. Inside `a`'s body, `b(x)` is hint-pure → `a`'s
    //   body classifies pure → `a` reported redundant.
    // - Drop `b`'s hint → analyzer probes `b` with declared_pure
    //   = {a}. `b`'s body `x + 1` is pure on its own (no calls
    //   at all) → `b` reported redundant.
    // Both hints are redundant.
    let src = r#"
    const b = (x) => x + 1;
    const a = (x) => b(x);
"#;
    let got = redundant_hints(src, &["a", "b"]);
    let names: BTreeSet<String> = got.iter().map(|(n, _)| n.clone()).collect();
    assert_eq!(
        names,
        BTreeSet::from(["a".to_string(), "b".to_string()]),
        "both hints in the pure chain should be flagged redundant; got {got:?}",
    );
}

#[test]
fn hint_load_bearing_on_impure_body_reports_only_transitively_redundant_hints() {
    // `a` calls `b`; `b`'s body writes globalThis → genuinely
    // impure. With BOTH hints in place, removing only `a`'s
    // hint leaves `b`'s hint shielding the `b()` call inside
    // `a`'s body — so `a`'s body still classifies pure
    // transitively. The analyzer correctly reports `a`'s hint
    // as redundant **given the current hint set**.
    //
    // `b`'s hint is genuinely load-bearing: removing it lets
    // `b`'s globalThis write classify the body as impure, and
    // `b` is no longer reported as pure-by-inference. Not
    // flagged as redundant.
    //
    // This is the right verdict: "given the current spec, this
    // specific hint can come out". If the author drops both
    // hints in sequence following two consecutive `/followups`
    // runs, the second run will surface `a` as no-longer-pure
    // and the author can re-add the hint or rework the body.
    let src = r#"
    function b() { globalThis.touched = true; return 1; }
    function a() { return b(); }
"#;
    let got = redundant_hints(src, &["a", "b"]);
    assert_eq!(
        got,
        vec![("a".to_string(), RedundantPurityReason::InferredPureFunction)],
        "with b's hint in place, a's hint is redundant; b's hint stays load-bearing",
    );
}

#[test]
fn no_hints_produces_no_warnings() {
    // Sanity: when no hints are declared, the warning list is
    // empty even for chunks with plenty of pure chunk-local
    // functions. The analyzer doesn't invent warnings about
    // "you could have added a hint here" — it only reports
    // existing hints that are redundant.
    let got = redundant_hints("const f = (x) => x + 1;", &[]);
    assert!(
        got.is_empty(),
        "empty declared_pure must produce no warnings; got {got:?}"
    );
}

#[test]
fn redundant_hint_on_let_with_whole_object_replacement_is_reported() {
    // The gaffer env_config shape: `let X = {…}` with whole-
    // object replacement writes; `purity: pure` on the
    // accessor function `getX` is redundant once X admits as
    // PlainData (Part 2). This is the test that pins the
    // motivating downstream removal — if it fails after some
    // future refactor, the gaffer hint-removal regresses.
    let src = r#"
    let envConfig = { REACT_APP_ENV: "production" };
    const applyOverrides = (n) => { envConfig = { ...envConfig, ...n }; };
    const getEnv = (n) => envConfig[n];
"#;
    let got = redundant_hints(src, &["getEnv"]);
    assert_eq!(
        got,
        vec![(
            "getEnv".to_string(),
            RedundantPurityReason::InferredPureFunction
        )]
    );
}

// --- Redundant `pure_members` hint detection ---------------------------

fn pure_member_hints(
    entries: &[(&str, &[&str])],
) -> Vec<(String, String, RedundantPureMemberReason)> {
    let declared_pure_members: BTreeMap<String, BTreeSet<String>> = entries
        .iter()
        .map(|(binding, props)| {
            (
                (*binding).to_string(),
                props.iter().map(|s| (*s).to_string()).collect(),
            )
        })
        .collect();
    // Minimal chunk — the redundant-pure_members check doesn't
    // consult the chunk body (the redundancy criterion is a
    // pure function of the hint set + `PURE_STATIC_CALLS`), so
    // any well-formed module works.
    let module = parse("const _ = 1;");
    let hints = AnalysisHints {
        declared_pure_members,
        ..AnalysisHints::default()
    };
    analyze_chunk(&module, &hints, None, |_| None)
        .redundant_pure_member_hints
        .into_iter()
        .map(|h| (h.binding_name, h.property, h.reason))
        .collect()
}

#[test]
fn redundant_pure_member_on_whitelisted_static_call_is_reported() {
    // `pure_members: [isArray]` on a binding named `Array` is a
    // no-op — `Array.isArray(...)` is already in
    // `PURE_STATIC_CALLS`. Spec author should drop the entry.
    let got = pure_member_hints(&[("Array", &["isArray"])]);
    assert_eq!(
        got,
        vec![(
            "Array".to_string(),
            "isArray".to_string(),
            RedundantPureMemberReason::WhitelistedStaticCall,
        )]
    );
}

#[test]
fn load_bearing_pure_member_on_user_binding_is_not_reported() {
    // `pure_members: [forwardRef]` on a user-named binding `b`
    // is the load-bearing case — without it, `b.forwardRef(...)`
    // stays Unknown (no path in the whitelist reaches a user
    // binding). MUST NOT be flagged.
    let got = pure_member_hints(&[("b", &["forwardRef"])]);
    assert!(
        got.is_empty(),
        "load-bearing pure_members entry on user binding must not be flagged; got {got:?}",
    );
}

#[test]
fn pure_member_on_object_freeze_is_not_reported() {
    // `Object.freeze(...)` IS in
    // `PURE_OBJECT_CALLS_ON_PLAIN_DATA` but NOT in
    // `PURE_STATIC_CALLS` — the auto-pure path requires the
    // plain-data arg shape, which the redundant check doesn't
    // verify per-callsite. MUST NOT be flagged: the spec hint
    // covers arbitrary arg shapes and is genuinely load-bearing
    // for non-literal callsites.
    let got = pure_member_hints(&[("Object", &["freeze", "entries"])]);
    assert!(
        got.is_empty(),
        "pure_members entries shadowed by argument-gated rules must not be flagged; got {got:?}",
    );
}

#[test]
fn pure_member_on_whitelist_receiver_unknown_prop_is_not_reported() {
    // `pure_members: [unknownProp]` on `Array` — `Array.unknownProp`
    // is not in `PURE_STATIC_CALLS`, so the hint is load-bearing
    // (covers a call the analyzer would otherwise classify
    // Unknown). MUST NOT be flagged.
    let got = pure_member_hints(&[("Array", &["unknownProp"])]);
    assert!(
        got.is_empty(),
        "pure_members on a non-whitelisted prop must not be flagged; got {got:?}",
    );
}
