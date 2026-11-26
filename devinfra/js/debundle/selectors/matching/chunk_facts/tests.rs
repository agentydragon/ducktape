use super::*;
use std::collections::{BTreeSet, HashSet};

fn extract(src: &str) -> Result<ChunkFacts, Unsupported> {
    js_ast::with_swc_globals(|| {
        extract_facts_items(&js_ast::parse_js_module_ast("<test>", src).unwrap().body)
    })
}

fn assert_kinds(facts: &ChunkFacts, expected: impl IntoIterator<Item = NodeKind>) {
    let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
    for expected in expected {
        assert!(
            kinds.contains(&expected),
            "kind {expected:?} present: {kinds:?}"
        );
    }
}

#[test]
fn extracts_member_call_with_string_arg_faithfully() {
    let facts = extract("const x = foo.bar(\"hello\");").expect("covered shape extracts");

    let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
    assert!(idents.contains("x"), "declarator name present: {idents:?}");
    assert!(idents.contains("foo"), "member object present: {idents:?}");

    let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
    assert_eq!(props, vec!["bar"], "member property name");
    let strings: Vec<&str> = facts.str_lit.iter().map(|(_, s)| s.as_str()).collect();
    assert_eq!(strings, vec!["hello"], "string-literal argument value");

    assert_kinds(
        &facts,
        [
            NodeKind::VarDecl,
            NodeKind::VarDeclarator,
            NodeKind::BindingIdent,
            NodeKind::Call,
            NodeKind::Member,
            NodeKind::PropName,
            NodeKind::Ident,
            NodeKind::StrLit,
        ],
    );

    // Every child edge references a node that exists (the projection is a tree).
    let nodes: BTreeSet<NodeId> = facts.node_kind.iter().map(|(id, _)| *id).collect();
    for (parent, _, child) in &facts.child {
        assert!(
            nodes.contains(parent) && nodes.contains(child),
            "child edge ({parent},{child}) references an unknown node",
        );
    }

    // One top-level statement, source_unit ordinal 0.
    assert_eq!(facts.top_level.len(), 1);
    assert_eq!(facts.top_level[0].1, 0);
}

#[test]
fn extracts_class_with_method_returning_literal() {
    // Identity = a getName() returning the class's own readable name, plus an
    // `extends` edge.
    let facts =
        extract("class ReadableName extends Base { getName() { return \"ReadableName\"; } }")
            .expect("covered shape extracts");

    let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
    assert!(idents.contains("ReadableName"), "class name: {idents:?}");
    assert!(idents.contains("Base"), "super class: {idents:?}");
    let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
    assert_eq!(props, vec!["getName"], "method name");
    let strings: Vec<&str> = facts.str_lit.iter().map(|(_, s)| s.as_str()).collect();
    assert_eq!(strings, vec!["ReadableName"], "returned literal");
    assert_eq!(facts.super_class.len(), 1, "one extends edge");

    assert_kinds(
        &facts,
        [
            NodeKind::ClassDecl,
            NodeKind::Class,
            NodeKind::Method,
            NodeKind::PropName,
            NodeKind::Function,
            NodeKind::Block,
            NodeKind::Return,
            NodeKind::StrLit,
        ],
    );
}

#[test]
fn extracts_common_expression_variants() {
    // (b + c) ? new D(e) : [f] — binary, conditional, new, array.
    let facts = extract("const a = b + c ? new D(e) : [f];").expect("covered shape extracts");

    assert_kinds(
        &facts,
        [
            NodeKind::Cond,
            NodeKind::Bin,
            NodeKind::New,
            NodeKind::Array,
        ],
    );
    // The `const` declaration keyword is recorded as an operator-class label
    // (so a `let` selector cannot match a `const`), then the binary `+`.
    let operators: Vec<&str> = facts.operator.iter().map(|(_, s)| s.as_str()).collect();
    assert_eq!(
        operators,
        vec!["const", "+"],
        "decl keyword then binary operator"
    );
    let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
    for name in ["a", "b", "c", "D", "e", "f"] {
        assert!(idents.contains(name), "ident {name} present: {idents:?}");
    }
}

