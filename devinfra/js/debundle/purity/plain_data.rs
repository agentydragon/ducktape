use super::*;

/// Collect chunk-top `const` / `let` / `var`-bound bindings whose
/// initializer(s) are plain-literal data shapes (plain object literal
/// with no accessors/methods/spreads/computed keys/`__proto__`, or
/// plain array literal) AND whose binding cell is never written
/// through anywhere in the chunk's AST. See `ChunkBinding::PlainData`
/// for the soundness argument.
///
/// `var` admission notes:
///
/// * `var X = init` at chunk top is hoisted but the initializer
///   evaluates at the source line. Pre-init reads on `X` see
///   `undefined`; member access `undefined.k` throws a TypeError —
///   a spec-mandated throw that fires no user-defined code, so
///   classifying member reads on `X` as pure remains sound (the
///   "pure" claim is "fires no user code", not "always succeeds").
/// * Multiple `var X = init_n` statements at chunk top are legal:
///   each is a no-op redeclaration of the binding cell plus an
///   assignment at the init line. Every init must independently be
///   a plain-literal shape; if any decl's init is non-plain, the
///   name is disqualified. The same name appearing both via a
///   `var X = plain` and later via `X = nonPlain` (ident assign)
///   is caught by the regular `PlainDataWriteScanner` ident-assign
///   check.
/// * `var X;` with no initializer contributes nothing to the
///   candidate; if accompanied by a later `var X = plain` /
///   `X = plain` write the binding still admits (init from the
///   plain decl/assign). If alone, X stays undefined forever and
///   isn't admitted (member-reads on `undefined` throw —
///   technically still pure, but uninteresting).
///
/// Returns the list of qualified names; the caller registers them as
/// `ChunkBinding::PlainData` in the graph.
pub(crate) fn collect_plain_data_bindings(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> Vec<String> {
    // Aggregate every chunk-top init expression per binding name.
    // The same name can have multiple inits for `var`-bound bindings
    // (`var X = a; var X = b;`); every init must independently pass
    // the plain-literal shape check or the name disqualifies.
    let mut inits_by_name: BTreeMap<String, Vec<(&Expr, VarDeclKind)>> = BTreeMap::new();
    for item in body {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            inits_by_name.entry(name).or_default().push((init, kind));
        }
    }
    let candidates: BTreeSet<String> = inits_by_name
        .into_iter()
        .filter(|(name, inits)| {
            inits
                .iter()
                .all(|(init, kind)| is_plain_data_init(init, name, *kind))
        })
        .map(|(name, _)| name)
        .collect();
    if candidates.is_empty() {
        return Vec::new();
    }
    // Scan the entire chunk body (including function/class bodies) for
    // anything that could install an accessor or change the prototype
    // of any candidate, or let the candidate's object reference ESCAPE
    // into code we can't analyze:
    //
    // * member writes / updates / deletes on `X.k` / `X[k]`;
    // * calls to the hostile builtins in `PLAIN_DATA_HOSTILE_BUILTINS`
    //   (`Object.{defineProperty,defineProperties,setPrototypeOf,assign}`,
    //   `Reflect.{defineProperty,set,setPrototypeOf,deleteProperty}`)
    //   with `X` as the first argument;
    // * any *escaping* bare reference to `X` — as a call/new argument,
    //   array element, object property value, alias RHS (`const Y = X`,
    //   `obj.k = X`), or any other position not on the scanner's short
    //   list of provably non-capturing reads (member receiver, spread
    //   source, `typeof`/`!`/`void` operand, return value / concise
    //   arrow body). Once the reference is aliased, a write through the
    //   alias (`Object.defineProperty(Y, …)`) would defeat the
    //   name-based write scan, so escape itself disqualifies.
    //
    // Plain data-property writes (`X.foo = bar`) don't install
    // accessors and don't change the prototype chain, but the
    // conservative check rejects them anyway — the cost is dropping
    // a few PlainData candidates that would still be sound, and the
    // benefit is one short rule that's easy to audit.
    let mut scanner = PlainDataWriteScanner {
        candidates: &candidates,
        shadowed,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    for item in body {
        item.as_module_item().visit_with(&mut scanner);
    }
    let disqualified = scanner.disqualified;
    candidates
        .into_iter()
        .filter(|n| !disqualified.contains(n))
        .collect()
}

const SAFE_PLAIN_ARRAY_METHODS: &[&str] = &["filter", "flatMap", "forEach", "map", "slice"];
const ARRAY_PRODUCING_METHODS: &[&str] = &["filter", "flatMap", "map", "slice"];

pub(crate) fn collect_plain_array_bindings(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> BTreeSet<String> {
    let mut const_inits = Vec::new();
    for item in body {
        const_inits.extend(
            plain_data_var_candidates(item.as_module_item())
                .into_iter()
                .filter_map(|(name, init, kind)| {
                    (kind == VarDeclKind::Const).then_some((name, init))
                }),
        );
    }
    let mut candidates = BTreeSet::new();
    loop {
        let before = candidates.len();
        for (name, init) in &const_inits {
            if expr_returns_plain_array(init, &candidates) {
                candidates.insert(name.clone());
            }
        }
        if candidates.len() == before {
            break;
        }
    }
    if candidates.is_empty() {
        return BTreeSet::new();
    }

    let mut plain_data_scanner = PlainDataWriteScanner {
        candidates: &candidates,
        shadowed,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    let mut method_scanner = PlainArrayMethodScanner {
        candidates: &candidates,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    for item in body {
        let item = item.as_module_item();
        item.visit_with(&mut plain_data_scanner);
        item.visit_with(&mut method_scanner);
    }

    let mut disqualified = plain_data_scanner.disqualified;
    disqualified.extend(method_scanner.disqualified);
    candidates
        .into_iter()
        .filter(|name| !disqualified.contains(name))
        .collect()
}

fn expr_returns_plain_array(expr: &Expr, known_plain_arrays: &BTreeSet<String>) -> bool {
    match strip_parens(expr) {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            let Expr::Member(member) = strip_parens(callee) else {
                return false;
            };
            matches!(
                &member.prop,
                MemberProp::Ident(prop) if ARRAY_PRODUCING_METHODS.contains(&prop.sym.as_ref())
            ) && expr_is_plain_array_receiver(member.obj.as_ref(), known_plain_arrays)
        }
        _ => false,
    }
}

fn expr_is_plain_array_receiver(expr: &Expr, known_plain_arrays: &BTreeSet<String>) -> bool {
    match strip_parens(expr) {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(_) => expr_returns_plain_array(expr, known_plain_arrays),
        _ => false,
    }
}

/// Chunk-top `const X = <init>` names whose init evaluates to a
/// **primitive** value, resolving references to earlier such consts.
/// `const` guarantees the binding never rebinds, and a primitive
/// carries no user accessors, so coercing a reference to the name
/// fires no user code regardless of how the init was computed (the
/// init's own evaluation effects are classified at its declaration
/// site — only the resulting value class matters here). `let` / `var`
/// are excluded: a later non-primitive rebind would invalidate the
/// claim.
///
/// Forward pass in source order: a const resolves only references to
/// consts declared before it — exactly the set visible without
/// hitting the temporal dead zone. Function/arrow inits never reach
/// here (`plain_data_var_candidates` filters them) and are not
/// primitive anyway.
pub(crate) fn collect_primitive_const_bindings(body: &[TopLevelItemView<'_>]) -> BTreeSet<String> {
    let mut primitives: BTreeSet<String> = BTreeSet::new();
    let no_local_shadow: BTreeSet<String> = BTreeSet::new();
    for item in body {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            if kind == VarDeclKind::Const
                && !primitives.contains(&name)
                && is_result_primitive(init, &primitives, &no_local_shadow)
            {
                primitives.insert(name);
            }
        }
    }
    primitives
}

/// Close the author-asserted fluent roots (`seed`, the projected
/// `fluent_exports` locals) over chunk-top `const X = <fluent chain>`
/// declarations: `const Base = k.object({...})` makes `Base` a fluent
/// root too, so `Base.extend({...})` downstream is admitted. `const`
/// only — a `let`/`var` cell can be rebound to an untrusted value.
///
/// Only the *value class* is derived here (the value came out of the
/// trusted API, so the author's deep-purity assertion covers it); the
/// init's own evaluation effects — impure arguments anywhere in the
/// chain — are classified separately at the declaration site by
/// `classify_fluent_chain`. This mirrors the
/// `primitive_const_bindings` value-class/evaluation split.
///
/// Fixpoint loop rather than a single forward pass so a chain rooted
/// in a later-declared const (legal for lazily-evaluated references,
/// and cheap to cover) still closes; bounded by one insertion per
/// chunk-top const.
pub(crate) fn collect_fluent_const_bindings(
    body: &[TopLevelItemView<'_>],
    seed: &BTreeSet<String>,
) -> BTreeSet<String> {
    let mut fluent = seed.clone();
    if fluent.is_empty() {
        return fluent;
    }
    loop {
        let mut changed = false;
        for item in body {
            for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
                if kind == VarDeclKind::Const
                    && !fluent.contains(&name)
                    && fluent_chain_root(init).is_some_and(|root| fluent.contains(root))
                {
                    fluent.insert(name);
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }
    fluent
}

/// The root identifier of a fluent chain — a chain of static
/// (non-computed) member reads, calls, and optional-chaining forms of
/// those, hanging off a bare `Ident`. Returns `None` for any other
/// shape: computed members are excluded because admitting the lookup
/// would additionally require the key expression to be
/// ToPropertyKey-safe, and `new` is excluded because construction off
/// a fluent API is not part of the asserted contract (builder APIs
/// chain calls, not constructors).
pub(crate) fn fluent_chain_root(expr: &Expr) -> Option<&str> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.sym.as_ref()),
        Expr::Member(member) => match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
            MemberProp::Computed(_) => None,
        },
        Expr::Call(call) => match &call.callee {
            Callee::Expr(callee) => fluent_chain_root(callee),
            _ => None,
        },
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => match &member.prop {
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
                MemberProp::Computed(_) => None,
            },
            OptChainBase::Call(opt_call) => fluent_chain_root(&opt_call.callee),
        },
        _ => None,
    }
}

/// Yield `(name, init)` pairs for every chunk-top single-declarator
/// `const X = <init>` / `let X = <init>` / `var X = <init>` whose
/// initializer is NOT a function/arrow expression (those go through
/// `Function`). Comma-list declarations are pre-split by
/// `top_level_item_views`, so each `VarDecl` we see here has exactly
/// one `decl`.
///
/// All three keyword forms flow through the same scanner; the
/// scanner's check is uniform ("no member writes or hostile builtin
/// calls, all ident writes have plain-literal RHS"), so the
/// distinction is only at the candidate-collection stage:
///
/// * `const` — binding cell is immutable at the language level; only
///   the chunk-wide checks for accessor installation post-init
///   matter.
/// * `let` — every `X = rhs` ident assign anywhere in the chunk must
///   have a plain-literal RHS (enforced by `PlainDataWriteScanner`).
/// * `var` — same as `let`, PLUS multiple chunk-top `var X = init`
///   redeclarations are valid; the caller checks every init for a
///   given name against the plain-literal shape rule. Pre-init reads
///   on a hoisted `var X` see `undefined` and `undefined.k` throws a
///   spec-mandated TypeError — sound for the read-purity claim ("no
///   user code fires") because the throw is engine-emitted, not a
///   user getter.
fn plain_data_var_candidates(item: &ModuleItem) -> Vec<(String, &Expr, VarDeclKind)> {
    let var = match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => var,
            _ => return Vec::new(),
        },
        _ => return Vec::new(),
    };
    let mut out = Vec::new();
    for decl in &var.decls {
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        if matches!(init, Expr::Fn(_) | Expr::Arrow(_)) {
            continue;
        }
        out.push((binding.id.sym.to_string(), init, var.kind));
    }
    out
}

