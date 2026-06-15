//! Property: every selector the minimizer synthesizes, re-matched against the
//! original chunk, resolves uniquely to the intended target binding(s) (gate 1).
//!
//! Cases build chunks of uniquely-named top-level declarations as real
//! `swc_ecma_ast` nodes — covering all the declaration shapes the retention
//! renderer branches on:
//!
//! - same-arity function declarations (exercises `minimize_function_selector`);
//! - single `const` declarations with call / object / literal initializers
//!   (exercises `minimize_var_selector`);
//! - class declarations whose methods contain calls / literals (exercises
//!   `minimize_class_selector`);
//! - multi-declarator `const`s, where 2+ declarators in one statement are
//!   selected as a binding group (exercises `minimize_var_group_selector` via
//!   `synthesize_simplest_selector_for_group` with multiple `NameBindingMember`s).
//!
//! The chunk is serialized with swc's own codegen and fed into the minimizer —
//! which parses it exactly as in production, so spans are real. The synthesized
//! `match_source` is then independently re-run through the production matcher:
//! the synthesizer proves uniqueness internally, and this verifies that
//! guarantee end-to-end across the input space the golden fixtures only sample
//! by hand. Every generated binding is uniquely named so the emitted chunk
//! always re-parses. Bounded for CI (~96 cases); override with `bbr test
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

fn binding_ident_pat(name: &str) -> Pat {
    Pat::Ident(BindingIdent {
        id: ident(name),
        type_ann: None,
    })
}

fn declarator(name: &str, init: Expr) -> VarDeclarator {
    VarDeclarator {
        span: DUMMY_SP,
        name: binding_ident_pat(name),
        init: Some(Box::new(init)),
        definite: false,
    }
}

fn const_var(decls: Vec<VarDeclarator>) -> Decl {
    Decl::Var(Box::new(VarDecl {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        kind: VarDeclKind::Const,
        declare: false,
        decls,
    }))
}

/// A single anonymous `const v = <init>` statement used inside function and
/// method bodies. Names are reassigned to be slot-unique by
/// `assign_unique_const_names` before the body is emitted.
fn const_decl(init: Expr) -> Stmt {
    Stmt::Decl(const_var(vec![declarator("v", init)]))
}

fn function_node(body: Vec<Stmt>) -> Function {
    let mut body = body;
    assign_unique_const_names(&mut body);
    Function {
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
    }
}

fn function_decl(name: &str, body: Vec<Stmt>) -> ModuleItem {
    ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
        ident: ident(name),
        declare: false,
        function: Box::new(function_node(body)),
    })))
}

fn class_method(name: &str, body: Vec<Stmt>) -> ClassMember {
    ClassMember::Method(ClassMethod {
        span: DUMMY_SP,
        key: PropName::Ident(IdentName::new(name.into(), DUMMY_SP)),
        function: Box::new(function_node(body)),
        kind: MethodKind::Method,
        is_static: false,
        accessibility: None,
        is_abstract: false,
        is_optional: false,
        is_override: false,
    })
}

fn class_decl(name: &str, methods: Vec<(usize, Vec<Stmt>)>) -> ModuleItem {
    let body = methods
        .into_iter()
        .map(|(method, stmts)| class_method(METHODS[method], stmts))
        .collect();
    ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
        ident: ident(name),
        declare: false,
        class: Box::new(Class {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            decorators: vec![],
            body,
            super_class: None,
            is_abstract: false,
            type_params: None,
            super_type_params: None,
            implements: vec![],
        }),
    })))
}

fn param(name: &str) -> Param {
    Param {
        span: DUMMY_SP,
        decorators: vec![],
        pat: binding_ident_pat(name),
    }
}

/// Give every generated `const` inside a body a slot-unique name so the emitted
/// body never redeclares a `const` (which would fail to re-parse).
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

/// Initializer for a top-level single/grouped `const` binding: a call, an
/// object literal, or a bare literal — the shapes `minimize_var_selector` and
/// `minimize_var_group_selector` peel anchors out of.
fn var_init_strategy() -> impl Strategy<Value = Expr> {
    prop_oneof![
        (0u8..8).prop_map(num),
        (0usize..STRINGS.len()).prop_map(|idx| string(STRINGS[idx])),
        (
            0usize..METHODS.len(),
            prop::collection::vec(arg_strategy(), 0..3)
        )
            .prop_map(|(method, args)| call(ident_expr(METHODS[method]), args)),
        prop::collection::vec((0usize..KEYS.len(), arg_strategy()), 1..3).prop_map(|entries| {
            object(
                entries
                    .into_iter()
                    .enumerate()
                    // Dedup keys within one object: re-parsing tolerates dupes,
                    // but distinct keys keep the generated shapes meaningful.
                    .map(|(slot, (_key, value))| (KEYS[slot % KEYS.len()], value))
                    .collect(),
            )
        }),
    ]
}

/// One generated top-level declaration plus the runtime binding names it
/// introduces. The test picks a target binding (or, for `Group`, a subset of
/// 2+ bindings) from these.
#[derive(Debug, Clone)]
enum GenItem {
    /// Single-binding declaration (function / single `const` / class).
    Single { item: ModuleItem, binding: String },
    /// Multi-declarator `const` with 2+ declarators, all eligible as a group.
    Group {
        item: ModuleItem,
        bindings: Vec<String>,
    },
}