#[test]
fn extracts_each_modeled_construct_with_its_node_kinds() {
    use NodeKind::*;
    let cases: [(&str, &[NodeKind]); 10] = [
        (
            "function f(x) { return g(x); }",
            &[FnDecl, Function, Block, Return, Call, Ident, BindingIdent],
        ),
        (
            "const a = { handler: function (x) { return x; }, ...rest };",
            &[Object, KeyValue, FnExpr, Function, Spread],
        ),
        ("a.b = c;", &[ExprStmt, Assign, Member]),
        (
            "if (a) { b(); } else throw c;",
            &[If, Block, ExprStmt, Call, Throw],
        ),
        (
            "class C { x = 1; constructor(a) { this.a = a; } }",
            &[ClassDecl, ClassProp, Constructor, This, Assign],
        ),
        (
            "for (const { a, b } of items) { log(`x${a}`); }",
            &[ForOf, ObjectPat, PatAssign, Tpl, TplQuasi],
        ),
        (
            "class C extends B { async m() { return await super.n()?.p; } }",
            &[SuperProp, Await, OptChain],
        ),
        (
            "import { a, b } from \"m\"; export default function () {}",
            &[Import, ImportSpecifier, ExportDefaultDecl, Function],
        ),
        ("const a = import.meta;", &[MetaPropImportMeta]),
        (
            "function f() { debugger; }\nconst a = tag`x${y}z`;",
            &[Debugger, TaggedTpl, Tpl, TplQuasi],
        ),
    ];
    for (src, kinds) in cases {
        let facts = extract(src).unwrap_or_else(|error| panic!("{src:?}: {error:?}"));
        assert_kinds(&facts, kinds.iter().copied());
    }
}

#[test]
fn records_function_names_assignment_operators_and_import_sources() {
    let facts = extract("function f() {}\na.b = c;\nimport \"m\";").expect("extracts");
    assert!(facts.ident_name.iter().any(|(_, s)| s == "f"));
    assert!(facts.operator.iter().any(|(_, s)| s == "="));
    assert!(facts.str_lit.iter().any(|(_, s)| s == "m"));
}

#[test]
fn extracts_private_class_members_and_access() {
    let facts =
        extract("class C { #x = 1; #m() { return this.#x; } get(other) { return other.#x; } }")
            .expect("private members extract");
    assert_kinds(
        &facts,
        [NodeKind::ClassDecl, NodeKind::ClassProp, NodeKind::Method],
    );
    // The declaring `#x` key, the `this.#x` use, and the `other.#x` use
    // are all recorded under the same `#`-prefixed prop_name label.
    let x_count = facts
        .prop_name
        .iter()
        .filter(|(_, name)| name == "#x")
        .count();
    assert_eq!(x_count, 3, "prop_name entries: {:?}", facts.prop_name);
    assert!(
        facts.prop_name.iter().any(|(_, name)| name == "#m"),
        "prop_name entries: {:?}",
        facts.prop_name
    );
}

#[test]
fn extracts_using_declarations() {
    let facts =
        extract("{ using a = open(); await using b = openAsync(); }").expect("using extracts");
    let operators: HashSet<&str> = facts.operator.iter().map(|(_, op)| op.as_str()).collect();
    assert!(operators.contains("using"), "operators: {operators:?}");
    assert!(
        operators.contains("await using"),
        "operators: {operators:?}"
    );
}

