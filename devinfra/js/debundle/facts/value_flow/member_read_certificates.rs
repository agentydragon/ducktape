//! Per-expression evidence for counted-loop and guarded snapshot reads.
//!
//! This is a source-shape proof only. It does not assign a type to a binding,
//! infer a loop bound, or make claims about purity or completion.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::OnceLock;

use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use binding_targets::{
    TargetAccessRecorder, binding_names, record_assign_target, record_pat_write,
    record_update_target,
};

use super::guarded_member_reads::{GuardedSnapshotUse, guarded_snapshot_uses};

#[derive(Clone, Copy)]
struct DiagnosticSpanSelector {
    lo: u32,
    hi: u32,
}

impl DiagnosticSpanSelector {
    fn parse(value: &str) -> Option<Self> {
        let value = value.trim();
        let (lo, hi) = value.split_once("..").or_else(|| value.split_once(':'))?;
        let lo: u32 = lo.parse().ok()?;
        let hi: u32 = hi.parse().ok()?;
        (lo <= hi).then_some(Self { lo, hi })
    }

    fn selects(self, span: Span) -> bool {
        self.lo <= span.lo.0 && span.hi.0 <= self.hi
    }

    fn overlaps(self, span: Span) -> bool {
        self.lo <= span.hi.0 && span.lo.0 <= self.hi
    }
}

fn diagnostic_selector() -> Option<DiagnosticSpanSelector> {
    // Diagnostic-only selector: either `lo..hi` or `lo:hi` selects candidate
    // spans contained by the supplied source-span window.
    static SELECTOR: OnceLock<Option<DiagnosticSpanSelector>> = OnceLock::new();
    *SELECTOR.get_or_init(|| {
        std::env::var("DEBUNDLE_DEBUG_GUARDED_MEMBER_READ_SPAN")
            .ok()
            .and_then(|value| DiagnosticSpanSelector::parse(&value))
    })
}

fn emit_selected_diagnostic(message: impl std::fmt::Display) {
    use std::sync::atomic::{AtomicUsize, Ordering};

    static LINES: AtomicUsize = AtomicUsize::new(0);
    if LINES.fetch_add(1, Ordering::Relaxed) < 12 {
        eprintln!("[guarded-member-census] {message}");
    }
}

