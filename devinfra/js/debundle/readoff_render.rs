//! Read-off selector renderer (W2 of the read-off minimization redesign; see
//! `plans/readoff_minimization.md`).
//!
//! W1 built the [`ShapeIndex`] and its [`ShapeIndex::minimal_anchor_set`]
//! read-off API, which returns the minimal [`AnchorSet`] — the smallest set of
//! the target's own features whose posting-list intersection is the singleton
//! `{target}`. W1's soundness test used a stand-in renderer; this is the
//! production one.
//!
//! ## What it does
//!
//! It maps an [`AnchorSet`] to a **kept-span set** (`BTreeSet<AnchorSpan>`) over
//! the target item's AST: the byte spans of exactly the concrete tokens the
//! read-off chose to pin. That kept set is then handed to the *existing*
//! `selector_codemod` AST-prune + swc-codegen machinery (`hole_stmts` /
//! `hole_expr` / `hole_object` / `emit_selector` via the per-form `render_with`
//! closure) — there is no second serializer. The matcher proves the result
//! (gate 1), exactly as the cover path does.
//!
//! ## Skeleton / arity anchors (W1 hand-off note #2)
//!
//! When the discriminating feature is **structural** — a function/var skeleton,
//! a member count, a param arity — it pins no literal, so it contributes **no
//! kept span**. Structure is pinned by the holed scaffold itself: the function
//! `render_with` emits one `ANYTHING` param per real param, so the rendered
//! selector matches only that arity; the var scaffold pins `const`/`let`/`var`
//! and the declarator shape. So a skeleton anchor is honored by rendering the
//! scaffold with an empty (or value-only) kept set and letting the matcher pin
//! the structure. Value-bearing anchors (string literals, object keys,
//! member/method names, member-path callees) map to the spans of the tokens
//! that exhibit them.
//!
//! ## Why a span set rather than a fresh AST
//!
//! The kept-span representation is exactly what every `selector_codemod` prune
//! function already consumes (`node_retains_any`). Reusing it means the read-off
//! path and the cover path render through identical code, so they cannot diverge
//! on hole placement, codegen, or the matcher gate.

use std::collections::BTreeSet;

use selector_candidate_index::SelectorFeature;
use shape_index::{AnchorSet, ShapeFeature};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// `(lo, hi)` byte offsets of a retained concrete token. Mirrors
/// `selector_codemod::AnchorSpan` (the prune machinery's kept-span type).
pub type AnchorSpan = (u32, u32);

/// The value-bearing anchors a read-off can pin to concrete source tokens.
/// Structural anchors (top-level kind, var kind, function arity, shape
/// skeletons) carry no value and are pinned by the holed scaffold, so they are
/// not collected here.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum ValueAnchor {
    StringLiteral(String),
    NumberLiteral(String),
    BoolLiteral(bool),
    ObjectKey(String),
    ClassMember(String),
    MemberProperty(String),
    /// A member-path callee (`a.foo`); only `.`-containing labels reach here
    /// (a bare-identifier callee is alpha-wildcarded and never an anchor).
    CallCallee(String),
}

impl ValueAnchor {
    /// The value-bearing anchors of an [`AnchorSet`]. Structural / skeleton
    /// features yield nothing — the scaffold pins them.
    fn from_anchor_set(anchor_set: &AnchorSet) -> BTreeSet<Self> {
        anchor_set
            .anchors
            .iter()
            .filter_map(|scored| match &scored.feature {
                ShapeFeature::Selector(feature) => Self::from_selector_feature(feature),
                ShapeFeature::Skeleton(_) => None,
            })
            .collect()
    }

    fn from_selector_feature(feature: &SelectorFeature) -> Option<Self> {
        match feature {
            SelectorFeature::StringLiteral(value) => Some(Self::StringLiteral(value.clone())),
            SelectorFeature::NumberLiteral(value) => Some(Self::NumberLiteral(value.clone())),
            SelectorFeature::BoolLiteral(value) => Some(Self::BoolLiteral(*value)),
            SelectorFeature::ObjectKey(label) => Some(Self::ObjectKey(label.clone())),
            SelectorFeature::ClassMember(label) => Some(Self::ClassMember(label.clone())),
            SelectorFeature::MemberProperty(label) => Some(Self::MemberProperty(label.clone())),
            SelectorFeature::CallCallee(label) => Some(Self::CallCallee(label.clone())),
            SelectorFeature::TopLevelKind(_)
            | SelectorFeature::VarKind(_)
            | SelectorFeature::FunctionArity(_)
            | SelectorFeature::ImportSource(_) => None,
        }
    }

