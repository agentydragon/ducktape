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
    value_flow: &crate::facts::ChunkValueFlow,
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
        .iter()
        .filter(|(name, inits)| {
            inits
                .iter()
                .all(|(init, kind)| is_plain_data_init(init, name, *kind))
        })
        .map(|(name, _)| name.clone())
        .collect();
    if candidates.is_empty() {
        return Vec::new();
    }
    let top_level_ids: BTreeMap<String, Id> = body
        .iter()
        .flat_map(|item| crate::facts::collect_declared_names(item.as_module_item()))
        .map(|id| (id.0.to_string(), id))
        .collect();
    let top_level_names: BTreeSet<String> = top_level_ids.keys().cloned().collect();
    // These are candidate facts, not an admission on their own. Every write,
    // alias escape and accessor/prototype installation still invalidates the
    // receiver during the whole-chunk scan below.
    let primitive_members: BTreeMap<Id, BTreeSet<String>> = candidates
        .iter()
        .filter_map(|name| {
            let [(init, VarDeclKind::Const)] = inits_by_name[name].as_slice() else {
                return None;
            };
            let Expr::Object(object) = init.unwrap_parens() else {
                return None;
            };
            let keys = crate::facts::primitive_literal_property_keys(object)?;
            Some((top_level_ids[name].clone(), keys))
        })
        .collect();
    let mut objects = BTreeSet::new();
    for name in &candidates {
        let inits = &inits_by_name[name];
        let candidate_objects: BTreeSet<_> = inits
            .iter()
            .filter_map(|(init, _)| match init.unwrap_parens() {
                Expr::Object(object) => Some(crate::facts::ValueOrigin::Object(object.span.lo.0)),
                Expr::Array(array) => Some(crate::facts::ValueOrigin::Object(array.span.lo.0)),
                _ => None,
            })
            .collect();
        if candidate_objects.len() == inits.len() {
            objects.extend(candidate_objects);
        }
    }
    let mut object_to_names = BTreeMap::new();
    let mut return_exempt = BTreeSet::new();
    for (object, names) in value_flow.aliases_by_object(&objects) {
        let aliases: BTreeSet<String> = names.intersection(&top_level_names).cloned().collect();
        return_exempt.extend(aliases.iter().cloned());
        object_to_names.insert(object, aliases);
    }
    let mut scan_candidates = candidates.clone();
    scan_candidates.extend(return_exempt.iter().cloned());
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
    // * references that escape into unknown code. Tracked direct-literal
    //   identities may pass through local aliases and returns; the scanner
    //   checks those aliases for later mutation. Other shapes remain
    //   conservative escapes.
    //
    // Plain data-property writes (`X.foo = bar`) don't install
    // accessors and don't change the prototype chain, but the
    // conservative check rejects them anyway — the cost is dropping
    // a few PlainData candidates that would still be sound, and the
    // benefit is one short rule that's easy to audit.
    let mut scanner = PlainDataWriteScanner {
        candidates: &scan_candidates,
        shadowed,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
        parameter_scopes: Vec::new(),
        value_flow: Some(value_flow),
        candidate_index: Some(
            value_flow.candidate_object_index(&object_to_names.keys().copied().collect()),
        ),
        object_to_names,
        return_exempt,
        primitive_members,
    };
    for item in body {
        let module_item = item.as_module_item();
        match module_item {
            // Direct chunk-top var initializers were all checked above.
            // Walk each expression so references to OTHER candidates
            // still participate in the escape scan, but leave these
            // known rebinds out of the nested-var disqualification path.
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.kind == VarDeclKind::Var => {
                for decl in &var.decls {
                    decl.name.visit_with(&mut scanner);
                    if let Some(init) = decl.init.as_deref() {
                        init.visit_with(&mut scanner);
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                if let Decl::Var(var) = &export.decl
                    && var.kind == VarDeclKind::Var
                {
                    for decl in &var.decls {
                        decl.name.visit_with(&mut scanner);
                        if let Some(init) = decl.init.as_deref() {
                            init.visit_with(&mut scanner);
                        }
                    }
                } else {
                    module_item.visit_with(&mut scanner);
                }
            }
            _ => module_item.visit_with(&mut scanner),
        }
    }
    let mut disqualified = scanner.disqualified;
    let object_to_names = scanner.object_to_names;
    let disqualified_aliases: BTreeSet<String> = object_to_names
        .values()
        .filter(|aliases| !aliases.is_disjoint(&disqualified))
        .flat_map(|aliases| aliases.iter().cloned())
        .collect();
    disqualified.extend(disqualified_aliases);
    candidates
        .into_iter()
        .filter(|name| !disqualified.contains(name))
        .collect()
}

/// A candidate enum own-key fact. Final binding stability is checked against
/// the canonical statement-fact rebinds outside `initializer_owner`.
#[derive(Clone, Debug)]
pub(crate) struct TsEnumNamedDataCandidate {
    pub(crate) initializer_owner: crate::StatementOrdinal,
    pub(crate) own_primitive_keys: BTreeSet<String>,
}

/// Exact named primitive fields installed by one TS enum initializer.
/// PlainData supplies the chunk-wide no-member-write/no-escape guarantee.
/// Multiple declarations, including destructuring declarations, are excluded.
pub(crate) fn collect_ts_enum_named_data_candidates(
    body: &[TopLevelItemView<'_>],
    plain_data_bindings: &[String],
) -> BTreeMap<Id, TsEnumNamedDataCandidate> {
    struct Declaration<'a> {
        initializer: Option<&'a Expr>,
        kind: VarDeclKind,
        direct: bool,
        owner: usize,
    }
    let plain: BTreeSet<&str> = plain_data_bindings.iter().map(String::as_str).collect();
    let mut declarations: BTreeMap<Id, Vec<Declaration<'_>>> = BTreeMap::new();
    for (owner, item) in body.iter().enumerate() {
        let declaration = match item.as_module_item() {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => Some(declaration.as_ref()),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                if let Decl::Var(declaration) = &export.decl {
                    Some(declaration.as_ref())
                } else {
                    None
                }
            }
            _ => None,
        };
        let Some(declaration) = declaration else {
            continue;
        };
        for declarator in &declaration.decls {
            for binding in binding_targets::binding_names(&declarator.name) {
                declarations.entry(binding).or_default().push(Declaration {
                    initializer: declarator.init.as_deref(),
                    kind: declaration.kind,
                    direct: matches!(&declarator.name, Pat::Ident(_)),
                    owner,
                });
            }
        }
    }
    declarations
        .into_iter()
        .filter_map(|(binding, declarations)| {
            let [declaration] = declarations.as_slice() else {
                return None;
            };
            if declaration.kind != VarDeclKind::Var
                || !declaration.direct
                || !plain.contains(binding.0.as_ref())
            {
                return None;
            }
            let init = declaration.initializer?;
            ts_enum_iife_named_data_keys(init, binding.0.as_ref()).map(|keys| {
                (
                    binding,
                    TsEnumNamedDataCandidate {
                        initializer_owner: crate::StatementOrdinal(declaration.owner),
                        own_primitive_keys: keys,
                    },
                )
            })
        })
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
        parameter_scopes: Vec::new(),
        value_flow: None,
        candidate_index: None,
        object_to_names: BTreeMap::new(),
        return_exempt: BTreeSet::new(),
        primitive_members: BTreeMap::new(),
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
    match expr.unwrap_parens() {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
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
    match expr.unwrap_parens() {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(_) => expr_returns_plain_array(expr, known_plain_arrays),
        _ => false,
    }
}

/// Chunk-top `const` names whose init evaluates to either a
/// ToString-safe primitive or a Symbol, resolving earlier const aliases.
/// `const` guarantees the binding never rebinds, and a primitive
/// carries no user accessors. Symbols are kept separate because
/// ToPropertyKey accepts them but ToString throws. The init's own
/// evaluation effects are classified at its declaration site; only
/// the resulting value class matters here. `let` / `var`
/// are excluded: a later non-primitive rebind would invalidate the
/// claim.
///
/// Forward pass in source order: a const resolves only references to
/// consts declared before it — exactly the set visible without
/// hitting the temporal dead zone. Function/arrow inits never reach
/// here (`plain_data_var_candidates` filters them) and are not
/// primitive anyway.
pub(crate) fn collect_const_value_classes(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> (BTreeSet<String>, BTreeSet<String>) {
    let mut primitives: BTreeSet<String> = BTreeSet::new();
    let mut symbols: BTreeSet<String> = BTreeSet::new();
    let mut symbol_write = SymbolGlobalWriteFinder::default();
    let no_local_shadow: BTreeSet<String> = BTreeSet::new();
    for item in body {
        item.as_module_item().visit_with(&mut symbol_write);
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            if kind != VarDeclKind::Const {
                continue;
            }
            if is_result_primitive(init, &primitives, &no_local_shadow) {
                primitives.insert(name);
            } else if is_symbol_const_init(init, &symbols, shadowed) {
                symbols.insert(name);
            }
        }
    }
    if symbol_write.hit {
        // A visible replacement can make `Symbol(...)` return an object.
        // Discard even earlier candidates so later computed-key uses cannot
        // rely on source-order assumptions about an untracked global cell.
        symbols.clear();
    }
    (primitives, symbols)
}

#[derive(Default)]
struct SymbolGlobalWriteFinder {
    hit: bool,
}

impl Visit for SymbolGlobalWriteFinder {
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                self.hit |= is_symbol_member(member);
            }
            other => {
                let mut target = SymbolTargetFinder::default();
                other.visit_with(&mut target);
                self.hit |= target.hit;
            }
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        if matches!(node.arg.unwrap_parens(), Expr::Ident(id) if id.sym.as_ref() == "Symbol")
            || matches!(node.arg.unwrap_parens(), Expr::Member(member) if is_symbol_member(member))
        {
            self.hit = true;
        }
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && matches!(node.arg.unwrap_parens(), Expr::Member(member) if is_symbol_member(member))
        {
            self.hit = true;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let Expr::Ident(receiver) = member.obj.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && matches!(
                (receiver.sym.as_ref(), property.sym.as_ref()),
                ("Object", "defineProperty" | "defineProperties" | "assign")
                    | ("Reflect", "defineProperty" | "set")
            )
            && node.args.len() >= 2
            && (matches!(node.args[1].expr.unwrap_parens(), Expr::Lit(Lit::Str(key)) if key.value.as_str() == Some("Symbol"))
                || matches!(node.args[1].expr.unwrap_parens(), Expr::Object(object) if object.props.iter().any(|prop| matches!(prop, PropOrSpread::Prop(prop) if matches!(prop.as_ref(), Prop::KeyValue(kv) if matches!(&kv.key, PropName::Ident(id) if id.sym.as_ref() == "Symbol"))))))
        {
            self.hit = true;
        }
        node.visit_children_with(self);
    }
}

