//! Source-only, report-only private class allocation certificates.
//!
//! This does not bless the tier-0 classifier or prove the remaining schedule.
//! Every earlier unit is a potential effect/throw barrier. A direct reader,
//! including a closure that captures the cell, must follow that entire prefix.
use std::collections::{BTreeMap, BTreeSet};

use binding_targets::{TargetAccessRecorder, declaration_ids};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Default)]
struct Census {
    declarations: BTreeMap<Id, usize>,
    reads: BTreeSet<Id>,
    writes: BTreeSet<Id>,
    incomplete: bool,
}

impl TargetAccessRecorder for Census {
    fn record_binding_write(&mut self, id: &Id) {
        self.writes.insert(id.clone());
    }
}

impl Visit for Census {
    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        *self.declarations.entry(binding.id.to_id()).or_default() += 1;
    }
    fn visit_ident(&mut self, ident: &Ident) {
        self.reads.insert(ident.to_id());
        // Refuse even shadowed eval: no dynamic-scope assumption is needed.
        if &*ident.sym == "eval" {
            self.incomplete = true;
        }
    }
    fn visit_fn_decl(&mut self, function: &FnDecl) {
        self.incomplete |= function.declare;
        *self.declarations.entry(function.ident.to_id()).or_default() += 1;
        function.function.visit_with(self);
    }
    fn visit_class_decl(&mut self, class: &ClassDecl) {
        self.incomplete |= class.declare;
        *self.declarations.entry(class.ident.to_id()).or_default() += 1;
        class.class.visit_with(self);
    }
    fn visit_fn_expr(&mut self, function: &FnExpr) {
        if let Some(name) = &function.ident {
            *self.declarations.entry(name.to_id()).or_default() += 1;
        }
        function.function.visit_with(self);
    }
    fn visit_class_expr(&mut self, class: &ClassExpr) {
        if let Some(name) = &class.ident {
            *self.declarations.entry(name.to_id()).or_default() += 1;
        }
        class.class.visit_with(self);
    }
    fn visit_var_decl(&mut self, declaration: &VarDecl) {
        self.incomplete |= declaration.declare;
        declaration.visit_children_with(self);
    }
    fn visit_import_decl(&mut self, _: &ImportDecl) {}
    fn visit_named_export(&mut self, _: &NamedExport) {}
    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        binding_targets::record_assign_target(&assign.left, self);
        assign.visit_children_with(self);
    }
    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        binding_targets::record_update_target(&update.arg, self);
        update.visit_children_with(self);
    }
    fn visit_for_head(&mut self, head: &ForHead) {
        if let ForHead::Pat(pattern) = head {
            binding_targets::record_pat_write(pattern, self);
        }
        head.visit_children_with(self);
    }
    fn visit_with_stmt(&mut self, statement: &WithStmt) {
        self.incomplete = true;
        statement.visit_children_with(self);
    }
    fn visit_invalid(&mut self, _: &Invalid) {
        self.incomplete = true;
    }
    fn visit_using_decl(&mut self, declaration: &UsingDecl) {
        self.incomplete = true;
        declaration.visit_children_with(self);
    }
}

/// One resolved AST supplies the census and every candidate. No ownership,
/// import-cycle optimism, helper spelling, or tier-0 effect fact is consulted.
pub struct SourceIndex<'a> {
    units: Vec<Unit<'a>>,
    census: Vec<Census>,
    declarations: BTreeMap<Id, usize>,
    writes: BTreeSet<Id>,
    exports: BTreeSet<Id>,
    incomplete: bool,
}

enum Unit<'a> {
    Item(&'a ModuleItem),
    Declarator(&'a VarDeclarator),
}

impl Unit<'_> {
    fn decl(&self) -> Option<&Decl> {
        match self {
            Self::Item(ModuleItem::Stmt(Stmt::Decl(decl))) => Some(decl),
            Self::Item(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export))) => {
                Some(&export.decl)
            }
            _ => None,
        }
    }
    fn class(&self) -> Option<(Id, &Class)> {
        let declarator = match self {
            Self::Declarator(declarator) => Some(*declarator),
            _ => match self.decl() {
                Some(Decl::Class(class)) => return Some((class.ident.to_id(), &class.class)),
                Some(Decl::Var(var)) if var.decls.len() == 1 => var.decls.first(),
                _ => None,
            },
        }?;
        let (Pat::Ident(name), Some(Expr::Class(class))) =
            (&declarator.name, declarator.init.as_deref())
        else {
            return None;
        };
        Some((name.id.to_id(), &class.class))
    }
    fn is_hoisted_function(&self) -> bool {
        matches!(self.decl(), Some(Decl::Fn(_)))
    }
}