/// Pure syntactic check for "plain-literal data shape": an object
/// literal whose own properties are exclusively `KeyValue` /
/// `Shorthand` / object spread (`{...src}`) with non-computed,
/// non-`__proto__` keys, or an array literal whose own elements are
/// values or array spreads (holes are fine — they read as
/// `undefined`, no getter).
///
/// **Spreads are permitted.** `{...src}` evaluates `src`'s own
/// enumerable properties via `CopyDataProperties` and writes the
/// resulting values to the target via `CreateDataPropertyOrThrow` —
/// which always produces a data descriptor, regardless of the
/// source's descriptor shape. So even `{...sourceWithGetters}`
/// yields a plain-data target. The spread itself fires source
/// getters AT INIT (impure event, classified independently by the
/// existing `ObjectSpread`/`ArraySpread` rules), but the resulting
/// receiver has no accessor channels, which is what this check
/// gates.
///
/// Value sub-expressions are intentionally NOT validated here: the
/// surrounding statement's at-init purity is the existing
/// classifier's concern, and the safety of post-init member reads
/// depends only on the receiver's shape (no accessors at init + no
/// post-init accessor installation, the latter enforced by
/// `PlainDataWriteScanner`).
pub(crate) fn is_plain_data_init(expr: &Expr, binding: &str, kind: VarDeclKind) -> bool {
    match expr {
        Expr::Paren(p) => is_plain_data_init(&p.expr, binding, kind),
        Expr::Object(obj) => obj.props.iter().all(is_plain_data_prop),
        Expr::Array(_) => true,
        // TS-enum-style IIFE: `((p) => (p.A = "a", p.B = "b", p))(X || {})`
        // produces a plain object at runtime by mutating the parameter
        // through data-property writes only and returning it. See
        // `is_ts_enum_iife_init_for_binding` for the syntactic shape
        // and soundness argument.
        Expr::Call(_) if kind == VarDeclKind::Var => {
            is_ts_enum_iife_init_for_binding(expr, binding)
        }
        _ => false,
    }
}

