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
    GLOBAL_OBJECT_ALIASES
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
/// Tainted statements get `dataflow_summarizable = false` — the
/// dataflow-aware S-chain falls back to the strict adjacent-impure
/// edge for them.
pub(crate) fn apply_global_escape_taint(
    body: &[TopLevelItemView<'_>],
    global_object_names: &BTreeSet<&'static str>,
    per_statement: &mut [StructuralStatementFacts],
) {
    #[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
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
            facts.dataflow_summarizable = false;
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
