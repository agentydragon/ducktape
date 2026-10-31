//! Heap-content cells each body touches: a top-level statement's own at-init
//! syntax and each callable's first-order pre-await body.
//!
//! This pass deliberately does not classify calls as pure. It records only
//! which array or certified native Set allocation an operation may observe or
//! mutate, using the shared chunk value-flow identities. The synchronous
//! interpreter composes a statement's calls: it unions the bodies of the
//! callables it invokes (`SynchronousCalls::heap_contents`), and an execution
//! it cannot complete is a heap-frontier barrier in the graph emitter.

use super::*;
use crate::facts::abstract_domain::value::{Kind, Value};

pub(crate) struct HeapContentProofs<'a> {
    pub local_class: &'a LocalClassHeapProof,
    pub set_iterable: &'a SetIterableProof,
    pub fresh_freeze: &'a sync_effects::execution_index::interpreter::FreshRecordFreezeHeapProof,
    pub native_reads: &'a sync_effects::execution_index::interpreter::PreparedSynchronousCallIndex,
    pub native_facts: &'a ValidatedNativeOperationFacts,
}

/// The heap-content cells of a chunk's bodies, and the report uses of the
/// native-read certificates the collection consumed per statement.
pub(crate) struct ChunkHeapContents {
    /// First-order pre-await body cells of each callable, keyed like the
    /// execution index's callables (the body's span start).
    pub callables: BTreeMap<u32, HeapContentCells>,
    pub native_read_uses: Vec<(
        Vec<crate::reports::schema::InferenceUse>,
        Vec<crate::reports::schema::NativeOperationInputUse>,
    )>,
}

pub(crate) fn annotate_heap_contents(
    body: &[TopLevelItemView<'_>],
    value_flow: &ChunkValueFlow,
    proofs: HeapContentProofs<'_>,
    facts: &mut [StatementFacts],
) -> ChunkHeapContents {
    let mut origins = ArrayOriginCollector::default();
    for item in body {
        item.as_module_item().visit_with(&mut origins);
    }
    let mut candidates = array_candidate_roots_from_origins(&origins.0);
    candidates.extend(
        value_flow
            .native_set_allocations()
            .keys()
            .copied()
            .map(|site| Value::Alloc(Kind::Set, site)),
    );
    let reachability = value_flow.candidate_object_index(&candidates);
    let mut callables = BTreeMap::new();
    let mut native_read_uses = Vec::with_capacity(body.len());
    for (item, fact) in body.iter().zip(facts) {
        let mut collector = ArrayContentCollector {
            value_flow,
            local_class_heap_proof: proofs.local_class,
            set_iterable_proof: proofs.set_iterable,
            fresh_freeze_proof: proofs.fresh_freeze,
            native_reads: proofs.native_reads,
            native_facts: proofs.native_facts,
            array_reachability: &reachability,
            array_origins: &origins.0,
            eager: HeapContentCells::default(),
            callables: &mut callables,
            deferred_allocations: BTreeSet::new(),
            bodies: Vec::new(),
            heap_native_integrity_uses: BTreeSet::new(),
            native_read_inferences: BTreeMap::new(),
            native_read_inputs: BTreeMap::new(),
            lazy_depth: 0,
            past_await: false,
            may_skip_await_depth: 0,
        };
        item.as_module_item().visit_with(&mut collector);
        if fact.kind == StatementKind::Export {
            collector.eager.opaque = true;
        }
        fact.heap_one_shot_allocation =
            var_decl_of_item(item.as_module_item()).and_then(|declaration| {
                match declaration.decls.as_slice() {
                    [declarator] if matches!(declarator.name, Pat::Ident(_)) => {
                        match declarator.init.as_deref()?.unwrap_parens() {
                            Expr::Array(array) => Some(EffectCell::ArrayContents(array.span.lo.0)),
                            _ => None,
                        }
                    }
                    _ => None,
                }
            });
        fact.heap_contents = collector.eager;
        fact.deferred_heap_allocations = collector.deferred_allocations;
        fact.heap_native_integrity_uses =
            collector.heap_native_integrity_uses.into_iter().collect();
        native_read_uses.push((
            collector.native_read_inferences.into_values().collect(),
            collector.native_read_inputs.into_values().collect(),
        ));
    }
    ChunkHeapContents {
        callables,
        native_read_uses,
    }
}

pub(crate) fn array_candidate_roots(body: &[TopLevelItemView<'_>]) -> BTreeSet<Value> {
    let mut origins = ArrayOriginCollector::default();
    for item in body {
        item.as_module_item().visit_with(&mut origins);
    }
    array_candidate_roots_from_origins(&origins.0)
}

fn array_candidate_roots_from_origins(origins: &BTreeSet<u32>) -> BTreeSet<Value> {
    origins
        .iter()
        .copied()
        .map(|site| Value::Alloc(Kind::Object, site))
        .collect()
}

#[derive(Default)]
struct ArrayOriginCollector(BTreeSet<u32>);

impl Visit for ArrayOriginCollector {
    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.0.insert(node.span.lo.0);
        node.visit_children_with(self);
    }
}

struct ArrayContentCollector<'a> {
    value_flow: &'a ChunkValueFlow,
    local_class_heap_proof: &'a LocalClassHeapProof,
    set_iterable_proof: &'a SetIterableProof,
    fresh_freeze_proof: &'a sync_effects::execution_index::interpreter::FreshRecordFreezeHeapProof,
    native_reads:
        &'a super::sync_effects::execution_index::interpreter::PreparedSynchronousCallIndex,
    native_facts: &'a ValidatedNativeOperationFacts,
    array_reachability: &'a CandidateObjectIndex<'a>,
    array_origins: &'a BTreeSet<u32>,
    /// The statement's own at-init cells.
    eager: HeapContentCells,
    callables: &'a mut BTreeMap<u32, HeapContentCells>,
    /// Allocation sites in any deferred position: their allocation may run
    /// any number of times, at or after this statement.
    deferred_allocations: BTreeSet<EffectCell>,
    /// The bodies entered around the cursor, innermost last, each with the
    /// lazy depth its code runs at: an execution-index callable key, or `None`
    /// for an object-literal accessor body, which the interpreter does not
    /// invoke as a callable. A class member's eager parts run in the
    /// enclosing body although its entry is already pushed.
    bodies: Vec<(Option<u32>, u32)>,
    heap_native_integrity_uses: BTreeSet<crate::reports::schema::NativeIntegrityUse>,
    native_read_inferences: BTreeMap<u32, crate::reports::schema::InferenceUse>,
    native_read_inputs: BTreeMap<u32, crate::reports::schema::NativeOperationInputUse>,
    lazy_depth: u32,
    past_await: bool,
    may_skip_await_depth: u32,
}

