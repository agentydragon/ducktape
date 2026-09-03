//! Cheap candidate indexing for source-match selector search.
//!
//! This crate is intentionally a prefilter, not a replacement for the
//! production `source_match` matcher. It builds one per-chunk inverted index of
//! top-level statement features, then answers "which body items or declared
//! bindings could this partial selector still match?" by intersecting posting
//! lists. A returned candidate set may contain false positives; it must not
//! omit a real matcher result.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use roaring::RoaringBitmap;
use rustc_hash::FxHashMap;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD,
    STMT_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE, hole_name_for,
    labeled_hole_name_for,
};
use spec::{AnonymousStatementSelector, BindingSourceKind, SourceMatchIdentifierMode};
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

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct IndexedBindingCandidate {
    pub body_idx: usize,
    pub binding_idx: usize,
    pub declarator_idx: Option<usize>,
    pub name: String,
    pub kind: BindingSourceKind,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct IndexedTopLevelCandidate {
    pub body_idx: usize,
    pub kind: TopLevelKind,
    pub declared_bindings: Vec<IndexedBindingCandidate>,
    pub features: BTreeSet<SelectorFeature>,
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
        let appended = self.body_indices.push(body_idx as u32);
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

    pub fn body_indices(&self) -> impl Iterator<Item = usize> + '_ {
        self.body_indices.iter().map(|idx| idx as usize)
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

#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct SelectorCandidateQuery {
    alternatives: Vec<SelectorCandidateAlternative>,
}

impl SelectorCandidateQuery {
    pub fn all() -> Self {
        Self {
            alternatives: vec![SelectorCandidateAlternative::default()],
        }
    }

    pub fn empty() -> Self {
        Self::default()
    }

    pub fn alternatives(&self) -> &[SelectorCandidateAlternative] {
        &self.alternatives
    }
}

#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct SelectorCandidateAlternative {
    features: BTreeSet<SelectorFeature>,
    binding_projection: Option<BindingProjection>,
}

impl SelectorCandidateAlternative {
    pub fn features(&self) -> &BTreeSet<SelectorFeature> {
        &self.features
    }

