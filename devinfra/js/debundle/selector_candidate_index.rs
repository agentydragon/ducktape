//! Per-statement feature extraction for `ShapeIndex`: the [`SelectorFeature`]s
//! (kind, literals, keys, member and callee names) one top-level statement
//! exhibits, and the [`CandidateSet`] bitmaps the index's posting lists are
//! built from.

use std::collections::BTreeSet;

use roaring::RoaringBitmap;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD,
    STMT_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE, hole_name_for, labeled_hole_name_for,
};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum TopLevelKind {
    ImportDeclaration,
    FunctionDeclaration,
    ClassDeclaration,
    VariableDeclaration,
    ExportedFunctionDeclaration,
    ExportedClassDeclaration,
    ExportedVariableDeclaration,
    ExpressionStatement,
    Statement,
    ModuleDeclaration,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum VarKind {
    Var,
    Let,
    Const,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum SelectorFeature {
    TopLevelKind(TopLevelKind),
    VarKind(VarKind),
    FunctionArity(usize),
    StringLiteral(String),
    /// A numeric literal, keyed by its canonical `f64::to_string` form so the
    /// index and matcher agree on identity (the matcher compares via
    /// `eq_ignore_span`, which is value equality). BigInt literals reuse this
    /// variant keyed by their decimal string.
    NumberLiteral(String),
    BoolLiteral(bool),
    ObjectKey(String),
    ClassMember(String),
    MemberProperty(String),
    CallCallee(String),
    ImportSource(String),
}

/// How a diagnostic names the feature to a spec author.
impl std::fmt::Display for SelectorFeature {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::TopLevelKind(kind) => write!(f, "top-level kind {kind:?}"),
            Self::VarKind(VarKind::Var) => f.write_str("`var` declaration"),
            Self::VarKind(VarKind::Let) => f.write_str("`let` declaration"),
            Self::VarKind(VarKind::Const) => f.write_str("`const` declaration"),
            Self::FunctionArity(arity) => write!(f, "function arity {arity}"),
            Self::StringLiteral(value) => write!(f, "string literal {value:?}"),
            Self::NumberLiteral(value) => write!(f, "number literal {value}"),
            Self::BoolLiteral(value) => write!(f, "boolean literal {value}"),
            Self::ObjectKey(key) => write!(f, "object key `{key}`"),
            Self::ClassMember(name) => write!(f, "class member `{name}`"),
            Self::MemberProperty(name) => write!(f, "property access `.{name}`"),
            Self::CallCallee(callee) => write!(f, "call of `{callee}(...)`"),
            Self::ImportSource(source) => write!(f, "import source {source:?}"),
        }
    }
}

/// A set of top-level body indices, backed by a [`RoaringBitmap`]. Body indices
/// are dense small integers `0..N` — exactly the workload roaring's compressed
/// bitmaps target: intersection is `&` / [`RoaringBitmap::intersection_len`] over
/// adaptive array/bitmap containers, with no ordered-tree traversal or per-element
/// allocation. The index-build perf lever for whole-chunk minimize (issue #2291).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CandidateSet {
    body_indices: RoaringBitmap,
}

impl CandidateSet {
    pub fn all(body_indices: impl IntoIterator<Item = usize>) -> Self {
        Self {
            body_indices: body_indices.into_iter().map(|idx| idx as u32).collect(),
        }
    }

    pub fn empty() -> Self {
        Self::default()
    }

    /// Append a body index strictly greater than every index appended so far. The
    /// index builders walk body items in order, so each posting list is produced
    /// already ascending and unique; `RoaringBitmap::push` appends in O(1) under
    /// exactly that precondition (and returns `false` if it is violated).
    pub fn push_ascending(&mut self, body_idx: usize) {
        let appended = self.body_indices.try_push(body_idx as u32).is_ok();
        debug_assert!(
            appended,
            "push_ascending requires strictly increasing indices"
        );
    }

    pub fn len(&self) -> usize {
        self.body_indices.len() as usize
    }

    pub fn is_empty(&self) -> bool {
        self.body_indices.is_empty()
    }

    pub fn contains(&self, body_idx: usize) -> bool {
        u32::try_from(body_idx).is_ok_and(|idx| self.body_indices.contains(idx))
    }

    pub fn intersect(&self, other: &CandidateSet) -> CandidateSet {
        CandidateSet {
            body_indices: &self.body_indices & &other.body_indices,
        }
    }

    /// Intersect into a reusable buffer, so a per-member loop can keep one scratch
    /// set instead of allocating a fresh one each step: `clone_from` reuses `out`'s
    /// container allocations, then `&=` intersects in place.
    pub fn intersect_into(&self, other: &CandidateSet, out: &mut CandidateSet) {
        out.body_indices.clone_from(&self.body_indices);
        out.body_indices &= &other.body_indices;
    }

