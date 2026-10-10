//! Read-off selector renderer.
//!
//! The [`ShapeIndex`] and its [`ShapeIndex::minimal_anchor_set`] read-off API
//! return the minimal [`AnchorSet`] — the smallest set of the target's own
//! features whose posting-list intersection is the singleton `{target}`. This
//! module renders that anchor set into the production `source_match` language.
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

use std::collections::{BTreeMap, BTreeSet};

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

    /// The value anchor exhibited by a literal, if its kind is indexed.
    fn from_lit(lit: &Lit) -> Option<Self> {
        match lit {
            Lit::Str(str_) => Some(Self::StringLiteral(str_.value.to_string_lossy().into())),
            Lit::Num(num) => Some(Self::NumberLiteral(num.value.to_string())),
            Lit::BigInt(bigint) => Some(Self::NumberLiteral(bigint.value.to_string())),
            Lit::Bool(bool_) => Some(Self::BoolLiteral(bool_.value)),
            Lit::Null(_) | Lit::Regex(_) | Lit::JSXText(_) => None,
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
        anchors: AnchorLookup::Single(&anchors),
        kept: vec![BTreeSet::new()],
    };
    item.visit_with(&mut collector);
    collector.kept.pop().expect("one anchor set has one result")
}

/// Collect spans for several anchor sets in one AST walk. Group read-off asks
/// for each feature separately; walking a large declaration once per feature
/// makes its ranking cost quadratic in the declaration's size.
pub fn kept_spans_for_anchor_sets(
    item: &ModuleItem,
    anchor_sets: &[AnchorSet],
) -> Vec<BTreeSet<AnchorSpan>> {
    let mut anchors: BTreeMap<ValueAnchor, Vec<usize>> = BTreeMap::new();
    for (index, anchor_set) in anchor_sets.iter().enumerate() {
        for anchor in ValueAnchor::from_anchor_set(anchor_set) {
            anchors.entry(anchor).or_default().push(index);
        }
    }
    let mut collector = SpanCollector {
        anchors: AnchorLookup::Batch(&anchors),
        kept: vec![BTreeSet::new(); anchor_sets.len()],
    };
    item.visit_with(&mut collector);
    collector.kept
}

/// Literal forms not yet represented in `SelectorFeature`. Tuple read-off tries
/// these after ranked features instead of losing coverage when its own slots
/// cannot resolve independently. Every resulting selector still needs proof.
pub fn unindexed_literal_spans(item: &ModuleItem) -> BTreeSet<AnchorSpan> {
    #[derive(Default)]
    struct UnindexedLiterals(BTreeSet<AnchorSpan>);

    impl Visit for UnindexedLiterals {
        fn visit_expr(&mut self, expr: &Expr) {
            if matches!(
                expr,
                Expr::Lit(Lit::Null(_) | Lit::Regex(_) | Lit::JSXText(_)) | Expr::Tpl(_)
            ) {
                let span = expr.span();
                self.0.insert((span.lo.0, span.hi.0));
            } else {
                expr.visit_children_with(self);
            }
        }
    }

    let mut collector = UnindexedLiterals::default();
    item.visit_with(&mut collector);
    collector.0
}

/// Walks the target item collecting the byte span of every token that exhibits
/// a chosen [`ValueAnchor`], mirroring the `SelectorFeature` taxonomy so the
/// span-level pin matches the feature the read-off scored.
enum AnchorLookup<'a> {
    Single(&'a BTreeSet<ValueAnchor>),
    Batch(&'a BTreeMap<ValueAnchor, Vec<usize>>),
}

struct SpanCollector<'a> {
    anchors: AnchorLookup<'a>,
    kept: Vec<BTreeSet<AnchorSpan>>,
}

impl SpanCollector<'_> {
    fn keep_matching(&mut self, anchor: ValueAnchor, span: Span) {
        match &self.anchors {
            AnchorLookup::Single(anchors) => {
                if anchors.contains(&anchor) {
                    self.kept[0].insert((span.lo.0, span.hi.0));
                }
            }
            AnchorLookup::Batch(anchors) => {
                if let Some(indices) = anchors.get(&anchor) {
                    for &index in indices {
                        self.kept[index].insert((span.lo.0, span.hi.0));
                    }
                }
            }
        }
    }
}

impl Visit for SpanCollector<'_> {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Expr::Lit(lit) = expr {
            if let Some(anchor) = ValueAnchor::from_lit(lit) {
                // Pin the literal node; the prune keeps it verbatim.
                self.keep_matching(anchor, lit.span());
            }
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Some(label) = member_callee_label(callee)
        {
            // Pin the callee member access; the prune keeps the property name
            // and holes the receiver, yielding `ANYTHING.foo(...)`.
            self.keep_matching(ValueAnchor::CallCallee(label), callee.span());
        }
        call.visit_children_with(self);
    }

    fn visit_member_prop(&mut self, prop: &MemberProp) {
        if let Some(label) = member_prop_label(prop) {
            self.keep_matching(ValueAnchor::MemberProperty(label), prop.span());
        }
        prop.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        for prop in &object.props {
            if let Some((label, key_span)) = object_key_label_span(prop) {
                self.keep_matching(ValueAnchor::ObjectKey(label), key_span);
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
            if let Some((label, key_span)) = object_pat_key_label_span(prop) {
                self.keep_matching(ValueAnchor::ObjectKey(label), key_span);
            }
            prop.visit_with(self);
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if let Some((label, key_span)) = class_member_label_span(member) {
            self.keep_matching(ValueAnchor::ClassMember(label), key_span);
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