/// Strategy for a single-binding top-level item; `name` is the unique binding
/// the declaration introduces.
fn single_item_strategy(name: String) -> impl Strategy<Value = GenItem> {
    let func = prop::collection::vec(stmt_strategy(), 1..4).prop_map({
        let name = name.clone();
        move |body| GenItem::Single {
            item: function_decl(&name, body),
            binding: name.clone(),
        }
    });
    let var = var_init_strategy().prop_map({
        let name = name.clone();
        move |init| GenItem::Single {
            item: ModuleItem::Stmt(Stmt::Decl(const_var(vec![declarator(&name, init)]))),
            binding: name.clone(),
        }
    });
    let class = prop::collection::vec(
        (
            0usize..METHODS.len(),
            prop::collection::vec(stmt_strategy(), 1..3),
        ),
        1..3,
    )
    .prop_map({
        let name = name.clone();
        move |methods| {
            // Dedup method names within one class so the emitted class re-parses.
            let methods = methods
                .into_iter()
                .enumerate()
                .map(|(slot, (_method, body))| (slot % METHODS.len(), body))
                .collect();
            GenItem::Single {
                item: class_decl(&name, methods),
                binding: name.clone(),
            }
        }
    });
    prop_oneof![func, var, class]
}

/// Strategy for a multi-declarator `const` group item; `names` are the unique
/// bindings (2+) the single statement introduces.
fn group_item_strategy(names: Vec<String>) -> impl Strategy<Value = GenItem> {
    prop::collection::vec(var_init_strategy(), names.len()).prop_map(move |inits| {
        let decls = names
            .iter()
            .zip(inits)
            .map(|(name, init)| declarator(name, init))
            .collect();
        GenItem::Group {
            item: ModuleItem::Stmt(Stmt::Decl(const_var(decls))),
            bindings: names.clone(),
        }
    })
}

/// One top-level item with a globally-unique binding name prefix `n{slot}`.
fn item_strategy(slot: usize) -> impl Strategy<Value = GenItem> {
    let single = single_item_strategy(format!("n{slot}b0"));
    // Group declarations always introduce 2-3 declarators so the group path is
    // exercised with multiple `NameBindingMember`s.
    let group = (2usize..4).prop_flat_map(move |count| {
        group_item_strategy((0..count).map(|i| format!("n{slot}b{i}")).collect())
    });
    prop_oneof![3 => single, 1 => group]
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 96, ..ProptestConfig::default() })]

    #[test]
    fn minimized_selector_uniquely_matches_target(
        items in (2usize..6).prop_flat_map(|count| {
            (0..count).map(item_strategy).collect::<Vec<_>>()
        }),
        target in any::<Index>(),
        group_drop in any::<Index>(),
    ) {
        let target_idx = target.index(items.len());
        let target_item = &items[target_idx];

        // The bindings that this case will assert resolve uniquely: a single
        // binding for `Single` items, or a 2+-binding subset for `Group` items.
        let target_bindings: Vec<String> = match target_item {
            GenItem::Single { binding, .. } => vec![binding.clone()],
            GenItem::Group { bindings, .. } => {
                // Keep at least 2 declarators in the group (drop at most one) so
                // `synthesize_simplest_selector_for_group` runs with multiple
                // `NameBindingMember`s on the multi-declarator path.
                if bindings.len() > 2 {
                    let drop = group_drop.index(bindings.len());
                    bindings
                        .iter()
                        .enumerate()
                        .filter(|(idx, _)| *idx != drop)
                        .map(|(_, name)| name.clone())
                        .collect()
                } else {
                    bindings.clone()
                }
            }
        };

        let module = Module {
            span: DUMMY_SP,
            body: items.iter().map(|item| match item {
                GenItem::Single { item, .. } | GenItem::Group { item, .. } => item.clone(),
            }).collect(),
            shebang: None,
        };
        let source = emit_module(&module);

        let outcome: Result<(), TestCaseError> = js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module_consuming("<proptest>", source.clone())
                .map_err(|err| TestCaseError::fail(format!("parse failed: {err}\n{source}")))?;
            let index = ChunkSelectorIndex::new(parsed);

            let decl_idx = index
                .binding_to_decl
                .get(&target_bindings[0])
                .and_then(|decls| decls.first())
                .copied()
                .expect("generated chunk declares the target binding");
            let expected_body_idx = index.decls[decl_idx].body_idx;

            // Distinct synthetic export names per group member, in binding order.
            let members: Vec<NameBindingMember> = target_bindings
                .iter()
                .enumerate()
                .map(|(member_index, binding)| NameBindingMember {
                    member_index,
                    export_name: format!("Export{member_index}"),
                    binding_name: binding.clone(),
                    comment: None,
                })
                .collect();

            // An ambiguous chunk (two alpha-identical declarations) cannot be
            // pinned even by the exact selector; the synthesizer errors and
            // there is nothing to assert.
            let Ok(group) =
                synthesize_simplest_selector_for_group(&index, decl_idx, &members, true)
            else {
                return Ok(());
            };

            if members.len() == 1 {
                let export = &members[0].export_name;
                let matched = matched_body_indices(&index, export, &group.match_source)
                    .map_err(|err| {
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
            } else {
                // Each member of the group must, on its own, re-match uniquely
                // to the shared declaration. The matcher resolves one member at
                // a time (target_binding = that export), so verifying every
                // member proves the whole group pins the right declaration.
                for member in &members {
                    let matched =
                        matched_body_indices(&index, &member.export_name, &group.match_source)
                            .map_err(|err| {
                                TestCaseError::fail(format!(
                                    "group selector failed to re-match member `{}`: {err}\nselector:\n{}\nchunk:\n{source}",
                                    member.export_name, group.match_source
                                ))
                            })?;
                    prop_assert_eq!(
                        &matched,
                        &BTreeSet::from([expected_body_idx]),
                        "group selector did not uniquely resolve member `{}`\nselector:\n{}\nchunk:\n{}",
                        member.export_name,
                        group.match_source,
                        source
                    );
                }
            }
            Ok(())
        });
        outcome?;
    }
}
