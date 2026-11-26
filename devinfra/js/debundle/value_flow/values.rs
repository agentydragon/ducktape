//! Interned origin identities and the shared-bitmap `Values` set algebra.

use super::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct OriginId(pub(super) u32);

/// Per-chunk, deterministic mapping between semantic origins and dense bitmap
/// positions. The vector order preserves `Value::Ord` when decoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OriginTable {
    pub(super) origins: Vec<Value>,
    pub(super) ids: HashMap<Value, OriginId>,
    pub(super) function_class_ids: RoaringBitmap,
}

impl OriginTable {
    pub(crate) fn from_origins(origins: impl IntoIterator<Item = Value>) -> Self {
        let mut origins: Vec<_> = origins.into_iter().chain([Value::Top]).collect();
        origins.sort_unstable();
        origins.dedup();
        let ids = origins
            .iter()
            .copied()
            .enumerate()
            .map(|(index, origin)| {
                (
                    origin,
                    OriginId(u32::try_from(index).expect("chunk origin count exceeds u32")),
                )
            })
            .collect();
        let function_class_ids = origins
            .iter()
            .enumerate()
            .filter(|(_, value)| matches!(value, Value::Alloc(Kind::Function | Kind::Class, _)))
            .map(|(index, _)| u32::try_from(index).expect("origin count exceeds u32"))
            .collect();
        Self {
            origins,
            ids,
            function_class_ids,
        }
    }

    pub(super) fn id(&self, origin: Value) -> OriginId {
        *self
            .ids
            .get(&origin)
            .expect("Value must belong to this chunk")
    }

    pub(super) fn origin(&self, id: OriginId) -> Value {
        self.origins[id.0 as usize]
    }

    #[cfg(test)]
    pub(crate) fn for_expr(expr: &Expr) -> Arc<Self> {
        let mut collector = AllocationCollector::default();
        expr.visit_with(&mut collector);
        Arc::new(Self::from_origins(collector.origins))
    }
}

#[cfg(test)]
#[derive(Default)]
pub(super) struct AllocationCollector {
    pub(super) origins: BTreeSet<Value>,
}