    /// Size of the intersection without materializing it — for ranking candidate
    /// features by how much they shrink the working set.
    pub fn intersection_len(&self, other: &CandidateSet) -> usize {
        self.body_indices.intersection_len(&other.body_indices) as usize
    }
}

/// The features of one top-level statement: its kind plus the literal, key,
/// member and callee tokens it carries.
pub fn top_level_features(item: &ModuleItem) -> BTreeSet<SelectorFeature> {
    let mut features = BTreeSet::from([SelectorFeature::TopLevelKind(top_level_kind(item))]);
    collect_features_for_item(item, &mut features);
    features
}

fn collect_features_for_item(item: &ModuleItem, features: &mut BTreeSet<SelectorFeature>) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => collect_decl_features(decl, features),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            collect_decl_features(&export.decl, features)
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            features.insert(SelectorFeature::ImportSource(
                import.src.value.to_string_lossy().to_string(),
            ));
        }
        _ => {}
    }
    item.visit_with(&mut AstFeatureCollector { features });
}

fn collect_decl_features(decl: &Decl, features: &mut BTreeSet<SelectorFeature>) {
    match decl {
        Decl::Fn(function) => {
            features.insert(SelectorFeature::FunctionArity(
                function.function.params.len(),
            ));
        }
        Decl::Class(class) => {
            for member in &class.class.body {
                if class_rest_hole_name(member).is_none()
                    && let Some(label) = class_member_label(member)
                {
                    features.insert(SelectorFeature::ClassMember(label));
                }
            }
        }
        Decl::Var(var) => {
            features.insert(SelectorFeature::VarKind(var_kind(var.kind)));
        }
        _ => {}
    }
}

struct AstFeatureCollector<'features> {
    features: &'features mut BTreeSet<SelectorFeature>,
}

impl Visit for AstFeatureCollector<'_> {
    fn visit_expr(&mut self, expr: &Expr) {
        if expr_hole_name(expr).is_some() {
            return;
        }
        if let Expr::Lit(lit) = expr {
            match lit {
                Lit::Str(str_) => {
                    let value = str_.value.to_string_lossy().to_string();
                    self.features.insert(SelectorFeature::StringLiteral(value));
                }
                // Number / bool literals are never wildcarded or holed in place
                // (only an `EXPR`/`ANYTHING` hole erases them, handled above),
                // and the matcher discriminates them by value (`eq_ignore_span`),
                // so indexing them keeps the candidate set a sound match superset
                // while letting a numeric/bool discriminator narrow it.
                Lit::Num(num) => {
                    self.features
                        .insert(SelectorFeature::NumberLiteral(num.value.to_string()));
                }
                Lit::BigInt(bigint) => {
                    self.features
                        .insert(SelectorFeature::NumberLiteral(bigint.value.to_string()));
                }
                Lit::Bool(bool_) => {
                    self.features
                        .insert(SelectorFeature::BoolLiteral(bool_.value));
                }
                Lit::Null(_) | Lit::Regex(_) | Lit::JSXText(_) => {}
            }
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if string_literal_regex_pattern_call(call) {
            return;
        }
        if let Some(callee) = callee_label(&call.callee) {
            self.features.insert(SelectorFeature::CallCallee(callee));
        }
        call.visit_children_with(self);
    }

    fn visit_member_prop(&mut self, prop: &MemberProp) {
        if let Some(label) = member_prop_label(prop) {
            self.features.insert(SelectorFeature::MemberProperty(label));
        }
        prop.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        for prop in &object.props {
            if object_property_list_hole_name(prop).is_some() {
                continue;
            }
            if let Some(label) = object_key_label(prop) {
                self.features.insert(SelectorFeature::ObjectKey(label));
            }
            prop.visit_with(self);
        }
    }

    fn visit_object_pat_prop(&mut self, prop: &ObjectPatProp) {
        // A destructured property key (`const { …, foo } = obj`, or
        // `{ foo: localBinding }`) is the source object's stable property name —
        // the matcher matches it exactly, exactly like an object-literal key —
        // not the (minified) local binding it introduces. Index it as the same
        // `ObjectKey` feature so a wide-destructure discriminator can be read off
        // and pinned. The minified binding itself is alpha-volatile and never a
        // feature.
        if let Some(label) = object_pat_key_label(prop) {
            self.features.insert(SelectorFeature::ObjectKey(label));
        }
        prop.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if class_rest_hole_name(member).is_some() {
            return;
        }
        if let Some(label) = class_member_label(member) {
            self.features.insert(SelectorFeature::ClassMember(label));
        }
        member.visit_children_with(self);
    }

    fn visit_stmt(&mut self, stmt: &Stmt) {
        if stmt_hole_name(stmt).is_some() {
            return;
        }
        stmt.visit_children_with(self);
    }

    fn visit_switch_case(&mut self, case: &SwitchCase) {
        // A `case CASE_REST:` hole contributes no feature: it stands in
        // for an arbitrary run of dropped cases, so a candidate switch
        // need not carry any literal from it.
        if case_rest_hole_name(case).is_some() {
            return;
        }
        case.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if declarator_list_hole_name(declarator).is_some() {
            declarator.init.visit_with(self);
            return;
        }
        declarator.visit_children_with(self);
    }
}

