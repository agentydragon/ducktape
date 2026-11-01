use super::*;
use crate::inference::{FactContext, IterativeInference, IterativeRules};

/// Identifier names that, when not shadowed by a chunk-top-level
/// declaration or import, evaluate to the global object in the
/// runtimes the debundler targets (browsers: `window` / `self` /
/// `frames` / `top`; Node and workers: `globalThis` / `self`).
const GLOBAL_OBJECT_ALIASES: [&str; 5] = ["globalThis", "window", "self", "frames", "top"];

/// The subset of [`GLOBAL_OBJECT_ALIASES`] no chunk-top-level
/// declaration (incl. block-hoisted `var`s) or import shadows.
/// Block-scoped `let`/`const` redeclarations of an alias *inside* a
/// top-level statement are not detected; treating such an access as
/// global only over-approximates the cell sets (extra `Sequenced`
/// edges), which is sound.
pub(crate) fn unshadowed_global_object_aliases(
    body: &[TopLevelItemView<'_>],
) -> BTreeSet<&'static str> {
    unshadowed_global_names(body, &GLOBAL_OBJECT_ALIASES)
}

/// The subset of `names` no chunk-top-level declaration or import shadows.
pub(crate) fn unshadowed_global_names(
    body: &[TopLevelItemView<'_>],
    names: &[&'static str],
) -> BTreeSet<&'static str> {
    let mut declared: BTreeSet<String> = BTreeSet::new();
    for item in body {
        let item = item.as_module_item();
        for id in collect_declared_names(item) {
            declared.insert(id.0.to_string());
        }
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for spec in &import.specifiers {
                let local = match spec {
                    ImportSpecifier::Named(named) => named.local.sym.as_ref(),
                    ImportSpecifier::Default(default) => default.local.sym.as_ref(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_ref(),
                };
                declared.insert(local.to_string());
            }
        }
    }
    names
        .iter()
        .copied()
        .filter(|alias| !declared.contains(*alias))
        .collect()
}

/// Global-object aliasing taint. A statement that lets the global
/// object escape as a *value* (`const g = globalThis;`,
/// `register(window)`, a function body returning `self`) defeats
/// per-cell tracking for every binding the value flows into:
/// `g.tag` reads/writes the same cells as `globalThis.tag` but the
/// per-statement summary only sees `Binding(g)`. Conservative rule:
///
/// 1. A statement containing a bare global-object alias outside
///    member-base position is tainted.
/// 2. Bindings written (declared/rebound) by tainted statements are
///    suspects.
/// 3. Any statement reading a suspect is tainted (fixpoint).
///
/// Tainted statements get the `GlobalObjectAliasEscape` havoc — the
/// dataflow-aware S-chain falls back to the strict adjacent-impure
/// edge for them — until [`resolve_global_alias_escape`] resolves it
/// against the values synchronous execution settles.
pub(crate) fn apply_global_escape_taint(
    body: &[TopLevelItemView<'_>],
    global_object_names: &BTreeSet<&'static str>,
    per_statement: &mut [StructuralStatementFacts],
) {
    #[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
    enum Key {
        Statement(usize),
        Binding(Id),
    }
    struct EscapeRules<'a>(&'a [StructuralStatementFacts]);
    impl IterativeRules for EscapeRules<'_> {
        type Key = Key;
        type Fact = ();
        type Rule = usize;

        fn evaluate(&self, index: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
            let facts = &self.0[*index];
            let escaped = context.read(Key::Statement(*index)).is_some()
                || facts
                    .reads
                    .eager
                    .iter()
                    .chain(&facts.reads.lazy)
                    .any(|id| context.read(Key::Binding(id.clone())).is_some());
            if escaped {
                context.propose(Key::Statement(*index), ());
                for id in facts
                    .declared
                    .iter()
                    .chain(&facts.rebinds.eager)
                    .chain(&facts.rebinds.lazy)
                {
                    context.propose(Key::Binding(id.clone()), ());
                }
            }
        }

        fn merge(_: &Key, _: &mut (), _: ()) -> bool {
            false
        }
    }
    let mut inference = IterativeInference::new(EscapeRules(per_statement));
    for (index, item) in body.iter().enumerate() {
        let mut finder = GlobalObjectEscapeFinder {
            names: global_object_names,
            escaped: false,
        };
        item.as_module_item().visit_with(&mut finder);
        if finder.escaped {
            inference.seed(Key::Statement(index), ());
        }
    }
    let closure = inference.solve(0..per_statement.len());
    for (index, facts) in per_statement.iter_mut().enumerate() {
        if closure.get(&Key::Statement(index)).is_some() {
            facts
                .havoc
                .insert(DataflowOpacityCause::GlobalObjectAliasEscape);
        }
    }
}

