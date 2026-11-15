//! Temporary observation-only census. No values, Arcs, Worlds or cells are retained.
//! Every production walk runs; shadow reuse is checked only after recomputation.

use super::super::value_model::{
    AbstractValue, CallSite, HeapAddress, InstanceOrigin, LexicalCellId, RetainedResultKind,
    SymbolicInputId, Values,
};
use super::super::world::World;
use crate::facts::abstract_domain::value::Value;
use serde::Serialize;
use source_identity::SourceUnitOrdinal;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Token {
    Value(Value),
    Symbolic(SymbolicInputId),
    Closure(HeapAddress, usize),
    Class(u32),
    Instance(u32, usize, InstanceOrigin),
    Deferred(usize),
    Audited(RetainedResultKind, usize),
    Descriptor(CallSite),
    CallbackFactory,
    InlineCallbackFactory,
    ForeignRecord,
    ObjectAssign,
    ArrayIntrinsic,
    Global,
}
impl Token {
    fn new(v: &AbstractValue) -> Self {
        match v {
            AbstractValue::Value(v) => Self::Value(*v),
            AbstractValue::SymbolicInput(v) => Self::Symbolic(*v),
            AbstractValue::Closure(a, c) => Self::Closure(*a, c.cache_identity()),
            AbstractValue::ClassMembers(k) => Self::Class(*k),
            AbstractValue::Instance(k, c, o) => Self::Instance(*k, c.cache_identity(), *o),
            AbstractValue::DeferredCallbacks(c) => Self::Deferred(c.cache_identity()),
            AbstractValue::AuditedRetainedResult(k, c) => Self::Audited(*k, c.cache_identity()),
            AbstractValue::OwnDescriptorMap(k) => Self::Descriptor(*k),
            AbstractValue::CallbackFactory => Self::CallbackFactory,
            AbstractValue::InlineCallbackFactory => Self::InlineCallbackFactory,
            AbstractValue::ForeignDeferredRecord => Self::ForeignRecord,
            AbstractValue::ObjectAssign => Self::ObjectAssign,
            AbstractValue::ArrayIntrinsic => Self::ArrayIntrinsic,
            AbstractValue::GlobalObject => Self::Global,
        }
    }
}
fn tokens<'a>(values: impl IntoIterator<Item = &'a AbstractValue>) -> Vec<Token> {
    let mut v: Vec<_> = values.into_iter().map(Token::new).collect();
    v.sort();
    v.dedup();
    v
}
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Dep {
    Properties(u32),
    Accessors(u32),
    Descriptor(CallSite),
    Cell(LexicalCellId),
    Escapes,
}
#[derive(Clone, PartialEq, Eq)]
enum Snapshot {
    Missing,
    Values(Vec<Token>),
    Properties(Vec<(Option<String>, Vec<Token>)>),
    Accessors(Vec<(String, Vec<Token>, Vec<Token>)>),
}
impl Dep {
    fn read(&self, w: &World) -> Snapshot {
        match self {
            Self::Properties(k) => w
                .surface
                .prefix_object_properties
                .get(k)
                .map_or(Snapshot::Missing, |m| {
                    Snapshot::Properties(m.iter().map(|(k, v)| (k.clone(), tokens(v))).collect())
                }),
            Self::Accessors(k) => {
                w.surface
                    .applied_accessors
                    .get(k)
                    .map_or(Snapshot::Missing, |m| {
                        Snapshot::Accessors(
                            m.iter()
                                .map(|(k, v)| (k.clone(), tokens(&v.getter), tokens(&v.setter)))
                                .collect(),
                        )
                    })
            }
            Self::Descriptor(k) => w.descriptors.get(k).map_or(Snapshot::Missing, |m| {
                Snapshot::Values(tokens(&m.retained_values))
            }),
            Self::Cell(k) => w
                .cells
                .get(*k)
                .map_or(Snapshot::Missing, |c| Snapshot::Values(tokens(&c.values))),
            Self::Escapes => Snapshot::Values(tokens(&w.publication.global_escaped_callbacks)),
        }
    }
}
#[derive(Default, Serialize)]
struct Counts {
    walks: u64,
    roots: u64,
    nodes: u64,
    edges: u64,
    dependency_reads: u64,
    empty_roots: u64,
    repeated_roots: u64,
    unchanged_roots: u64,
    unchanged_root_nodes: u64,
    unchanged_root_edges: u64,
    unchanged_roots_across_owners: u64,
    unchanged_root_nodes_across_owners: u64,
    changed_roots: u64,
    changed_dependencies: u64,
    previous_dependencies: u64,
    changed_dependency_histogram: BTreeMap<usize, u64>,
    expansion_events: u64,
    repeated_expansions: u64,
    unchanged_expansions: u64,
    unchanged_expansion_edges: u64,
    unchanged_expansions_across_owners: u64,
    changed_expansions: u64,
    walk_cache_clears: u64,
    node_cache_clears: u64,
    pool_operations: u64,
    pool_passes: u64,
    multi_pass_operations: u64,
    pool_pass_histogram: BTreeMap<u64, u64>,
    pool_size_histogram: BTreeMap<usize, u64>,
    pool_members: u64,
    fallbacks: u64,
    recursive_fallbacks: u64,
    pool_size_fallbacks: u64,
    expansion_fallbacks: u64,
    depth_fallbacks: u64,
}
struct Entry {
    deps: BTreeMap<Dep, Snapshot>,
    result: Vec<Token>,
    owner: Option<SourceUnitOrdinal>,
}
struct NodeEntry {
    deps: BTreeMap<Dep, Snapshot>,
    owner: Option<SourceUnitOrdinal>,
}
#[derive(Default)]
struct State {
    counts: Counts,
    walks: BTreeMap<Vec<Token>, Entry>,
    nodes: BTreeMap<(Token, bool, bool), NodeEntry>,
}
pub(super) struct Walk {
    roots: Vec<Token>,
    deps: BTreeSet<Dep>,
    pub(super) nodes: u64,
    edges: u64,
}
impl Walk {
    pub(super) fn new<'a>(roots: impl IntoIterator<Item = &'a AbstractValue>) -> Self {
        Self {
            roots: tokens(roots),
            deps: BTreeSet::new(),
            nodes: 0,
            edges: 0,
        }
    }
}
pub(super) struct Probe {
    chunk: Option<String>,
    state: RefCell<State>,
    dir: std::path::PathBuf,
}
impl Probe {
    pub(super) fn new(chunk: Option<String>) -> Option<Self> {
        std::env::var_os("DEBUNDLE_REUSE_CENSUS_DIR").map(|dir| Self {
            chunk,
            state: RefCell::default(),
            dir: dir.into(),
        })
    }
    pub(super) fn node(
        &self,
        trace: &mut Walk,
        value: &AbstractValue,
        expand: bool,
        properties: bool,
        escapes: bool,
        edges: u64,
        world: &World,
        owner: Option<SourceUnitOrdinal>,
    ) {
        trace.edges += edges;
        if !expand && !escapes {
            return;
        }
        let mut deps = BTreeSet::new();
        if expand {
            match value {
                AbstractValue::Value(Value::Alloc(kind, k)) if kind.tracks_prefix_references() => {
                    if properties {
                        deps.insert(Dep::Properties(*k));
                    }
                    deps.insert(Dep::Accessors(*k));
                }
                AbstractValue::Closure(_, c) => {
                    deps.extend(c.values.cells.values().copied().map(Dep::Cell));
                }
                AbstractValue::OwnDescriptorMap(k) => {
                    deps.insert(Dep::Descriptor(*k));
                }
                _ => {}
            }
        }
        if escapes {
            deps.insert(Dep::Escapes);
        }
        trace.deps.extend(deps.iter().cloned());
        let snapshots: BTreeMap<_, _> = deps
            .into_iter()
            .map(|k| {
                let v = k.read(world);
                (k, v)
            })
            .collect();
        let key = (Token::new(value), properties, escapes);
        let mut state = self.state.borrow_mut();
        state.counts.expansion_events += 1;
        if let Some(old) = state.nodes.get(&key) {
            let same = old.deps == snapshots;
            let cross = old.owner != owner;
            state.counts.repeated_expansions += 1;
            if same {
                state.counts.unchanged_expansions += 1;
                state.counts.unchanged_expansion_edges += edges;
                if cross {
                    state.counts.unchanged_expansions_across_owners += 1;
                }
            } else {
                state.counts.changed_expansions += 1;
            }
        }
        if state.nodes.len() >= 16384 {
            state.nodes.clear();
            state.counts.node_cache_clears += 1;
        }
        state.nodes.insert(
            key,
            NodeEntry {
                deps: snapshots,
                owner,
            },
        );
    }
    pub(super) fn walk(
        &self,
        trace: Walk,
        result: &Values,
        world: &World,
        owner: Option<SourceUnitOrdinal>,
    ) {
        let mut state = self.state.borrow_mut();
        state.counts.walks += 1;
        state.counts.roots += trace.roots.len() as u64;
        state.counts.nodes += trace.nodes;
        state.counts.edges += trace.edges;
        state.counts.dependency_reads += trace.deps.len() as u64;
        if trace.roots.is_empty() {
            state.counts.empty_roots += 1;
        }
        let result = tokens(result);
        if let Some(old) = state.walks.get(&trace.roots) {
            let total = old.deps.len();
            let changed = old
                .deps
                .iter()
                .filter(|(k, v)| k.read(world) != **v)
                .count();
            let cross = old.owner != owner;
            if changed == 0 {
                assert!(
                    old.result == result,
                    "unchanged retained-graph inputs changed callback result"
                );
            }
            state.counts.repeated_roots += 1;
            if changed == 0 {
                state.counts.unchanged_roots += 1;
                state.counts.unchanged_root_nodes += trace.nodes;
                state.counts.unchanged_root_edges += trace.edges;
                if cross {
                    state.counts.unchanged_roots_across_owners += 1;
                    state.counts.unchanged_root_nodes_across_owners += trace.nodes;
                }
            } else {
                state.counts.changed_roots += 1;
                state.counts.changed_dependencies += changed as u64;
                state.counts.previous_dependencies += total as u64;
                *state
                    .counts
                    .changed_dependency_histogram
                    .entry(changed)
                    .or_default() += 1;
            }
        }
        let deps = trace
            .deps
            .into_iter()
            .map(|k| {
                let v = k.read(world);
                (k, v)
            })
            .collect();
        if state.walks.len() >= 512 {
            state.walks.clear();
            state.counts.walk_cache_clears += 1;
        }
        state.walks.insert(
            trace.roots,
            Entry {
                deps,
                result,
                owner,
            },
        );
    }
    pub(super) fn pool_iterations(&self, passes: u64) {
        let mut s = self.state.borrow_mut();
        s.counts.pool_operations += 1;
        s.counts.pool_passes += passes;
        if passes > 1 {
            s.counts.multi_pass_operations += 1;
        }
        *s.counts.pool_pass_histogram.entry(passes).or_default() += 1;
    }
    pub(super) fn pool(&self, size: usize) {
        let mut s = self.state.borrow_mut();
        s.counts.pool_members += size as u64;
        *s.counts.pool_size_histogram.entry(size).or_default() += 1;
    }
    pub(super) fn fallback(&self, recursive: bool, size: bool, expansion: bool) {
        let mut s = self.state.borrow_mut();
        s.counts.fallbacks += 1;
        if recursive {
            s.counts.recursive_fallbacks += 1;
        } else if size {
            s.counts.pool_size_fallbacks += 1;
        } else if expansion {
            s.counts.expansion_fallbacks += 1;
        } else {
            s.counts.depth_fallbacks += 1;
        }
    }
}
impl Drop for Probe {
    fn drop(&mut self) {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        std::fs::create_dir_all(&self.dir).expect("census output directory");
        let path = self.dir.join(format!(
            "census-{}.json",
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(
            path,
            serde_json::to_vec_pretty(
                &serde_json::json!({"chunk":self.chunk,"counts":self.state.borrow().counts}),
            )
            .expect("encode census"),
        )
        .expect("write census");
    }
}
