//! A bounded proof of a snapshot's property at one switch-arm use.
//!
//! The receiver, key and snapshot must be local and uncaptured (checked by the
//! shared function census). Between `value = record[key]` and `out = value`,
//! this recognizer permits only a null comparison and literal switch labels.
//! Entering a non-fallthrough literal arm therefore proves the original read's
//! key. A getter cannot rebind these locals, and its return/effects stay in the
//! ordinary member analysis. This changes value provenance only: it neither
//! emits a second read nor suppresses the original read or its effects.

use swc_common::Span;
use swc_ecma_ast::*;

pub(super) struct GuardedSnapshotUse {
    pub(super) span: Span,
    pub(super) receiver: Id,
    pub(super) key: Id,
    pub(super) snapshot: Id,
    pub(super) property: String,
}

struct Snapshot {
    receiver: Id,
    key: Id,
    value: Id,
}

fn ident(expr: &Expr) -> Option<&Ident> {
    match expr.unwrap_parens() {
        Expr::Ident(id) => Some(id),
        _ => None,
    }
}

fn strip_block(mut stmt: &Stmt) -> &Stmt {
    while let Stmt::Block(block) = stmt {
        let [only] = block.stmts.as_slice() else {
            break;
        };
        stmt = only;
    }
    stmt
}

fn snapshot(value: Id, read: &Expr) -> Option<Snapshot> {
    let Expr::Member(member) = read.unwrap_parens() else {
        return None;
    };
    let MemberProp::Computed(property) = &member.prop else {
        return None;
    };
    let receiver = ident(&member.obj)?.to_id();
    let key = ident(&property.expr)?.to_id();
    if receiver == key || receiver == value || key == value {
        return None;
    }
    Some(Snapshot {
        receiver,
        key,
        value,
    })
}

fn assignment_snapshot(expr: &Expr) -> Option<Snapshot> {
    let Expr::Assign(assign) = expr.unwrap_parens() else {
        return None;
    };
    if assign.op != AssignOp::Assign {
        return None;
    }
    let AssignTarget::Simple(SimpleAssignTarget::Ident(value)) = &assign.left else {
        return None;
    };
    snapshot(value.id.to_id(), &assign.right)
}

fn statement_snapshot(stmt: &Stmt) -> Option<Snapshot> {
    match stmt {
        Stmt::Expr(expr) => assignment_snapshot(&expr.expr),
        Stmt::Decl(Decl::Var(decl)) => {
            let [declarator] = decl.decls.as_slice() else {
                return None;
            };
            let Pat::Ident(value) = &declarator.name else {
                return None;
            };
            snapshot(value.id.to_id(), declarator.init.as_deref()?)
        }
        _ => None,
    }
}

fn condition_snapshot(test: &Expr) -> Option<Snapshot> {
    // Any prefix is evaluated before the snapshot. Do not admit a suffix that
    // might run code, coerce a value, or rebind one of the three locals.
    let tail = match test.unwrap_parens() {
        Expr::Bin(binary) if binary.op == BinaryOp::LogicalAnd => &binary.right,
        other => other,
    };
    let Expr::Seq(sequence) = tail.unwrap_parens() else {
        return None;
    };
    let [read, check] = sequence.exprs.as_slice() else {
        return None;
    };
    let snapshot = assignment_snapshot(read)?;
    if !non_null_check(check, &snapshot.value) {
        return None;
    }
    Some(snapshot)
}

fn non_null_check(test: &Expr, value: &Id) -> bool {
    let Expr::Bin(check) = test.unwrap_parens() else {
        return false;
    };
    check.op == BinaryOp::NotEq
        && ident(&check.left).is_some_and(|left| left.to_id() == *value)
        && matches!(check.right.unwrap_parens(), Expr::Lit(Lit::Null(_)))
}

fn switch_statement(stmt: &Stmt) -> Option<&SwitchStmt> {
    match strip_block(stmt) {
        Stmt::Switch(switch) => Some(switch),
        _ => None,
    }
}

pub(super) fn guarded_snapshot_uses(node: &ForInStmt) -> Vec<GuardedSnapshotUse> {
    let Some((snapshot, switch)) = snapshot_switch(strip_block(&node.body)) else {
        return Vec::new();
    };
    if ident(&switch.discriminant).map(Ident::to_id) != Some(snapshot.key.clone())
        || switch.cases.iter().any(|case| {
            case.test
                .as_deref()
                .is_some_and(|test| !matches!(test.unwrap_parens(), Expr::Lit(_)))
        })
    {
        return Vec::new();
    }
    let mut uses = Vec::new();
    for (index, case) in switch.cases.iter().enumerate() {
        // A preceding arm must terminate, even when its label is a literal.
        // Grouped labels and other control-flow shapes conservatively decline.
        if index > 0 && !switch.cases[index - 1].cons.last().is_some_and(terminates) {
            continue;
        }
        let Some(Expr::Lit(Lit::Str(property))) = case.test.as_deref().map(Expr::unwrap_parens)
        else {
            continue;
        };
        let [
            Stmt::Expr(statement),
            Stmt::Break(BreakStmt { label: None, .. }),
        ] = case.cons.as_slice()
        else {
            continue;
        };
        let Expr::Assign(assign) = statement.expr.unwrap_parens() else {
            continue;
        };
        let AssignTarget::Simple(SimpleAssignTarget::Ident(target)) = &assign.left else {
            continue;
        };
        let Some(value) = ident(&assign.right) else {
            continue;
        };
        if assign.op != AssignOp::Assign
            || value.to_id() != snapshot.value
            || [&snapshot.receiver, &snapshot.key, &snapshot.value].contains(&&target.id.to_id())
        {
            continue;
        }
        uses.push(GuardedSnapshotUse {
            span: value.span,
            receiver: snapshot.receiver.clone(),
            key: snapshot.key.clone(),
            snapshot: snapshot.value.clone(),
            property: property.value.to_string_lossy().into_owned(),
        });
    }
    uses
}

fn terminates(stmt: &Stmt) -> bool {
    matches!(
        stmt,
        Stmt::Break(BreakStmt { label: None, .. }) | Stmt::Return(_) | Stmt::Throw(_)
    )
}

fn snapshot_switch(stmt: &Stmt) -> Option<(Snapshot, &SwitchStmt)> {
    match strip_block(stmt) {
        Stmt::Block(block) => {
            let [read, next] = block.stmts.as_slice() else {
                return None;
            };
            let snapshot = statement_snapshot(read)?;
            let next = strip_block(next);
            let switch = match next {
                Stmt::Switch(switch) => switch,
                Stmt::If(if_) if non_null_check(&if_.test, &snapshot.value) => {
                    switch_statement(&if_.cons)?
                }
                _ => return None,
            };
            Some((snapshot, switch))
        }
        Stmt::If(if_) => {
            if let Some(snapshot) = condition_snapshot(&if_.test)
                && let Some(switch) = switch_statement(&if_.cons)
            {
                return Some((snapshot, switch));
            }
            // The condition belongs to the prefix only. It may call code or
            // otherwise be arbitrary as long as the read and its null guard
            // both happen afterward in the consequent.
            snapshot_switch(strip_block(&if_.cons))
        }
        _ => None,
    }
}
