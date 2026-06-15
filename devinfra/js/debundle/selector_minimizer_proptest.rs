//! Property: every selector the minimizer synthesizes, re-matched against the
//! original chunk, resolves uniquely to the intended target binding (gate 1).
//!
//! Each case generates a small chunk of same-arity function declarations whose
//! bodies are built from a spec that maps onto the AST shapes the retention
//! renderer branches on — numeric/string literals, method calls with multiple
//! arguments, and object-literal arguments. The spec is rendered to JS and
//! parsed so the minimizer sees real source spans (it reads original text via
//! `source_for_span`), exactly as in production. We synthesize a minimized
//! selector for one target, then *independently* re-run the production matcher
//! on the produced `match_source`: the synthesizer proves uniqueness
//! internally, and this verifies that guarantee end-to-end across the input
//! space the golden fixtures only sample by hand. Bounded for CI; override with
//! `bbr test //devinfra/js/debundle:selector_codemod_test
//! --test_env=PROPTEST_CASES=2000`.

use std::collections::BTreeSet;

use proptest::prelude::*;
use proptest::sample::Index;

use super::{
    ChunkSelectorIndex, NameBindingMember, matched_body_indices,
    synthesize_simplest_selector_for_group,
};

const METHODS: [&str; 3] = ["foo", "bar", "baz"];
const STRINGS: [&str; 3] = ["alpha", "beta", "gamma"];
const KEYS: [&str; 3] = ["kind", "mode", "size"];

#[derive(Debug, Clone)]
enum ArgSpec {
    Num(u8),
    Str(u8),
    Obj { key_id: u8, value: u8 },
}

#[derive(Debug, Clone)]
struct CallSpec {
    recv_a: bool,
    method_id: u8,
    args: Vec<ArgSpec>,
}

#[derive(Debug, Clone)]
enum StmtSpec {
    ConstNum { value: u8 },
    ConstStr { value_id: u8 },
    ConstCall(CallSpec),
    CallStmt(CallSpec),
    Return,
}

fn arg_strategy() -> impl Strategy<Value = ArgSpec> {
    prop_oneof![
        (0u8..4).prop_map(ArgSpec::Num),
        (0u8..3).prop_map(ArgSpec::Str),
        (0u8..3, 0u8..4).prop_map(|(key_id, value)| ArgSpec::Obj { key_id, value }),
    ]
}

fn call_strategy() -> impl Strategy<Value = CallSpec> {
    (
        any::<bool>(),
        0u8..3,
        prop::collection::vec(arg_strategy(), 0..3),
    )
        .prop_map(|(recv_a, method_id, args)| CallSpec {
            recv_a,
            method_id,
            args,
        })
}

fn stmt_strategy() -> impl Strategy<Value = StmtSpec> {
    prop_oneof![
        (0u8..4).prop_map(|value| StmtSpec::ConstNum { value }),
        (0u8..3).prop_map(|value_id| StmtSpec::ConstStr { value_id }),
        call_strategy().prop_map(StmtSpec::ConstCall),
        call_strategy().prop_map(StmtSpec::CallStmt),
        Just(StmtSpec::Return),
    ]
}

fn render_arg(arg: &ArgSpec) -> String {
    match arg {
        ArgSpec::Num(value) => value.to_string(),
        ArgSpec::Str(value_id) => format!("\"{}\"", STRINGS[*value_id as usize]),
        ArgSpec::Obj { key_id, value } => format!("{{ {}: {value} }}", KEYS[*key_id as usize]),
    }
}

fn render_call(call: &CallSpec) -> String {
    let recv = if call.recv_a { "a" } else { "b" };
    let args = call
        .args
        .iter()
        .map(render_arg)
        .collect::<Vec<_>>()
        .join(", ");
    format!("{recv}.{}({args})", METHODS[call.method_id as usize])
}

fn render_stmt(spec: &StmtSpec, slot: usize) -> String {
    match spec {
        StmtSpec::ConstNum { value } => format!("const v{slot} = {value};"),
        StmtSpec::ConstStr { value_id } => {
            format!("const v{slot} = mk(\"{}\");", STRINGS[*value_id as usize])
        }
        StmtSpec::ConstCall(call) => format!("const v{slot} = {};", render_call(call)),
        StmtSpec::CallStmt(call) => format!("{};", render_call(call)),
        StmtSpec::Return => "return a;".to_string(),
    }
}

fn render_chunk(items: &[Vec<StmtSpec>]) -> String {
    items
        .iter()
        .enumerate()
        .map(|(idx, stmts)| {
            let body = stmts
                .iter()
                .enumerate()
                .map(|(slot, stmt)| format!("  {}", render_stmt(stmt, slot)))
                .collect::<Vec<_>>()
                .join("\n");
            format!("function f{idx}(a, b) {{\n{body}\n}}")
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 96, ..ProptestConfig::default() })]

    #[test]
    fn minimized_selector_uniquely_matches_target(
        items in prop::collection::vec(prop::collection::vec(stmt_strategy(), 1..4), 2..5),
        target in any::<Index>(),
    ) {
        let source = render_chunk(&items);
        let runtime = format!("f{}", target.index(items.len()));
        let export = "TargetBinding";

        let outcome: Result<(), TestCaseError> = js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module_consuming("<proptest>", source.clone())
                .map_err(|err| TestCaseError::fail(format!("parse failed: {err}\n{source}")))?;
            let index = ChunkSelectorIndex::new(parsed);

            let decl_idx = index
                .binding_to_decl
                .get(&runtime)
                .and_then(|decls| decls.first())
                .copied()
                .expect("generated chunk declares the target function");
            let expected_body_idx = index.decls[decl_idx].body_idx;

            let members = [NameBindingMember {
                member_index: 0,
                export_name: export.to_string(),
                binding_name: runtime.clone(),
                comment: None,
            }];

            // An ambiguous chunk (two alpha-identical declarations) cannot be
            // pinned even by the exact selector; the synthesizer errors and
            // there is nothing to assert.
            let Ok(group) = synthesize_simplest_selector_for_group(&index, decl_idx, &members, true)
            else {
                return Ok(());
            };

            let matched = matched_body_indices(&index, export, &group.match_source).map_err(|err| {
                TestCaseError::fail(format!(
                    "synthesized selector failed to re-match: {err}\nselector:\n{}\nchunk:\n{source}",
                    group.match_source
                ))
            })?;
            prop_assert_eq!(
                &matched,
                &BTreeSet::from([expected_body_idx]),
                "selector did not uniquely resolve the target\nselector:\n{}\nchunk:\n{}",
                group.match_source,
                source
            );
            Ok(())
        });
        outcome?;
    }
}