    /// Whether `lit` exhibits one of the value anchors, mapping each concrete
    /// literal kind to the feature taxonomy the read-off scored.
    fn matches_lit(anchors: &BTreeSet<ValueAnchor>, lit: &Lit) -> bool {
        match lit {
            Lit::Str(str_) => anchors.contains(&ValueAnchor::StringLiteral(
                str_.value.to_string_lossy().into(),
            )),
            Lit::Num(num) => anchors.contains(&ValueAnchor::NumberLiteral(num.value.to_string())),
            Lit::BigInt(bigint) => {
                anchors.contains(&ValueAnchor::NumberLiteral(bigint.value.to_string()))
            }
            Lit::Bool(bool_) => anchors.contains(&ValueAnchor::BoolLiteral(bool_.value)),
            Lit::Null(_) | Lit::Regex(_) | Lit::JSXText(_) => false,
        }
    }
}

/// Kept byte spans, in the target item, that exhibit the read-off's value
/// anchors. Returned to the per-form `render_with` so the prune retains exactly
/// those tokens and holes everything else.
///
/// A structural-only read-off (skeleton / arity / kind) yields an empty set:
/// the holed scaffold alone discriminates, and the matcher proves it.
pub fn kept_spans_for_anchor_set(
    item: &ModuleItem,
    anchor_set: &AnchorSet,
) -> BTreeSet<AnchorSpan> {
    let anchors = ValueAnchor::from_anchor_set(anchor_set);
    let mut collector = SpanCollector {
        anchors: &anchors,
        kept: BTreeSet::new(),
    };
    item.visit_with(&mut collector);
    collector.kept
}

/// Walks the target item collecting the byte span of every token that exhibits
/// a chosen [`ValueAnchor`], mirroring the `SelectorCandidateIndex` feature
/// taxonomy so the span-level pin matches the feature the read-off scored.
struct SpanCollector<'a> {
    anchors: &'a BTreeSet<ValueAnchor>,
    kept: BTreeSet<AnchorSpan>,
}

impl SpanCollector<'_> {
    fn keep(&mut self, span: Span) {
        self.kept.insert((span.lo.0, span.hi.0));
    }
}

impl Visit for SpanCollector<'_> {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Expr::Lit(lit) = expr {
            if ValueAnchor::matches_lit(self.anchors, lit) {
                // Pin the literal node; the prune keeps it verbatim.
                self.keep(lit.span());
            }
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Some(label) = member_callee_label(callee)
            && self.anchors.contains(&ValueAnchor::CallCallee(label))
        {
            // Pin the callee member access; the prune keeps the property name
            // and holes the receiver, yielding `ANYTHING.foo(...)`.
            self.keep(callee.span());
        }
        call.visit_children_with(self);
    }

    fn visit_member_prop(&mut self, prop: &MemberProp) {
        if let Some(label) = member_prop_label(prop)
            && self.anchors.contains(&ValueAnchor::MemberProperty(label))
        {
            self.keep(prop.span());
        }
        prop.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        for prop in &object.props {
            if let Some((label, key_span)) = object_key_label_span(prop)
                && self.anchors.contains(&ValueAnchor::ObjectKey(label))
            {
                self.keep(key_span);
            }
            prop.visit_with(self);
        }
    }

    fn visit_object_pat(&mut self, pat: &ObjectPat) {
        // A destructured property key is the same `ObjectKey` anchor as an
        // object-literal key (the stable source property name). Pin the key
        // token so the prune keeps it (and holes the rest of the pattern with
        // the object-property run hole), exactly as `visit_object_lit` does for
        // literals.
        for prop in &pat.props {
            if let Some((label, key_span)) = object_pat_key_label_span(prop)
                && self.anchors.contains(&ValueAnchor::ObjectKey(label))
            {
                self.keep(key_span);
            }
            prop.visit_with(self);
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if let Some((label, key_span)) = class_member_label_span(member)
            && self.anchors.contains(&ValueAnchor::ClassMember(label))
        {
            self.keep(key_span);
        }
        member.visit_children_with(self);
    }
}

/// `a.foo` for a member-access callee, mirroring `selector_candidate_index`'s
/// `callee_label` for the member case (a bare identifier is never an anchor).
fn member_callee_label(expr: &Expr) -> Option<String> {
    let Expr::Member(member) = expr else {
        return None;
    };
    let object = expr_label(&member.obj)?;
    let prop = member_prop_label(&member.prop)?;
    Some(format!("{object}.{prop}"))
}

fn expr_label(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Ident(ident) => Some(ident.sym.to_string()),
        Expr::Member(member) => {
            let object = expr_label(&member.obj)?;
            let prop = member_prop_label(&member.prop)?;
            Some(format!("{object}.{prop}"))
        }
        _ => None,
    }
}

fn member_prop_label(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::PrivateName(private) => Some(format!("#{}", private.name)),
        MemberProp::Computed(_) => None,
    }
}

fn object_key_label_span(prop: &PropOrSpread) -> Option<(String, Span)> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    match prop.as_ref() {
        Prop::Shorthand(ident) => Some((ident.sym.to_string(), ident.span)),
        Prop::KeyValue(kv) => prop_name_label_span(&kv.key),
        Prop::Assign(assign) => Some((assign.key.sym.to_string(), assign.key.span)),
        Prop::Getter(getter) => prop_name_label_span(&getter.key),
        Prop::Setter(setter) => prop_name_label_span(&setter.key),
        Prop::Method(method) => prop_name_label_span(&method.key),
    }
}

