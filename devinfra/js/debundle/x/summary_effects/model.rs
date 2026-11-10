//! Abstract domain and constraint store of the summary-based effect prototype.
//!
//! Values are sets of abstract objects. Primitives are not represented: an
//! empty set means "primitive, `undefined`, or unreached". Every expression
//! that can evaluate to an object must therefore produce a non-empty set.
//!
//! Value flow through named properties is merged across objects: one node
//! per property name, which keeps the constraint graph near-linear in the
//! program. Numeric-keyed and unknown-keyed contents, `prototype` and each
//! object's collapsed contents stay per object. Effect cells, in
//! `effects.rs`, are always per object.

use rustc_hash::{FxHashMap, FxHashSet};

use crate::bits::Bits;

pub type Node = u32;
pub type Obj = u32;
pub type Unit = u32;
pub type PropId = u32;
pub type Var = u32;
pub type CxId = u32;

/// Every object the analysis does not model: imports, host objects, results
/// of unknown calls. Reading or calling it is a call into Escaped.
pub const ESC: Obj = 0;
/// The intrinsic prototypes, as one object that ends every prototype chain.
pub const INTRINSIC: Obj = 1;
/// An unknown object reached from an unresolved global identifier. Unlike
/// `ESC` it may be an intrinsic prototype or constructor, so a store on it
/// can change what any property read finds.
pub const GLOBAL: Obj = 2;
/// A global-derived unknown value that may be a symbol or an intrinsic
/// prototype even after a call: `Symbol` itself, `Object.getOwnPropertySymbols`,
/// `Reflect.ownKeys`, `Object.getPrototypeOf`, and what they yield.
pub const SYMBOLS: Obj = 3;

/// A key the analysis knows nothing about.
pub const ANY: PropId = 0;
/// A numeric key: an array element.
pub const INDEX: PropId = 1;
/// Pseudo-properties under which every getter and setter is reachable.
pub const GETTERS: PropId = 2;
pub const SETTERS: PropId = 3;

/// A node whose set is always empty; only ever an edge source.
pub const EMPTY_NODE: Node = 0;
/// A node holding exactly `ESC`; only ever an edge source.
pub const ESC_NODE: Node = 1;
/// Values that flowed somewhere unmodeled. Objects arriving here are escaped.
pub const ESCAPED_SINK: Node = 2;
/// Objects stored through a key that may be a symbol or an object.
pub const EXOTIC_SINK: Node = 3;
/// Every thrown value.
pub const THROWN: Node = 4;
/// A node holding exactly `GLOBAL`; only ever an edge source.
pub const GLOBAL_NODE: Node = 5;
/// Values stored on a global-derived unknown object under an unknown key.
const GLOBAL_STAR: Node = 6;
/// Every user-defined `toString`, `valueOf` and `Symbol.toPrimitive`.
const COERCE_METHODS: Node = 7;
/// Unused; keeps the node numbering stable.
const RESERVED: Node = 8;
/// Unused; keeps the node numbering stable.
const RESERVED_2: Node = 9;
/// A node holding exactly `SYMBOLS`; only ever an edge source.
pub const SYMBOLS_NODE: Node = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ObjKind {
    Unknown,
    Intrinsic,
    /// Some intact intrinsic method or value read under this property name.
    Native(PropId),
    Plain,
    Array,
    Regex,
    /// A builtin's fresh result when every operand was primitive.
    NativeResult,
    Function(u32),
}

/// A per-object collapsed view, looked up through the prototype chain.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Slot {
    /// Every value stored on the object under any key.
    All,
    Getters,
    Setters,
}

pub struct ObjInfo {
    pub kind: ObjKind,
    /// The unit whose code holds the allocation site.
    pub unit: Unit,
    pub span_lo: u32,
    /// Own data properties present from allocation on.
    pub born: FxHashSet<PropId>,
    pub proto: Node,
    pub escaped: bool,
    pub exotic: bool,
    slots: FxHashMap<Slot, Node>,
    chains: FxHashMap<Slot, Node>,
    /// Per-object storage of the properties not merged by name.
    own: FxHashMap<PropId, Node>,
    /// Values stored under unknown keys, which any named read may find.
    star: Option<Node>,
    /// Values stored under numeric keys.
    elems: Option<Node>,
    /// Destinations of numeric reads, linked once the object has elements.
    elems_waiters: Vec<Node>,
    /// Destinations of named reads on the object: linked once it holds
    /// unknown-key values.
    named_readers: Vec<Node>,
    /// Destinations of other reads on the object. All readers get `ESC`
    /// once the object escapes.
    readers: Vec<Node>,
}

pub struct FnInfo {
    pub obj: Obj,
    pub unit: Unit,
    pub params: Vec<Node>,
    /// Receives every argument of every call.
    pub all_args: Node,
    /// `None` for an arrow function, whose `this` is lexical.
    pub this: Option<Node>,
    /// What a caller receives.
    pub result: Node,
    pub has_prototype: bool,
    pub prototype: Option<Obj>,
}

/// Why a site is a call into Escaped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize)]
#[serde(rename_all = "snake_case")]
#[repr(u8)]
pub enum Reason {
    ImportCall,
    ImportMember,
    GlobalCall,
    GlobalMember,
    UnknownCall,
    UnknownMember,
    EscapedObject,
    Builtin,
    Coercion,
    Suspension,
    Unmodeled,
}

pub const REASONS: [Reason; 11] = [
    Reason::ImportCall,
    Reason::ImportMember,
    Reason::GlobalCall,
    Reason::GlobalMember,
    Reason::UnknownCall,
    Reason::UnknownMember,
    Reason::EscapedObject,
    Reason::Builtin,
    Reason::Coercion,
    Reason::Suspension,
    Reason::Unmodeled,
];

impl Reason {
    pub fn bit(self) -> u16 {
        1 << self as u8
    }
}

/// The syntactic root of a callee or member base, used only to attribute an
/// opaque site to a cause.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Root {
    Import,
    Global,
    Other,
}

impl Root {
    pub fn call(self) -> Reason {
        match self {
            Self::Import => Reason::ImportCall,
            Self::Global => Reason::GlobalCall,
            Self::Other => Reason::UnknownCall,
        }
    }