pub(crate) fn is_plain_data_prop(prop: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(prop) = prop else {
        // `PropOrSpread::Spread` is the `{...src}` form — permitted
        // (see `is_plain_data_init` doc-comment for the
        // CopyDataProperties soundness argument).
        return true;
    };
    match prop.as_ref() {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => match &kv.key {
            // `{__proto__: X}` in an object literal SETS the prototype
            // (ES262 §13.2.5.5); if X carries accessor properties, reads
            // on the parent literal fire them. Reject explicitly.
            // The computed form `{["__proto__"]: X}` does NOT set the
            // prototype (per spec), but it's a computed key — rejected
            // below.
            PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
            PropName::Str(s) => s.value.to_string_lossy() != "__proto__",
            PropName::Num(_) | PropName::BigInt(_) => true,
            PropName::Computed(_) => false,
        },
        // Methods, getters, setters, and `{key=value}` shorthand assign
        // are all rejected — only data properties qualify.
        Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
    }
}

/// Visitor that disqualifies plain-data candidates seen as the target
/// of a member write/update/delete, as the first argument to one of a
/// small set of accessor- / prototype-installing built-ins, or in any
/// ESCAPING position.
///
/// **Escape analysis.** The write scan above is name-based: it only
/// catches mutations spelled through the candidate's own identifier.
/// An alias (`const Y = X; Object.defineProperty(Y, …)`) or any other
/// captured reference (call argument, array element, object property
/// value) defeats it. The scanner therefore treats every bare
/// candidate `Ident` as an escape — and disqualifies — unless the
/// occurrence is in one of a short list of provably non-capturing
/// read positions:
///
/// * member-access receiver (`X.k`, `X[k]`, `X?.k`) — a read;
/// * spread source (`{...X}`, `[...X]`, `f(...X)`) — copies values /
///   iterates; the receiver object's identity is not captured;
/// * `typeof X` / `!X` / `void X` operands — transient inspection;
/// * single argument of `Object.{keys,values,entries,freeze,
///   fromEntries}` with the global `Object` unshadowed — read-only /
///   descriptor-tightening builtins that install no accessors (the
///   same set `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admits);
/// * the vetted `X || (X = {})` argument of a recognized TS-enum
///   IIFE init (`is_ts_enum_iife_call_for_binding`);
/// * `return X` / concise arrow body `() => X` — accepted by scope
///   decision: consumers of chunk-internal return values (like
///   importers of an exported PlainData binding) are assumed not to
///   install accessors on it; see docs/purity_soundness.md §
///   "ChunkBinding::PlainData" for the residual-assumption note.
///   Export specifiers (`export { X }`) are non-escaping under the
///   same assumption.
///
/// **Function/arrow parameter scope tracking.** When entering a
/// function or arrow body whose parameters include names that
/// collide with candidates, those names refer to the inner parameter
/// (NOT the outer chunk-top binding) for the duration of the body.
/// Writes on the inner alias would otherwise spuriously disqualify
/// the outer candidate — the canonical TS-enum IIFE shape uses
/// `(function (X) { X["A"] = "a"; })(X || (X = {}))` with the param
/// named the same as the binding. The scanner pushes a "shadowing"
/// set on each function/arrow scope and checks it before
/// disqualifying.
///
/// Block-scoped `let X` / `const X` / inner `var X` declarations also
/// shadow the outer X within their scope, but tracking those
/// requires walking block scopes (a larger change). For now only
/// function/arrow parameters are tracked — the cases this misses
/// are conservative false negatives (we reject an outer X that
/// would have been admissible), never false positives.
pub(crate) struct PlainDataWriteScanner<'a> {
    candidates: &'a BTreeSet<String>,
    /// Chunk-top shadowed-globals set (A8): consulted by the
    /// `Object.{keys,…}(X)` non-escaping-position exemption, which
    /// must not fire when the chunk rebinds `Object`.
    shadowed: &'a BTreeSet<&'static str>,
    disqualified: BTreeSet<String>,
    /// Stack of per-scope candidate-shadowing sets. Each scope entry
    /// is the subset of `candidates` shadowed by the function/arrow
    /// parameters at that level. A name is "shadowed at this point
    /// in the traversal" iff it appears in any active stack entry.
    shadowing_scopes: Vec<BTreeSet<String>>,
}