impl<'a> SourceIndex<'a> {
    pub fn new(module: &'a Module) -> Self {
        let mut units = Vec::new();
        let mut exports = BTreeSet::new();
        let mut incomplete = false;
        for item in &module.body {
            match js_ast::split_var_declarators(item) {
                Some(declarators) => units.extend(declarators.iter().map(Unit::Declarator)),
                None => units.push(Unit::Item(item)),
            }
            match item {
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    exports.extend(declaration_ids(&export.decl));
                }
                ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_none() => {
                    for specifier in &export.specifiers {
                        if let ExportSpecifier::Named(named) = specifier
                            && let ModuleExportName::Ident(local) = &named.orig
                        {
                            exports.insert(local.to_id());
                        }
                    }
                }
                ModuleItem::ModuleDecl(
                    ModuleDecl::ExportDefaultExpr(_)
                    | ModuleDecl::ExportDefaultDecl(_)
                    | ModuleDecl::TsExportAssignment(_)
                    | ModuleDecl::TsImportEquals(_)
                    | ModuleDecl::TsNamespaceExport(_),
                ) => incomplete = true,
                _ => {}
            }
        }
        let mut census = Vec::new();
        let mut declarations = BTreeMap::new();
        let mut writes = BTreeSet::new();
        for unit in &units {
            let mut scan = Census::default();
            match unit {
                Unit::Item(item) => item.visit_with(&mut scan),
                Unit::Declarator(declarator) => declarator.visit_with(&mut scan),
            }
            incomplete |= scan.incomplete;
            for (id, count) in &scan.declarations {
                *declarations.entry(id.clone()).or_default() += count;
            }
            writes.extend(scan.writes.iter().cloned());
            census.push(scan);
        }
        Self {
            units,
            census,
            declarations,
            writes,
            exports,
            incomplete,
        }
    }

    pub fn unit_count(&self) -> usize {
        self.units.len()
    }

    pub fn certificates(&self) -> Vec<Candidate> {
        self.units
            .iter()
            .enumerate()
            .filter_map(|(init, unit)| {
                let (id, class) = unit.class()?;
                let readers: Vec<usize> = self
                    .census
                    .iter()
                    .enumerate()
                    .filter(|(owner, census)| *owner != init && census.reads.contains(&id))
                    .map(|(owner, _)| owner)
                    .collect();
                let refusal = if self.incomplete {
                    Some(Refusal::IncompleteCensus)
                } else if self.exports.contains(&id) {
                    Some(Refusal::ExportedCell)
                } else if self.declarations.get(&id) != Some(&1) || self.writes.contains(&id) {
                    Some(Refusal::RedeclaredOrRebound)
                } else if !plain_class(class) {
                    Some(Refusal::EagerClassEvaluation)
                } else if readers
                    .iter()
                    .any(|owner| self.units[*owner].is_hoisted_function())
                {
                    Some(Refusal::HoistedReader)
                } else if readers.iter().any(|owner| *owner < init) {
                    Some(Refusal::AlreadyAvailableReader)
                } else {
                    None
                };
                Some(Candidate {
                    init,
                    binding: id.0.to_string(),
                    resolved_id: format!("{}#{:?}", id.0, id.1),
                    readers,
                    refusal,
                })
            })
            .collect()
    }
}

fn plain_class(class: &Class) -> bool {
    class.super_class.is_none()
        && class.decorators.is_empty()
        && class.body.iter().all(|member| match member {
            ClassMember::Method(method) => {
                method.kind == MethodKind::Method
                    && !matches!(method.key, PropName::Computed(_))
                    && method.function.decorators.is_empty()
                    && method
                        .function
                        .params
                        .iter()
                        .all(|param| param.decorators.is_empty())
            }
            ClassMember::Constructor(constructor) => {
                constructor.params.iter().all(|param| match param {
                    ParamOrTsParamProp::Param(param) => param.decorators.is_empty(),
                    ParamOrTsParamProp::TsParamProp(_) => false,
                })
            }
            ClassMember::Empty(_) => true,
            _ => false,
        })
}

#[derive(Debug, Clone, Copy, serde::Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Refusal {
    IncompleteCensus,
    ExportedCell,
    RedeclaredOrRebound,
    EagerClassEvaluation,
    HoistedReader,
    AlreadyAvailableReader,
}

#[derive(serde::Serialize)]
pub struct Candidate {
    pub init: usize,
    pub binding: String,
    pub resolved_id: String,
    pub readers: Vec<usize>,
    pub refusal: Option<Refusal>,
}

#[derive(Debug, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConstraintKind {
    InitBeforeRead,
    PrefixBeforePublication,
}

#[derive(serde::Serialize)]
pub struct Violation {
    pub before: usize,
    pub after: usize,
    pub kind: ConstraintKind,
}

impl Candidate {
    /// Check constraints generated without looking at the proposed schedule.
    /// Even lazy captures get an init edge; this deliberately rejects some
    /// valid schedules rather than needing a callable activation proof.
    pub fn violations(&self, positions: &[usize]) -> Vec<Violation> {
        let mut violations = Vec::new();
        if self.refusal.is_some() {
            return violations;
        }
        for &reader in &self.readers {
            if positions[self.init] > positions[reader] {
                violations.push(Violation {
                    before: self.init,
                    after: reader,
                    kind: ConstraintKind::InitBeforeRead,
                });
            }
            for before in 0..reader {
                if before != self.init && positions[before] > positions[reader] {
                    violations.push(Violation {
                        before,
                        after: reader,
                        kind: ConstraintKind::PrefixBeforePublication,
                    });
                }
            }
        }
        violations
    }

    pub fn fence_count(&self) -> usize {
        self.readers.iter().sum()
    }
}

pub fn positions(order: &[usize], count: usize) -> anyhow::Result<Vec<usize>> {
    anyhow::ensure!(
        order.len() == count,
        "order has {} units, source has {count}",
        order.len()
    );
    let mut positions = vec![usize::MAX; count];
    for (position, &unit) in order.iter().enumerate() {
        anyhow::ensure!(
            unit < count && positions[unit] == usize::MAX,
            "order is not a unique full ordinal permutation"
        );
        positions[unit] = position;
    }
    Ok(positions)
}