#[cfg(test)]
impl Visit for AllocationCollector {
    fn visit_function(&mut self, node: &Function) {
        self.origins
            .insert(Value::Alloc(Kind::Function, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.origins
            .insert(Value::Alloc(Kind::Function, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_class(&mut self, node: &Class) {
        self.origins
            .insert(Value::Alloc(Kind::Class, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        self.origins
            .insert(Value::Alloc(Kind::Object, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.origins
            .insert(Value::Alloc(Kind::Object, node.span.lo.0));
        node.visit_children_with(self);
    }
}

impl Default for OriginTable {
    fn default() -> Self {
        Self::from_origins([])
    }
}

/// A set of semantic origins. All sets in one flow analysis share its immutable
/// origin table, so Roaring ids have identical meaning.
#[derive(Clone)]
pub struct Values {
    pub(super) bitmap: Arc<RoaringBitmap>,
    pub(super) unknown: bool,
    pub(super) origins: Arc<OriginTable>,
}

impl PartialEq for Values {
    fn eq(&self, other: &Self) -> bool {
        self.bitmap == other.bitmap
            && self.unknown == other.unknown
            && self.origins == other.origins
    }
}

impl Eq for Values {}

impl std::fmt::Debug for Values {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.iter()).finish()
    }
}

impl Values {
    pub(crate) fn empty(origins: Arc<OriginTable>) -> Self {
        Self {
            bitmap: Arc::new(RoaringBitmap::new()),
            unknown: false,
            origins,
        }
    }

    pub(crate) fn singleton(origins: Arc<OriginTable>, value: Value) -> Self {
        let mut values = Self::empty(origins);
        values.insert(value);
        values
    }

    pub fn is_empty(&self) -> bool {
        self.bitmap.is_empty() && !self.unknown
    }

    #[cfg(test)]
    pub(super) fn len(&self) -> usize {
        self.bitmap.len() as usize + usize::from(self.unknown)
    }

    #[cfg(test)]
    pub(crate) fn bitmap_payload_bytes(&self) -> u64 {
        let statistics = self.bitmap.statistics();
        statistics.n_bytes_array_containers
            + statistics.n_bytes_bitset_containers
            + statistics.n_bytes_run_containers
    }

    #[cfg(test)]
    pub(crate) fn shares_bitmap(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.bitmap, &other.bitmap)
    }

    #[cfg(test)]
    pub(crate) fn contains(&self, value: &Value) -> bool {
        if *value == Value::Top {
            return self.unknown;
        }
        let Some(id) = self.origins.ids.get(value) else {
            return false;
        };
        self.bitmap.contains(id.0)
    }

    pub(crate) fn insert(&mut self, value: Value) -> bool {
        if value == Value::Top {
            return !std::mem::replace(&mut self.unknown, true);
        }
        let id = self.origins.id(value).0;
        Arc::make_mut(&mut self.bitmap).insert(id)
    }

    pub fn iter(&self) -> ValuesIter<'_> {
        ValuesIter {
            ids: self.bitmap.iter(),
            origins: &self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }

    /// Merge another value set, returning whether it contributed new origins.
    pub(crate) fn union_with(&mut self, other: &Self) -> bool {
        assert!(Arc::ptr_eq(&self.origins, &other.origins));
        let mut changed = union_bitmap(&mut self.bitmap, &other.bitmap);
        if other.unknown && !self.unknown {
            self.unknown = true;
            changed = true;
        }
        changed
    }

    pub(super) fn without_function_class(&self) -> Self {
        if self.bitmap.is_disjoint(&self.origins.function_class_ids) {
            return self.clone();
        }
        Self {
            bitmap: Arc::new(self.bitmap.as_ref() - &self.origins.function_class_ids),
            unknown: self.unknown,
            origins: self.origins.clone(),
        }
    }

    pub(super) fn difference(&self, delivered: &Self) -> Self {
        assert!(Arc::ptr_eq(&self.origins, &delivered.origins));
        Self {
            bitmap: Arc::new(self.bitmap.as_ref() - delivered.bitmap.as_ref()),
            unknown: self.unknown && !delivered.unknown,
            origins: self.origins.clone(),
        }
    }

    pub(super) fn union(mut self, other: Self) -> Self {
        self.union_with(&other);
        self
    }
}

pub(super) fn union_bitmap(into: &mut Arc<RoaringBitmap>, from: &Arc<RoaringBitmap>) -> bool {
    union_bitmap_observed(into, from, |_| {})
}

pub(super) fn union_bitmap_observed(
    into: &mut Arc<RoaringBitmap>,
    from: &Arc<RoaringBitmap>,
    observe: impl FnOnce(work_metrics::Operation),
) -> bool {
    use work_metrics::Operation;
    if Arc::ptr_eq(into, from) {
        observe(Operation::BitmapPointerSkip);
        return false;
    }
    if from.is_subset(into) {
        observe(Operation::BitmapSubsetSkip);
        return false;
    }
    if into.is_subset(from) {
        observe(Operation::BitmapSupersetAdoption);
        *into = from.clone();
        return true;
    }
    observe(Operation::BitmapCopyOnWriteUnion);
    *Arc::make_mut(into) |= from.as_ref();
    true
}

pub(super) fn merge_values_with_scratch(
    current: &mut Values,
    contribution: Values,
    scratch: &mut FlowMergeScratch,
    observe: impl FnMut(work_metrics::Operation),
) -> bool {
    assert!(Arc::ptr_eq(&current.origins, &contribution.origins));
    let bitmap_changed = scratch.merge_bitmap(&mut current.bitmap, &contribution.bitmap, observe);
    let unknown_changed = contribution.unknown && !current.unknown;
    current.unknown |= contribution.unknown;
    bitmap_changed || unknown_changed
}

/// Per-rule-activation memo for the exact ordered bitmap joins that can recur
/// between a source write's receiver-specific and object-wide destinations.
/// Weak input witnesses avoid retaining old bitmap payloads; only at most eight
/// computed union outputs can be held strongly until this activation ends.
pub(super) const FLOW_JOIN_MEMO_CAPACITY: usize = 8;

#[derive(Clone)]
pub(super) enum BitmapJoinResult {
    SubsetSkip,
    SupersetAdoption,
    Union(Arc<RoaringBitmap>),
}

pub(super) struct BitmapJoinMemoEntry {
    pub(super) into_address: usize,
    pub(super) from_address: usize,
    pub(super) into: Weak<RoaringBitmap>,
    pub(super) from: Weak<RoaringBitmap>,
    pub(super) result: BitmapJoinResult,
}

pub(super) struct FlowMergeScratch {
    pub(super) entries: [Option<BitmapJoinMemoEntry>; FLOW_JOIN_MEMO_CAPACITY],
    pub(super) next: usize,
    #[cfg(test)]
    pub(super) work: Option<Rc<RefCell<FlowWorkStats>>>,
}

impl Default for FlowMergeScratch {
    fn default() -> Self {
        Self {
            entries: std::array::from_fn(|_| None),
            next: 0,
            #[cfg(test)]
            work: None,
        }
    }
}

impl FlowMergeScratch {
    #[cfg(test)]
    pub(super) fn with_work(work: Rc<RefCell<FlowWorkStats>>) -> Self {
        Self {
            work: Some(work),
            ..Self::default()
        }
    }

    pub(super) fn merge_bitmap(
        &mut self,
        into: &mut Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        mut observe: impl FnMut(work_metrics::Operation),
    ) -> bool {
        use work_metrics::Operation;
        if Arc::ptr_eq(into, from) {
            observe(Operation::BitmapPointerSkip);
            return false;
        }
        if let Some(result) = self.lookup(into, from) {
            #[cfg(test)]
            self.record_work(|work| work.bitmap_join_memo_hits += 1);
            observe(Operation::BitmapJoinMemoHit);
            return Self::apply(into, from, result);
        }

        #[cfg(test)]
        self.record_work(|work| work.bitmap_join_memo_misses += 1);
        observe(Operation::BitmapJoinMemoMiss);
        #[cfg(test)]
        self.record_work(|work| work.bitmap_subset_checks += 1);
        if from.is_subset(into) {
            observe(Operation::BitmapSubsetSkip);
            self.remember(into, from, BitmapJoinResult::SubsetSkip);
            return false;
        }

        #[cfg(test)]
        self.record_work(|work| work.bitmap_subset_checks += 1);
        if into.is_subset(from) {
            observe(Operation::BitmapSupersetAdoption);
            self.remember(into, from, BitmapJoinResult::SupersetAdoption);
            *into = from.clone();
            return true;
        }

        // Arc::make_mut clones a shared bitmap and dissociates Weak witnesses
        // before mutating a uniquely-owned allocation. Cache the old pair only
        // when another strong owner guarantees the witness remains stable.
        let cache_old_pair = Arc::strong_count(into) > 1;
        let into_witness =
            cache_old_pair.then(|| (Arc::as_ptr(into) as usize, Arc::downgrade(into)));
        #[cfg(test)]
        self.record_work(|work| work.bitmap_copy_on_write_unions += 1);
        *Arc::make_mut(into) |= from.as_ref();
        if let Some((into_address, into_witness)) = into_witness {
            self.remember_witnessed(
                into_address,
                into_witness,
                from,
                BitmapJoinResult::Union(into.clone()),
            );
        }
        observe(Operation::BitmapCopyOnWriteUnion);
        true
    }

    pub(super) fn lookup(
        &self,
        into: &Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
    ) -> Option<BitmapJoinResult> {
        let into_address = Arc::as_ptr(into) as usize;
        let from_address = Arc::as_ptr(from) as usize;
        self.entries.iter().flatten().find_map(|entry| {
            if entry.into_address != into_address || entry.from_address != from_address {
                return None;
            }
            let witnessed_into = entry.into.upgrade()?;
            let witnessed_from = entry.from.upgrade()?;
            (Arc::ptr_eq(&witnessed_into, into) && Arc::ptr_eq(&witnessed_from, from))
                .then(|| entry.result.clone())
        })
    }

    pub(super) fn remember(
        &mut self,
        into: &Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) {
        self.remember_witnessed(
            Arc::as_ptr(into) as usize,
            Arc::downgrade(into),
            from,
            result,
        );
    }

    pub(super) fn remember_witnessed(
        &mut self,
        into_address: usize,
        into: Weak<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) {
        self.entries[self.next] = Some(BitmapJoinMemoEntry {
            into_address,
            from_address: Arc::as_ptr(from) as usize,
            into,
            from: Arc::downgrade(from),
            result,
        });
        self.next = (self.next + 1) % FLOW_JOIN_MEMO_CAPACITY;
    }

    pub(super) fn apply(
        into: &mut Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) -> bool {
        match result {
            BitmapJoinResult::SubsetSkip => false,
            BitmapJoinResult::SupersetAdoption => {
                *into = from.clone();
                true
            }
            BitmapJoinResult::Union(union) => {
                *into = union;
                true
            }
        }
    }

    #[cfg(test)]
    pub(super) fn record_work(&self, update: impl FnOnce(&mut FlowWorkStats)) {
        if let Some(work) = &self.work {
            update(&mut work.borrow_mut());
        }
    }
}

impl Extend<Value> for Values {
    fn extend<T: IntoIterator<Item = Value>>(&mut self, iter: T) {
        for origin in iter {
            self.insert(origin);
        }
    }
}

pub struct ValuesIter<'a> {
    pub(super) ids: RoaringIter<'a>,
    pub(super) origins: &'a OriginTable,
    pub(super) unknown: bool,
    pub(super) yielded_unknown: bool,
}

impl Iterator for ValuesIter<'_> {
    type Item = Value;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.ids.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(Value::Top);
        }
        None
    }
}

pub struct ValuesIntoIter {
    pub(super) ids: RoaringIntoIter,
    pub(super) origins: Arc<OriginTable>,
    pub(super) unknown: bool,
    pub(super) yielded_unknown: bool,
}

impl Iterator for ValuesIntoIter {
    type Item = Value;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.ids.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(Value::Top);
        }
        None
    }
}