impl PlainDataWriteScanner<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.shadowing_scopes.push(scope);
        f(self);
        self.shadowing_scopes.pop();
    }

    /// Collect the subset of `candidates` shadowed by `params`.
    /// Parameter defaults/rest/destructuring all introduce bindings
    /// for the whole parameter scope. That matters for emitted helper
    /// shapes like `(i, m = helper, d = m.f || (m.f = [])) => ...`:
    /// the `m.f = []` write targets the parameter `m`, not a
    /// chunk-top plain-data object also named `m`.
    fn shadowed_by_params<'a, I>(&self, params: I) -> BTreeSet<String>
    where
        I: IntoIterator<Item = &'a Pat>,
    {
        let mut out = BTreeSet::new();
        for param in params {
            self.collect_shadowed_by_pat(param, &mut out);
        }
        out
    }

    fn collect_shadowed_by_pat(&self, pat: &Pat, out: &mut BTreeSet<String>) {
        match pat {
            Pat::Ident(ident) => {
                let name = ident.id.sym.as_ref();
                if self.candidates.contains(name) {
                    out.insert(name.to_string());
                }
            }
            Pat::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
            Pat::Assign(assign) => self.collect_shadowed_by_pat(&assign.left, out),
            Pat::Array(array) => {
                for elem in array.elems.iter().flatten() {
                    self.collect_shadowed_by_pat(elem, out);
                }
            }
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(kv) => self.collect_shadowed_by_pat(&kv.value, out),
                        ObjectPatProp::Assign(assign) => {
                            let name = assign.key.id.sym.as_ref();
                            if self.candidates.contains(name) {
                                out.insert(name.to_string());
                            }
                        }
                        ObjectPatProp::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
                    }
                }
            }
            Pat::Invalid(_) | Pat::Expr(_) => {}
        }
    }
}