#[test]
fn coverage_report_tallies_per_statement() {
    // One extractable statement; one blocked by an unmodeled class member.
    // `PrivateMethod`/`PrivateProp` (`#x`) are modeled now (see
    // `class_member`/`private_name_key`) — `TsIndexSignature` is the
    // still-unmodeled member kind used here as the canary. One
    // statement's gap does not abort the tally of the other.
    let report = js_ast::with_swc_globals(|| {
        coverage_report(
            &js_ast::parse_js_module_ast(
                "<test>",
                "const a = \"s\";\nclass C { [k: string]: number; }\n",
            )
            .unwrap(),
        )
    });
    assert_eq!(report.total, 2);
    assert_eq!(report.covered, 1);
    assert_eq!(report.unsupported.get("class_member"), Some(&1));
}

#[test]
fn unmodeled_construct_errors_loudly_not_silently() {
    // A TS index signature class member stays unmodeled (TS-only syntax,
    // not something emitted JS ever needs). Fail-closed means a hard
    // error here, never a silently-incomplete fact set that would let a
    // query under-constrain.
    let error = extract("class C { [k: string]: number; }").unwrap_err();
    assert_eq!(error.context, "class_member");
}

fn relational_facts(src: &str, needs: RelationalFactNeeds) -> RelationalFacts {
    js_ast::with_swc_globals(|| {
        extract_relational_facts(&js_ast::parse_js_module_ast("<test>", src).unwrap(), needs)
    })
}

fn member_reads(src: &str) -> Vec<(SourceUnitOrdinal, MemberReadFact)> {
    relational_facts(
        src,
        RelationalFactNeeds {
            member_reads: true,
            ..Default::default()
        },
    )
    .member_reads
}

#[test]
fn member_reads_by_ordinal_records_object_and_member() {
    // A helper reading `ctx.uniqueId` (object is a bare ident), and a second
    // statement reading `foo().label` (object is a call — no object ident).
    let reads =
        member_reads("function helper(ctx) { return ctx.uniqueId; }\nconst v = foo().label;\n");
    assert_eq!(
        reads,
        vec![
            (
                SourceUnitOrdinal(0),
                MemberReadFact {
                    object: Some("ctx".to_string()),
                    member: "uniqueId".to_string(),
                },
            ),
            (
                SourceUnitOrdinal(1),
                MemberReadFact {
                    object: None,
                    member: "label".to_string(),
                },
            ),
        ],
    );
}

#[test]
fn member_reads_by_ordinal_skips_computed_access() {
    // `obj[expr]` is computed — no static property name — so it contributes no
    // member-read row, while the sibling `obj.kept` does.
    let reads = member_reads("function f(obj, k) { return obj[k] + obj.kept; }\n");
    assert_eq!(
        reads,
        vec![(
            SourceUnitOrdinal(0),
            MemberReadFact {
                object: Some("obj".to_string()),
                member: "kept".to_string(),
            },
        )],
    );
}

fn call_args(src: &str) -> Vec<CallArgumentFact> {
    relational_facts(
        src,
        RelationalFactNeeds {
            call_arguments: true,
            ..Default::default()
        },
    )
    .call_arguments
}

#[test]
fn call_argument_uses_records_object_none_for_deep_callee() {
    // A deeper callee chain `a.b.register(X)` has no bare-ident object, so the
    // object is `None` but the callee member and argument are still recorded.
    let facts = call_args("a.b.register(X);\n");
    assert_eq!(
        facts,
        vec![CallArgumentFact {
            argument: "X".to_string(),
            callee_member: "register".to_string(),
            callee_object: None,
            arg_index: 0,
        }],
    );
}

#[test]
fn call_argument_uses_skips_non_member_callee_and_non_ident_args() {
    // A bare callee (`register(X)`) has no callee member name to pin by, and a
    // non-identifier argument (`new Y()`, an object literal) names no static
    // target binding — both contribute nothing. Only the member-callee call
    // with a bare-ident argument yields a row.
    let facts =
        call_args("register(X);\nr.register(new Y());\nr.register({ a: 1 });\nr.register(Kept);\n");
    assert_eq!(
        facts,
        vec![CallArgumentFact {
            argument: "Kept".to_string(),
            callee_member: "register".to_string(),
            callee_object: Some("r".to_string()),
            arg_index: 0,
        }],
    );
}