impl IntoIterator for Values {
    type Item = Value;
    type IntoIter = ValuesIntoIter;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIntoIter {
            ids: Arc::unwrap_or_clone(self.bitmap).into_iter(),
            origins: self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }
}

impl<'a> IntoIterator for &'a Values {
    type Item = Value;
    type IntoIter = ValuesIter<'a>;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIter {
            ids: self.bitmap.iter(),
            origins: &self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }
}

/// Accumulate one expression directly into the compressed set. In particular,
/// a union does not materialize the result of each child union along the way.
pub(super) struct ValuesBuilder {
    pub(super) values: Values,
    #[cfg(test)]
    pub(super) work: Rc<Cell<usize>>,
}

impl ValuesBuilder {
    pub(super) fn new(origins: Arc<OriginTable>) -> Self {
        Self {
            values: Values::empty(origins),
            #[cfg(test)]
            work: Rc::new(Cell::new(0)),
        }
    }

    #[cfg(test)]
    pub(super) fn with_work(origins: Arc<OriginTable>, work: Rc<Cell<usize>>) -> Self {
        Self {
            values: Values::empty(origins),
            work,
        }
    }

    pub(super) fn child(&self) -> Self {
        #[cfg(test)]
        {
            Self {
                values: Values::empty(self.values.origins.clone()),
                work: self.work.clone(),
            }
        }
        #[cfg(not(test))]
        {
            Self::new(self.values.origins.clone())
        }
    }

    pub(super) fn push(&mut self, value: Value) {
        self.values.insert(value);
        #[cfg(test)]
        {
            self.work.set(self.work.get() + 1);
        }
    }

    pub(super) fn extend(&mut self, values: &Values) {
        if values.is_empty() {
            return;
        }
        assert!(Arc::ptr_eq(&self.values.origins, &values.origins));
        self.values.union_with(values);
        #[cfg(test)]
        {
            self.work.set(self.work.get() + values.len());
        }
    }

    pub(super) fn finish(self) -> Values {
        self.values
    }
}