    pub fn member(self) -> Reason {
        match self {
            Self::Import => Reason::ImportMember,
            Self::Global => Reason::GlobalMember,
            Self::Other => Reason::UnknownMember,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Arg {
    pub node: Node,
    /// The node holds the elements of a spread argument.
    pub spread: bool,
}

#[derive(Debug, Clone)]
pub enum Cx {
    Load {
        base: Node,
        prop: PropId,
        dst: Node,
        root: Root,
    },
    Store {
        base: Node,
        prop: PropId,
        src: Node,
        /// The base expression is a member chain off a global identifier.
        global_rooted: bool,
    },
    Call {
        callee: Node,
        this: Option<Node>,
        args: Vec<Arg>,
        dst: Node,
        new_site: Option<Obj>,
        root: Root,
        /// The call is a coercion's own `ToPrimitive` step.
        coercion: bool,
    },
    /// The receiver-dependent part of the native methods a call may reach;
    /// subscribed on the call's receiver.
    NativeReceiver {
        call: CxId,
    },
    /// `ToPrimitive`, `ToPropertyKey`, `instanceof`'s right operand, over
    /// `closure`: the value and, transitively, the elements of its arrays.
    /// `callee` collects the conversion methods the objects define.
    Coerce {
        val: Node,
        closure: Node,
        callee: Node,
    },
    /// The iteration protocol; `dst` receives the produced values and
    /// `callee` collects user iterator methods.
    Iterate {
        val: Node,
        dst: Node,
        callee: Node,
    },
    /// An operation that runs code only on an unknown or escaped object.
    Touch {
        val: Node,
    },
    /// Adds `from -> to` once any object reaches the subscribed node.
    OnAny {
        from: Node,
        to: Node,
    },
    /// Subscribed on a computed store's key: the stored-on objects become
    /// exotic once the key may be a symbol.
    ExoticKey {
        target: Node,
    },
    /// Links `chain(q, slot) -> into` for each prototype `q`.
    ProtoLink {
        slot: Slot,
        into: Node,
    },
    EscapedSink,
    ExoticSink,
}

/// What to do for an operation on an object once a key that may be a symbol
/// is stored on it: its methods become the operation's callees, and an
/// iteration's values become unknown.
#[derive(Debug, Clone, Copy)]
struct ProtocolWaiter {
    callee: Node,
    iteration: Option<Node>,
}

#[derive(Default)]
struct NativeCallState {
    behaviors: u8,
    receiver: Option<CxId>,
}

pub struct CxInfo {
    pub cx: Cx,
    pub unit: Unit,
    pub span_lo: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VarKind {
    Local,
    Import,
}

pub struct VarInfo {
    pub node: Node,
    pub name: String,
    pub kind: VarKind,
    pub exported: bool,
    /// Declared directly in the module scope.
    pub top_level: bool,
}

#[derive(Debug, Clone, Copy)]
pub struct VarAccess {
    pub unit: Unit,
    pub var: Var,
    pub write: bool,
}

#[derive(Default)]
pub struct UnitInfo {
    /// The function object whose body this unit is; `None` for a top-level
    /// statement and for the Escaped pseudo-unit.
    pub function: Option<Obj>,
    pub havoc: bool,
    /// Reads of unresolved globals and imports, and writes of globals.
    pub reads_unknown_heap: bool,
    pub writes_unknown_heap: bool,
    /// Opaque sites decided at generation time.
    pub static_reasons: u16,
}

#[derive(Default)]
pub struct Interner {
    names: Vec<String>,
    ids: FxHashMap<String, PropId>,
}

impl Interner {
    pub fn intern(&mut self, name: &str) -> PropId {
        if let Some(id) = self.ids.get(name) {
            return *id;
        }
        let id = self.names.len() as PropId;
        self.names.push(name.to_string());
        self.ids.insert(name.to_string(), id);
        id
    }

    pub fn get(&self, name: &str) -> Option<PropId> {
        self.ids.get(name).copied()
    }

    pub fn name(&self, id: PropId) -> &str {
        &self.names[id as usize]
    }
}

/// Property names the intrinsic prototypes define. A read that reaches the end
/// of a prototype chain under one of these yields a native value.
const INTRINSIC_NAMES: &[&str] = &[
    "constructor",
    "toString",
    "toLocaleString",
    "valueOf",
    "hasOwnProperty",
    "isPrototypeOf",
    "propertyIsEnumerable",
    "__defineGetter__",
    "__defineSetter__",
    "__lookupGetter__",
    "__lookupSetter__",
    "call",
    "apply",
    "bind",
    "length",
    "name",
    "caller",
    "arguments",
    "at",
    "concat",
    "copyWithin",
    "entries",
    "every",
    "fill",
    "filter",
    "find",
    "findIndex",
    "findLast",
    "findLastIndex",
    "flat",
    "flatMap",
    "forEach",
    "includes",
    "indexOf",
    "join",
    "keys",
    "lastIndexOf",
    "map",
    "pop",
    "push",
    "reduce",
    "reduceRight",
    "reverse",
    "shift",
    "slice",
    "some",
    "sort",
    "splice",
    "toReversed",
    "toSorted",
    "toSpliced",
    "unshift",
    "values",
    "with",
    "anchor",
    "big",
    "blink",
    "bold",
    "charAt",
    "charCodeAt",
    "codePointAt",
    "endsWith",
    "fixed",
    "fontcolor",
    "fontsize",
    "isWellFormed",
    "italics",
    "link",
    "localeCompare",
    "match",
    "matchAll",
    "normalize",
    "padEnd",
    "padStart",
    "repeat",
    "replace",
    "replaceAll",
    "search",
    "small",
    "split",
    "startsWith",
    "strike",
    "sub",
    "substr",
    "substring",
    "sup",
    "toLocaleLowerCase",
    "toLocaleUpperCase",
    "toLowerCase",
    "toUpperCase",
    "toWellFormed",
    "trim",
    "trimEnd",
    "trimLeft",
    "trimRight",
    "trimStart",
    "toExponential",
    "toFixed",
    "toPrecision",
    "description",
    "exec",
    "test",
    "compile",
    "flags",
    "global",
    "ignoreCase",
    "multiline",
    "dotAll",
    "unicode",
    "unicodeSets",
    "sticky",
    "hasIndices",
    "source",
    "lastIndex",
    "then",
    "catch",
    "finally",
    "next",
    "return",
    "throw",
    "get",
    "set",
    "has",
    "add",
    "delete",
    "clear",
    "size",
    "message",
    "stack",
    "cause",
    "asIntN",
    "@@iterator",
    "@@asyncIterator",
    "@@hasInstance",
    "@@toPrimitive",
    "@@toStringTag",
    "@@species",
    "@@match",
    "@@matchAll",
    "@@replace",
    "@@search",
    "@@split",
    "@@unscopables",
    "@@isConcatSpreadable",
];

pub struct Model {
    pub pts: Vec<Bits>,
    pub succ: Vec<Vec<Node>>,
    edges: FxHashSet<(Node, Node)>,
    pub subs: Vec<Vec<CxId>>,
    delta: Vec<Vec<Obj>>,
    queued: Vec<bool>,
    worklist: Vec<Node>,

    pub objs: Vec<ObjInfo>,
    pub fns: Vec<FnInfo>,
    pub cxs: Vec<CxInfo>,
    pub units: Vec<UnitInfo>,
    pub vars: Vec<VarInfo>,
    pub var_accesses: Vec<VarAccess>,
    pub props: Interner,

    obj_nodes: FxHashMap<Obj, Node>,
    natives: FxHashMap<PropId, Obj>,
    intrinsic_names: FxHashSet<PropId>,
    by_name: FxHashMap<PropId, Node>,
    /// Loads and stores whose by-name edges are in place.
    linked: FxHashSet<CxId>,
    native_result: Option<Obj>,
    /// Call constraints whose summary-less native callees were applied.
    unknown_native_calls: FxHashSet<CxId>,
    /// Operations to redo as user protocol calls once the object turns
    /// exotic.
    protocol_waiters: FxHashMap<Obj, Vec<ProtocolWaiter>>,
    native_calls: FxHashMap<CxId, NativeCallState>,
    /// Names with a getter or setter anywhere in the chunk.
    pub getter_names: FxHashSet<PropId>,
    pub setter_names: FxHashSet<PropId>,
    /// Property names some `delete` removes; `delete_any` for a computed key.
    pub deleted: FxHashSet<PropId>,
    pub delete_any: bool,
    /// The pseudo-properties accessor functions are stored under.
    pub pseudo: FxHashSet<PropId>,
    pub escaped_fns: Vec<Obj>,
    pub stats: SolveStats,
    /// Constraints only register during generation; solving applies them.
    solving: bool,
    /// Coercions already requested per value and unit.
    coercions: FxHashMap<(Node, Unit), Node>,
    prototype_prop: PropId,
    /// Global functions whose results may be symbols.
    symbol_sources: FxHashSet<PropId>,
    /// Treat every unknown value used as a computed store key as a possible
    /// well-known symbol.
    pub unknown_keys_may_be_symbols: bool,
}

#[derive(Default, Debug, Clone, serde::Serialize)]
pub struct SolveStats {
    pub copy_edges: usize,
    pub propagations: u64,
    pub constraint_applications: u64,
}

impl Default for Model {
    fn default() -> Self {
        Self::new()
    }
}

impl Model {
    pub fn new() -> Self {
        let mut model = Self {
            pts: Vec::new(),
            succ: Vec::new(),
            edges: FxHashSet::default(),
            subs: Vec::new(),
            delta: Vec::new(),
            queued: Vec::new(),
            worklist: Vec::new(),
            objs: Vec::new(),
            fns: Vec::new(),
            cxs: Vec::new(),
            units: Vec::new(),
            vars: Vec::new(),
            var_accesses: Vec::new(),
            props: Interner::default(),
            obj_nodes: FxHashMap::default(),
            natives: FxHashMap::default(),
            intrinsic_names: FxHashSet::default(),
            by_name: FxHashMap::default(),
            linked: FxHashSet::default(),
            native_result: None,
            unknown_native_calls: FxHashSet::default(),
            protocol_waiters: FxHashMap::default(),
            native_calls: FxHashMap::default(),
            getter_names: FxHashSet::default(),
            setter_names: FxHashSet::default(),
            deleted: FxHashSet::default(),
            delete_any: false,
            pseudo: FxHashSet::default(),
            escaped_fns: Vec::new(),
            stats: SolveStats::default(),
            solving: false,
            coercions: FxHashMap::default(),
            prototype_prop: 0,
            symbol_sources: FxHashSet::default(),
            unknown_keys_may_be_symbols: false,
        };
        for (name, expected) in [
            ("*", ANY),
            ("#index", INDEX),
            ("\0get:*", GETTERS),
            ("\0set:*", SETTERS),
        ] {
            assert_eq!(model.props.intern(name), expected);
        }
        model.pseudo.extend([GETTERS, SETTERS]);
        model.prototype_prop = model.props.intern("prototype");
        for expected in [
            EMPTY_NODE,
            ESC_NODE,
            ESCAPED_SINK,
            EXOTIC_SINK,
            THROWN,
            GLOBAL_NODE,
            GLOBAL_STAR,
            COERCE_METHODS,
            RESERVED,
            RESERVED_2,
            SYMBOLS_NODE,
        ] {
            assert_eq!(model.node(), expected);
        }
        assert_eq!(model.raw_obj(ObjKind::Unknown, 0, 0), ESC);
        assert_eq!(model.raw_obj(ObjKind::Intrinsic, 0, 0), INTRINSIC);
        assert_eq!(model.raw_obj(ObjKind::Unknown, 0, 0), GLOBAL);
        assert_eq!(model.raw_obj(ObjKind::Unknown, 0, 0), SYMBOLS);
        for name in INTRINSIC_NAMES {
            let prop = model.props.intern(name);
            model.intrinsic_names.insert(prop);
        }
        model.pts[ESC_NODE as usize].insert(ESC);
        model.pts[GLOBAL_NODE as usize].insert(GLOBAL);
        model.pts[SYMBOLS_NODE as usize].insert(SYMBOLS);
        for name in ["getOwnPropertySymbols", "ownKeys", "getPrototypeOf"] {
            let prop = model.props.intern(name);
            model.symbol_sources.insert(prop);
        }
        for name in ["toString", "valueOf", "@@toPrimitive"] {
            let prop = model.props.intern(name);
            let methods = model.named(prop);
            model.edge(methods, COERCE_METHODS);
        }
        model
    }

    pub fn node(&mut self) -> Node {
        self.pts.push(Bits::default());
        self.succ.push(Vec::new());
        self.subs.push(Vec::new());
        self.delta.push(Vec::new());
        self.queued.push(false);
        (self.pts.len() - 1) as Node
    }

    fn raw_obj(&mut self, kind: ObjKind, unit: Unit, span_lo: u32) -> Obj {
        let proto = self.node();
        self.objs.push(ObjInfo {
            kind,
            unit,
            span_lo,
            born: FxHashSet::default(),
            proto,
            escaped: false,
            exotic: false,
            slots: FxHashMap::default(),
            chains: FxHashMap::default(),
            own: FxHashMap::default(),
            star: None,
            elems: None,
            elems_waiters: Vec::new(),
            named_readers: Vec::new(),
            readers: Vec::new(),
        });
        (self.objs.len() - 1) as Obj
    }

    /// A modeled allocation site. Its prototype chain ends at the intrinsics.
    pub fn alloc(&mut self, kind: ObjKind, unit: Unit, span_lo: u32) -> Obj {
        let obj = self.raw_obj(kind, unit, span_lo);
        let proto = self.objs[obj as usize].proto;
        self.add(proto, INTRINSIC);
        obj
    }

    pub fn is_alloc(&self, obj: Obj) -> bool {
        !matches!(
            self.objs[obj as usize].kind,
            ObjKind::Unknown | ObjKind::Intrinsic | ObjKind::Native(_)
        )
    }

    pub fn native(&mut self, name: PropId) -> Obj {
        if let Some(obj) = self.natives.get(&name) {
            return *obj;
        }
        let obj = self.raw_obj(ObjKind::Native(name), 0, 0);
        self.natives.insert(name, obj);
        obj
    }

    fn unknown_node(&self, obj: Obj) -> Node {
        match obj {
            GLOBAL => GLOBAL_NODE,
            SYMBOLS => SYMBOLS_NODE,
            _ => ESC_NODE,
        }
    }

    /// A node holding exactly `obj`.
    pub fn obj_node(&mut self, obj: Obj) -> Node {
        if let Some(node) = self.obj_nodes.get(&obj) {
            return *node;
        }
        let node = self.node();
        self.obj_nodes.insert(obj, node);
        self.add(node, obj);
        node
    }

    pub fn add(&mut self, node: Node, obj: Obj) {
        debug_assert!(!matches!(
            node,
            EMPTY_NODE | ESC_NODE | GLOBAL_NODE | SYMBOLS_NODE
        ));
        if self.pts[node as usize].insert(obj) {
            self.delta[node as usize].push(obj);
            if !self.queued[node as usize] {
                self.queued[node as usize] = true;
                self.worklist.push(node);
            }
        }
    }

    pub fn edge(&mut self, from: Node, to: Node) {
        if from == to || from == EMPTY_NODE || !self.edges.insert((from, to)) {
            return;
        }
        assert!(
            !matches!(to, EMPTY_NODE | ESC_NODE | GLOBAL_NODE | SYMBOLS_NODE),
            "edge into a constant node"
        );
        self.succ[from as usize].push(to);
        self.stats.copy_edges += 1;
        let current: Vec<Obj> = self.pts[from as usize].iter().collect();
        for obj in current {
            self.add(to, obj);
        }
    }

    /// A coercion of `val`, performed by `unit`. Returns what user-defined
    /// conversion methods may produce.
    pub fn coerce(&mut self, val: Node, unit: Unit, span_lo: u32) -> Node {
        if val == EMPTY_NODE {
            return EMPTY_NODE;
        }
        if let Some(result) = self.coercions.get(&(val, unit)) {
            return *result;
        }
        let closure = self.node();
        let callee = self.node();
        self.edge(val, closure);
        let dst = self.node();
        self.constraint(
            Cx::Call {
                callee,
                this: Some(closure),
                args: Vec::new(),
                dst,
                new_site: None,
                root: Root::Other,
                coercion: true,
            },
            unit,
            span_lo,
        );
        self.constraint(
            Cx::Coerce {
                val,
                closure,
                callee,
            },
            unit,
            span_lo,
        );
        self.coercions.insert((val, unit), dst);
        dst
    }

    /// An iteration of `val`, performed by `unit`; returns the produced
    /// values.
    pub fn iterate(&mut self, val: Node, unit: Unit, span_lo: u32) -> Node {
        let dst = self.node();
        let callee = self.node();
        let results = self.node();
        self.constraint(
            Cx::Call {
                callee,
                this: Some(val),
                args: Vec::new(),
                dst: results,
                new_site: None,
                root: Root::Other,
                coercion: false,
            },
            unit,
            span_lo,
        );
        // What a user iterator returns is driven by the protocol, not by
        // modeled code.
        self.edge(results, ESCAPED_SINK);
        self.constraint(Cx::Iterate { val, dst, callee }, unit, span_lo);
        dst
    }

    pub fn constraint(&mut self, cx: Cx, unit: Unit, span_lo: u32) -> CxId {
        let on = match &cx {
            Cx::Load { base, .. } | Cx::Store { base, .. } => Some(*base),
            Cx::Call { callee, .. } => Some(*callee),
            Cx::Coerce { closure, .. } => Some(*closure),
            Cx::Iterate { val, .. } | Cx::Touch { val } => Some(*val),
            Cx::NativeReceiver { .. }
            | Cx::OnAny { .. }
            | Cx::ExoticKey { .. }
            | Cx::ProtoLink { .. }
            | Cx::EscapedSink
            | Cx::ExoticSink => None,
        };
        if let Cx::Load { prop, dst, .. } = &cx {
            // The base may be a primitive, whose properties are intrinsic.
            let (prop, dst) = (*prop, *dst);
            if prop == ANY || self.intrinsic_names.contains(&prop) {
                let native = self.native(prop);
                self.add(dst, native);
            }
        }
        let id = self.cxs.len() as CxId;
        self.cxs.push(CxInfo { cx, unit, span_lo });
        if let Some(node) = on {
            self.subscribe(node, id);
        }
        id
    }

    pub fn subscribe(&mut self, node: Node, cx: CxId) {
        self.subs[node as usize].push(cx);
        if !self.solving {
            // The node's pending delta still holds everything it has.
            return;
        }
        let current: Vec<Obj> = self.pts[node as usize].iter().collect();
        for obj in current {
            self.apply(cx, obj);
        }
    }

    /// Whether values under this property are kept per object.
    fn per_object(&self, prop: PropId) -> bool {
        prop == self.prototype_prop
    }

    /// The node for every value stored under `prop` on any object.
    pub fn named(&mut self, prop: PropId) -> Node {
        debug_assert!(prop != ANY && prop != INDEX);
        if let Some(node) = self.by_name.get(&prop) {
            return *node;
        }
        let node = self.node();
        self.by_name.insert(prop, node);
        node
    }

    fn own(&mut self, obj: Obj, prop: PropId) -> Node {
        if let Some(node) = self.objs[obj as usize].own.get(&prop) {
            return *node;
        }
        let node = self.node();
        self.objs[obj as usize].own.insert(prop, node);
        let all = self.slot(obj, Slot::All);
        self.edge(node, all);
        node
    }

    pub fn slot(&mut self, obj: Obj, slot: Slot) -> Node {
        if let Some(node) = self.objs[obj as usize].slots.get(&slot) {
            return *node;
        }
        let node = self.node();
        self.objs[obj as usize].slots.insert(slot, node);
        if slot == Slot::All && self.objs[obj as usize].escaped {
            self.edge(node, ESCAPED_SINK);
        }
        node
    }

    pub fn elems(&mut self, obj: Obj) -> Node {
        if let Some(node) = self.objs[obj as usize].elems {
            return node;
        }
        let node = self.node();
        self.objs[obj as usize].elems = Some(node);
        if self.objs[obj as usize].escaped {
            self.add(node, ESC);
        }
        for waiter in std::mem::take(&mut self.objs[obj as usize].elems_waiters) {
            self.edge(node, waiter);
        }
        node
    }

    /// The node for values stored under unknown keys on `obj`.
    fn star(&mut self, obj: Obj) -> Node {
        if let Some(node) = self.objs[obj as usize].star {
            return node;
        }
        let node = self.node();
        self.objs[obj as usize].star = Some(node);
        for reader in self.objs[obj as usize].named_readers.clone() {
            self.edge(node, reader);
        }
        node
    }

    /// Records `value` as stored on the allocation `obj` under `prop`.
    pub fn store_value(&mut self, obj: Obj, prop: PropId, value: Node) {
        debug_assert!(self.is_alloc(obj));
        if value == EMPTY_NODE {
            return;
        }
        let slot = match prop {
            GETTERS => Slot::Getters,
            SETTERS => Slot::Setters,
            _ => Slot::All,
        };
        let slot = self.slot(obj, slot);
        self.edge(value, slot);
        match prop {
            GETTERS | SETTERS => {}
            ANY => {
                let star = self.star(obj);
                self.edge(value, star);
                let elems = self.elems(obj);
                self.edge(value, elems);
            }
            INDEX => {
                let elems = self.elems(obj);
                self.edge(value, elems);
            }
            _ if self.per_object(prop) => {
                let own = self.own(obj, prop);
                self.edge(value, own);
            }
            _ => {
                let named = self.named(prop);
                self.edge(value, named);
            }
        }
    }

    /// Whether `prop` is an own data property of `obj` from allocation on, so
    /// a read of it never consults the prototype chain.
    pub fn born_with(&self, obj: Obj, prop: PropId) -> bool {
        prop != ANY
            && prop != INDEX
            && !self.delete_any
            && !self.deleted.contains(&prop)
            && self.objs[obj as usize].born.contains(&prop)
    }

    /// The node holding a slot of `obj` and of its prototype chain.
    fn chain(&mut self, obj: Obj, slot: Slot) -> Node {
        match self.objs[obj as usize].kind {
            ObjKind::Unknown => return self.unknown_node(obj),
            ObjKind::Intrinsic | ObjKind::Native(_) => return EMPTY_NODE,
            _ => {}
        }
        if let Some(node) = self.objs[obj as usize].chains.get(&slot) {
            return *node;
        }
        let node = self.node();
        self.objs[obj as usize].chains.insert(slot, node);
        if slot == Slot::All {
            self.function_prototype(obj);
        }
        let own = self.slot(obj, slot);
        self.edge(own, node);
        let proto = self.objs[obj as usize].proto;
        let link = self.constraint(Cx::ProtoLink { slot, into: node }, 0, 0);
        self.subscribe(proto, link);
        node
    }

    /// A non-arrow function's `prototype` object, allocated on first use.
    pub fn function_prototype(&mut self, obj: Obj) -> Option<Obj> {
        let ObjKind::Function(index) = self.objs[obj as usize].kind else {
            return None;
        };
        let info = &self.fns[index as usize];
        if !info.has_prototype {
            return None;
        }
        if let Some(prototype) = info.prototype {
            return Some(prototype);
        }
        let (unit, span_lo) = (
            self.objs[obj as usize].unit,
            self.objs[obj as usize].span_lo,
        );
        let prototype = self.alloc(ObjKind::Plain, unit, span_lo);
        self.fns[index as usize].prototype = Some(prototype);
        let constructor_prop = self.props.intern("constructor");
        self.objs[obj as usize].born.insert(self.prototype_prop);
        self.objs[prototype as usize].born.insert(constructor_prop);
        let prototype_node = self.obj_node(prototype);
        self.store_value(obj, self.prototype_prop, prototype_node);
        let function_node = self.obj_node(obj);
        self.store_value(prototype, constructor_prop, function_node);
        Some(prototype)
    }

    pub fn function(&self, obj: Obj) -> Option<&FnInfo> {
        match self.objs[obj as usize].kind {
            ObjKind::Function(index) => Some(&self.fns[index as usize]),
            _ => None,
        }
    }

    pub fn solve(&mut self) {
        self.solving = true;
        let escaped = self.constraint(Cx::EscapedSink, 0, 0);
        self.subscribe(ESCAPED_SINK, escaped);
        let exotic = self.constraint(Cx::ExoticSink, 0, 0);
        self.subscribe(EXOTIC_SINK, exotic);
        self.edge(THROWN, ESCAPED_SINK);
        // The constant nodes were filled directly; deliver their contents to
        // the constraints subscribed on them.
        for (node, obj) in [
            (ESC_NODE, ESC),
            (GLOBAL_NODE, GLOBAL),
            (SYMBOLS_NODE, SYMBOLS),
        ] {
            self.delta[node as usize].push(obj);
            self.queued[node as usize] = true;
            self.worklist.push(node);
        }
        let progress = std::env::var_os("SUMMARY_EFFECTS_PROGRESS").is_some();
        let mut steps = 0u64;
        while let Some(node) = self.worklist.pop() {
            steps += 1;
            if progress && steps.is_multiple_of(500_000) {
                eprintln!(
                    "steps={steps} nodes={} objs={} cxs={} worklist={} {:?}",
                    self.pts.len(),
                    self.objs.len(),
                    self.cxs.len(),
                    self.worklist.len(),
                    self.stats,
                );
            }
            self.queued[node as usize] = false;
            let delta = std::mem::take(&mut self.delta[node as usize]);
            let successors = self.succ[node as usize].clone();
            for successor in successors {
                for obj in &delta {
                    self.stats.propagations += 1;
                    self.add(successor, *obj);
                }
            }
            let subscribers = self.subs[node as usize].clone();
            for cx in subscribers {
                for obj in &delta {
                    self.apply(cx, *obj);
                }
            }
        }
    }

    /// The object standing for every native call's fresh result: a value
    /// built only from primitives and more such values.
    fn native_result(&mut self) -> Obj {
        if let Some(obj) = self.native_result {
            return obj;
        }
        let obj = self.alloc(ObjKind::NativeResult, 0, 0);
        self.native_result = Some(obj);
        let node = self.obj_node(obj);
        self.store_value(obj, INDEX, node);
        obj
    }

    fn apply_load(&mut self, id: CxId, obj: Obj, prop: PropId, dst: Node) {
        if !self.is_alloc(obj) {
            // A native's own properties are intrinsic, added at creation.
            if self.objs[obj as usize].kind == ObjKind::Unknown {
                let derived = if obj == GLOBAL && self.symbol_sources.contains(&prop) {
                    SYMBOLS
                } else {
                    obj
                };
                self.add(dst, derived);
            }
            return;
        }
        match prop {
            ANY => {
                let all = self.chain(obj, Slot::All);
                self.edge(all, dst);
                self.edge(GLOBAL_STAR, dst);
            }
            GETTERS | SETTERS => {
                let slot = if prop == GETTERS {
                    Slot::Getters
                } else {
                    Slot::Setters
                };
                let accessors = self.chain(obj, slot);
                self.edge(accessors, dst);
            }
            _ => {
                if prop == INDEX {
                    match self.objs[obj as usize].elems {
                        Some(elems) => self.edge(elems, dst),
                        None => self.objs[obj as usize].elems_waiters.push(dst),
                    }
                } else if self.per_object(prop) {
                    self.function_prototype(obj);
                    let own = self.own(obj, prop);
                    self.edge(own, dst);
                } else {
                    if self.linked.insert(id) {
                        let named = self.named(prop);
                        self.edge(named, dst);
                        self.edge(GLOBAL_STAR, dst);
                    }
                    if let Some(star) = self.objs[obj as usize].star {
                        self.edge(star, dst);
                    }
                    self.objs[obj as usize].named_readers.push(dst);
                }
                // Unknown code may have stored anything on an escaped object.
                if self.objs[obj as usize].escaped {
                    self.add(dst, ESC);
                } else if prop == INDEX || self.per_object(prop) {
                    self.objs[obj as usize].readers.push(dst);
                }
            }
        }
    }

    fn apply_store(&mut self, obj: Obj, prop: PropId, src: Node, global_rooted: bool) {
        if self.is_alloc(obj) {
            self.store_value(obj, prop, src);
            return;
        }
        self.edge(src, ESCAPED_SINK);
        if obj == GLOBAL || obj == SYMBOLS {
            // The target may be an intrinsic prototype, which every read of
            // that name then finds.
            match prop {
                // An unknown key reaches every read in the chunk, so only a
                // store written against a global counts: an alias that
                // reaches here by value flow alone is assumed not to be an
                // intrinsic prototype.
                ANY | INDEX | GETTERS | SETTERS => {
                    if global_rooted {
                        self.edge(src, GLOBAL_STAR);
                    }
                }
                _ => {
                    let named = self.named(prop);
                    self.edge(src, named);
                }
            }
        }
    }

    fn apply(&mut self, id: CxId, obj: Obj) {
        self.stats.constraint_applications += 1;
        match self.cxs[id as usize].cx.clone() {
            Cx::Load { prop, dst, .. } => self.apply_load(id, obj, prop, dst),
            Cx::Store {
                prop,
                src,
                global_rooted,
                ..
            } => self.apply_store(obj, prop, src, global_rooted),
            Cx::Call {
                this,
                args,
                dst,
                new_site,
                coercion,
                ..
            } => match self.objs[obj as usize].kind {
                ObjKind::Function(index) => {
                    self.bind_call(index, this, &args, dst, new_site);
                }
                ObjKind::Native(name) => {
                    let behavior = native_behavior(self.props.name(name));
                    self.native_call(id, behavior, this, &args, dst, coercion);
                }
                ObjKind::Unknown | ObjKind::Intrinsic => {
                    self.escape_call(this, &args);
                    // A global function returns a fresh or host object, not
                    // an intrinsic prototype; `Object.getPrototypeOf` and the
                    // symbol sources are the exceptions, and stay `SYMBOLS`.
                    self.add(dst, if obj == SYMBOLS { SYMBOLS } else { ESC });
                }
                ObjKind::Plain | ObjKind::Array | ObjKind::Regex | ObjKind::NativeResult => {}
            },
            Cx::NativeReceiver { call } => {
                let behaviors = self.native_calls[&call].behaviors;
                for behavior in RECEIVER_BEHAVIORS {
                    if behaviors & behavior.bit() != 0 {
                        self.native_receiver(call, behavior, obj);
                    }
                }
            }
            Cx::Coerce {
                closure, callee, ..
            } => {
                if !self.is_alloc(obj) {
                    return;
                }
                match self.objs[obj as usize].kind {
                    ObjKind::Array | ObjKind::NativeResult => {
                        let elements = self.elems(obj);
                        self.edge(elements, closure);
                    }
                    _ => self.edge(COERCE_METHODS, callee),
                }
                if self.objs[obj as usize].escaped {
                    self.add(closure, ESC);
                } else {
                    self.objs[obj as usize].readers.push(closure);
                }
                self.await_exotic(
                    obj,
                    ProtocolWaiter {
                        callee,
                        iteration: None,
                    },
                );
            }
            Cx::Iterate { dst, callee, .. } => {
                let waiter = ProtocolWaiter {
                    callee,
                    iteration: Some(dst),
                };
                match self.objs[obj as usize].kind {
                    ObjKind::Array | ObjKind::NativeResult => {
                        let elements = self.elems(obj);
                        self.edge(elements, dst);
                        if self.objs[obj as usize].escaped {
                            self.add(dst, ESC);
                        } else {
                            self.objs[obj as usize].readers.push(dst);
                        }
                        self.await_exotic(obj, waiter);
                    }
                    ObjKind::Unknown => self.add(dst, obj),
                    ObjKind::Intrinsic | ObjKind::Native(_) => self.add(dst, ESC),
                    _ => self.run_protocol_methods(obj, waiter),
                }
            }
            Cx::Touch { .. } => {}
            Cx::OnAny { from, to } => self.edge(from, to),
            Cx::ExoticKey { target } => {
                // A symbol source yields symbols. Other unknown values are
                // assumed not to be well-known symbols unless the analysis
                // is told otherwise. An object key converts through user
                // code, whose result the generator subscribes as well.
                if obj == SYMBOLS
                    || (self.unknown_keys_may_be_symbols
                        && self.objs[obj as usize].kind == ObjKind::Unknown)
                {
                    self.edge(target, EXOTIC_SINK);
                }
            }
            Cx::ProtoLink { slot, into } => {
                let chain = self.chain(obj, slot);
                self.edge(chain, into);
            }
            Cx::EscapedSink => self.mark_escaped(obj),
            Cx::ExoticSink => {
                if self.is_alloc(obj) && !self.objs[obj as usize].exotic {
                    self.objs[obj as usize].exotic = true;
                    for waiter in self.protocol_waiters.remove(&obj).unwrap_or_default() {
                        self.run_protocol_methods(obj, waiter);
                    }
                }
            }
        }
    }

    /// The native behaviors a call with a receiver may reach.
    pub fn native_behaviors(&self, call: CxId) -> Vec<NativeBehavior> {
        let behaviors = self
            .native_calls
            .get(&call)
            .map_or(0, |state| state.behaviors);
        RECEIVER_BEHAVIORS
            .into_iter()
            .filter(|behavior| behaviors & behavior.bit() != 0)
            .collect()
    }

    /// Runs the object's protocol methods for the operation if a key that
    /// may be a symbol is ever stored on it.
    fn await_exotic(&mut self, obj: Obj, waiter: ProtocolWaiter) {
        if self.objs[obj as usize].exotic {
            self.run_protocol_methods(obj, waiter);
        } else {
            self.protocol_waiters.entry(obj).or_default().push(waiter);
        }
    }

    /// A user-defined coercion or iterator: every function stored anywhere
    /// on `obj` or its prototypes may run. An iteration's values are
    /// whatever they produce.
    fn run_protocol_methods(&mut self, obj: Obj, waiter: ProtocolWaiter) {
        let methods = self.chain(obj, Slot::All);
        self.edge(methods, waiter.callee);
        if let Some(dst) = waiter.iteration {
            self.add(dst, ESC);
        }
    }

    fn mark_escaped(&mut self, obj: Obj) {
        if !self.is_alloc(obj) || self.objs[obj as usize].escaped {
            return;
        }
        self.objs[obj as usize].escaped = true;
        self.function_prototype(obj);
        // Everything stored on it is reachable, and unknown code may store
        // anything on it.
        let all = self.slot(obj, Slot::All);
        self.edge(all, ESCAPED_SINK);
        for slot in [Slot::Getters, Slot::Setters] {
            let accessors = self.slot(obj, slot);
            self.edge(accessors, ESCAPED_SINK);
        }
        self.add(all, ESC);
        if let Some(elems) = self.objs[obj as usize].elems {
            self.add(elems, ESC);
        }
        let readers = std::mem::take(&mut self.objs[obj as usize].readers);
        for reader in readers
            .into_iter()
            .chain(self.objs[obj as usize].named_readers.clone())
        {
            self.add(reader, ESC);
        }
        let proto = self.objs[obj as usize].proto;
        self.edge(proto, ESCAPED_SINK);
        if let ObjKind::Function(index) = self.objs[obj as usize].kind {
            self.escaped_fns.push(obj);
            let info = &self.fns[index as usize];
            let (params, all_args, this, result) =
                (info.params.clone(), info.all_args, info.this, info.result);
            for param in params {
                self.add(param, ESC);
            }
            self.add(all_args, ESC);
            if let Some(this) = this {
                self.add(this, ESC);
            }
            self.edge(result, ESCAPED_SINK);
        }
    }

    fn escape_call(&mut self, this: Option<Node>, args: &[Arg]) {
        if let Some(this) = this {
            self.edge(this, ESCAPED_SINK);
        }
        for arg in args {
            self.edge(arg.node, ESCAPED_SINK);
        }
    }

    fn bind_call(
        &mut self,
        index: u32,
        this: Option<Node>,
        args: &[Arg],
        dst: Node,
        new_site: Option<Obj>,
    ) {
        let info = &self.fns[index as usize];
        let (obj, params, all_args, own_this, result) = (
            info.obj,
            info.params.clone(),
            info.all_args,
            info.this,
            info.result,
        );
        let mut positional = true;
        for (position, arg) in args.iter().enumerate() {
            self.edge(arg.node, all_args);
            positional &= !arg.spread;
            if positional {
                if let Some(param) = params.get(position) {
                    self.edge(arg.node, *param);
                }
            } else {
                for param in &params {
                    self.edge(arg.node, *param);
                }
            }
        }
        if let Some(own_this) = own_this {
            if let Some(new_site) = new_site {
                self.add(own_this, new_site);
                self.add(dst, new_site);
                self.function_prototype(obj);
                let prototypes = self.own(obj, self.prototype_prop);
                let proto = self.objs[new_site as usize].proto;
                self.edge(prototypes, proto);
            } else if let Some(this) = this {
                self.edge(this, own_this);
            }
        }
        self.edge(result, dst);
    }

    fn native_call(
        &mut self,
        id: CxId,
        behavior: NativeBehavior,
        this: Option<Node>,
        args: &[Arg],
        dst: Node,
        coercion: bool,
    ) {
        let (unit, span_lo) = (self.cxs[id as usize].unit, self.cxs[id as usize].span_lo);
        let Some(this) = this.filter(|_| behavior != NativeBehavior::Unknown) else {
            if !self.unknown_native_calls.insert(id) {
                return;
            }
            self.escape_call(this, args);
            // Fresh when every operand is primitive, unknown otherwise.
            let fresh = self.native_result();
            self.add(dst, fresh);
            for operand in this.into_iter().chain(args.iter().map(|arg| arg.node)) {
                let taint = self.constraint(
                    Cx::OnAny {
                        from: ESC_NODE,
                        to: dst,
                    },
                    unit,
                    span_lo,
                );
                self.subscribe(operand, taint);
            }
            return;
        };
        let state = self.native_calls.entry(id).or_default();
        if state.behaviors & behavior.bit() != 0 {
            return;
        }
        state.behaviors |= behavior.bit();
        let receiver_cx = state.receiver;
        match behavior {
            NativeBehavior::Unknown => unreachable!("filtered above"),
            // A coercion already covers its arrays' elements.
            NativeBehavior::ToPrimitive if coercion => {}
            NativeBehavior::ToPrimitive => {
                self.coerce(this, unit, span_lo);
            }
            NativeBehavior::ValueOf => self.edge(this, dst),
            NativeBehavior::OwnPropertyQuery => {
                for arg in args {
                    self.coerce(arg.node, unit, span_lo);
                }
            }
            NativeBehavior::Call | NativeBehavior::Apply => {
                // The receivers are the functions called.
                let call_this = args.first().map(|arg| arg.node);
                let call_args = if behavior == NativeBehavior::Call {
                    args.iter().skip(1).copied().collect()
                } else {
                    match args.get(1) {
                        Some(list) => vec![Arg {
                            node: self.iterate(list.node, unit, span_lo),
                            spread: true,
                        }],
                        None => Vec::new(),
                    }
                };
                self.constraint(
                    Cx::Call {
                        callee: this,
                        this: call_this,
                        args: call_args,
                        dst,
                        new_site: None,
                        root: Root::Other,
                        coercion: false,
                    },
                    unit,
                    span_lo,
                );
            }
            NativeBehavior::Bind | NativeBehavior::HasInstance => {}
        }
        // The receiver-dependent part, for each receiver now and later.
        match receiver_cx {
            Some(_) => {
                let receivers: Vec<Obj> = self.pts[this as usize].iter().collect();
                for receiver in receivers {
                    self.native_receiver(id, behavior, receiver);
                }
            }
            None => {
                let receiver_cx = self.constraint(Cx::NativeReceiver { call: id }, unit, span_lo);
                self.native_calls
                    .get_mut(&id)
                    .expect("inserted above")
                    .receiver = Some(receiver_cx);
                self.subscribe(this, receiver_cx);
                if !self.solving {
                    unreachable!("native calls are only applied while solving");
                }
            }
        }
    }

    fn native_receiver(&mut self, call: CxId, behavior: NativeBehavior, receiver: Obj) {
        let Cx::Call {
            this: Some(this),
            args,
            dst,
            ..
        } = self.cxs[call as usize].cx.clone()
        else {
            unreachable!("a native receiver belongs to a call with a receiver");
        };
        if !self.is_alloc(receiver) {
            // An unknown receiver, or `call`/`apply`/`bind` of a native.
            if self.objs[receiver as usize].kind == ObjKind::Unknown || behavior.invokes_receiver()
            {
                self.escape_call(Some(this), &args);
                self.add(dst, ESC);
            }
            return;
        }
        if behavior == NativeBehavior::Bind {
            // The bound function is the function itself, called later with
            // shifted arguments and the bound receiver.
            self.add(dst, receiver);
            if let Some(info) = self.function(receiver) {
                let (params, all_args, own_this) = (info.params.clone(), info.all_args, info.this);
                for param in &params {
                    self.edge(all_args, *param);
                }
                for (position, arg) in args.iter().enumerate() {
                    self.edge(arg.node, all_args);
                    if position == 0
                        && let Some(own_this) = own_this
                    {
                        self.edge(arg.node, own_this);
                    }
                }
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeBehavior {
    /// No summary: operands escape, and the call is opaque unless every
    /// operand is primitive.
    Unknown,
    /// `toString`/`toLocaleString`: silent on an ordinary object; an array
    /// joins, coercing its elements.
    ToPrimitive,
    ValueOf,
    /// `hasOwnProperty` and friends: coerce the key, run no other code.
    OwnPropertyQuery,
    Call,
    Apply,
    Bind,
    /// `Function.prototype[Symbol.hasInstance]`: walks prototype chains and
    /// runs no code on a modeled receiver.
    HasInstance,
}

pub const RECEIVER_BEHAVIORS: [NativeBehavior; 7] = [
    NativeBehavior::HasInstance,
    NativeBehavior::ToPrimitive,
    NativeBehavior::ValueOf,
    NativeBehavior::OwnPropertyQuery,
    NativeBehavior::Call,
    NativeBehavior::Apply,
    NativeBehavior::Bind,
];

impl NativeBehavior {
    fn bit(self) -> u8 {
        match self {
            Self::Unknown => 0,
            Self::ToPrimitive => 1,
            Self::ValueOf => 2,
            Self::OwnPropertyQuery => 4,
            Self::Call => 8,
            Self::Apply => 16,
            Self::Bind => 32,
            Self::HasInstance => 64,
        }
    }

    pub fn invokes_receiver(self) -> bool {
        matches!(self, Self::Call | Self::Apply | Self::Bind)
    }
}

pub fn native_behavior(name: &str) -> NativeBehavior {
    match name {
        "toString" | "toLocaleString" => NativeBehavior::ToPrimitive,
        "valueOf" => NativeBehavior::ValueOf,
        "hasOwnProperty" | "isPrototypeOf" | "propertyIsEnumerable" => {
            NativeBehavior::OwnPropertyQuery
        }
        "@@hasInstance" => NativeBehavior::HasInstance,
        "call" => NativeBehavior::Call,
        "apply" => NativeBehavior::Apply,
        "bind" => NativeBehavior::Bind,
        _ => NativeBehavior::Unknown,
    }
}