fn object_pat_key_label_span(prop: &ObjectPatProp) -> Option<(String, Span)> {
    match prop {
        ObjectPatProp::KeyValue(kv) => prop_name_label_span(&kv.key),
        ObjectPatProp::Assign(assign) => Some((assign.key.id.sym.to_string(), assign.key.id.span)),
        ObjectPatProp::Rest(_) => None,
    }
}

fn class_member_label_span(member: &ClassMember) -> Option<(String, Span)> {
    match member {
        ClassMember::Constructor(ctor) => prop_name_label_span(&ctor.key),
        ClassMember::Method(method) => prop_name_label_span(&method.key),
        ClassMember::PrivateMethod(method) => {
            Some((format!("#{}", method.key.name), method.key.span))
        }
        ClassMember::ClassProp(prop) => prop_name_label_span(&prop.key),
        ClassMember::PrivateProp(prop) => Some((format!("#{}", prop.key.name), prop.key.span)),
        ClassMember::AutoAccessor(_)
        | ClassMember::StaticBlock(_)
        | ClassMember::TsIndexSignature(_)
        | ClassMember::Empty(_) => None,
    }
}

fn prop_name_label_span(name: &PropName) -> Option<(String, Span)> {
    match name {
        PropName::Ident(ident) => Some((ident.sym.to_string(), ident.span)),
        PropName::Str(str_) => Some((str_.value.to_string_lossy().to_string(), str_.span)),
        PropName::Num(num) => Some((num.value.to_string(), num.span)),
        PropName::BigInt(bigint) => Some((bigint.value.to_string(), bigint.span)),
        PropName::Computed(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use shape_index::ShapeIndex;

    fn parse(source: &str) -> Module {
        js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<test>", source).unwrap())
    }

    /// The substring of `source` covered by a kept span, for asserting which
    /// token a kept anchor pins.
    fn span_text<'a>(source: &'a str, span: &AnchorSpan) -> &'a str {
        // swc byte positions are 1-based (BytePos 0 is the dummy sentinel).
        &source[(span.0 - 1) as usize..(span.1 - 1) as usize]
    }

    #[test]
    fn number_literal_anchor_maps_to_the_token_span() {
        // The numeric argument is item 0's only discriminator; the read-off pins
        // it and the kept span covers exactly that number literal.
        let source = r#"const a = make(call(), 123);
const b = make(call(), 456);"#;
        let module = parse(source);
        let index = ShapeIndex::new(&module);
        let anchor_set = index.minimal_anchor_set(0).unwrap();
        let kept = kept_spans_for_anchor_set(&module.body[0], &anchor_set);
        assert!(
            kept.iter().any(|s| span_text(source, s) == "123"),
            "expected a kept span covering `123`, got {:?}",
            kept.iter()
                .map(|s| span_text(source, s))
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn value_anchor_maps_to_the_token_span() {
        // Item 0's unique string literal is the read-off anchor; the kept span
        // must cover exactly that literal so the prune pins it.
        let source = r#"const a = make("widget-token");
const b = make("other-token");"#;
        let module = parse(source);
        let index = ShapeIndex::new(&module);
        let anchor_set = index.minimal_anchor_set(0).unwrap();
        let kept = kept_spans_for_anchor_set(&module.body[0], &anchor_set);
        let pinned: Vec<&str> = kept.iter().map(|s| span_text(source, s)).collect();
        assert_eq!(pinned, vec![r#""widget-token""#]);
    }

    #[test]
    fn member_property_anchor_pins_the_property_name() {
        // `.now` is unique to item 0 among the chunk, so the read-off pins the
        // member property; the kept span covers the `now` accessor token.
        let source = r#"const a = read(Date.now());
const b = read(other());"#;
        let module = parse(source);
        let index = ShapeIndex::new(&module);
        let anchor_set = index.minimal_anchor_set(0).unwrap();
        let kept = kept_spans_for_anchor_set(&module.body[0], &anchor_set);
        assert!(
            kept.iter().any(|s| span_text(source, s) == "now"),
            "expected a kept span covering `now`, got {:?}",
            kept.iter()
                .map(|s| span_text(source, s))
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn structural_only_read_off_keeps_no_span() {
        // The lone function is discriminated by its declaration kind / arity
        // alone (a structural read-off), so no concrete token is pinned: the
        // holed scaffold carries the whole pin.
        let source = r#"function only(x) { return x; }
const v = 1;"#;
        let module = parse(source);
        let index = ShapeIndex::new(&module);
        let anchor_set = index.minimal_anchor_set(0).unwrap();
        // The structural feature carries no value, so it maps to no kept span.
        let kept = kept_spans_for_anchor_set(&module.body[0], &anchor_set);
        assert!(
            kept.is_empty(),
            "structural read-off must pin no concrete token; got {:?}",
            kept.iter()
                .map(|s| span_text(source, s))
                .collect::<Vec<_>>()
        );
    }
}