impl ArrayContentCollector<'_> {
    /// The cells the cursor's position records into: the statement's at-init
    /// cells, the innermost callable's first-order pre-await body, or none
    /// for code that runs only after a suspension or in an accessor body.
    fn cells(&mut self) -> Option<&mut HeapContentCells> {
        if self.lazy_depth == 0 {
            return Some(&mut self.eager);
        }
        if self.past_await {
            return None;
        }
        let (key, depth) = *self
            .bodies
            .iter()
            .rev()
            .find(|(_, depth)| *depth <= self.lazy_depth)?;
        // Code nested in a body the collector does not key (a getter of an
        // object literal inside a class field) is no callable's body.
        let key = key.filter(|_| depth == self.lazy_depth)?;
        Some(self.callables.entry(key).or_default())
    }

    fn record(&mut self, ids: &BTreeSet<u32>, read: bool, write: bool, opaque: bool) {
        let Some(cells) = self.cells() else {
            return;
        };
        if read {
            cells
                .reads
                .extend(ids.iter().map(|id| EffectCell::ArrayContents(*id)));
        }
        if write {
            cells
                .writes
                .extend(ids.iter().map(|id| EffectCell::ArrayContents(*id)));
        }
        cells.opaque |= opaque;
    }

    fn record_opaque(&mut self) {
        if let Some(cells) = self.cells() {
            cells.opaque = true;
        }
    }

    fn record_allocation(&mut self, cell: EffectCell) {
        if self.lazy_depth > 0 {
            self.deferred_allocations.insert(cell.clone());
        }
        if let Some(cells) = self.cells() {
            cells.allocations.insert(cell);
        }
    }

    /// Visit a callable as `key`; its body runs one lazy level deeper.
    fn descend_body(&mut self, key: Option<u32>, visit: impl FnOnce(&mut Self)) {
        self.bodies.push((key, self.lazy_depth + 1));
        visit(self);
        self.bodies.pop();
    }

    fn array_ids(&self, expr: &Expr) -> (BTreeSet<u32>, bool) {
        let values = self.value_flow.values_of_expr(expr);
        let mut arrays = BTreeSet::new();
        let mut unknown = false;
        let mut possible = 0usize;
        for value in values {
            match value {
                Value::Alloc(Kind::Object, object) if self.array_origins.contains(&object) => {
                    arrays.insert(object);
                    possible += 1;
                }
                Value::Top => unknown = true,
                _ => possible += 1,
            }
        }
        let ambiguous = unknown || possible > 1 || arrays.len() > 1;
        (arrays, ambiguous)
    }

    fn record_set_expr(&mut self, expr: &Expr, read: bool, write: bool, opaque: bool) {
        let values = self.value_flow.values_of_expr(expr);
        let mut sets = BTreeSet::new();
        let mut unknown = false;
        let mut count = 0;
        for value in values {
            match value {
                Value::Alloc(Kind::Set, site) => {
                    sets.insert(site);
                    count += 1;
                }
                Value::Top => unknown = true,
                _ => count += 1,
            }
        }
        let ambiguous = unknown || count > 1;
        // An unknown receiver may alias any certified Set, while an unrelated
        // receiver must not create a Set frontier merely for being a member.
        let opaque = (!sets.is_empty() && (opaque || ambiguous))
            || (unknown && !self.value_flow.native_set_allocations().is_empty());
        let Some(cells) = self.cells() else {
            return;
        };
        if read {
            cells
                .reads
                .extend(sets.iter().map(|site| EffectCell::SetContents(*site)));
        }
        if write {
            cells
                .writes
                .extend(sets.iter().map(|site| EffectCell::SetContents(*site)));
        }
        cells.opaque |= opaque;
    }

    fn record_array_expr(&mut self, expr: &Expr, read: bool, write: bool, opaque: bool) {
        let (arrays, ambiguous) = self.array_ids(expr);
        if arrays.is_empty() {
            if ambiguous {
                self.record_opaque();
            }
            return;
        }
        self.record(&arrays, read, write, opaque || ambiguous);
    }

    fn record_escaped_expr(&mut self, expr: &Expr) {
        let values = self.value_flow.values_of_expr(expr);
        let mut arrays = BTreeSet::new();
        self.array_reachability
            .for_values(&values, |origin| match origin {
                Value::Alloc(Kind::Object, object) if self.array_origins.contains(&object) => {
                    arrays.insert(EffectCell::ArrayContents(object));
                }
                Value::Alloc(Kind::Set, site) => {
                    arrays.insert(EffectCell::SetContents(site));
                }
                _ => {}
            });
        if !arrays.is_empty()
            && let Some(cells) = self.cells()
        {
            cells.reads.extend(arrays.iter().cloned());
            cells.writes.extend(arrays);
            cells.opaque = true;
        }
    }

    fn member_content_kind(&self, member: &MemberExpr) -> Option<bool> {
        match &member.prop {
            MemberProp::Ident(name) if name.sym == "length" => Some(false),
            MemberProp::Ident(name) if is_array_index(name.sym.as_ref()) => Some(false),
            MemberProp::Ident(_) => Some(true),
            MemberProp::Computed(prop) => match prop.expr.unwrap_parens() {
                Expr::Lit(Lit::Str(value)) if value.value.to_string_lossy() == "length" => {
                    Some(false)
                }
                Expr::Lit(Lit::Str(value)) if is_array_index(&value.value.to_string_lossy()) => {
                    Some(false)
                }
                Expr::Lit(Lit::Num(value)) if is_numeric_index(value.value) => Some(false),
                // A dynamic key may name an index or `length`, and a getter
                // or setter can run. Track the receiver and make it opaque.
                _ => Some(true),
            },
            MemberProp::PrivateName(_) => None,
        }
    }

    fn visit_member_children(&mut self, member: &MemberExpr) {
        member.obj.visit_with(self);
        if let MemberProp::Computed(prop) = &member.prop {
            prop.expr.visit_with(self);
        }
    }

    fn record_member_read(&mut self, member: &MemberExpr) {
        if let Some((proof, evidence, _)) = self.native_reads.native_own_data_read(member.span) {
            self.native_read_inferences.insert(member.span.lo.0, proof);
            if let Some(evidence) = evidence {
                self.native_read_inputs.insert(
                    member.span.lo.0,
                    crate::reports::schema::NativeOperationInputUse {
                        evidence,
                        source_location: None,
                        span: member.span,
                    },
                );
            }
            return;
        }
        self.record_set_expr(&member.obj, true, false, true);
        if let Some(opaque) = self.member_content_kind(member) {
            self.record_array_expr(&member.obj, true, false, opaque);
        }
    }

    fn record_member_write(&mut self, member: &MemberExpr, read: bool) {
        self.record_set_expr(&member.obj, read, true, true);
        if self.member_content_kind(member).is_some() {
            // A static key identifies which array's contents are involved,
            // but it does not prove an own writable data property. The write
            // may hit an inherited setter or a descriptor changed after the
            // array escaped. Keep the exact cell plus the opaque barrier;
            // trusted binding/global summaries do not assert descriptor
            // integrity.
            self.record_array_expr(&member.obj, read, true, true);
        }
    }
}