/// Defensive visitor that collects candidate-named idents in a
/// compound assignment target (e.g. inside `[a, ...rest] = …` or
/// `({x} = …)`) so the scanner can disqualify them. The scanner
/// uses this only on shapes outside the explicit Member / Simple-Ident
/// arms — destructuring etc.
struct IdentVisitor<'a> {
    candidates: &'a BTreeSet<String>,
    hit: BTreeSet<String>,
}

impl Visit for IdentVisitor<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if self.candidates.contains(node.sym.as_ref()) {
            self.hit.insert(node.sym.to_string());
        }
    }
}

impl PlainDataWriteScanner<'_> {
    fn disqualify_if_candidate(&mut self, name: &str) {
        if self.is_shadowed(name) {
            // The reference targets a shadowing inner binding (e.g. a
            // function parameter), not the outer chunk-top candidate.
            // Writes on the inner binding don't affect the outer
            // object so they don't disqualify it.
            return;
        }
        if self.candidates.contains(name) {
            self.disqualified.insert(name.to_string());
        }
    }

    /// If `expr` is a member access whose receiver is a candidate Ident,
    /// disqualify that candidate. Handles plain Member and OptChain Member
    /// receivers, walking through `Paren` wrappers.
    fn disqualify_member_receiver(&mut self, expr: &Expr) {
        let mut cur = expr;
        loop {
            match cur {
                Expr::Paren(p) => cur = &p.expr,
                Expr::Member(member) => {
                    if let Expr::Ident(recv) = member.obj.as_ref() {
                        self.disqualify_if_candidate(recv.sym.as_ref());
                    }
                    return;
                }
                Expr::OptChain(opt) => match &*opt.base {
                    OptChainBase::Member(member) => {
                        if let Expr::Ident(recv) = member.obj.as_ref() {
                            self.disqualify_if_candidate(recv.sym.as_ref());
                        }
                        return;
                    }
                    OptChainBase::Call(_) => return,
                },
                _ => return,
            }
        }
    }
}

