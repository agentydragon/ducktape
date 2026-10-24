//! Narrow structural proofs for standard `Set` construction from fresh arrays.
//!
//! The proof is shared by purity classification and synchronous callback
//! analysis. It only records exact constructor spans; consumers still apply
//! their own surrounding purity/effect checks.

use super::*;

#[derive(Debug, Clone, Default)]
pub(crate) struct SetIterableProof {
    constructor_starts: BTreeSet<u32>,
}

impl SetIterableProof {
    pub(crate) fn analyze(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        flow: &ChunkValueFlow,
    ) -> Self {
        if shadowed.contains("Set") {
            return Self::default();
        }

        let candidates = collect_const_primitive_arrays(body);
        let mut scanner = ArrayUseScanner {
            candidates: &candidates,
            flow,
            shadowed,
            invalid: BTreeSet::new(),
            alias_consumers: Vec::new(),
            direct_literal_calls: BTreeSet::new(),
            has_dynamic_scope: false,
        };
        for item in body {
            item.as_module_item().visit_with(&mut scanner);
        }
        if scanner.has_dynamic_scope {
            return Self::default();
        }

        let mut invalid = scanner.invalid;
        let mut aliases_by_source = BTreeMap::<Id, Vec<Id>>::new();
        for (alias, candidate) in &candidates {
            if let Some(source) = &candidate.source {
                aliases_by_source
                    .entry(source.clone())
                    .or_default()
                    .push(alias.clone());
            }
        }
        let mut pending: Vec<_> = invalid.iter().cloned().collect();
        while let Some(id) = pending.pop() {
            let Some(candidate) = candidates.get(&id) else {
                continue;
            };
            if let Some(source) = &candidate.source
                && invalid.insert(source.clone())
            {
                pending.push(source.clone());
            }
            if let Some(aliases) = aliases_by_source.get(&id) {
                for alias in aliases {
                    if invalid.insert(alias.clone()) {
                        pending.push(alias.clone());
                    }
                }
            }
        }

        let mut constructor_starts = scanner.direct_literal_calls;
        for (array, start) in scanner.alias_consumers {
            if !invalid.contains(&array) {
                constructor_starts.insert(start);
            }
        }
        Self { constructor_starts }
    }

    pub(crate) fn contains_constructor(&self, span: Span) -> bool {
        self.constructor_starts.contains(&span.lo.0)
    }
}

#[derive(Debug)]
struct ArrayCandidate {
    source: Option<Id>,
}

fn collect_const_primitive_arrays(body: &[TopLevelItemView<'_>]) -> BTreeMap<Id, ArrayCandidate> {
    let mut candidates = BTreeMap::new();
    for item in body {
        let module_item = item.as_module_item();
        // An exported declaration is already published outside this chunk.
        let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = module_item else {
            continue;
        };
        if var.kind != VarDeclKind::Const || var.decls.len() != 1 {
            continue;
        }
        let decl = &var.decls[0];
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        let source = match init.unwrap_parens() {
            Expr::Array(array) if array_is_primitive_literal(array) => None,
            Expr::Ident(ident) if candidates.contains_key(&ident.to_id()) => Some(ident.to_id()),
            _ => continue,
        };
        candidates.insert(binding.id.to_id(), ArrayCandidate { source });
    }
    candidates
}

fn array_is_primitive_literal(array: &ArrayLit) -> bool {
    array.elems.iter().all(|element| {
        let Some(element) = element else {
            return false;
        };
        if element.spread.is_some() {
            return false;
        }
        matches!(
            element.expr.unwrap_parens(),
            Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
        )
    })
}

fn is_standard_set_constructor(
    callee: &Expr,
    shadowed: &BTreeSet<&'static str>,
    flow: &ChunkValueFlow,
) -> bool {
    !shadowed.contains("Set")
        && matches!(callee.unwrap_parens(), Expr::Ident(ident)
            if ident.sym.as_ref() == "Set" && !flow.is_declared(&ident.to_id()))
}

fn has_fresh_primitive_array_argument(args: &[ExprOrSpread]) -> bool {
    matches!(
        args,
        [ExprOrSpread { spread: None, expr }]
            if matches!(expr.unwrap_parens(), Expr::Array(array) if array_is_primitive_literal(array))
    )
}

struct ArrayUseScanner<'a> {
    candidates: &'a BTreeMap<Id, ArrayCandidate>,
    flow: &'a ChunkValueFlow,
    shadowed: &'a BTreeSet<&'static str>,
    invalid: BTreeSet<Id>,
    alias_consumers: Vec<(Id, u32)>,
    direct_literal_calls: BTreeSet<u32>,
    has_dynamic_scope: bool,
}

impl Visit for ArrayUseScanner<'_> {
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(binding) = &node.name
            && let Some(candidate) = self.candidates.get(&binding.id.to_id())
        {
            // Candidate declaration identifiers are not value uses. An alias
            // initializer is also an approved reference to the same array;
            // a primitive array initializer has no candidate references.
            if candidate.source.is_none()
                && let Some(init) = node.init.as_deref()
            {
                init.visit_with(self);
            }
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        node.callee.visit_with(self);
        let args = node.args.as_deref().unwrap_or_default();
        if is_standard_set_constructor(&node.callee, self.shadowed, self.flow) {
            if has_fresh_primitive_array_argument(args) {
                self.direct_literal_calls.insert(node.span.lo.0);
            }
            if let [ExprOrSpread { spread: None, expr }] = args
                && let Expr::Ident(array) = expr.unwrap_parens()
                && self.candidates.contains_key(&array.to_id())
            {
                self.alias_consumers.push((array.to_id(), node.span.lo.0));
                return;
            }
        }
        if let Some(args) = node.args.as_deref() {
            args.visit_with(self);
        }
    }

    fn visit_ident(&mut self, node: &Ident) {
        let id = node.to_id();
        if self.candidates.contains_key(&id) {
            self.invalid.insert(id);
        }
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        self.has_dynamic_scope = true;
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(&node.callee, Callee::Expr(callee)
            if matches!(callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "eval"))
        {
            self.has_dynamic_scope = true;
        }
        node.visit_children_with(self);
    }
}
