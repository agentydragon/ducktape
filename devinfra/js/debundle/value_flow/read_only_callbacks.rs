//! Closed source shapes whose invocation can only read lexical cells and
//! return primitives. Returned bindings still need a proof at the queried cut.

use super::*;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReadOnlyCallback<Binding: Ord> {
    pub reads: BTreeSet<Binding>,
    pub returned: BTreeSet<Binding>,
}

impl<Binding: Ord> ReadOnlyCallback<Binding> {
    pub(super) fn map<Other: Ord>(
        &self,
        mut binding: impl FnMut(&Binding) -> Other,
    ) -> ReadOnlyCallback<Other> {
        ReadOnlyCallback {
            reads: self.reads.iter().map(&mut binding).collect(),
            returned: self.returned.iter().map(binding).collect(),
        }
    }
}

fn operand(expr: &Expr) -> Option<Option<Id>> {
    match expr.unwrap_parens() {
        Expr::Ident(id) => Some(Some(id.to_id())),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_)) => {
            Some(None)
        }
        _ => None,
    }
}

pub(super) fn expression(expr: &Expr, symbols: &mut FlowSymbols) -> Option<BTreeSet<BindingSlot>> {
    Some(
        operand(expr)?
            .map(|id| symbols.binding(id))
            .into_iter()
            .collect(),
    )
}

pub(super) fn block(
    statements: &[Stmt],
    symbols: &mut FlowSymbols,
) -> Option<BTreeSet<BindingSlot>> {
    let mut returned = BTreeSet::new();
    for statement in statements {
        match statement {
            Stmt::Empty(_) => {}
            Stmt::Expr(statement) => {
                operand(&statement.expr)?;
            }
            Stmt::Return(statement) => {
                if let Some(expr) = &statement.arg {
                    returned.extend(expression(expr, symbols)?);
                }
            }
            _ => return None,
        }
    }
    Some(returned)
}