#[test]
fn call_argument_uses_tolerates_unmodeled_statement() {
    // The unsupported index signature discards an earlier registration
    // in the same statement. The next statement still contributes its call.
    let facts = call_args(
        "function f() { r.register(Dropped); class C { [k: string]: number; } }\n\
         r.register(Kept);",
    );
    assert_eq!(
        facts,
        vec![CallArgumentFact {
            argument: "Kept".to_string(),
            callee_member: "register".to_string(),
            callee_object: Some("r".to_string()),
            arg_index: 0,
        }],
    );
}

fn decorate_calls(src: &str) -> Vec<DecorateCallFact> {
    relational_facts(
        src,
        RelationalFactNeeds {
            decorate_calls: true,
            ..Default::default()
        },
    )
    .decorate_calls
}

#[test]
fn decorate_call_uses_records_property_decorator_without_flags() {
    // The 3-argument property form `H([d], C.prototype, "m")` (no flags) is
    // still a decorate application: the member literal is the last argument.
    let facts = decorate_calls("H([Mobx.observable], C.prototype, \"animation\");\n");
    assert_eq!(
        facts,
        vec![DecorateCallFact {
            callee: "H".to_string(),
            class_anchor: "C".to_string(),
            member: Some("animation".to_string()),
        }],
    );
}

#[test]
fn decorate_call_uses_records_class_decorator_shape() {
    // The class-decorator form `H([d], C)` carries no member literal.
    let facts = decorate_calls("H([ClassDecorator], C);\n");
    assert_eq!(
        facts,
        vec![DecorateCallFact {
            callee: "H".to_string(),
            class_anchor: "C".to_string(),
            member: None,
        }],
    );
}

#[test]
fn decorate_call_uses_requires_array_first_argument() {
    // Without the decorator-array first argument, a 2/3-argument call is an
    // arbitrary call, not a decorate application — no row. The structural array
    // guard is what keeps `passed_to_call`-shaped registrations
    // (`h(x, C.prototype, "m")`) from being misread as decorate applications.
    let facts = decorate_calls("h(x, C.prototype, \"m\");\nObject.assign(C, D);\n");
    assert!(facts.is_empty());
}

#[test]
fn decorate_call_uses_requires_prototype_member_or_bare_class() {
    // A 2nd argument that is a member other than `.prototype`, or a computed
    // access, is not the decorate class anchor — fail-closed, no row.
    let facts = decorate_calls(
        "H([d], C.notPrototype, \"m\");\nH([d], C[k], \"m\");\nH([d], obj.deep.prototype, \"m\");\n",
    );
    assert!(facts.is_empty());
}

#[test]
fn decorate_call_uses_skips_non_string_member_and_overlong_calls() {
    // A non-string 3rd argument (the member slot) is not the decorate shape,
    // and a 5th-or-later positional argument means it is some other call.
    let facts =
        decorate_calls("H([d], C.prototype, notAString);\nH([d], C.prototype, \"m\", 2, extra);\n");
    assert!(facts.is_empty());
}

#[test]
fn decorate_call_uses_tolerates_unmodeled_statement() {
    let facts = decorate_calls(
        "function f() { H([d], C.prototype, \"dropped\", 2); class C { [k: string]: number; } }\n\
         H([d], C.prototype, \"kept\", 2);",
    );
    assert_eq!(
        facts,
        vec![DecorateCallFact {
            callee: "H".to_string(),
            class_anchor: "C".to_string(),
            member: Some("kept".to_string()),
        }],
    );
}

fn intrinsic_aliases(src: &str) -> Vec<IntrinsicAliasFact> {
    relational_facts(
        src,
        RelationalFactNeeds {
            intrinsic_aliases: true,
            ..Default::default()
        },
    )
    .intrinsic_aliases
}