fn local_binding_id(id: &Id, ids: &mut Vec<Id>) -> usize {
    if let Some(index) = ids.iter().position(|known| known == id) {
        index
    } else {
        ids.push(id.clone());
        ids.len() - 1
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct MemberReadCertificates {
    spans: BTreeSet<(u32, u32)>,
    guarded: BTreeMap<(u32, u32), (Id, String)>,
}

impl MemberReadCertificates {
    pub(super) fn contains(&self, span: Span) -> bool {
        self.spans.contains(&(span.lo.0, span.hi.0))
    }

    pub(super) fn guarded_projection(&self, span: Span) -> Option<&(Id, String)> {
        self.guarded.get(&(span.lo.0, span.hi.0))
    }

    pub(super) fn collect(body: &[super::super::TopLevelItemView<'_>]) -> Self {
        let mut census = FunctionCensus::default();
        for item in body {
            item.as_module_item().visit_with(&mut census);
        }
        census.certificates()
    }
}

#[derive(Default)]
struct FunctionCensus {
    functions: Vec<FunctionFacts>,
    stack: Vec<usize>,
}

#[derive(Default)]
struct FunctionFacts {
    parent: Option<usize>,
    certifiable_loops: bool,
    is_async: bool,
    is_generator: bool,
    parameters: HashSet<Id>,
    declarations: std::collections::HashMap<Id, Vec<(u32, u32)>>,
    writes: std::collections::HashMap<Id, Vec<Span>>,
    references: HashSet<Id>,
    captured_bindings: HashSet<Id>,
    dynamic_scope: bool,
    nested_dynamic_scope: bool,
    loops: Vec<CountedLoop>,
    member_reads: std::collections::HashMap<Id, Vec<Span>>,
    guarded_uses: Vec<GuardedSnapshotUse>,
}

#[derive(Clone)]
struct CountedLoop {
    binding: Id,
    declaration_span: (u32, u32),
    test: Option<Span>,
    body: Span,
}

impl FunctionCensus {
    fn current(&self) -> Option<usize> {
        self.stack.last().copied()
    }

    fn current_mut(&mut self) -> Option<&mut FunctionFacts> {
        let index = self.current()?;
        self.functions.get_mut(index)
    }

    fn record_declaration(&mut self, id: Id, span: Span) {
        if let Some(function) = self.current_mut() {
            function
                .declarations
                .entry(id)
                .or_default()
                .push((span.lo.0, span.hi.0));
        }
    }

    fn with_function(
        &mut self,
        certifiable_loops: bool,
        is_async: bool,
        is_generator: bool,
        parameters: impl IntoIterator<Item = Id>,
        visit: impl FnOnce(&mut Self),
    ) {
        let index = self.functions.len();
        let parent = self.current();
        self.functions.push(FunctionFacts {
            parent,
            certifiable_loops,
            is_async,
            is_generator,
            parameters: parameters.into_iter().collect(),
            ..FunctionFacts::default()
        });
        self.stack.push(index);
        visit(self);
        self.stack.pop();
    }

    fn record_writes(&mut self, span: Span, ids: impl IntoIterator<Item = Id>) {
        if let Some(function) = self.current_mut() {
            for id in ids {
                function.writes.entry(id).or_default().push(span);
            }
        }
    }

    fn certificates(mut self) -> MemberReadCertificates {
        let mut spans = BTreeSet::new();
        let mut guarded = BTreeMap::new();
        let mut owners: std::collections::HashMap<Id, BTreeSet<usize>> =
            std::collections::HashMap::new();
        for (function_index, function) in self.functions.iter().enumerate() {
            for id in function.declarations.keys() {
                owners.entry(id.clone()).or_default().insert(function_index);
            }
        }
        let ambiguous: HashSet<_> = owners
            .iter()
            .filter_map(|(id, owners)| (owners.len() > 1).then_some(id.clone()))
            .collect();
        for function_index in 0..self.functions.len() {
            let references: Vec<_> = self.functions[function_index]
                .references
                .iter()
                .cloned()
                .collect();
            for id in references {
                if ambiguous.contains(&id) {
                    continue;
                }
                if let Some(owner) = owners.get(&id).and_then(|owners| owners.first()).copied()
                    && owner != function_index
                {
                    self.functions[owner].captured_bindings.insert(id);
                }
            }
        }
        // A reverse creation-order fold propagates the one-bit dynamic-scope
        // barrier through the function tree without copying identifier sets
        // through every ancestor.
        for child in (0..self.functions.len()).rev() {
            if (self.functions[child].dynamic_scope || self.functions[child].nested_dynamic_scope)
                && let Some(parent) = self.functions[child].parent
            {
                self.functions[parent].nested_dynamic_scope = true;
            }
        }
        for (function_index, function) in self.functions.iter_mut().enumerate() {
            for writes in function.writes.values_mut() {
                writes.sort_unstable_by_key(|span| span.lo.0);
            }
            for reads in function.member_reads.values_mut() {
                reads.sort_unstable_by_key(|span| span.lo.0);
            }
            let function_scope_blocked = function.is_async
                || function.is_generator
                || function.dynamic_scope
                || function.nested_dynamic_scope;
            for use_ in &function.guarded_uses {
                let roles = [&use_.receiver, &use_.key, &use_.snapshot];
                let roles_eligible = roles.into_iter().all(|id| {
                    function.declarations.contains_key(id)
                        && !function.captured_bindings.contains(id)
                        && !ambiguous.contains(id)
                });
                let admitted =
                    function.certifiable_loops && !function_scope_blocked && roles_eligible;

                if diagnostic_selector().is_some_and(|selector| selector.selects(use_.span)) {
                    let mut local_ids = Vec::new();
                    let binding_ids = roles.map(|id| local_binding_id(id, &mut local_ids));
                    let declared = roles.map(|id| function.declarations.contains_key(id));
                    let captured = roles.map(|id| function.captured_bindings.contains(id));
                    let role_ambiguous = roles.map(|id| ambiguous.contains(id));
                    let same_owner = roles.iter().all(|id| {
                        owners.get(*id).is_some_and(|owners| {
                            owners.len() == 1 && owners.contains(&function_index)
                        })
                    });
                    let declarations = roles.map(|id| {
                        function
                            .declarations
                            .get(id)
                            .map(|spans| {
                                spans
                                    .iter()
                                    .map(|(lo, hi)| format!("{lo}..{hi}"))
                                    .collect::<Vec<_>>()
                                    .join(",")
                            })
                            .unwrap_or_else(|| "-".to_owned())
                    });
                    emit_selected_diagnostic(format_args!(
                        "use={}..{} property={} function={} local_ids=receiver:{},key:{},snapshot:{} declarations=[{},{},{}] same_owner={} declared=[{},{},{}] captured=[{},{},{}] ambiguous=[{},{},{}] certifiable={} async={} generator={} dynamic_scope={} nested_dynamic_scope={} admitted={}",
                        use_.span.lo.0,
                        use_.span.hi.0,
                        use_.property,
                        function_index,
                        binding_ids[0],
                        binding_ids[1],
                        binding_ids[2],
                        declarations[0],
                        declarations[1],
                        declarations[2],
                        same_owner,
                        declared[0],
                        declared[1],
                        declared[2],
                        captured[0],
                        captured[1],
                        captured[2],
                        role_ambiguous[0],
                        role_ambiguous[1],
                        role_ambiguous[2],
                        function.certifiable_loops,
                        function.is_async,
                        function.is_generator,
                        function.dynamic_scope,
                        function.nested_dynamic_scope,
                        admitted,
                    ));
                }

                if admitted {
                    guarded.insert(
                        (use_.span.lo.0, use_.span.hi.0),
                        (use_.receiver.clone(), use_.property.clone()),
                    );
                }
            }
            if function_scope_blocked {
                continue;
            }
            for loop_ in &function.loops {
                let declarations = function.declarations.get(&loop_.binding);
                if !function.certifiable_loops
                    || function.parameters.contains(&loop_.binding)
                    || function.captured_bindings.contains(&loop_.binding)
                    || ambiguous.contains(&loop_.binding)
                    || declarations.is_none_or(|declarations| {
                        declarations.len() != 1 || declarations[0] != loop_.declaration_span
                    })
                {
                    continue;
                }
                let writes = function
                    .writes
                    .get(&loop_.binding)
                    .map(Vec::as_slice)
                    .unwrap_or_default();
                if loop_.test.is_some_and(|test| has_write(writes, test))
                    || has_write(writes, loop_.body)
                {
                    continue;
                }
                let reads = function
                    .member_reads
                    .get(&loop_.binding)
                    .map(Vec::as_slice)
                    .unwrap_or_default();
                if let Some(test) = loop_.test {
                    add_member_reads(reads, test, &mut spans);
                }
                add_member_reads(reads, loop_.body, &mut spans);
            }
        }
        MemberReadCertificates { spans, guarded }
    }
}

fn span_contains(outer: Span, inner: Span) -> bool {
    outer.lo.0 <= inner.lo.0 && inner.hi.0 <= outer.hi.0
}

fn has_write(writes: &[Span], range: Span) -> bool {
    let start = writes.partition_point(|span| span.lo.0 < range.lo.0);
    writes[start..]
        .iter()
        .take_while(|span| span.lo.0 <= range.hi.0)
        .any(|span| span_contains(range, *span))
}

fn add_member_reads(reads: &[Span], range: Span, spans: &mut BTreeSet<(u32, u32)>) {
    let start = reads.partition_point(|span| span.lo.0 < range.lo.0);
    for span in reads[start..]
        .iter()
        .take_while(|span| span.lo.0 <= range.hi.0)
    {
        if span_contains(range, *span) {
            spans.insert((span.lo.0, span.hi.0));
        }
    }
}

fn counted_loop(node: &ForStmt) -> Option<CountedLoop> {
    let VarDeclOrExpr::VarDecl(declaration) = node.init.as_ref()? else {
        return None;
    };
    if !matches!(declaration.kind, VarDeclKind::Var | VarDeclKind::Let)
        || declaration.decls.len() != 1
    {
        return None;
    }
    let declarator = declaration.decls.first()?;
    let Pat::Ident(binding) = &declarator.name else {
        return None;
    };
    let init = declarator.init.as_deref()?;
    if !matches!(
        init.unwrap_parens(),
        Expr::Lit(Lit::Num(_) | Lit::BigInt(_))
    ) {
        return None;
    }
    let update = node.update.as_deref()?;
    let Expr::Update(update) = update.unwrap_parens() else {
        return None;
    };
    if !matches!(update.op, UpdateOp::PlusPlus | UpdateOp::MinusMinus) {
        return None;
    }
    let Expr::Ident(update_binding) = update.arg.unwrap_parens() else {
        return None;
    };
    let binding = binding.id.to_id();
    if update_binding.to_id() != binding {
        return None;
    }
    Some(CountedLoop {
        binding,
        declaration_span: (
            binding_span(&declarator.name).lo.0,
            binding_span(&declarator.name).hi.0,
        ),
        test: node.test.as_deref().map(Spanned::span),
        body: node.body.span(),
    })
}

fn binding_span(pattern: &Pat) -> Span {
    match pattern {
        Pat::Ident(binding) => binding.id.span,
        _ => pattern.span(),
    }
}

#[derive(Default)]
struct WriteIds(HashSet<Id>);

impl TargetAccessRecorder for WriteIds {
    fn record_binding_write(&mut self, id: &Id) {
        self.0.insert(id.clone());
    }
}

impl FunctionCensus {
    fn record_pattern_declarations(&mut self, pattern: &Pat) {
        let span = binding_span(pattern);
        for id in binding_names(pattern) {
            self.record_declaration(id, span);
        }
    }
}

impl Visit for FunctionCensus {
    fn visit_ident(&mut self, node: &Ident) {
        if let Some(function) = self.current_mut() {
            function.references.insert(node.to_id());
        }
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        self.record_pattern_declarations(&node.name);
        node.visit_children_with(self);
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.record_declaration(node.ident.to_id(), node.ident.span);
        node.function.visit_with(self);
    }

    fn visit_fn_expr(&mut self, node: &FnExpr) {
        let parameters = node
            .function
            .params
            .iter()
            .flat_map(|parameter| binding_names(&parameter.pat));
        self.with_function(
            true,
            node.function.is_async,
            node.function.is_generator,
            parameters,
            |this| {
                if let Some(name) = &node.ident {
                    this.record_declaration(name.to_id(), name.span);
                }
                for parameter in &node.function.params {
                    this.record_pattern_declarations(&parameter.pat);
                }
                node.function.visit_children_with(this);
            },
        );
    }

    fn visit_function(&mut self, node: &Function) {
        let parameters = node
            .params
            .iter()
            .flat_map(|parameter| binding_names(&parameter.pat));
        self.with_function(true, node.is_async, node.is_generator, parameters, |this| {
            for parameter in &node.params {
                this.record_pattern_declarations(&parameter.pat);
            }
            node.visit_children_with(this);
        });
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let parameters = node.params.iter().flat_map(binding_names);
        self.with_function(true, node.is_async, false, parameters, |this| {
            for parameter in &node.params {
                this.record_pattern_declarations(parameter);
            }
            node.visit_children_with(this);
        });
    }

    fn visit_class(&mut self, node: &Class) {
        self.with_class(node, None);
    }

    fn visit_class_expr(&mut self, node: &ClassExpr) {
        self.with_class(&node.class, node.ident.as_ref());
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        self.record_declaration(node.ident.to_id(), node.ident.span);
        self.with_class(&node.class, None);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        node.key.visit_with(self);
        node.function.visit_with(self);
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        node.key.visit_with(self);
        node.function.visit_with(self);
    }

    fn visit_catch_clause(&mut self, node: &CatchClause) {
        if let Some(parameter) = &node.param {
            self.record_pattern_declarations(parameter);
        }
        node.visit_children_with(self);
    }

    fn visit_for_stmt(&mut self, node: &ForStmt) {
        if let (Some(function), Some(loop_)) = (self.current(), counted_loop(node))
            && self.functions[function].certifiable_loops
        {
            self.functions[function].loops.push(loop_);
        }
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        let uses = guarded_snapshot_uses(node);
        if let Some(selector) = diagnostic_selector()
            && selector.overlaps(node.span)
            && !uses.iter().any(|use_| selector.selects(use_.span))
        {
            emit_selected_diagnostic(format_args!(
                "loop={}..{} candidate=none candidate_count={}",
                node.span.lo.0,
                node.span.hi.0,
                uses.len(),
            ));
        }
        if let Some(function) = self.current_mut() {
            function.guarded_uses.extend(uses);
        }
        let mut writes = WriteIds::default();
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, &mut writes);
        }
        self.record_writes(node.span, writes.0);
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        let mut writes = WriteIds::default();
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, &mut writes);
        }
        self.record_writes(node.span, writes.0);
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        let mut writes = WriteIds::default();
        record_assign_target(&node.left, &mut writes);
        self.record_writes(node.span, writes.0);
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        let mut writes = WriteIds::default();
        record_update_target(&node.arg, &mut writes);
        self.record_writes(node.span, writes.0);
        node.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        if let Some(function) = self.current_mut() {
            function.dynamic_scope = true;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(&node.callee, Callee::Expr(callee) if matches!(callee.unwrap_parens(), Expr::Ident(ident) if ident.sym == *"eval"))
            && let Some(function) = self.current_mut()
        {
            function.dynamic_scope = true;
        }
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if let MemberProp::Computed(computed) = &node.prop
            && let Expr::Ident(identifier) = computed.expr.unwrap_parens()
            && let Some(function) = self.current_mut()
        {
            function
                .member_reads
                .entry(identifier.to_id())
                .or_default()
                .push(node.span);
        }
        node.visit_children_with(self);
    }
}

