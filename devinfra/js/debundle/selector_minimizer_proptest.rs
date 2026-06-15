//! Property: every selector the minimizer synthesizes, re-matched against the
//! original chunk, resolves uniquely to the intended target binding (gate 1).
//!
//! Cases build chunks of same-arity function declarations as real `swc_ecma_ast`
//! nodes (the call / literal / object shapes the retention renderer branches on),
//! serialize them with swc's own codegen, and feed the resulting source into the
//! minimizer — which parses it exactly as in production, so spans are real. The
//! synthesized `match_source` is then independently re-run through the production
//! matcher: the synthesizer proves uniqueness internally, and this verifies that
//! guarantee end-to-end across the input space the golden fixtures only sample by
//! hand. Bounded for CI; override with `bbr test
//! //devinfra/js/debundle:selector_codemod_test --test_env=PROPTEST_CASES=2000`.

use std::collections::BTreeSet;

use proptest::prelude::*;
use proptest::sample::Index;
use swc_common::sync::Lrc;
use swc_common::{DUMMY_SP, SourceMap, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};

use super::{
    ChunkSelectorIndex, NameBindingMember, matched_body_indices,
    synthesize_simplest_selector_for_group,
};

const METHODS: [&str; 3] = ["foo", "bar", "baz"];
const STRINGS: [&str; 3] = ["alpha", "beta", "gamma"];
const KEYS: [&str; 3] = ["kind", "mode", "size"];

fn ident(name: &str) -> Ident {
    Ident::new_no_ctxt(name.into(), DUMMY_SP)
}

fn ident_expr(name: &str) -> Expr {
    Expr::Ident(ident(name))
}

fn num(value: u8) -> Expr {
    Expr::Lit(Lit::Num(Number {
        span: DUMMY_SP,
        value: f64::from(value),
        raw: None,
    }))
}

fn string(value: &str) -> Expr {
    Expr::Lit(Lit::Str(Str {
        span: DUMMY_SP,
        value: value.into(),
        raw: None,
    }))
}

fn member(obj: Expr, prop: &str) -> Expr {
    Expr::Member(MemberExpr {
        span: DUMMY_SP,
        obj: Box::new(obj),
        prop: MemberProp::Ident(IdentName::new(prop.into(), DUMMY_SP)),
    })
}

fn call(callee: Expr, args: Vec<Expr>) -> Expr {
    Expr::Call(CallExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Callee::Expr(Box::new(callee)),
        args: args
            .into_iter()
            .map(|expr| ExprOrSpread {
                spread: None,
                expr: Box::new(expr),
            })
            .collect(),
        type_args: None,
    })
}

fn object(entries: Vec<(&str, Expr)>) -> Expr {
    Expr::Object(ObjectLit {
        span: DUMMY_SP,
        props: entries
            .into_iter()
            .map(|(key, value)| {
                PropOrSpread::Prop(Box::new(Prop::KeyValue(KeyValueProp {
                    key: PropName::Ident(IdentName::new(key.into(), DUMMY_SP)),
                    value: Box::new(value),
                })))
            })
            .collect(),
    })
}

fn const_decl(init: Expr) -> Stmt {
    Stmt::Decl(Decl::Var(Box::new(VarDecl {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        kind: VarDeclKind::Const,
        declare: false,
        decls: vec![VarDeclarator {
            span: DUMMY_SP,
            name: Pat::Ident(BindingIdent {
                id: ident("v"),
                type_ann: None,
            }),
            init: Some(Box::new(init)),
            definite: false,
        }],
    })))
}

fn function_decl(name: &str, mut body: Vec<Stmt>) -> ModuleItem {
    assign_unique_const_names(&mut body);
    ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
        ident: ident(name),
        declare: false,
        function: Box::new(Function {
            params: vec![param("a"), param("b")],
            decorators: vec![],
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            body: Some(BlockStmt {
                span: DUMMY_SP,
                ctxt: SyntaxContext::empty(),
                stmts: body,
            }),
            is_generator: false,
            is_async: false,
            type_params: None,
            return_type: None,
        }),
    })))
}

fn param(name: &str) -> Param {
    Param {
        span: DUMMY_SP,
        decorators: vec![],
        pat: Pat::Ident(BindingIdent {
            id: ident(name),
            type_ann: None,
        }),
    }
}

/// Give every generated `const` a slot-unique name so the emitted body never
/// redeclares a `const` (which would fail to re-parse).
fn assign_unique_const_names(stmts: &mut [Stmt]) {
    let mut slot = 0;
    for stmt in stmts {
        if let Stmt::Decl(Decl::Var(var)) = stmt {
            for declarator in &mut var.decls {
                if let Pat::Ident(binding) = &mut declarator.name {
                    binding.id = ident(&format!("v{slot}"));
                    slot += 1;
                }
            }
        }
    }
}

fn emit_module(module: &Module) -> String {
    let cm: Lrc<SourceMap> = Lrc::default();
    let mut buf = Vec::new();
    {
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm: cm.clone(),
            comments: None,
            wr: JsWriter::new(cm, "\n", &mut buf, None),
        };
        emitter.emit_module(module).expect("emit generated module");
    }
    String::from_utf8(buf).expect("emitted module is utf-8")
}

fn arg_strategy() -> impl Strategy<Value = Expr> {
    prop_oneof![
        (0u8..4).prop_map(num),
        (0usize..STRINGS.len()).prop_map(|idx| string(STRINGS[idx])),
        (0usize..KEYS.len(), 0u8..4).prop_map(|(key, value)| object(vec![(KEYS[key], num(value))])),
    ]
}

fn call_strategy() -> impl Strategy<Value = Expr> {
    (
        any::<bool>(),
        0usize..METHODS.len(),
        prop::collection::vec(arg_strategy(), 0..3),
    )
        .prop_map(|(recv_a, method, args)| {
            call(
                member(ident_expr(if recv_a { "a" } else { "b" }), METHODS[method]),
                args,
            )
        })
}

fn stmt_strategy() -> impl Strategy<Value = Stmt> {
    prop_oneof![
        (0u8..4).prop_map(|value| const_decl(num(value))),
        (0usize..STRINGS.len())
            .prop_map(|idx| const_decl(call(ident_expr("mk"), vec![string(STRINGS[idx])]))),
        call_strategy().prop_map(|expr| Stmt::Expr(ExprStmt {
            span: DUMMY_SP,
            expr: Box::new(expr),
        })),
        call_strategy().prop_map(const_decl),
        Just(Stmt::Return(ReturnStmt {
            span: DUMMY_SP,
            arg: Some(Box::new(ident_expr("a"))),
        })),
    ]
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 96, ..ProptestConfig::default() })]

    #[test]
    fn minimized_selector_uniquely_matches_target(
        bodies in prop::collection::vec(prop::collection::vec(stmt_strategy(), 1..4), 2..5),
        target in any::<Index>(),
    ) {
        let target_idx = target.index(bodies.len());
        let module = Module {
            span: DUMMY_SP,
            body: bodies
                .into_iter()
                .enumerate()
                .map(|(idx, body)| function_decl(&format!("f{idx}"), body))
                .collect(),
            shebang: None,
        };
        let source = emit_module(&module);
        let runtime = format!("f{target_idx}");
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
