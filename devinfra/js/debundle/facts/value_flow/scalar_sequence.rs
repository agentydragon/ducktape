//! Local scalar overwrites within one comma expression.
//!
//! These candidates still need the enclosing function's binding-ownership,
//! capture and dynamic-scope checks. No fact escapes the sequence expression.

use std::collections::{BTreeSet, HashSet};

use binding_targets::{TargetAccessRecorder, record_assign_target, record_update_target};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

pub(super) struct ScalarSequenceUses {
    pub required_bindings: BTreeSet<Id>,
    pub uses: Vec<Ident>,
}

pub(super) fn scalar_sequence_uses(sequence: &SeqExpr) -> Option<ScalarSequenceUses> {
    let mut scalar = HashSet::new();
    let mut result = ScalarSequenceUses {
        required_bindings: BTreeSet::new(),
        uses: Vec::new(),
    };
    for term in &sequence.exprs {
        if !scalar.is_empty() {
            let mut accesses = TermAccesses::default();
            term.visit_with(&mut accesses);
            // Kill even conditional or later writes before inspecting this
            // term's reads. This deliberately forgoes precision within a term,
            // without a control-flow graph or arbitrary expression replay.
            for binding in &accesses.writes {
                scalar.remove(binding);
            }
            result.uses.extend(
                accesses
                    .reads
                    .into_iter()
                    .filter(|read| scalar.contains(&read.to_id())),
            );
        }

        if let Expr::Assign(assignment) = term.unwrap_parens()
            && assignment.op == AssignOp::Assign
            && let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assignment.left
            && primitive_result(&assignment.right, &scalar)
        {
            let binding = binding.id.to_id();
            result.required_bindings.insert(binding.clone());
            scalar.insert(binding);
        }
    }
    (!result.uses.is_empty()).then_some(result)
}

/// A normal result of these operators is primitive even when coercion runs
/// user code or throws. Effects and completion are checked elsewhere. A local
/// identifier needs an earlier definite scalar overwrite in this sequence.
fn primitive_result(expression: &Expr, scalar: &HashSet<Id>) -> bool {
    let mut pending = vec![expression];
    while let Some(expression) = pending.pop() {
        match expression.unwrap_parens() {
            Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_))
            | Expr::Tpl(_)
            | Expr::Unary(_) => {}
            Expr::Bin(binary)
                if !matches!(
                    binary.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) => {}
            Expr::Ident(identifier) if scalar.contains(&identifier.to_id()) => {}
            Expr::Cond(conditional) => {
                pending.push(&conditional.cons);
                pending.push(&conditional.alt);
            }
            _ => return false,
        }
    }
    true
}

#[derive(Default)]
struct TermAccesses {
    reads: Vec<Ident>,
    writes: HashSet<Id>,
}

impl TargetAccessRecorder for TermAccesses {
    fn record_binding_write(&mut self, id: &Id) {
        self.writes.insert(id.clone());
    }
}

impl Visit for TermAccesses {
    fn visit_ident(&mut self, identifier: &Ident) {
        self.reads.push(identifier.clone());
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        record_assign_target(&assignment.left, self);
        assignment.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        record_update_target(&update.arg, self);
        update.visit_children_with(self);
    }

    // The enclosing census separately discovers captures, including class
    // initialization references, and rejects every affected proof binding.
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_class(&mut self, _: &Class) {}
}