impl Visit for PlainDataWriteScanner<'_> {
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            // `X.k = …` / `X[k] = …` — member write. Disqualify
            // regardless of RHS shape: even when the RHS is a
            // plain literal, the write installs a property on the
            // existing `X` object (whose other reads we were
            // promising to keep pure post-init). Plain data writes
            // are sound for read-purity but the conservative rule
            // is "X must never be written through" so the chunk-
            // wide invariant reads simply.
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Expr::Ident(recv) = member.obj.as_ref() {
                    self.disqualify_if_candidate(recv.sym.as_ref());
                }
            }
            // `X = rhs` — plain identifier reassignment. Only
            // candidates registered as `let` can encounter this
            // legitimately (`const` re-assign is a syntax error).
            // Admit only when `rhs` is itself a plain-literal data
            // shape — anything else (a call, an Ident referencing
            // an unknown source, a binary op, etc.) could produce
            // an accessor-bearing value at runtime, which would
            // break the read-purity invariant for subsequent
            // member reads on `X`.
            //
            // For non-`let` candidates this branch can still fire
            // (e.g. an inner-scope shadow assignment in a function
            // body) but disqualifying conservatively is safe —
            // false negatives never violate soundness, and tracking
            // scopes here would be a larger lift than the case
            // requires.
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                if self.candidates.contains(binding.sym.as_ref())
                    && !is_plain_data_init(&node.right, binding.sym.as_ref(), VarDeclKind::Let)
                {
                    self.disqualify_if_candidate(binding.sym.as_ref());
                }
            }
            // Compound assignment targets (paren wrappers, opt-chains)
            // and destructuring targets (`AssignTarget::Pat`) don't
            // come up in chunk-top binding mutator patterns we
            // intend to admit; disqualify defensively if they
            // mention a candidate name.
            _ => {
                let mut idents = IdentVisitor {
                    candidates: self.candidates,
                    hit: BTreeSet::new(),
                };
                node.left.visit_with(&mut idents);
                for name in idents.hit {
                    self.disqualify_if_candidate(&name);
                }
            }
        }
        // For `=` the RHS still needs to descend (e.g. an inner
        // nested write to a candidate inside the RHS expression).
        // For compound `+=` etc., even on an admitted plain-RHS
        // shape, the read-modify-write semantics make it unsafe;
        // SWC's `AssignExpr` carries `op`, but we already
        // disqualified all simple-Ident assigns whose RHS isn't a
        // plain literal — and any compound op produces a non-literal
        // value at runtime (`X += {a:1}` is `X = X + {a:1}` which
        // is a string/number). So no further check needed.
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        // `X++` / `--X` on the binding cell itself produces a
        // number; not a plain-literal shape. Disqualify Ident
        // targets in addition to the existing Member-target rule.
        if let Expr::Ident(ident) = node.arg.as_ref() {
            self.disqualify_if_candidate(ident.sym.as_ref());
        }
        self.disqualify_member_receiver(&node.arg);
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete {
            self.disqualify_member_receiver(&node.arg);
        }
        // `typeof X` / `!X` / `void X` are transient inspections of
        // the value — the reference isn't captured. Skip the bare
        // candidate Ident so the escape default doesn't fire.
        if matches!(node.op, UnaryOp::TypeOf | UnaryOp::Bang | UnaryOp::Void)
            && matches!(strip_parens(&node.arg), Expr::Ident(_))
        {
            return;
        }
        node.visit_children_with(self);
    }

    // ESCAPE DEFAULT: any bare candidate Ident reached by the
    // traversal is a captured reference (alias RHS, call/new arg,
    // array element, object property value, shorthand prop, …) and
    // disqualifies. The non-capturing read positions on the short
    // list in the struct doc-comment are skipped by the overrides
    // below before traversal reaches the Ident.
    fn visit_ident(&mut self, node: &Ident) {
        self.disqualify_if_candidate(node.sym.as_ref());
    }

    // Binding positions (declarator names, params, catch bindings)
    // introduce a binding rather than reading the candidate's value
    // — not an escape.
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    // `export { X }` is non-escaping by scope decision (see the
    // struct doc-comment): importers of an exported PlainData
    // binding are assumed not to install accessors on it.
    fn visit_export_named_specifier(&mut self, _node: &ExportNamedSpecifier) {}

    // `import { <exported> as <local> }` — the local side is a
    // BindingIdent (already skipped); the imported side is the SOURCE
    // module's export name, pure module metadata that never reads any
    // local binding's value. Without this override the escape default
    // fired on the imported-name `Ident`, disqualifying an unrelated
    // local candidate that happens to share its spelling — real
    // bundles import hundreds of single-letter vendor export names,
    // so candidate enums named `j`/`a`/… were lost to coincidence.
    fn visit_import_named_specifier(&mut self, _node: &ImportNamedSpecifier) {}

    // Member-access receivers are reads, not captures. Skip an
    // Ident receiver (candidate or not); everything else (nested
    // receivers, computed keys) is traversed normally.
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if !matches!(strip_parens(&node.obj), Expr::Ident(_)) {
            node.obj.visit_with(self);
        }
        node.prop.visit_with(self);
    }

    // Spread sources (`{...X}`, `[...X]`, `f(...X)`) copy values /
    // iterate; the receiver object's identity is not captured.
    fn visit_spread_element(&mut self, node: &SpreadElement) {
        if matches!(strip_parens(&node.expr), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, node: &ExprOrSpread) {
        if node.spread.is_some() && matches!(strip_parens(&node.expr), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    // `return X` is non-escaping by scope decision (see the struct
    // doc-comment's residual-assumption note).
    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(arg) = node.arg.as_deref()
            && matches!(strip_parens(arg), Expr::Ident(_))
        {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        // Hostile accessor-/prototype-installing builtins with the
        // candidate as first arg. Redundant with the escape default
        // (a call arg is an escape) but kept as an explicit,
        // self-documenting check.
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.as_ref()
            && let (Expr::Ident(obj), MemberProp::Ident(prop)) = (member.obj.as_ref(), &member.prop)
            && PLAIN_DATA_HOSTILE_BUILTINS
                .iter()
                .any(|(r, p)| *r == obj.sym.as_ref() && *p == prop.sym.as_ref())
            && let Some(arg) = node.args.first()
            && arg.spread.is_none()
            && let Expr::Ident(target) = arg.expr.as_ref()
        {
            self.disqualify_if_candidate(target.sym.as_ref());
        }
        // `Object.{keys,values,entries,freeze,fromEntries}(X)` with
        // the global `Object` unshadowed — read-only /
        // descriptor-tightening builtins that install no accessors
        // (the same set `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admits).
        // Skip the argument so the escape default doesn't fire.
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.as_ref()
            && let (Expr::Ident(obj), MemberProp::Ident(prop)) = (member.obj.as_ref(), &member.prop)
            && obj.sym.as_ref() == "Object"
            && !self.shadowed.contains("Object")
            && PURE_OBJECT_CALLS_ON_PLAIN_DATA
                .iter()
                .any(|(_, p)| *p == prop.sym.as_ref())
            && node.args.len() == 1
            && node.args[0].spread.is_none()
            && matches!(strip_parens(&node.args[0].expr), Expr::Ident(_))
        {
            // Receiver `Object` and the static prop carry no
            // candidate refs; nothing else to visit.
            return;
        }
        // The vetted `X || (X = {})` argument of a recognized
        // TS-enum IIFE init: the only callee shapes the recognizer
        // admits are inline arrow/function expressions whose bodies
        // mutate their own parameter — visit the callee (the
        // param-scope tracking exempts same-named param writes) and
        // skip the vetted argument.
        if self
            .candidates
            .iter()
            .any(|c| !self.is_shadowed(c) && is_ts_enum_iife_call_for_binding(node, c))
        {
            node.callee.visit_with(self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        self.with_scope(scope, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            // A concise body that is a bare candidate Ident is the
            // `() => X` return position — non-escaping by the same
            // scope decision as `return X`.
            match node.body.as_ref() {
                BlockStmtOrExpr::Expr(expr) if matches!(strip_parens(expr), Expr::Ident(_)) => {}
                body => body.visit_with(s),
            }
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        self.with_scope(scope, |s| node.visit_children_with(s));
    }
}

struct PlainArrayMethodScanner<'a> {
    candidates: &'a BTreeSet<String>,
    disqualified: BTreeSet<String>,
    shadowing_scopes: Vec<BTreeSet<String>>,
}

impl PlainArrayMethodScanner<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.shadowing_scopes.push(scope);
        f(self);
        self.shadowing_scopes.pop();
    }

    fn disqualify_if_candidate(&mut self, name: &str) {
        if !self.is_shadowed(name) && self.candidates.contains(name) {
            self.disqualified.insert(name.to_string());
        }
    }

    fn shadowed_by_params<'a, I>(&self, params: I) -> BTreeSet<String>
    where
        I: IntoIterator<Item = &'a Pat>,
    {
        let mut out = BTreeSet::new();
        for param in params {
            self.collect_shadowed_by_pat(param, &mut out);
        }
        out
    }

    fn collect_shadowed_by_pat(&self, pat: &Pat, out: &mut BTreeSet<String>) {
        match pat {
            Pat::Ident(ident) => {
                let name = ident.id.sym.as_ref();
                if self.candidates.contains(name) {
                    out.insert(name.to_string());
                }
            }
            Pat::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
            Pat::Assign(assign) => self.collect_shadowed_by_pat(&assign.left, out),
            Pat::Array(array) => {
                for elem in array.elems.iter().flatten() {
                    self.collect_shadowed_by_pat(elem, out);
                }
            }
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(kv) => self.collect_shadowed_by_pat(&kv.value, out),
                        ObjectPatProp::Assign(assign) => {
                            let name = assign.key.sym.as_ref();
                            if self.candidates.contains(name) {
                                out.insert(name.to_string());
                            }
                        }
                        ObjectPatProp::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
                    }
                }
            }
            Pat::Expr(_) | Pat::Invalid(_) => {}
        }
    }
}

impl Visit for PlainArrayMethodScanner<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = strip_parens(callee)
            && let Expr::Ident(recv) = strip_parens(member.obj.as_ref())
        {
            let allowed = match &member.prop {
                MemberProp::Ident(prop) => SAFE_PLAIN_ARRAY_METHODS.contains(&prop.sym.as_ref()),
                MemberProp::Computed(_) | MemberProp::PrivateName(_) => false,
            };
            if !allowed {
                self.disqualify_if_candidate(recv.sym.as_ref());
            }
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        self.with_scope(scope, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            node.body.visit_with(s);
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        self.with_scope(scope, |s| node.visit_children_with(s));
    }
}