    pub fn binding_projection(&self) -> Option<&BindingProjection> {
        self.binding_projection.as_ref()
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct BindingProjection {
    pub binding_idx: usize,
    pub kind: BindingSourceKind,
}

pub struct SelectorCandidateIndex {
    items: Vec<IndexedTopLevelCandidate>,
    feature_to_body_indices: FxHashMap<SelectorFeature, CandidateSet>,
}

impl SelectorCandidateIndex {
    pub fn new(module: &Module) -> Self {
        let mut items = Vec::with_capacity(module.body.len());
        let mut feature_to_body_indices: FxHashMap<SelectorFeature, CandidateSet> =
            FxHashMap::default();
        for (body_idx, item) in module.body.iter().enumerate() {
            let indexed = IndexedTopLevelCandidate::from_module_item(body_idx, item);
            for feature in &indexed.features {
                feature_to_body_indices
                    .entry(feature.clone())
                    .or_default()
                    .push_ascending(body_idx);
            }
            items.push(indexed);
        }
        Self {
            items,
            feature_to_body_indices,
        }
    }

    pub fn top_level_candidates(&self) -> &[IndexedTopLevelCandidate] {
        &self.items
    }

    pub fn candidate(&self, body_idx: usize) -> Option<&IndexedTopLevelCandidate> {
        self.items.get(body_idx)
    }

    pub fn candidate_set_for_feature(&self, feature: &SelectorFeature) -> CandidateSet {
        self.feature_to_body_indices
            .get(feature)
            .cloned()
            .unwrap_or_default()
    }

    pub fn candidate_set_for_features<'a>(
        &self,
        features: impl IntoIterator<Item = &'a SelectorFeature>,
    ) -> CandidateSet {
        // A feature absent from the index has an empty posting list, so the whole
        // intersection is empty; no features at all means every item qualifies.
        let postings: Option<Vec<&CandidateSet>> = features
            .into_iter()
            .map(|feature| self.feature_to_body_indices.get(feature))
            .collect();
        let Some(mut postings) = postings else {
            return CandidateSet::empty();
        };
        postings.sort_by_key(|posting| posting.len());
        let Some((first, rest)) = postings.split_first() else {
            return CandidateSet::all(self.items.iter().map(|item| item.body_idx));
        };
        // Intersect smallest-first into a reused scratch buffer.
        let mut acc = (*first).clone();
        let mut scratch = CandidateSet::empty();
        for next in rest.iter().copied() {
            acc.intersect_into(next, &mut scratch);
            std::mem::swap(&mut acc, &mut scratch);
            if acc.is_empty() {
                break;
            }
        }
        acc
    }

    pub fn query_for_source_match(
        selector: &AnonymousStatementSelector,
    ) -> Result<SelectorCandidateQuery> {
        js_ast::with_swc_globals(|| {
            let parsed =
                js_ast::parse_js_module_ast("<selector candidate query>", &selector.match_source)
                    .with_context(|| {
                    format!(
                        "parsing source_match selector for candidate indexing:\n{}",
                        selector.match_source
                    )
                })?;
            Ok(query_for_selector_items(&parsed.body, selector))
        })
    }

    pub fn candidate_set_for_query(&self, query: &SelectorCandidateQuery) -> CandidateSet {
        if query.alternatives.is_empty() {
            return CandidateSet::empty();
        }
        // Union the per-alternative candidate sets.
        let mut merged = RoaringBitmap::new();
        for alternative in &query.alternatives {
            merged |= self
                .candidate_set_for_features(&alternative.features)
                .body_indices;
        }
        CandidateSet {
            body_indices: merged,
        }
    }

    pub fn candidate_set_for_source_match(
        &self,
        selector: &AnonymousStatementSelector,
    ) -> Result<CandidateSet> {
        Ok(self.candidate_set_for_query(&Self::query_for_source_match(selector)?))
    }

    pub fn candidate_bindings_for_set(&self, set: &CandidateSet) -> Vec<IndexedBindingCandidate> {
        set.body_indices()
            .filter_map(|body_idx| self.candidate(body_idx))
            .flat_map(|candidate| candidate.declared_bindings.iter().cloned())
            .collect()
    }

    pub fn candidate_bindings_for_query(
        &self,
        query: &SelectorCandidateQuery,
    ) -> Vec<IndexedBindingCandidate> {
        let mut bindings = BTreeMap::new();
        for alternative in &query.alternatives {
            let set = self.candidate_set_for_features(&alternative.features);
            for body_idx in set.body_indices() {
                let Some(candidate) = self.candidate(body_idx) else {
                    continue;
                };
                let projected = alternative
                    .binding_projection
                    .and_then(|projection| projected_binding(candidate, projection));
                let iter: Box<dyn Iterator<Item = IndexedBindingCandidate>> =
                    if let Some(binding) = projected {
                        Box::new(std::iter::once(binding))
                    } else {
                        Box::new(candidate.declared_bindings.iter().cloned())
                    };
                for binding in iter {
                    bindings.insert((binding.body_idx, binding.binding_idx), binding);
                }
            }
        }
        bindings.into_values().collect()
    }

    pub fn candidate_bindings_for_source_match(
        &self,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<IndexedBindingCandidate>> {
        Ok(self.candidate_bindings_for_query(&Self::query_for_source_match(selector)?))
    }
}

impl IndexedTopLevelCandidate {
    fn from_module_item(body_idx: usize, item: &ModuleItem) -> Self {
        let kind = top_level_kind(item);
        let mut features = BTreeSet::from([SelectorFeature::TopLevelKind(kind)]);
        collect_features_for_item(item, None, &mut features);
        let declared_bindings = declared_bindings(item, body_idx);
        Self {
            body_idx,
            kind,
            declared_bindings,
            features,
        }
    }
}

fn projected_binding(
    candidate: &IndexedTopLevelCandidate,
    projection: BindingProjection,
) -> Option<IndexedBindingCandidate> {
    candidate
        .declared_bindings
        .get(projection.binding_idx)
        .filter(|binding| binding.kind == projection.kind)
        .cloned()
}

fn query_for_selector_items(
    items: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> SelectorCandidateQuery {
    let target_indices = target_item_indices(items, selector);
    if target_indices.is_empty() {
        return SelectorCandidateQuery::empty();
    }

    let alternatives = target_indices
        .into_iter()
        .filter_map(|item_idx| {
            let item = items.get(item_idx)?;
            if module_item_list_hole_name(item).is_some() {
                return Some(SelectorCandidateAlternative::default());
            }
            let mut features =
                BTreeSet::from([SelectorFeature::TopLevelKind(top_level_kind(item))]);
            collect_features_for_item(item, Some(selector), &mut features);
            let binding_projection = selector
                .target_binding
                .as_deref()
                .and_then(|target| binding_projection_for_target(item, target));
            Some(SelectorCandidateAlternative {
                features,
                binding_projection,
            })
        })
        .collect::<Vec<_>>();

    if alternatives.is_empty() {
        SelectorCandidateQuery::empty()
    } else {
        SelectorCandidateQuery { alternatives }
    }
}

fn target_item_indices(items: &[ModuleItem], selector: &AnonymousStatementSelector) -> Vec<usize> {
    if let Some(target_binding) = selector.target_binding.as_deref() {
        return items
            .iter()
            .enumerate()
            .filter_map(|(idx, item)| {
                declared_bindings(item, idx)
                    .iter()
                    .any(|binding| binding.name == target_binding)
                    .then_some(idx)
            })
            .collect();
    }
    match items {
        [] => Vec::new(),
        [single] if module_item_list_hole_name(single).is_some() => vec![0],
        [_] => vec![0],
        _ => items
            .iter()
            .enumerate()
            .filter_map(|(idx, item)| module_item_list_hole_name(item).is_none().then_some(idx))
            .collect(),
    }
}

fn binding_projection_for_target(
    item: &ModuleItem,
    target_binding: &str,
) -> Option<BindingProjection> {
    if item_var_decl(item).is_some_and(|var| {
        var.decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
    }) {
        return None;
    }
    let matches = declared_bindings(item, 0)
        .into_iter()
        .filter_map(|binding| {
            (binding.name == target_binding).then_some(BindingProjection {
                binding_idx: binding.binding_idx,
                kind: binding.kind,
            })
        })
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [single] => Some(*single),
        _ => None,
    }
}

fn collect_features_for_item(
    item: &ModuleItem,
    selector: Option<&AnonymousStatementSelector>,
    features: &mut BTreeSet<SelectorFeature>,
) {
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
    item.visit_with(&mut AstFeatureCollector::new(selector, features));
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

struct AstFeatureCollector<'a, 'features> {
    selector: Option<&'a AnonymousStatementSelector>,
    features: &'features mut BTreeSet<SelectorFeature>,
}

impl<'a, 'features> AstFeatureCollector<'a, 'features> {
    fn new(
        selector: Option<&'a AnonymousStatementSelector>,
        features: &'features mut BTreeSet<SelectorFeature>,
    ) -> Self {
        Self { selector, features }
    }

    fn selector_uses_exact_identifiers(&self) -> bool {
        self.selector
            .is_none_or(|selector| selector.identifiers == SourceMatchIdentifierMode::Exact)
    }
}

impl Visit for AstFeatureCollector<'_, '_> {
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
        if self.selector_uses_exact_identifiers()
            && let Some(callee) = callee_label(&call.callee)
        {
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

fn declared_bindings(item: &ModuleItem, body_idx: usize) -> Vec<IndexedBindingCandidate> {
    let mut bindings = Vec::new();
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => {
            declared_bindings_for_decl(decl, body_idx, &mut bindings);
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            declared_bindings_for_decl(&export.decl, body_idx, &mut bindings);
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            for specifier in &import.specifiers {
                let name = match specifier {
                    ImportSpecifier::Named(named) => named.local.sym.to_string(),
                    ImportSpecifier::Default(default) => default.local.sym.to_string(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
                };
                push_binding(
                    &mut bindings,
                    body_idx,
                    None,
                    name,
                    BindingSourceKind::ImportSpecifier,
                );
            }
        }
        _ => {}
    }
    bindings
}

fn declared_bindings_for_decl(
    decl: &Decl,
    body_idx: usize,
    bindings: &mut Vec<IndexedBindingCandidate>,
) {
    match decl {
        Decl::Fn(function) => push_binding(
            bindings,
            body_idx,
            None,
            function.ident.sym.to_string(),
            BindingSourceKind::FunctionDeclaration,
        ),
        Decl::Class(class) => push_binding(
            bindings,
            body_idx,
            None,
            class.ident.sym.to_string(),
            BindingSourceKind::ClassDeclaration,
        ),
        Decl::Var(var) => {
            for (declarator_idx, declarator) in var.decls.iter().enumerate() {
                if declarator_list_hole_name(declarator).is_some() {
                    continue;
                }
                for name in binding_targets::binding_name_strings(&declarator.name) {
                    push_binding(
                        bindings,
                        body_idx,
                        Some(declarator_idx),
                        name,
                        BindingSourceKind::VariableDeclarator,
                    );
                }
            }
        }
        _ => {}
    }
}

fn push_binding(
    bindings: &mut Vec<IndexedBindingCandidate>,
    body_idx: usize,
    declarator_idx: Option<usize>,
    name: String,
    kind: BindingSourceKind,
) {
    bindings.push(IndexedBindingCandidate {
        body_idx,
        binding_idx: bindings.len(),
        declarator_idx,
        name,
        kind,
    });
}

fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
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

fn module_item_list_hole_name(item: &ModuleItem) -> Option<&str> {
    let ModuleItem::Stmt(Stmt::Expr(expr)) = item else {
        return None;
    };
    let Expr::Ident(ident) = expr.expr.as_ref() else {
        return None;
    };
    labeled_hole_name_for(ident.sym.as_ref(), STMT_LIST_HOLE_KEYWORD)
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
    use std::collections::BTreeSet;

    fn parse_module(source: &str) -> Module {
        js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<test>", source).unwrap())
    }

    fn selector(match_source: &str) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
        }
    }

    fn exact_selector(match_source: &str) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            identifiers: SourceMatchIdentifierMode::Exact,
            ..selector(match_source)
        }
    }

    fn body_indices(set: CandidateSet) -> Vec<usize> {
        set.body_indices().collect()
    }

    /// The exact top-level body indices a selector resolves to, via the fact
    /// `ChunkResolver` — the correctness ground truth the candidate-index
    /// prefilter must be a sound superset of.
    fn exact_matches(runtime: &Module, selector: &AnonymousStatementSelector) -> Vec<usize> {
        use source_match::legacy_resolver::SelectorResolver;
        js_ast::with_swc_globals(|| {
            source_match::legacy_resolver::ChunkResolver::new(runtime)
                .resolve_anonymous_groups("<test>", selector)
                .unwrap()
                .into_iter()
                .flatten()
                .collect()
        })
    }

    #[test]
    fn indexes_top_level_statement_features_for_intersection() {
        let runtime = parse_module(
            r#"const alpha = makeWidget("shared-token", { role: "button" });
const beta = makeOther("shared-token", { role: "button" });
let gamma = makeWidget("shared-token", { role: "button" });
class Panel { render() {} mount() {} }
class Worker { mount() {} }
function combine(first, second) { return first + second; }
sideEffect();"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);

        let shared_const = BTreeSet::from([
            SelectorFeature::TopLevelKind(TopLevelKind::VariableDeclaration),
            SelectorFeature::VarKind(VarKind::Const),
            SelectorFeature::StringLiteral("shared-token".to_string()),
        ]);
        assert_eq!(
            body_indices(index.candidate_set_for_features(&shared_const)),
            vec![0, 1]
        );

        let narrowed = index.candidate_set_for_features(&shared_const).intersect(
            &index
                .candidate_set_for_feature(&SelectorFeature::CallCallee("makeWidget".to_string())),
        );
        assert_eq!(body_indices(narrowed), vec![0]);
    }

    #[test]
    fn source_match_query_is_sound_for_class_selectors() {
        let runtime = parse_module(
            r#"class Panel { render() {} mount() {} }
class Worker { mount() {} }
function render(value) { return value; }"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        let selector = selector("class ReadableName {\n  render() {}\n  ANYTHING;\n}");

        let exact = exact_matches(&runtime, &selector);
        let candidate_set = index.candidate_set_for_source_match(&selector).unwrap();

        assert_eq!(exact, vec![0]);
        assert!(exact.into_iter().all(|idx| candidate_set.contains(idx)));
        assert_eq!(body_indices(candidate_set), vec![0]);
    }

    #[test]
    fn source_match_query_is_sound_for_switch_case_rest_selectors() {
        let runtime = parse_module(
            r#"switch (a) { case "x": doX(); case "y": doY(); }
switch (b) { case "z": doZ(); }
function unrelated() { return 1; }"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        // The discriminating `case "x":` literal must keep the candidate
        // set a sound superset; the `case CASE_REST:` holes contribute no
        // feature, so only the switch that carries `"x"` survives.
        let selector = selector(
            "switch (ANYTHING) {\n  case CASE_REST:\n  case \"x\":\n    STMT_LIST;\n  case CASE_REST:\n}",
        );

        let exact = exact_matches(&runtime, &selector);
        let candidate_set = index.candidate_set_for_source_match(&selector).unwrap();

        assert_eq!(exact, vec![0]);
        assert!(exact.into_iter().all(|idx| candidate_set.contains(idx)));
    }

    #[test]
    fn number_and_bool_literal_features_keep_candidate_set_a_match_superset() {
        // The number/bool literal taxonomy extension must stay a sound prefilter:
        // for every selector pinning a numeric or boolean discriminator, the
        // candidate set must contain every body index the production matcher
        // returns (it may contain extras, never miss a true match).
        let runtime = parse_module(
            r#"const alpha = make(123, { enabled: true });
const beta = make(456, { enabled: true });
const gamma = make(123, { enabled: false });
const delta = make(123);"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);

        for selector_source in [
            r#"const readable = make(123, ANYTHING);"#,
            r#"const readable = make(456, ANYTHING);"#,
            r#"const readable = make(ANYTHING, { enabled: true });"#,
            r#"const readable = make(ANYTHING, { enabled: false });"#,
            r#"const readable = make(123, { enabled: false });"#,
        ] {
            let selector = selector(selector_source);
            let matched = exact_matches(&runtime, &selector);
            let candidate_set = index.candidate_set_for_source_match(&selector).unwrap();
            assert!(
                matched.iter().all(|idx| candidate_set.contains(*idx)),
                "candidate set must be a match superset for {selector_source:?}: \
                 matched={matched:?} candidates={:?}",
                body_indices(candidate_set.clone())
            );
        }
    }

    #[test]
    fn number_literal_feature_narrows_to_the_discriminating_item() {
        // The numeric argument is the sole discriminator; indexing it lets the
        // prefilter narrow to the single item carrying that value.
        let runtime = parse_module(
            r#"const alpha = make(call(), 123);
const beta = make(call(), 456);"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        let only_123 =
            index.candidate_set_for_feature(&SelectorFeature::NumberLiteral(123.0_f64.to_string()));
        assert_eq!(body_indices(only_123), vec![0]);
    }

    #[test]
    fn alpha_queries_do_not_treat_callee_identifiers_as_exact() {
        let runtime = parse_module(
            r#"const alpha = makeWidget("shared-token");
const beta = makeOther("shared-token");"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        let selector = selector(r#"const readable = makeWidget("shared-token");"#);
        let query = SelectorCandidateIndex::query_for_source_match(&selector).unwrap();

        assert!(
            !query.alternatives()[0]
                .features()
                .contains(&SelectorFeature::CallCallee("makeWidget".to_string()))
        );
        assert_eq!(
            body_indices(index.candidate_set_for_query(&query)),
            vec![0, 1]
        );
    }

    #[test]
    fn exact_queries_can_use_callee_identifiers() {
        let runtime = parse_module(
            r#"const alpha = makeWidget("shared-token");
const alpha = makeOther("shared-token");"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        let selector = exact_selector(r#"const alpha = makeWidget("shared-token");"#);
        let query = SelectorCandidateIndex::query_for_source_match(&selector).unwrap();

        assert!(
            query.alternatives()[0]
                .features()
                .contains(&SelectorFeature::CallCallee("makeWidget".to_string()))
        );
        assert_eq!(body_indices(index.candidate_set_for_query(&query)), vec![0]);
    }

    #[test]
    fn target_binding_projection_returns_candidate_bindings() {
        let runtime = parse_module(
            r#"const runtimeName = "stable-token";
const other = "other-token";
function stableFn() {}"#,
        );
        let index = SelectorCandidateIndex::new(&runtime);
        let mut selector = selector(r#"const readableName = "stable-token";"#);
        selector.target_binding = Some("readableName".to_string());

        let bindings = index
            .candidate_bindings_for_source_match(&selector)
            .unwrap()
            .into_iter()
            .map(|binding| (binding.body_idx, binding.name, binding.kind))
            .collect::<Vec<_>>();

        assert_eq!(
            bindings,
            vec![(
                0,
                "runtimeName".to_string(),
                BindingSourceKind::VariableDeclarator
            )]
        );
    }
}