#[derive(Default)]
struct SymbolTargetFinder {
    hit: bool,
}

impl Visit for SymbolTargetFinder {
    fn visit_ident(&mut self, ident: &Ident) {
        self.hit |= ident.sym.as_ref() == "Symbol";
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.hit |= is_symbol_member(member);
        member.visit_children_with(self);
    }
}

fn is_symbol_member(member: &MemberExpr) -> bool {
    match &member.prop {
        MemberProp::Ident(id) => id.sym.as_ref() == "Symbol",
        MemberProp::Computed(key) => {
            matches!(key.expr.unwrap_parens(), Expr::Lit(Lit::Str(value)) if value.value.as_str() == Some("Symbol"))
        }
        MemberProp::PrivateName(_) => false,
    }
}

fn is_symbol_const_init(
    init: &Expr,
    symbols: &BTreeSet<String>,
    shadowed: &BTreeSet<&'static str>,
) -> bool {
    match init.unwrap_parens() {
        Expr::Ident(id) => symbols.contains(id.sym.as_ref()),
        Expr::Call(call) if !shadowed.contains("Symbol") => {
            matches!(&call.callee, Callee::Expr(callee) if matches!(callee.unwrap_parens(), Expr::Ident(id) if id.sym.as_ref() == "Symbol"))
                && call.args.len() <= 1
                && call.args.iter().all(|arg| {
                    arg.spread.is_none()
                        && matches!(
                            arg.expr.unwrap_parens(),
                            Expr::Lit(
                                Lit::Str(_)
                                    | Lit::Num(_)
                                    | Lit::Bool(_)
                                    | Lit::Null(_)
                                    | Lit::BigInt(_)
                            )
                        )
                })
        }
        _ => false,
    }
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
    match expr.unwrap_parens() {
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
/// * a direct return of a literal identity tracked by the shared
///   chunk value-flow pass; callers and aliases are scanned separately;
/// * Export specifiers (`export { X }`) do not introduce a new alias
///   inside this chunk. Cross-chunk consumers are covered by the
///   documented residual assumption in docs/purity_soundness.md.
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
    parameter_scopes: Vec<BTreeSet<String>>,
    value_flow: Option<&'a crate::facts::ChunkValueFlow>,
    candidate_index: Option<crate::facts::CandidateObjectIndex>,
    object_to_names: BTreeMap<crate::facts::ValueOrigin, BTreeSet<String>>,
    return_exempt: BTreeSet<String>,
    primitive_members: BTreeMap<Id, BTreeSet<String>>,
}

impl PlainDataWriteScanner<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(
        &mut self,
        scope: BTreeSet<String>,
        params: BTreeSet<String>,
        f: F,
    ) {
        self.shadowing_scopes.push(scope);
        self.parameter_scopes.push(params);
        f(self);
        self.parameter_scopes.pop();
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

    fn disqualify_value_escape(&mut self, expr: &Expr) {
        let Some(flow) = self.value_flow else {
            return;
        };
        struct ReferencedIds<'a> {
            ids: BTreeSet<Id>,
            primitive_members: &'a BTreeMap<Id, BTreeSet<String>>,
        }
        impl Visit for ReferencedIds<'_> {
            fn visit_ident(&mut self, ident: &Ident) {
                self.ids.insert(ident.to_id());
            }

            fn visit_member_expr(&mut self, member: &MemberExpr) {
                if let Expr::Ident(receiver) = member.obj.unwrap_parens()
                    && let MemberProp::Ident(property) = &member.prop
                    && self
                        .primitive_members
                        .get(&receiver.to_id())
                        .is_some_and(|keys| keys.contains(property.sym.as_ref()))
                {
                    // Passing the literal value cannot expose its receiver.
                    // Keep every other value/capture on the conservative path.
                    return;
                }
                member.visit_children_with(self);
            }
        }
        let mut referenced = ReferencedIds {
            ids: BTreeSet::new(),
            primitive_members: &self.primitive_members,
        };
        expr.visit_with(&mut referenced);
        let index = self.candidate_index.as_ref().unwrap();
        let mut escaped = BTreeSet::new();
        if referenced.ids.is_empty() {
            index.for_values(&flow.values_of_expr(expr), |object| {
                escaped.insert(object);
            });
        }
        for id in referenced.ids {
            if self
                .parameter_scopes
                .iter()
                .any(|scope| scope.contains(id.0.as_ref()))
            {
                continue;
            }
            index.for_binding(&id, |object| {
                escaped.insert(object);
            });
        }
        for object in escaped {
            if let Some(aliases) = self.object_to_names.get(&object) {
                self.disqualified.extend(aliases.iter().cloned());
            }
        }
    }

    fn disqualify_var_pattern(&mut self, pat: &Pat) {
        let mut names = BTreeSet::new();
        self.collect_shadowed_by_pat(pat, &mut names);
        for name in names {
            self.disqualify_if_candidate(&name);
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
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        // A known local alias keeps the identity visible to this scan. Its
        // later uses are visited under the expanded candidate name set.
        if self.value_flow.is_some()
            && let Pat::Ident(binding) = &node.name
            && self.return_exempt.contains(binding.id.sym.as_ref())
            && let Some(init) = node.init.as_deref()
            && let Expr::Ident(source) = init.unwrap_parens()
            && self.return_exempt.contains(source.sym.as_ref())
        {
            node.name.visit_with(self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_var_decl(&mut self, node: &VarDecl) {
        if node.kind == VarDeclKind::Var {
            for decl in &node.decls {
                if decl.init.is_some() {
                    // A nested block/loop var initializer may replace a
                    // PlainData binding with any runtime value. Direct
                    // chunk-top var initializers are visited as expressions
                    // by `collect_plain_data_bindings` only after every init
                    // has passed the shape check.
                    self.disqualify_var_pattern(&decl.name);
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if let ForHead::VarDecl(var) = &node.left {
            for decl in &var.decls {
                // The loop assigns the enumerated key into the binding each
                // iteration even though the declarator has no initializer.
                self.disqualify_var_pattern(&decl.name);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if let ForHead::VarDecl(var) = &node.left {
            for decl in &var.decls {
                // The loop assigns each yielded value into the binding.
                self.disqualify_var_pattern(&decl.name);
            }
        }
        node.visit_children_with(self);
    }

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
                self.disqualify_value_escape(&member.obj);
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
        if matches!(
            &node.left,
            AssignTarget::Simple(SimpleAssignTarget::Member(_))
        ) {
            self.disqualify_value_escape(&node.right);
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
        if let Expr::Member(member) = node.arg.unwrap_parens() {
            self.disqualify_value_escape(&member.obj);
        }
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete {
            self.disqualify_member_receiver(&node.arg);
            if let Expr::Member(member) = node.arg.unwrap_parens() {
                self.disqualify_value_escape(&member.obj);
            }
        }
        // `typeof X` / `!X` / `void X` are transient inspections of
        // the value — the reference isn't captured. Skip the bare
        // candidate Ident so the escape default doesn't fire.
        if matches!(node.op, UnaryOp::TypeOf | UnaryOp::Bang | UnaryOp::Void)
            && matches!(node.arg.unwrap_parens(), Expr::Ident(_))
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

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(arg) = node.arg.as_deref()
            && let Expr::Ident(ident) = arg.unwrap_parens()
            && self.return_exempt.contains(ident.sym.as_ref())
        {
            return;
        }
        node.visit_children_with(self);
    }

    // Member-access receivers are reads, not captures. Skip an
    // Ident receiver (candidate or not); everything else (nested
    // receivers, computed keys) is traversed normally.
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if !matches!(node.obj.unwrap_parens(), Expr::Ident(_)) {
            node.obj.visit_with(self);
        }
        node.prop.visit_with(self);
    }

    // Spread sources (`{...X}`, `[...X]`, `f(...X)`) copy values /
    // iterate; the receiver object's identity is not captured.
    fn visit_spread_element(&mut self, node: &SpreadElement) {
        if matches!(node.expr.unwrap_parens(), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, node: &ExprOrSpread) {
        if node.spread.is_some() && matches!(node.expr.unwrap_parens(), Expr::Ident(_)) {
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
            && matches!(node.args[0].expr.unwrap_parens(), Expr::Ident(_))
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
        if let Some(binding) = ts_enum_iife_argument_binding(node)
            && self.candidates.contains(binding)
            && !self.is_shadowed(binding)
            && is_ts_enum_iife_call_for_binding(node, binding)
        {
            node.callee.visit_with(self);
            return;
        }
        for arg in &node.args {
            self.disqualify_value_escape(&arg.expr);
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        let params = node
            .params
            .iter()
            .flat_map(binding_targets::binding_names)
            .map(|id| id.0.to_string())
            .collect();
        self.with_scope(scope, params, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            match node.body.as_ref() {
                ArrowFunctionBody::Expr(expr)
                    if matches!(expr.unwrap_parens(), Expr::Ident(ident) if s.return_exempt.contains(ident.sym.as_ref())) => {}
                body => body.visit_with(s),
            }
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        let params = node
            .params
            .iter()
            .flat_map(|p| binding_targets::binding_names(&p.pat))
            .map(|id| id.0.to_string())
            .collect();
        self.with_scope(scope, params, |s| node.visit_children_with(s));
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
            && let Expr::Member(member) = callee.unwrap_parens()
            && let Expr::Ident(recv) = member.obj.as_ref().unwrap_parens()
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
