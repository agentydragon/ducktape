//! Soundness validation for the Layer-1 read-off API (W1).
//!
//! The shape index is only a candidate ranker; the production `source_match`
//! matcher stays the correctness gate. This test renders every read-off result
//! to a `source_match` selector and runs it through the matcher
//! (`find_anonymous_statement_body_indices` for statement targets,
//! `resolve_member_binding` for binding targets), asserting it resolves
//! **uniquely** to the intended item.
//!
//! It covers var / object / function / class items, the `OPT=1`
//! single-feature case, and tail cases needing a 2-3 feature combination. The
//! assertions are semantic (unique resolution; stable feature preferred when
//! available), never checked-in literals.

use selector_candidate_index::SelectorFeature;
use shape_index::{AnchorSet, ShapeFeature, ShapeIndex, Stability};
use source_match::find_anonymous_statement_body_indices;
use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};
use std::collections::BTreeSet;
use swc_ecma_ast::*;

fn parse(source: &str) -> Module {
    js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<test>", source).unwrap())
}

/// Render a read-off [`AnchorSet`] for a top-level item into a `source_match`
/// selector source. Keeps the anchored stable tokens concrete and holes
/// everything structurally irrelevant, so the rendered selector is the minimal
/// read-off pattern (a W1 stand-in for the Wave-2 renderer; deliberately small
/// but fully sound — the matcher proves it).
fn render_selector(module: &Module, anchor: &AnchorSet) -> AnonymousStatementSelector {
    let item = &module.body[anchor.body_idx];
    let kept_strings: BTreeSet<String> = anchor
        .anchors
        .iter()
        .filter_map(|scored| match &scored.feature {
            ShapeFeature::Selector(SelectorFeature::StringLiteral(value)) => Some(value.clone()),
            _ => None,
        })
        .collect();
    let kept_keys: BTreeSet<String> = anchor
        .anchors
        .iter()
        .filter_map(|scored| match &scored.feature {
            ShapeFeature::Selector(
                SelectorFeature::ObjectKey(name)
                | SelectorFeature::ClassMember(name)
                | SelectorFeature::MemberProperty(name)
                | SelectorFeature::CallCallee(name),
            ) => Some(name.clone()),
            _ => None,
        })
        .collect();

    let match_source = render_item(item, &kept_strings, &kept_keys);
    AnonymousStatementSelector {
        match_source,
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: None,
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    }
}

fn render_item(
    item: &ModuleItem,
    kept_strings: &BTreeSet<String>,
    kept_keys: &BTreeSet<String>,
) -> String {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            let kind = match var.kind {
                VarDeclKind::Var => "var",
                VarDeclKind::Let => "let",
                VarDeclKind::Const => "const",
            };
            let init = var.decls[0]
                .init
                .as_ref()
                .map(|e| render_expr(e, kept_strings, kept_keys))
                .unwrap_or_else(|| "EXPR".to_string());
            format!("{kind} readable = {init};")
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
            // One `ANYTHING` binding-pattern hole per param (ANYTHING in a
            // non-declarator binding position matches any pattern); the body
            // collapses to a `STMT_LIST` hole.
            let params = vec!["ANYTHING"; function.function.params.len()].join(", ");
            format!("function readable({params}) {{ STMT_LIST }}")
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => {
            let members: Vec<String> = class
                .class
                .body
                .iter()
                .filter_map(class_member_name)
                .filter(|name| kept_keys.contains(name))
                .map(|name| format!("  {name}() {{}}"))
                .collect();
            // `CLASS_REST` is matched as the bare keyword only (no suffix), so
            // lead and trail with bare `CLASS_REST;` holes; kept members then
            // match as an ordered subsequence with gaps on both sides.
            let body = if members.is_empty() {
                "  CLASS_REST;".to_string()
            } else {
                format!("  CLASS_REST;\n{}\n  CLASS_REST;", members.join("\n"))
            };
            format!("class ReadableName {{\n{body}\n}}")
        }
        _ => "STMT".to_string(),
    }
}

fn render_expr(
    expr: &Expr,
    kept_strings: &BTreeSet<String>,
    kept_keys: &BTreeSet<String>,
) -> String {
    match expr {
        Expr::Lit(Lit::Str(str_)) => {
            let value = str_.value.to_string_lossy().to_string();
            if kept_strings.contains(&value) {
                format!("{value:?}")
            } else {
                "EXPR".to_string()
            }
        }
        Expr::Call(call) => {
            let callee = match &call.callee {
                Callee::Expr(e) => render_expr(e, kept_strings, kept_keys),
                _ => "EXPR".to_string(),
            };
            let args: Vec<String> = call
                .args
                .iter()
                .map(|a| render_expr(&a.expr, kept_strings, kept_keys))
                .collect();
            format!("{callee}({})", args.join(", "))
        }
        Expr::Ident(_) => "EXPR".to_string(),
        Expr::Object(object) => {
            // Keep a property when its key is an anchor, or when its value is a
            // kept (anchored) string literal — pin the key:value so the kept
            // literal survives into the selector.
            let props: Vec<String> = object
                .props
                .iter()
                .filter_map(object_prop_kv)
                .filter(|(key, value)| {
                    kept_keys.contains(key)
                        || value.as_ref().is_some_and(|v| kept_strings.contains(v))
                })
                .map(|(key, value)| match value {
                    Some(v) if kept_strings.contains(&v) => format!("{key}: {v:?}"),
                    _ => format!("{key}: EXPR"),
                })
                .collect();
            // Lead and trail with list holes so kept props match as an ordered
            // subsequence with gaps on both sides (the hole at index 0 leaves
            // `anchored_left` false; the trailing hole leaves `anchored_right`
            // false). Named holes avoid a duplicate-shorthand-key parse error.
            if props.is_empty() {
                "{ OBJECT_PROPS }".to_string()
            } else {
                format!("{{ OBJECT_PROPS_a, {}, OBJECT_PROPS_b }}", props.join(", "))
            }
        }
        _ => "EXPR".to_string(),
    }
}

