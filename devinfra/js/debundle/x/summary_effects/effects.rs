//! Effect sets: what each code unit reads, writes and calls, read off the
//! solved points-to graph, and their closure over the call graph.

use rustc_hash::{FxHashMap, FxHashSet};

use crate::model::{
    ANY, Cx, ESC, INDEX, Model, NativeBehavior, Node, Obj, ObjKind, PropId, REASONS, Reason, Unit,
    Var, native_behavior,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Cell {
    Var(Var),
    Field(Obj, PropId),
    /// What unknown-key stores on the object wrote.
    Star(Obj),
    /// The object's numeric-keyed properties: array contents.
    Elems(Obj),
    /// Every property of the object: a computed-key read or write.
    AnyOf(Obj),
    /// The state of everything unmodeled: unknown objects, globals, other
    /// chunks' bindings, and every escaped object.
    UnknownHeap,
    Intrinsic(PropId),
    IntrinsicStar,
    AnyIntrinsic,
}

pub type CellId = u32;

#[derive(Default)]
pub struct CellTable {
    pub cells: Vec<Cell>,
    ids: FxHashMap<Cell, CellId>,
}

impl CellTable {
    pub fn id(&mut self, cell: Cell) -> CellId {
        if let Some(id) = self.ids.get(&cell) {
            return *id;
        }
        let id = self.cells.len() as CellId;
        self.cells.push(cell);
        self.ids.insert(cell, id);
        id
    }
}

/// An access is `cell << 1 | write`.
pub fn access(cell: CellId, write: bool) -> u32 {
    cell << 1 | u32::from(write)
}

#[derive(Default, Clone)]
pub struct Summary {
    /// Sorted, deduplicated accesses.
    pub accesses: Vec<u32>,
    pub reasons: u16,
    pub havoc: bool,
}

pub struct Effects {
    pub cells: CellTable,
    pub own: Vec<Summary>,
    pub calls: Vec<Vec<Unit>>,
    /// The closure of each top-level source unit over what it may call
    /// during initialization, without the Escaped pool.
    pub statements: Vec<Summary>,
    /// Everything a call into Escaped may do.
    pub escaped: Summary,
    pub reason_examples: FxHashMap<Reason, Vec<u32>>,
    pub opaque_sites: FxHashMap<Reason, u64>,
    pub builtin_sites: FxHashMap<PropId, u64>,
    pub cell_vars: FxHashSet<Var>,
    pub stats: EffectStats,
}

#[derive(Default, Debug, Clone, serde::Serialize)]
pub struct EffectStats {
    pub call_edges: usize,
    pub components: usize,
    pub largest_component: usize,
    pub summary_accesses_total: usize,
    pub largest_summary: usize,
    pub escaped_functions: usize,
    pub escaped_objects: usize,
    pub units_reaching_escaped: usize,
}

struct Collector<'a> {
    m: &'a Model,
    cells: CellTable,
    own: Vec<Summary>,
    calls: Vec<Vec<Unit>>,
    reason_examples: FxHashMap<Reason, Vec<u32>>,
    opaque_sites: FxHashMap<Reason, u64>,
    builtin_sites: FxHashMap<PropId, u64>,
}