fn top_level_kind(item: &ModuleItem) -> TopLevelKind {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => TopLevelKind::ImportDeclaration,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Fn(_) => TopLevelKind::ExportedFunctionDeclaration,
            Decl::Class(_) => TopLevelKind::ExportedClassDeclaration,
            Decl::Var(_) => TopLevelKind::ExportedVariableDeclaration,
            _ => TopLevelKind::ModuleDeclaration,
        },
        ModuleItem::ModuleDecl(_) => TopLevelKind::ModuleDeclaration,
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => TopLevelKind::FunctionDeclaration,
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(_))) => TopLevelKind::ClassDeclaration,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))) => TopLevelKind::VariableDeclaration,
        ModuleItem::Stmt(Stmt::Expr(_)) => TopLevelKind::ExpressionStatement,
        ModuleItem::Stmt(_) => TopLevelKind::Statement,
    }
}

fn var_kind(kind: VarDeclKind) -> VarKind {
    match kind {
        VarDeclKind::Var => VarKind::Var,
        VarDeclKind::Let => VarKind::Let,
        VarDeclKind::Const => VarKind::Const,
    }
}

fn expr_hole_name(expr: &Expr) -> Option<&str> {
    let Expr::Ident(ident) = expr else {
        return None;
    };
    let name = ident.sym.as_ref();
    hole_name_for(name, EXPR_HOLE_KEYWORD).or_else(|| hole_name_for(name, ANYTHING_HOLE_KEYWORD))
}

fn stmt_hole_name(stmt: &Stmt) -> Option<&str> {
    let Stmt::Expr(expr) = stmt else {
        return None;
    };
    let Expr::Ident(ident) = expr.expr.as_ref() else {
        return None;
    };
    let name = ident.sym.as_ref();
    hole_name_for(name, STMT_HOLE_KEYWORD).or_else(|| hole_name_for(name, ANYTHING_HOLE_KEYWORD))
}

fn declarator_list_hole_name(declarator: &VarDeclarator) -> Option<&str> {
    let Pat::Ident(ident) = &declarator.name else {
        return None;
    };
    let name = ident.id.sym.as_ref();
    labeled_hole_name_for(name, DECLARATORS_HOLE_KEYWORD)
        .or_else(|| hole_name_for(name, ANYTHING_HOLE_KEYWORD))
}

fn object_property_list_hole_name(prop: &PropOrSpread) -> Option<&str> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    match prop.as_ref() {
        Prop::Shorthand(ident) => hole_name_for(ident.sym.as_ref(), ANYTHING_HOLE_KEYWORD),
        _ => None,
    }
}

fn class_rest_hole_name(member: &ClassMember) -> Option<&str> {
    let ClassMember::ClassProp(prop) = member else {
        return None;
    };
    if prop.value.is_some() {
        return None;
    }
    let PropName::Ident(ident) = &prop.key else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), ANYTHING_HOLE_KEYWORD)
}

fn case_rest_hole_name(case: &SwitchCase) -> Option<&str> {
    if !case.cons.is_empty() {
        return None;
    }
    let Some(Expr::Ident(ident)) = case.test.as_deref() else {
        return None;
    };
    labeled_hole_name_for(ident.sym.as_ref(), CASE_REST_HOLE_KEYWORD)
}

fn string_literal_regex_pattern_call(call: &CallExpr) -> bool {
    let Callee::Expr(callee) = &call.callee else {
        return false;
    };
    matches!(
        callee.as_ref(),
        Expr::Ident(ident) if ident.sym.as_ref() == STRING_LITERAL_REGEX_PREDICATE
    )
}

fn callee_label(callee: &Callee) -> Option<String> {
    match callee {
        Callee::Expr(expr) => expr_label(expr),
        Callee::Super(_) => Some("super".to_string()),
        Callee::Import(_) => Some("import".to_string()),
    }
}