impl LazyBoundary for ArrayContentCollector<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }

    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
    }
}

impl Visit for ArrayContentCollector<'_> {
    fn visit_array_lit(&mut self, node: &ArrayLit) {
        // Materialization initializes this fresh identity's contents. Keep an
        // allocation cell even when all later observations use an unsupported
        // protocol and therefore cannot name a precise array read.
        self.record_allocation(EffectCell::ArrayContents(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        self.record_member_read(node);
        self.visit_member_children(node);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if let Some(member) = assign_member_target(&node.left) {
            self.record_member_write(member, !matches!(node.op, AssignOp::Assign));
            self.record_escaped_expr(&node.right);
            self.visit_member_children(member);
        } else {
            node.left.visit_with(self);
        }
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        if let Expr::Member(member) = node.arg.unwrap_parens() {
            self.record_member_write(member, true);
            self.visit_member_children(member);
        } else {
            node.arg.visit_with(self);
        }
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && let Expr::Member(member) = node.arg.unwrap_parens()
        {
            self.record_member_write(member, false);
            self.visit_member_children(member);
        } else {
            node.arg.visit_with(self);
        }
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if self.fresh_freeze_proof.contains(node)
            && let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            // The selected original Object.freeze observes only this new
            // accessor-free record. Its callee slots are author-certified;
            // visit the callee's children and property values to retain every
            // nested call, allocation, read, and write in their evaluation.
            self.visit_member_children(member);
            for arg in &node.args {
                arg.visit_with(self);
            }
            return;
        }
        // This exact original native call cannot touch array contents or
        // invoke a callback. The same validated site proves item purity.
        if self.native_facts.symbol_for_literal_proof(node).is_some() {
            return;
        }
        match &node.callee {
            Callee::Expr(callee) if let Expr::Member(member) = callee.unwrap_parens() => {
                self.record_set_expr(&member.obj, true, true, true);
                self.record_array_expr(&member.obj, true, true, true);
                self.visit_member_children(member);
                self.record_opaque();
            }
            Callee::Expr(callee) => {
                let callee_values = self.value_flow.values_of_expr(callee);
                let callee_origins: Vec<_> = callee_values.iter().collect();
                let is_direct_identifier = matches!(callee.unwrap_parens(), Expr::Ident(_));
                let known_local = is_direct_identifier
                    && !callee_origins.is_empty()
                    && callee_origins
                        .iter()
                        .all(|value| matches!(value, Value::Alloc(Kind::Function, _)));
                if !known_local {
                    self.record_opaque();
                }
                callee.visit_with(self);
            }
            Callee::Import(_) | Callee::Super(_) => self.record_opaque(),
        }
        for arg in &node.args {
            self.record_escaped_expr(&arg.expr);
            self.record_array_expr(&arg.expr, true, true, true);
            arg.visit_with(self);
        }
    }

    fn visit_opt_call(&mut self, node: &OptCall) {
        // Optional invocation can reach a callable whose captured heap effects
        // are not represented by this source-unit summary.
        self.record_opaque();
        node.visit_children_with(self);
    }

    fn visit_tagged_tpl(&mut self, node: &TaggedTpl) {
        // A tag is a call with an arbitrary protocol. The synchronous-call
        // interpreter also records its precision loss; retain the barrier
        // here for analyses that do not request that interpreter pass.
        self.record_opaque();
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        let certified_set = self
            .value_flow
            .native_set_allocations()
            .contains_key(&node.span.lo.0);
        if certified_set {
            self.record_allocation(EffectCell::SetContents(node.span.lo.0));
        }
        if certified_set {
            // The exact author premise covers the constructor and the dense
            // primitive array's iterator/next protocol at this site.
        } else if let Some(dependencies) = self
            .local_class_heap_proof
            .dependencies_for_construction(node.span)
        {
            if !dependencies.is_empty() {
                self.heap_native_integrity_uses.insert(
                    crate::reports::schema::NativeIntegrityUse {
                        guard: crate::reports::schema::NativeIntegrityGuard::LocalClassConstruction,
                        value_input_refs: dependencies.value_inputs.clone(),
                        no_accessor_install_refs: dependencies
                            .no_accessor_install_callables
                            .clone(),
                        program_premises: dependencies.program_premises.clone(),
                    },
                );
            }
        } else {
            // Only this operation's body is certified. Other invocations of
            // the same callee still contribute their own promotion/barrier.
            self.record_opaque();
        }
        node.callee.visit_with(self);
        if let Some(args) = &node.args {
            for arg in args {
                // A certified constructor consumes one immediate fresh array
                // without retaining it. Its own read cannot observe earlier
                // heap state. Visit children below to retain spread-source
                // reads and every nested allocation/effect.
                if !certified_set {
                    self.record_escaped_expr(&arg.expr);
                    self.record_array_expr(&arg.expr, true, true, true);
                }
                arg.visit_with(self);
            }
        }
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.record_set_expr(&node.right, true, false, true);
        self.record_array_expr(&node.right, true, false, true);
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        // Enumerating keys can observe array indices, including holes and
        // keys installed through inherited properties. It is a heap read
        // even though it has no MemberExpr in the source.
        self.record_array_expr(&node.right, true, false, true);
        node.visit_children_with(self);
    }

    fn visit_spread_element(&mut self, node: &SpreadElement) {
        let certified = self
            .set_iterable_proof
            .stable_spread_dependencies(node.expr.span())
            .is_some();
        self.record_set_expr(&node.expr, true, false, !certified);
        self.record_array_expr(&node.expr, true, false, !certified);
        node.expr.visit_with(self);
    }

    fn visit_expr_or_spread(&mut self, node: &ExprOrSpread) {
        if let Some(token) = node.spread {
            // A certified primitive-array source reads its exact allocation
            // contents. Unsupported iterables retain the opaque heap barrier.
            let certified = self
                .set_iterable_proof
                .stable_array_spread_dependencies(token)
                .is_some();
            self.record_array_expr(&node.expr, true, false, !certified);
        }
        node.expr.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if matches!(node.name, Pat::Array(_) | Pat::Object(_))
            && let Some(init) = &node.init
        {
            self.record_array_expr(init, true, false, true);
        }
        node.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        self.record_opaque();
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        self.descend_body(Some(node.span.lo.0), |collector| {
            lazy_visit_function(collector, node);
        });
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.descend_body(Some(node.span.lo.0), |collector| {
            lazy_visit_arrow_expr(collector, node);
        });
    }

    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        self.descend_body(None, |collector| lazy_visit_getter_prop(collector, node));
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        self.descend_body(None, |collector| lazy_visit_setter_prop(collector, node));
    }

    fn visit_if_stmt(&mut self, node: &IfStmt) {
        lazy_visit_if_stmt(self, node);
    }

    fn visit_cond_expr(&mut self, node: &CondExpr) {
        lazy_visit_cond_expr(self, node);
    }

    fn visit_bin_expr(&mut self, node: &BinExpr) {
        lazy_visit_bin_expr(self, node);
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.visit_children_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        // The execution index keys a constructor and an instance field or
        // accessor initializer by the member's span; methods by their
        // function, which `visit_function` enters.
        let key = match member {
            ClassMember::Constructor(constructor) => constructor.span.lo.0,
            ClassMember::ClassProp(prop) if !prop.is_static => prop.span.lo.0,
            ClassMember::PrivateProp(prop) if !prop.is_static => prop.span.lo.0,
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => accessor.span.lo.0,
            _ => {
                lazy_visit_class_member(self, member);
                return;
            }
        };
        self.descend_body(Some(key), |collector| {
            lazy_visit_class_member(collector, member);
        });
    }
}

fn is_array_index(value: &str) -> bool {
    value
        .parse::<u32>()
        .is_ok_and(|index| index != u32::MAX && index.to_string() == value)
}

fn is_numeric_index(value: f64) -> bool {
    value.is_finite() && value >= 0.0 && value < u32::MAX as f64 && value.fract() == 0.0
}

fn assign_member_target(target: &AssignTarget) -> Option<&MemberExpr> {
    let AssignTarget::Simple(simple) = target else {
        return None;
    };
    match simple {
        SimpleAssignTarget::Member(member) => Some(member),
        SimpleAssignTarget::Paren(paren) => match paren.expr.unwrap_parens() {
            Expr::Member(member) => Some(member),
            _ => None,
        },
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::{is_array_index, is_numeric_index};

    #[test]
    fn array_index_keys_match_ecmascript_index_range() {
        assert!(is_array_index("0"));
        assert!(is_array_index("4294967294"));
        assert!(!is_array_index("4294967295"));
        assert!(!is_array_index("01"));

        assert!(is_numeric_index(0.0));
        assert!(is_numeric_index(4_294_967_294.0));
        assert!(!is_numeric_index(4_294_967_295.0));
        assert!(!is_numeric_index(1.5));
    }
}