#[test]
fn intrinsic_alias_uses_records_object_intrinsic_aliases() {
    // The esbuild decorate-trio companions: `var X = Object.defineProperty` /
    // `var X = Object.getOwnPropertyDescriptor`. Each row's target is the alias
    // binding; the property is the re-minify-invariant intrinsic method name.
    // The structural shape — not the declaration keyword — is the invariant, so
    // the input mixes `var`, `let` and `const`.
    let facts = intrinsic_aliases(
        "var p = Object.defineProperty;\n\
         let g = Object.getOwnPropertyDescriptor;\n\
         const d = Object.defineProperty;\n",
    );
    assert_eq!(
        facts,
        vec![
            IntrinsicAliasFact {
                binding: "p".to_string(),
                property: "defineProperty".to_string(),
            },
            IntrinsicAliasFact {
                binding: "g".to_string(),
                property: "getOwnPropertyDescriptor".to_string(),
            },
            IntrinsicAliasFact {
                binding: "d".to_string(),
                property: "defineProperty".to_string(),
            },
        ],
    );
}

#[test]
fn intrinsic_alias_uses_keeps_aliases_from_decorator_helper_declarator_run() {
    // Real bundles commonly emit the two Object intrinsic aliases and the
    // decorator helper in one multi-declarator `var` statement. The helper body
    // is irrelevant to alias extraction; unsupported helper internals must not
    // make us drop the sibling alias declarators.
    let facts = intrinsic_aliases(
        r#"var define = Object.defineProperty,
  descriptor = Object.getOwnPropertyDescriptor,
  decorate = (decorators, target, key, kind) => {
for (var desc = descriptor(target, key), index = decorators.length - 1, fn; index >= 0; index--)
  (fn = decorators[index]) && (desc = fn(target, key, desc) || desc);
return (desc && define(target, key, desc), desc);
  };"#,
    );
    assert_eq!(
        facts,
        vec![
            IntrinsicAliasFact {
                binding: "define".to_string(),
                property: "defineProperty".to_string(),
            },
            IntrinsicAliasFact {
                binding: "descriptor".to_string(),
                property: "getOwnPropertyDescriptor".to_string(),
            },
        ],
    );
}

#[test]
fn intrinsic_alias_uses_requires_object_base() {
    // An alias off some other identifier (a local, or another global) is not an
    // `Object` intrinsic alias — fail-closed, no row.
    let facts =
        intrinsic_aliases("var a = Reflect.defineProperty;\nvar b = notObject.defineProperty;\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_requires_non_computed_property() {
    // A computed access `Object[k]` carries no static property name — no row.
    let facts = intrinsic_aliases("var p = Object[\"defineProperty\"];\nvar q = Object[k];\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_fails_closed_on_shadowed_object_declaration() {
    // A chunk-top-level `var Object = ...` shadows the global intrinsic, so the
    // alias might read off the local `Object` — the whole chunk yields no rows.
    let facts = intrinsic_aliases("var Object = {};\nvar p = Object.defineProperty;\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_fails_closed_on_class_named_object() {
    let facts = intrinsic_aliases("class Object {}\nvar p = Object.defineProperty;\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_fails_closed_on_imported_object() {
    // An imported `Object` is a module-local binding, not the global intrinsic.
    let facts =
        intrinsic_aliases("import { Object } from \"./shim\";\nvar p = Object.defineProperty;\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_fails_closed_on_reassigned_object() {
    // A bare `Object = ...` reassignment defeats the intrinsic-identity guard.
    let facts = intrinsic_aliases("Object = shim;\nvar p = Object.defineProperty;\n");
    assert!(facts.is_empty());
}

#[test]
fn intrinsic_alias_uses_tolerates_unmodeled_statement() {
    let facts = intrinsic_aliases(
        "function f() { var dropped = Object.create; class C { [k: string]: number; } }\n\
         var p = Object.defineProperty;",
    );
    assert_eq!(
        facts,
        vec![IntrinsicAliasFact {
            binding: "p".to_string(),
            property: "defineProperty".to_string(),
        }],
    );
}