impl FunctionCensus {
    fn with_class(&mut self, node: &Class, name: Option<&Ident>) {
        // Fields, decorators, and static blocks can run at different times.
        // Keep class-body references behind a conservative boundary so they
        // cannot silently mutate a surrounding loop counter.
        self.with_function(false, false, false, std::iter::empty(), |this| {
            if let Some(name) = name {
                this.record_declaration(name.to_id(), name.span);
            }
            node.visit_children_with(this);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::top_level_item_views;

    #[test]
    fn guarded_snapshot_proof_is_per_use_and_per_region() {
        let module = raw_js_test_support::parse(
            r#"
function choose(record) {
  var key, value, result;
  for (key in record)
    if (record.hasOwnProperty(key) && ((value = record[key]), value != null))
      switch (key) {
        case "ignored": result = value; break;
        case "payload": result = value; break;
        default: result = value;
      }
  key = "elsewhere";
  record = {};
  value = null;
  return result;
}
"#,
        );
        let certificates = MemberReadCertificates::collect(&top_level_item_views(&module.body));
        let properties: BTreeSet<_> = certificates
            .guarded
            .values()
            .map(|(_, property)| property.as_str())
            .collect();
        assert_eq!(properties, BTreeSet::from(["ignored", "payload"]));
        assert_eq!(certificates.guarded.len(), 2);
    }

    #[test]
    fn guarded_snapshot_proof_rejects_interference_and_fallthrough() {
        let cases = [
            // A getter can mutate captured locals before the switch.
            r#"function f(record) { let value, result; let key; function capture() { key = 'payload'; } for (key in record) { value = record[key]; switch (key) { case 'payload': result = value; break; } } }"#,
            r#"function f(record) { let value, result; for (const key in record) { value = record[key]; value = record.compare; switch (key) { case 'payload': result = value; break; } } }"#,
            r#"function f(record) { let value, result; for (const key in record) { value = record[key]; switch (key) { case 'compare': result = value; case 'payload': result = value; break; } } }"#,
            r#"function f(record) { let value, result; for (const key in record) { value = record[key]; switch (key) { case mutate(): break; case 'payload': result = value; break; } } }"#,
            r#"function f(record) { let value, result; for (const key in record) { value = record[key]; switch (key) { case 'payload': mutate(); result = value; break; } } }"#,
            r#"function f(record) { let value, result; for (const key in record) { value = record[key]; switch (key) { case 'payload': result = value; break; } } eval('record = other'); }"#,
            r#"async function f(record) { let value, result; for (const key in record) { value = record[key]; switch (key) { case 'payload': result = value; break; } } }"#,
            r#"function f(record) { let result; for (const key in record) { record = record[key]; switch (key) { case 'payload': result = record; break; } } }"#,
        ];
        for source in cases {
            let module = raw_js_test_support::parse(source);
            let certificates = MemberReadCertificates::collect(&top_level_item_views(&module.body));
            assert!(
                certificates.guarded.is_empty(),
                "unsafe guarded read: {source}"
            );
        }
    }

    #[derive(Default)]
    struct ComputedIdentifierReads(Vec<Span>);

    impl Visit for ComputedIdentifierReads {
        fn visit_member_expr(&mut self, node: &MemberExpr) {
            if matches!(&node.prop, MemberProp::Computed(computed) if matches!(computed.expr.unwrap_parens(), Expr::Ident(_)))
            {
                self.0.push(node.span);
            }
            node.visit_children_with(self);
        }
    }

    fn certified_reads(source: &str) -> (MemberReadCertificates, Vec<Span>) {
        let module = raw_js_test_support::parse(source);
        let body = top_level_item_views(&module.body);
        let certificates = MemberReadCertificates::collect(&body);
        let mut reads = ComputedIdentifierReads::default();
        module.visit_with(&mut reads);
        (certificates, reads.0)
    }

    #[test]
    fn certifies_only_bare_reads_in_a_stable_counted_loop() {
        let source = r#"
function read(items) {
  var selected;
  for (var index = 0; index < 4; index++) {
    selected = items[index];
  }
  index = "ref";
  const later = items[index];
  return [selected, later];
}
"#;
        let (certificates, reads) = certified_reads(source);
        assert_eq!(reads.len(), 2);
        assert!(certificates.contains(reads[0]));
        assert!(!certificates.contains(reads[1]));
    }

    #[test]
    fn rejects_writes_captures_dynamic_scope_and_unsupported_shapes() {
        let cases = [
            r#"function f(items) { for (let i = 0; i < 4; i++) { i = 2; items[i]; } }"#,
            r#"function f(items) { for (let i = 0; (items[i], i = 2); i++) {} }"#,
            r#"function f(items, values) { for (let i = 0; i < 4; i++) { ([i] = values); items[i]; } }"#,
            r#"function f(items, values) { for (let i = 0; i < 4; i++) { for (i of values) {} items[i]; } }"#,
            r#"function f(items, values) { for (let i = 0; i < 4; i++) { for (i in values) {} items[i]; } }"#,
            r#"function f(items) { for (let i = 0; i < 4; i++) { (() => i)(); items[i]; } }"#,
            r#"function f(items) { for (let i = 0; i < 4; i++) { (() => eval('i = 2'))(); items[i]; } }"#,
            r#"function f(items) { for (let i = 0; i < 4; i++) { with ({}) { items[i]; } } }"#,
            r#"function f(i, items) { for (var i = 0; i < 4; i++) { items[i]; } }"#,
            r#"function f(items) { for (var i = 0; i < 4; i++) { var i; items[i]; } }"#,
            r#"function f(items) { for (let i = 0; i < 4; ) { items[i]; } }"#,
            r#"function f(items) { for (let { i } = {}; i < 4; i++) { items[i]; } }"#,
            r#"async function f(items) { for (let i = 0; i < 4; i++) { items[i]; } }"#,
            r#"function* f(items) { for (let i = 0; i < 4; i++) { items[i]; } }"#,
            r#"function f(items) { class C { get value() { return i = 'ref'; } } for (var i = 0; i < 4; i++) { items[i]; } }"#,
            r#"function f(items) { class C { field = (i = 'ref'); } for (var i = 0; i < 4; i++) { items[i]; } }"#,
        ];
        for source in cases {
            let (certificates, reads) = certified_reads(source);
            assert_eq!(
                reads.len(),
                1,
                "test source should contain one computed read: {source}"
            );
            assert!(
                !certificates.contains(reads[0]),
                "unsafe loop read was certified: {source}"
            );
        }
    }

    #[test]
    fn nested_callable_reads_do_not_inherit_an_outer_certificate() {
        let source = r#"function f(items) { for (var i = 0; i < 4; i++) { const nested = () => items[i]; items[i]; } }"#;
        let (certificates, reads) = certified_reads(source);
        assert_eq!(reads.len(), 2);
        assert!(reads.iter().all(|span| !certificates.contains(*span)));
    }

    #[test]
    fn membership_uses_the_complete_member_span() {
        let source = r#"function f(items) { for (let i = 0; i < 4; i++) { items[i]; } }"#;
        let (certificates, reads) = certified_reads(source);
        let [span] = reads.as_slice() else {
            panic!("one computed member read");
        };
        assert!(certificates.contains(*span));
        let partial = Span::new(span.lo, swc_common::BytePos(span.hi.0 - 1));
        assert!(!certificates.contains(partial));

        let source = r#"function f(items, key) { for (let index = 0; index < 4; index++) { items[index][key]; } }"#;
        let (certificates, reads) = certified_reads(source);
        assert_eq!(reads.len(), 2);
        assert_eq!(reads[0].lo, reads[1].lo);
        assert!(!certificates.contains(reads[0]));
        assert!(certificates.contains(reads[1]));
    }

    #[test]
    fn unrelated_read_keys_do_not_enter_a_counter_index() {
        let mut source = String::from(
            "function f(items, key) { for (let index = 0; index < 4; index++) { items[index]; ",
        );
        for _ in 0..512 {
            source.push_str("items[key];");
        }
        source.push_str("} }");
        let (certificates, reads) = certified_reads(&source);
        assert_eq!(reads.len(), 513);
        assert!(certificates.contains(reads[0]));
        assert!(reads[1..].iter().all(|span| !certificates.contains(*span)));
    }
}