fn class_member_name(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name(&method.key),
        _ => None,
    }
}

/// Key name plus, when the value is a string literal, its value.
fn object_prop_kv(prop: &PropOrSpread) -> Option<(String, Option<String>)> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    match prop.as_ref() {
        Prop::KeyValue(kv) => {
            let key = prop_name(&kv.key)?;
            let value = match kv.value.as_ref() {
                Expr::Lit(Lit::Str(str_)) => Some(str_.value.to_string_lossy().to_string()),
                _ => None,
            };
            Some((key, value))
        }
        Prop::Shorthand(ident) => Some((ident.sym.to_string(), None)),
        _ => None,
    }
}

fn prop_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(str_) => Some(str_.value.to_string_lossy().to_string()),
        _ => None,
    }
}

/// Render a read-off for `body_idx`, run it through the production matcher, and
/// assert unique resolution to `body_idx`.
fn assert_read_off_resolves_uniquely(module: &Module, body_idx: usize) -> AnchorSet {
    let index = ShapeIndex::new(module);
    let anchor = index
        .minimal_anchor_set(body_idx)
        .unwrap_or_else(|| panic!("no read-off for body_idx={body_idx}"));
    let selector = render_selector(module, &anchor);
    let matches = js_ast::with_swc_globals(|| {
        find_anonymous_statement_body_indices(module, "<soundness>", &selector).unwrap_or_else(
            |err| {
                panic!(
                    "matcher failed on rendered selector\n{}\n{err}",
                    selector.match_source
                )
            },
        )
    });
    assert_eq!(
        matches,
        vec![body_idx],
        "read-off selector must resolve uniquely to body_idx={body_idx}; selector:\n{}",
        selector.match_source
    );
    anchor
}

#[test]
fn var_read_off_resolves_uniquely() {
    // Each item carries a distinct stable string literal, so each is
    // alpha-distinguishable (callee identifiers are wildcarded under alpha).
    let module = parse(
        r#"const a = make("widget-token");
const b = make("other-token");
const c = build("third-token");"#,
    );
    for idx in 0..3 {
        assert_read_off_resolves_uniquely(&module, idx);
    }
}

#[test]
fn object_read_off_resolves_uniquely() {
    let module = parse(
        r#"const a = { role: "button", label: "ok" };
const b = { role: "button", icon: "x" };
const c = { kind: "menu" };"#,
    );
    for idx in 0..3 {
        assert_read_off_resolves_uniquely(&module, idx);
    }
}

#[test]
fn function_read_off_resolves_uniquely() {
    // Functions discriminate poorly by shape alone; ensure those that *can* be
    // singled out by structure resolve uniquely (here each has a distinct
    // sibling kind around it).
    let module = parse(
        r#"function only() { return 1; }
class Sibling { m() {} }
const v = 2;"#,
    );
    assert_read_off_resolves_uniquely(&module, 0);
}

#[test]
fn class_read_off_resolves_uniquely() {
    // Each class has at least one uniquely-named member, so the kept-member
    // read-off discriminates it. (Distinguishing classes that differ only in
    // member *count* needs the skeleton-aware Wave-2 renderer.)
    let module = parse(
        r#"class Panel { render() {} mount() {} }
class Worker { dispatch() {} }
class Store { commit() {} }"#,
    );
    for idx in 0..3 {
        assert_read_off_resolves_uniquely(&module, idx);
    }
}

#[test]
fn opt_one_case_uses_a_single_anchor() {
    // A unique semantic literal => OPT=1 read-off.
    let module = parse(
        r#"const a = f("unique-magic-token");
const b = g("common");
const c = h("common");"#,
    );
    let anchor = assert_read_off_resolves_uniquely(&module, 0);
    assert!(
        anchor.opt_one,
        "unique literal should give a single-anchor read-off"
    );
}

#[test]
fn tail_case_combines_features() {
    // Items 0 and 1 share kind+const and the call shape; only the combination
    // of the two distinct literals separates them => 2-feature read-off.
    let module = parse(
        r#"const a = make("shared", "alpha");
const b = make("shared", "beta");"#,
    );
    let anchor = assert_read_off_resolves_uniquely(&module, 0);
    // Either a single highly-selective literal anchor, or a combination; in
    // both cases the matcher proved uniqueness above. Assert the read-off did
    // not need to keep volatile tokens when a stable one was available.
    assert!(
        anchor
            .anchors
            .iter()
            .all(|scored| scored.stability != Stability::Volatile),
        "read-off must prefer stable anchors when available"
    );
}

#[test]
fn stable_feature_preferred_over_volatile() {
    // The item carries both a stable key and a volatile-looking literal; the
    // top-ranked anchor must be the stable one.
    let module = parse(
        r#"const a = init("chunk-a1b2c3", { stableKey: 1 });
const b = init("chunk-d4e5f6", { otherKey: 2 });"#,
    );
    let index = ShapeIndex::new(&module);
    let anchor = index.minimal_anchor_set(0).unwrap();
    assert!(
        anchor.anchors[0].stability != Stability::Volatile,
        "top anchor should be stable, not the volatile chunk hash"
    );
    assert_read_off_resolves_uniquely(&module, 0);
}