fn expr_label(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Ident(ident)
            if hole_name_for(ident.sym.as_ref(), ANYTHING_HOLE_KEYWORD).is_none() =>
        {
            Some(ident.sym.to_string())
        }
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

fn object_key_label(prop: &PropOrSpread) -> Option<String> {
    let PropOrSpread::Prop(prop) = prop else {
        return None;
    };
    match prop.as_ref() {
        Prop::Shorthand(ident)
            if hole_name_for(ident.sym.as_ref(), ANYTHING_HOLE_KEYWORD).is_none() =>
        {
            Some(ident.sym.to_string())
        }
        Prop::KeyValue(prop) => prop_name_label(&prop.key),
        Prop::Assign(prop)
            if hole_name_for(prop.key.sym.as_ref(), ANYTHING_HOLE_KEYWORD).is_none() =>
        {
            Some(prop.key.sym.to_string())
        }
        Prop::Getter(prop) => prop_name_label(&prop.key),
        Prop::Setter(prop) => prop_name_label(&prop.key),
        Prop::Method(prop) => prop_name_label(&prop.key),
        _ => None,
    }
}

/// The stable property-name label of a destructure-pattern property, or `None`
/// for the `ANYTHING` list hole, a rest element, or a computed key. Mirrors
/// [`object_key_label`] for the object-pattern case.
fn object_pat_key_label(prop: &ObjectPatProp) -> Option<String> {
    match prop {
        ObjectPatProp::KeyValue(kv) => prop_name_label(&kv.key),
        ObjectPatProp::Assign(assign)
            if hole_name_for(assign.key.id.sym.as_ref(), ANYTHING_HOLE_KEYWORD).is_none() =>
        {
            Some(assign.key.id.sym.to_string())
        }
        ObjectPatProp::Assign(_) | ObjectPatProp::Rest(_) => None,
    }
}

fn class_member_label(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name_label(&method.key),
        ClassMember::PrivateMethod(method) => Some(format!("#{}", method.key.name)),
        ClassMember::ClassProp(prop) => prop_name_label(&prop.key),
        ClassMember::PrivateProp(prop) => Some(format!("#{}", prop.key.name)),
        ClassMember::AutoAccessor(_) => None,
        ClassMember::StaticBlock(_) | ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {
            None
        }
    }
}

fn prop_name_label(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident)
            if hole_name_for(ident.sym.as_ref(), ANYTHING_HOLE_KEYWORD).is_none() =>
        {
            Some(ident.sym.to_string())
        }
        PropName::Ident(_) => None,
        PropName::Str(str_) => Some(str_.value.to_string_lossy().to_string()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::BigInt(bigint) => Some(bigint.value.to_string()),
        PropName::Computed(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn features(source: &str) -> Vec<BTreeSet<SelectorFeature>> {
        let module =
            js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<test>", source).unwrap());
        module.body.iter().map(top_level_features).collect()
    }

    #[test]
    fn var_statement_features_are_its_kind_and_value_tokens() {
        assert_eq!(
            features(r#"const alpha = makeWidget("shared-token", { role: "button" }, 123, true);"#),
            [BTreeSet::from([
                SelectorFeature::TopLevelKind(TopLevelKind::VariableDeclaration),
                SelectorFeature::VarKind(VarKind::Const),
                SelectorFeature::CallCallee("makeWidget".to_string()),
                SelectorFeature::StringLiteral("shared-token".to_string()),
                SelectorFeature::ObjectKey("role".to_string()),
                SelectorFeature::StringLiteral("button".to_string()),
                SelectorFeature::NumberLiteral("123".to_string()),
                SelectorFeature::BoolLiteral(true),
            ])]
        );
    }

    #[test]
    fn declaration_features_name_members_arity_and_destructured_keys() {
        // A destructured property key is the source object's stable property
        // name, not the minified local it binds.
        assert_eq!(
            features(
                r#"class Panel { render() {} mount() {} }
function combine(first, second) { return first.sum + 1; }
const { foo: localName, bar } = source;"#
            ),
            [
                BTreeSet::from([
                    SelectorFeature::TopLevelKind(TopLevelKind::ClassDeclaration),
                    SelectorFeature::ClassMember("render".to_string()),
                    SelectorFeature::ClassMember("mount".to_string()),
                ]),
                BTreeSet::from([
                    SelectorFeature::TopLevelKind(TopLevelKind::FunctionDeclaration),
                    SelectorFeature::FunctionArity(2),
                    SelectorFeature::MemberProperty("sum".to_string()),
                    SelectorFeature::NumberLiteral("1".to_string()),
                ]),
                BTreeSet::from([
                    SelectorFeature::TopLevelKind(TopLevelKind::VariableDeclaration),
                    SelectorFeature::VarKind(VarKind::Const),
                    SelectorFeature::ObjectKey("foo".to_string()),
                    SelectorFeature::ObjectKey("bar".to_string()),
                ]),
            ]
        );
    }
}