/// Detects a global-object alias used as a value. `globalThis.x` /
/// `window[k]` use the alias as a property base — not an escape —
/// so member-base positions are skipped; any other occurrence
/// (initializer, argument, return value, array/object element)
/// counts.
struct GlobalObjectEscapeFinder<'a> {
    names: &'a BTreeSet<&'static str>,
    escaped: bool,
}

impl Visit for GlobalObjectEscapeFinder<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if self.names.contains(node.sym.as_ref()) {
            self.escaped = true;
        }
    }
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        match node.obj.unwrap_parens() {
            Expr::Ident(ident) if self.names.contains(ident.sym.as_ref()) => {}
            other => other.visit_with(self),
        }
        node.prop.visit_with(self);
    }
}

/// Resolve a statement's `GlobalObjectAliasEscape` against the bindings that
/// may hold the global object where it starts (`aliases`, settled by
/// synchronous execution). When every at-init use of an alias is the base of
/// a static-key member read, those reads are `GlobalProp` cells and the
/// statement's summary is complete for them; any other use (a value position,
/// a write, a dynamic key) keeps the havoc. Bodies that run later reach the
/// global object through their own callers' opaque-call havoc.
pub(crate) fn resolve_global_alias_escape(
    item: &ModuleItem,
    global_object_names: &BTreeSet<&'static str>,
    aliases: &BTreeSet<Id>,
    fact: &mut StatementFacts,
) {
    let mut uses = GlobalAliasUses {
        global_object_names,
        aliases,
        reads: BTreeSet::new(),
        escaped: false,
        lazy_depth: 0,
        past_await: false,
    };
    item.visit_with(&mut uses);
    if !uses.escaped {
        fact.havoc
            .remove(&DataflowOpacityCause::GlobalObjectAliasEscape);
        fact.global_reads.extend(uses.reads);
    }
}

/// `global_object_names` are the unshadowed global-object names, whose member
/// accesses the collector already records as cells; `aliases` are bindings
/// that may hold the global object.
struct GlobalAliasUses<'a> {
    global_object_names: &'a BTreeSet<&'static str>,
    aliases: &'a BTreeSet<Id>,
    reads: BTreeSet<String>,
    escaped: bool,
    lazy_depth: u32,
    past_await: bool,
}

impl LazyBoundary for GlobalAliasUses<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
}

impl GlobalAliasUses<'_> {
    fn is_alias(&self, expr: &Expr) -> bool {
        matches!(expr.unwrap_parens(), Expr::Ident(ident) if self.aliases.contains(&ident.to_id()))
    }

    fn is_global_name(&self, expr: &Expr) -> bool {
        matches!(expr.unwrap_parens(), Expr::Ident(ident)
            if self.global_object_names.contains(ident.sym.as_ref()))
    }
}

impl Visit for GlobalAliasUses<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        if self.lazy_depth == 0
            && (self.aliases.contains(&ident.to_id())
                || self.global_object_names.contains(ident.sym.as_ref()))
        {
            self.escaped = true;
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if self.lazy_depth == 0 && self.is_global_name(&member.obj) {
            if let MemberProp::Computed(key) = &member.prop {
                key.visit_with(self);
            }
            return;
        }
        if self.lazy_depth == 0 && self.is_alias(&member.obj) {
            match &member.prop {
                MemberProp::Ident(key) => {
                    self.reads.insert(key.sym.to_string());
                }
                MemberProp::Computed(ComputedPropName { expr, .. }) => match expr.unwrap_parens() {
                    Expr::Lit(Lit::Str(key)) => {
                        self.reads.insert(key.value.to_string_lossy().into_owned());
                    }
                    other => {
                        self.escaped = true;
                        other.visit_with(self);
                    }
                },
                MemberProp::PrivateName(_) => self.escaped = true,
            }
            return;
        }
        member.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        // A write through an alias stays opaque.
        if self.lazy_depth == 0
            && let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left
            && self.is_alias(&member.obj)
        {
            self.escaped = true;
        }
        assign.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        if self.lazy_depth == 0
            && let Expr::Member(member) = update.arg.unwrap_parens()
            && self.is_alias(&member.obj)
        {
            self.escaped = true;
        }
        update.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, unary: &UnaryExpr) {
        if self.lazy_depth == 0
            && unary.op == UnaryOp::Delete
            && let Expr::Member(member) = unary.arg.unwrap_parens()
            && self.is_alias(&member.obj)
        {
            self.escaped = true;
        }
        unary.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        lazy_visit_function(self, node);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        lazy_visit_arrow_expr(self, node);
    }

    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        lazy_visit_getter_prop(self, node);
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        lazy_visit_setter_prop(self, node);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        lazy_visit_class_member(self, member);
    }
}
