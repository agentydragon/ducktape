//! Reads whose result depends on when they run relative to writers the
//! reading chunk's dependency graph has no node for.
//!
//! `Purity::Pure` keeps a statement off the S-chain, on the premise that the
//! cells it reads are ordered by the graph's own binding and heap timelines.
//! Those timelines cover the reading chunk only. A read of another chunk's
//! cell, made directly through an import or inside an imported function's
//! body, is therefore reported as an effect, so its statement is sequenced
//! with every other impure statement.

use super::*;
use binding_targets::binding_names;

/// The cells of one chunk whose reads are order-dependent for a consumer.
#[derive(Debug, Default, Clone)]
pub(crate) struct OrderDependentCells {
    /// A reference to one of these reads a cell whose value can change.
    cells: BTreeSet<Id>,
    /// A member read through one of these observes heap contents no consumer
    /// graph orders, although the binding itself never changes.
    member_roots: BTreeSet<Id>,
}

/// One order-dependent read: the binding and where it is read.
pub(crate) struct OrderDependentRead {
    pub(crate) name: String,
    pub(crate) span: Span,
}

impl OrderDependentCells {
    /// The cells a statement of this chunk reads from other chunks: every
    /// named or default import `is_stable` does not vouch for. A namespace
    /// import is an immutable binding of an object whose member reads are
    /// classified where they occur.
    pub(crate) fn imported(
        body: &[TopLevelItemView<'_>],
        is_stable: impl Fn(&str) -> bool,
    ) -> Self {
        Self {
            cells: imported_bindings(body)
                .filter(|id| !is_stable(id.0.as_ref()))
                .collect(),
            member_roots: BTreeSet::new(),
        }
    }

    /// The cells an exported function of this chunk exposes to its importers:
    /// the imported cells, and every binding declared outside a function
    /// body unless it is declared once, never written, and holds a function,
    /// a class or a primitive (`primitive_bindings`). A function or class
    /// object still has properties, so a member read through any declared
    /// binding is order-dependent too.
    pub(crate) fn exported(
        body: &[TopLevelItemView<'_>],
        is_stable_import: impl Fn(&str) -> bool,
        primitive_bindings: &BTreeMap<String, analysis_model::PrimKinds>,
    ) -> Self {
        let mut cells = Self::imported(body, is_stable_import).cells;
        let declared = declared_outside_functions(body);
        let immutable =
            immutable_declarations(body.iter().map(TopLevelItemView::as_module_item), |name| {
                primitive_bindings.contains_key(name)
            });
        cells.extend(declared.difference(&immutable).cloned());
        Self {
            cells,
            member_roots: declared,
        }
    }

    /// The first order-dependent read `visit` reaches. Nested function bodies
    /// are skipped, as in the classifier: creating a closure reads nothing,
    /// and a call that runs it is classified at the call. An immediately
    /// invoked function expression runs at once, so its body is walked.
    pub(crate) fn first_read(
        &self,
        visit: impl FnOnce(&mut dyn Visit),
    ) -> Option<OrderDependentRead> {
        if self.cells.is_empty() && self.member_roots.is_empty() {
            return None;
        }
        let mut finder = ReadFinder {
            cells: self,
            found: None,
        };
        visit(&mut finder);
        finder.found
    }
}

/// Local bindings of the named and default imports of `body`.
fn imported_bindings<'a>(body: &'a [TopLevelItemView<'_>]) -> impl Iterator<Item = Id> + 'a {
    body.iter()
        .filter_map(|item| match item.as_module_item() {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => Some(import),
            _ => None,
        })
        .flat_map(|import| &import.specifiers)
        .filter_map(|specifier| match specifier {
            ImportSpecifier::Named(named) => Some(named.local.to_id()),
            ImportSpecifier::Default(default) => Some(default.local.to_id()),
            ImportSpecifier::Namespace(_) => None,
        })
}

/// Top-level declarations of `items` that hold one value for the whole
/// program: a function or class declaration, or a simple declarator with no
/// initializer, a function or class expression, or a value `is_primitive`
/// proves primitive, where the binding is declared once and never written.
pub(crate) fn immutable_declarations<'a>(
    items: impl IntoIterator<Item = &'a ModuleItem>,
    is_primitive: impl Fn(&str) -> bool,
) -> BTreeSet<Id> {
    let mut writes = WriteCollector::default();
    let mut declarations = BTreeMap::<Id, usize>::new();
    let mut candidates = BTreeSet::new();
    for item in items {
        item.visit_with(&mut writes);
        let decl = match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => &export.decl,
            _ => continue,
        };
        match decl {
            Decl::Fn(function) => {
                *declarations.entry(function.ident.to_id()).or_default() += 1;
                candidates.insert(function.ident.to_id());
            }
            Decl::Class(class) => {
                *declarations.entry(class.ident.to_id()).or_default() += 1;
                candidates.insert(class.ident.to_id());
            }
            Decl::Var(var) => {
                for declarator in &var.decls {
                    for id in binding_names(&declarator.name) {
                        *declarations.entry(id).or_default() += 1;
                    }
                    let Pat::Ident(binding) = &declarator.name else {
                        continue;
                    };
                    let immutable_value = match declarator.init.as_deref().map(Expr::unwrap_parens)
                    {
                        None => true,
                        Some(Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_)) => true,
                        Some(Expr::Lit(literal)) if !matches!(literal, Lit::Regex(_)) => true,
                        Some(_) => is_primitive(binding.id.sym.as_ref()),
                    };
                    if immutable_value {
                        candidates.insert(binding.id.to_id());
                    }
                }
            }
            _ => {}
        }
    }
    candidates
        .into_iter()
        .filter(|id| declarations[id] == 1 && !writes.written.contains(id))
        .collect()
}