impl Collector<'_> {
    fn touch(&mut self, unit: Unit, cell: Cell, write: bool) {
        let id = self.cells.id(cell);
        self.own[unit as usize].accesses.push(access(id, write));
    }

    fn opaque(&mut self, unit: Unit, reason: Reason, span_lo: u32) {
        // The pair check gives a unit with a reason the Escaped summary,
        // which reads and writes everything unmodeled.
        self.own[unit as usize].reasons |= reason.bit();
        *self.opaque_sites.entry(reason).or_default() += 1;
        let examples = self.reason_examples.entry(reason).or_default();
        if examples.len() < 5 {
            examples.push(span_lo);
        }
    }

    fn objects(&self, node: Node) -> Vec<Obj> {
        self.m.pts[node as usize].iter().collect()
    }

    fn read_field(&mut self, unit: Unit, obj: Obj, prop: PropId) {
        if prop == ANY {
            self.touch(unit, Cell::AnyOf(obj), false);
        } else if prop == INDEX {
            self.touch(unit, Cell::Elems(obj), false);
        } else {
            self.touch(unit, Cell::Field(obj, prop), false);
            self.touch(unit, Cell::Star(obj), false);
        }
    }

    /// A property read on `obj`: its own cell, then its prototype chain's.
    fn load(&mut self, unit: Unit, obj: Obj, prop: PropId, reason: Reason, span_lo: u32) {
        let mut pending = vec![obj];
        let mut seen = FxHashSet::default();
        while let Some(current) = pending.pop() {
            if !seen.insert(current) {
                continue;
            }
            match self.m.objs[current as usize].kind {
                ObjKind::Unknown => {
                    self.touch(unit, Cell::UnknownHeap, false);
                    self.opaque(unit, reason, span_lo);
                }
                ObjKind::Intrinsic | ObjKind::Native(_) => {}
                _ => {
                    self.read_field(unit, current, prop);
                    if self.m.objs[current as usize].escaped {
                        self.touch(unit, Cell::UnknownHeap, false);
                        self.opaque(unit, Reason::EscapedObject, span_lo);
                    }
                    if !self.m.born_with(current, prop) {
                        pending.extend(
                            self.m.pts[self.m.objs[current as usize].proto as usize].iter(),
                        );
                    }
                }
            }
        }
    }

    fn escaped_alloc(&self, obj: Obj) -> bool {
        self.m.is_alloc(obj) && self.m.objs[obj as usize].escaped
    }

    fn collect(&mut self) {
        for id in 0..self.m.cxs.len() {
            let info = &self.m.cxs[id];
            let (unit, span_lo) = (info.unit, info.span_lo);
            match &info.cx {
                Cx::Load {
                    base, prop, root, ..
                } => {
                    if self.m.pseudo.contains(prop) {
                        // The accessor lookup that shadows a real read.
                        continue;
                    }
                    // The base may be a primitive, whose methods are intrinsic.
                    if *prop == ANY {
                        self.touch(unit, Cell::AnyIntrinsic, false);
                    } else if *prop == INDEX {
                        // No intrinsic prototype has numeric-keyed properties.
                    } else {
                        self.touch(unit, Cell::Intrinsic(*prop), false);
                        self.touch(unit, Cell::IntrinsicStar, false);
                    }
                    for obj in self.objects(*base) {
                        self.load(unit, obj, *prop, root.member(), span_lo);
                    }
                }
                Cx::Store { base, prop, .. } => {
                    for obj in self.objects(*base) {
                        if self.m.is_alloc(obj) {
                            match *prop {
                                ANY => self.touch(unit, Cell::AnyOf(obj), true),
                                INDEX => self.touch(unit, Cell::Elems(obj), true),
                                prop => self.touch(unit, Cell::Field(obj, prop), true),
                            }
                            if self.m.objs[obj as usize].escaped {
                                self.opaque(unit, Reason::EscapedObject, span_lo);
                            }
                        } else {
                            if obj != ESC {
                                // The target may be an intrinsic prototype.
                                if *prop == ANY || *prop == INDEX {
                                    self.touch(unit, Cell::AnyIntrinsic, true);
                                } else {
                                    self.touch(unit, Cell::Intrinsic(*prop), true);
                                }
                            }
                            self.opaque(unit, Reason::UnknownMember, span_lo);
                        }
                    }
                }
                Cx::Call {
                    callee,
                    this,
                    args,
                    root,
                    ..
                } => {
                    for obj in self.objects(*callee) {
                        match self.m.objs[obj as usize].kind {
                            ObjKind::Function(index) => {
                                self.calls[unit as usize].push(self.m.fns[index as usize].unit);
                            }
                            ObjKind::Unknown | ObjKind::Intrinsic => {
                                self.opaque(unit, root.call(), span_lo);
                            }
                            ObjKind::Native(name) => {
                                let behavior = native_behavior(self.m.props.name(name));
                                let receiverless = this.is_none();
                                if behavior == NativeBehavior::Unknown || receiverless {
                                    let all_primitive = this
                                        .iter()
                                        .chain(args.iter().map(|arg| &arg.node))
                                        .all(|node| self.m.pts[*node as usize].is_empty());
                                    if !all_primitive {
                                        *self.builtin_sites.entry(name).or_default() += 1;
                                        self.opaque(unit, Reason::Builtin, span_lo);
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Cx::NativeReceiver { call } => {
                    let Cx::Call {
                        this: Some(this), ..
                    } = &self.m.cxs[*call as usize].cx
                    else {
                        unreachable!("a native receiver belongs to a call with a receiver");
                    };
                    for behavior in self.m.native_behaviors(*call) {
                        for receiver in self.objects(*this) {
                            if !self.m.is_alloc(receiver) {
                                if self.m.objs[receiver as usize].kind == ObjKind::Unknown
                                    || behavior.invokes_receiver()
                                {
                                    self.opaque(unit, Reason::Builtin, span_lo);
                                }
                                continue;
                            }
                            if self.m.objs[receiver as usize].escaped {
                                self.opaque(unit, Reason::EscapedObject, span_lo);
                            }
                            if behavior == NativeBehavior::OwnPropertyQuery {
                                self.touch(unit, Cell::AnyOf(receiver), false);
                            }
                        }
                    }
                }
                Cx::Coerce { closure, .. } => {
                    for obj in self.objects(*closure) {
                        match self.m.objs[obj as usize].kind {
                            ObjKind::Unknown => self.opaque(unit, Reason::Coercion, span_lo),
                            ObjKind::Intrinsic | ObjKind::Native(_) => {}
                            kind => {
                                if self.m.objs[obj as usize].escaped {
                                    self.opaque(unit, Reason::EscapedObject, span_lo);
                                }
                                if matches!(kind, ObjKind::Array | ObjKind::NativeResult) {
                                    self.touch(unit, Cell::Elems(obj), false);
                                }
                            }
                        }
                    }
                }
                Cx::Iterate { val, .. } => {
                    for obj in self.objects(*val) {
                        let info = &self.m.objs[obj as usize];
                        match info.kind {
                            ObjKind::Unknown => self.opaque(unit, Reason::Coercion, span_lo),
                            ObjKind::Intrinsic | ObjKind::Native(_) => {}
                            ObjKind::Array | ObjKind::NativeResult
                                if !info.exotic && !info.escaped =>
                            {
                                self.touch(unit, Cell::Elems(obj), false);
                            }
                            _ => {
                                self.touch(unit, Cell::AnyOf(obj), false);
                                let reason = if info.escaped {
                                    Reason::EscapedObject
                                } else {
                                    Reason::Coercion
                                };
                                self.opaque(unit, reason, span_lo);
                            }
                        }
                    }
                }
                Cx::Touch { val } => {
                    for obj in self.objects(*val) {
                        if self.m.objs[obj as usize].kind == ObjKind::Unknown {
                            self.opaque(unit, Reason::UnknownMember, span_lo);
                        } else if self.escaped_alloc(obj) {
                            self.opaque(unit, Reason::EscapedObject, span_lo);
                        }
                    }
                }
                Cx::OnAny { .. }
                | Cx::ExoticKey { .. }
                | Cx::ProtoLink { .. }
                | Cx::EscapedSink
                | Cx::ExoticSink => {}
            }
        }
    }
}

/// Variables more than one unit touches, and exported ones: the only
/// bindings whose state one statement can observe from another.
fn cell_vars(m: &Model) -> FxHashSet<Var> {
    let mut first_unit: FxHashMap<Var, Unit> = FxHashMap::default();
    let mut shared = FxHashSet::default();
    for access in &m.var_accesses {
        match first_unit.get(&access.var) {
            Some(unit) if *unit != access.unit => {
                shared.insert(access.var);
            }
            Some(_) => {}
            None => {
                first_unit.insert(access.var, access.unit);
            }
        }
    }
    for (var, info) in m.vars.iter().enumerate() {
        if info.exported {
            shared.insert(var as Var);
        }
    }
    shared
}

fn merge_sorted(into: &mut Vec<u32>, from: &[u32]) {
    into.extend_from_slice(from);
}

fn finish(accesses: &mut Vec<u32>) {
    accesses.sort_unstable();
    accesses.dedup();
}

/// Tarjan's algorithm, iteratively; components come out callees-first.
fn components(calls: &[Vec<Unit>]) -> Vec<Vec<Unit>> {
    let n = calls.len();
    let mut index = vec![u32::MAX; n];
    let mut low = vec![0u32; n];
    let mut on_stack = vec![false; n];
    let mut stack = Vec::new();
    let mut out = Vec::new();
    let mut next = 0u32;
    for root in 0..n {
        if index[root] != u32::MAX {
            continue;
        }
        let mut frames = vec![(root, 0usize)];
        index[root] = next;
        low[root] = next;
        next += 1;
        stack.push(root);
        on_stack[root] = true;
        while let Some((unit, position)) = frames.last_mut() {
            let unit = *unit;
            if let Some(callee) = calls[unit].get(*position) {
                *position += 1;
                let callee = *callee as usize;
                if index[callee] == u32::MAX {
                    index[callee] = next;
                    low[callee] = next;
                    next += 1;
                    stack.push(callee);
                    on_stack[callee] = true;
                    frames.push((callee, 0));
                } else if on_stack[callee] {
                    low[unit] = low[unit].min(index[callee]);
                }
            } else {
                frames.pop();
                if let Some((caller, _)) = frames.last() {
                    low[*caller] = low[*caller].min(low[unit]);
                }
                if low[unit] == index[unit] {
                    let mut component = Vec::new();
                    loop {
                        let member = stack.pop().expect("the root is on the stack");
                        on_stack[member] = false;
                        component.push(member as Unit);
                        if member == unit {
                            break;
                        }
                    }
                    out.push(component);
                }
            }
        }
    }
    out
}

pub fn summarize(m: &Model, statement_count: usize, escaped_unit: Unit) -> Effects {
    let unit_count = m.units.len();
    let mut collector = Collector {
        m,
        cells: CellTable::default(),
        own: vec![Summary::default(); unit_count],
        calls: vec![Vec::new(); unit_count],
        reason_examples: FxHashMap::default(),
        opaque_sites: FxHashMap::default(),
        builtin_sites: FxHashMap::default(),
    };
    let cell_vars = cell_vars(m);
    for access in &m.var_accesses {
        if cell_vars.contains(&access.var) {
            collector.touch(access.unit, Cell::Var(access.var), access.write);
        }
    }
    for (unit, info) in m.units.iter().enumerate() {
        let unit = unit as Unit;
        collector.own[unit as usize].havoc = info.havoc;
        if info.reads_unknown_heap {
            collector.touch(unit, Cell::UnknownHeap, false);
        }
        if info.writes_unknown_heap {
            collector.touch(unit, Cell::UnknownHeap, true);
        }
        for reason in REASONS {
            if info.static_reasons & reason.bit() != 0 {
                collector.opaque(unit, reason, 0);
            }
        }
    }
    collector.collect();
    // Unknown code reads and writes everything unmodeled, reads every
    // exported binding and calls every escaped function.
    collector.touch(escaped_unit, Cell::UnknownHeap, false);
    collector.touch(escaped_unit, Cell::UnknownHeap, true);
    for (var, info) in m.vars.iter().enumerate() {
        if info.exported {
            collector.touch(escaped_unit, Cell::Var(var as Var), false);
        }
    }
    for function in &m.escaped_fns {
        let unit = m
            .function(*function)
            .expect("escaped_fns holds functions")
            .unit;
        collector.calls[escaped_unit as usize].push(unit);
    }
    let Collector {
        cells,
        mut own,
        mut calls,
        reason_examples,
        opaque_sites,
        builtin_sites,
        ..
    } = collector;
    for accesses in own.iter_mut().map(|summary| &mut summary.accesses) {
        finish(accesses);
    }
    for callees in &mut calls {
        callees.sort_unstable();
        callees.dedup();
    }

    let components = components(&calls);
    let mut component_of = vec![0u32; unit_count];
    for (id, component) in components.iter().enumerate() {
        for unit in component {
            component_of[*unit as usize] = id as u32;
        }
    }
    let mut summaries: Vec<Summary> = Vec::with_capacity(components.len());
    let mut stats = EffectStats {
        call_edges: calls.iter().map(Vec::len).sum(),
        components: components.len(),
        escaped_functions: m.escaped_fns.len(),
        escaped_objects: m.objs.iter().filter(|obj| obj.escaped).count(),
        ..EffectStats::default()
    };
    for (id, component) in components.iter().enumerate() {
        let mut summary = Summary::default();
        let mut callees = FxHashSet::default();
        for unit in component {
            let unit_own = &own[*unit as usize];
            merge_sorted(&mut summary.accesses, &unit_own.accesses);
            summary.reasons |= unit_own.reasons;
            summary.havoc |= unit_own.havoc;
            for callee in &calls[*unit as usize] {
                let callee = component_of[*callee as usize];
                if callee != id as u32 {
                    callees.insert(callee);
                }
            }
        }
        for callee in callees {
            let callee = &summaries[callee as usize];
            merge_sorted(&mut summary.accesses, &callee.accesses);
            summary.reasons |= callee.reasons;
            summary.havoc |= callee.havoc;
        }
        finish(&mut summary.accesses);
        stats.largest_component = stats.largest_component.max(component.len());
        stats.summary_accesses_total += summary.accesses.len();
        stats.largest_summary = stats.largest_summary.max(summary.accesses.len());
        summaries.push(summary);
    }
    stats.units_reaching_escaped = (0..unit_count)
        .filter(|unit| summaries[component_of[*unit] as usize].reasons != 0)
        .count();
    let statements = (0..statement_count)
        .map(|unit| summaries[component_of[unit] as usize].clone())
        .collect();
    let escaped = summaries[component_of[escaped_unit as usize] as usize].clone();
    Effects {
        cells,
        own,
        calls,
        statements,
        escaped,
        reason_examples,
        opaque_sites,
        builtin_sites,
        cell_vars,
        stats,
    }
}