/// Every binding declared in `body` outside a function body: top-level
/// declarations, and the block-level and hoisted `var` declarations a
/// chunk-top function can also capture.
fn declared_outside_functions(body: &[TopLevelItemView<'_>]) -> BTreeSet<Id> {
    let mut collector = DeclarationCollector::default();
    for item in body {
        item.as_module_item().visit_with(&mut collector);
    }
    collector.declared
}

#[derive(Default)]
struct DeclarationCollector {
    declared: BTreeSet<Id>,
}

impl Visit for DeclarationCollector {
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_class(&mut self, _: &Class) {}
    fn visit_import_decl(&mut self, _: &ImportDecl) {}

    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.declared.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.declared.insert(node.ident.to_id());
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        self.declared.insert(node.ident.to_id());
    }
}

/// Every identifier in an assignment, update or loop-head target anywhere in
/// the visited source, nested functions included. A member target counts its
/// object: over-approximating the written bindings only removes immutability.
#[derive(Default)]
struct WriteCollector {
    written: BTreeSet<Id>,
}

struct TargetIdentifiers<'a>(&'a mut BTreeSet<Id>);

impl Visit for TargetIdentifiers<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        self.0.insert(node.to_id());
    }
}

impl Visit for WriteCollector {
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        node.left
            .visit_with(&mut TargetIdentifiers(&mut self.written));
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        node.arg
            .visit_with(&mut TargetIdentifiers(&mut self.written));
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        self.visit_loop_head(&node.left);
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.visit_loop_head(&node.left);
        node.visit_children_with(self);
    }
}

impl WriteCollector {
    fn visit_loop_head(&mut self, head: &ForHead) {
        if let ForHead::Pat(pattern) = head {
            pattern.visit_with(&mut TargetIdentifiers(&mut self.written));
        }
    }
}

struct ReadFinder<'a> {
    cells: &'a OrderDependentCells,
    found: Option<OrderDependentRead>,
}

impl ReadFinder<'_> {
    fn record(&mut self, ident: &Ident) {
        self.found.get_or_insert_with(|| OrderDependentRead {
            name: ident.sym.to_string(),
            span: ident.span,
        });
    }
}

impl Visit for ReadFinder<'_> {
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_constructor(&mut self, _: &Constructor) {}
    fn visit_getter_prop(&mut self, _: &GetterProp) {}
    fn visit_setter_prop(&mut self, _: &SetterProp) {}
    // An export list is linkage: it evaluates no reference.
    fn visit_named_export(&mut self, _: &NamedExport) {}

    fn visit_ident(&mut self, node: &Ident) {
        if self.cells.cells.contains(&node.to_id()) {
            self.record(node);
        }
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if let Expr::Ident(object) = node.obj.unwrap_parens()
            && self.cells.member_roots.contains(&object.to_id())
        {
            self.record(object);
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            match callee.unwrap_parens() {
                Expr::Fn(function) => function.function.visit_children_with(self),
                Expr::Arrow(arrow) => arrow.visit_children_with(self),
                _ => {}
            }
        }
        node.visit_children_with(self);
    }
}
