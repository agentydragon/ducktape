use super::*;
use crate::facts::{ChunkValueFlow, DynamicScopeFact, own_data_keys};

/// Prepared value shape and whole-chunk identity guards for the ordinary
/// declaration classifier. The initializer still follows its normal purity
/// path, including call/argument effects and proof dependencies.
pub(crate) struct FreshRecordDestructuringContext<'a> {
    pub(crate) flow: &'a ChunkValueFlow,
    pub(crate) dynamic_scope: DynamicScopeFact,
    pub(crate) rebindings: &'a BTreeSet<Id>,
}

impl FreshRecordDestructuringContext<'_> {
    pub(crate) fn has_own_data_fields(&self, pattern: &Pat, init: &Expr) -> bool {
        let Pat::Object(pattern) = pattern else {
            return false;
        };
        let keys = match init.unwrap_parens() {
            Expr::Object(object) => own_data_keys(object),
            Expr::Call(call) => {
                if self.dynamic_scope.present {
                    return false;
                }
                let Callee::Expr(callee) = &call.callee else {
                    return false;
                };
                let Expr::Ident(ident) = callee.unwrap_parens() else {
                    return false;
                };
                if self.rebindings.contains(&ident.to_id()) {
                    return false;
                }
                self.flow.fresh_returned_record_keys(call).cloned()
            }
            _ => None,
        };
        let Some(keys) = keys else {
            return false;
        };
        pattern
            .props
            .iter()
            .map(|prop| match prop {
                ObjectPatProp::KeyValue(pair) if matches!(pair.value.as_ref(), Pat::Ident(_)) => {
                    match &pair.key {
                        PropName::Ident(ident) => Some(ident.sym.to_string()),
                        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
                        _ => None,
                    }
                }
                _ => None,
            })
            .collect::<Option<Vec<_>>>()
            .is_some_and(|selected| selected.iter().all(|key| keys.contains(key)))
    }
}

/// An ordered, closed function body that writes only to data fields already
/// owned by one newly allocated record. The single SWC parse supplies this
/// certificate to both body purity and synchronous callback analysis.
#[derive(Debug, Clone)]
pub(crate) struct FreshRecordProof {
    pub(crate) object: u32,
    pub(crate) local: Id,
    pub(crate) write_spans: BTreeSet<u32>,
}

pub(crate) fn fresh_record_proof(params: &[&Pat], body: &FunctionBody) -> Option<FreshRecordProof> {
    let parameters: BTreeSet<Id> = params
        .iter()
        .map(|param| match param {
            Pat::Ident(binding) => Some(binding.id.to_id()),
            _ => None,
        })
        .collect::<Option<_>>()?;
    let [first, middle @ .., last] = body.stmts.as_slice() else {
        return None;
    };
    if middle.is_empty() {
        return None;
    }
    let Stmt::Decl(Decl::Var(declaration)) = first else {
        return None;
    };
    let [declarator] = declaration.decls.as_slice() else {
        return None;
    };
    let (Pat::Ident(binding), Some(initializer)) = (&declarator.name, &declarator.init) else {
        return None;
    };
    if declaration.kind != VarDeclKind::Const || parameters.contains(&binding.id.to_id()) {
        return None;
    }
    let Expr::Object(object) = initializer.unwrap_parens() else {
        return None;
    };
    let mut own_fields = BTreeSet::new();
    for property in &object.props {
        if !is_plain_data_prop(property) {
            return None;
        }
        let PropOrSpread::Prop(property) = property else {
            return None;
        };
        let Prop::KeyValue(pair) = property.as_ref() else {
            return None;
        };
        let name = match &pair.key {
            PropName::Ident(name) => name.sym.to_string(),
            PropName::Str(name) => name.value.to_string_lossy().into_owned(),
            _ => return None,
        };
        if !own_fields.insert(name) || !matches!(pair.value.unwrap_parens(), Expr::Lit(_)) {
            return None;
        }
    }
    let Stmt::Return(ret) = last else {
        return None;
    };
    if !matches!(ret.arg.as_deref().map(Expr::unwrap_parens), Some(Expr::Ident(id)) if id.to_id() == binding.id.to_id())
    {
        return None;
    }
    let mut written = BTreeSet::new();
    let mut write_spans = BTreeSet::new();
    for statement in middle {
        let Stmt::Expr(expression) = statement else {
            return None;
        };
        let Expr::Assign(assignment) = expression.expr.unwrap_parens() else {
            return None;
        };
        if assignment.op != AssignOp::Assign {
            return None;
        }
        let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left else {
            return None;
        };
        let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
            return None;
        };
        let MemberProp::Ident(property) = &member.prop else {
            return None;
        };
        let name = property.sym.to_string();
        if receiver.to_id() != binding.id.to_id()
            || !own_fields.contains(&name)
            || !written.insert(name)
            || !matches!(assignment.right.unwrap_parens(), Expr::Ident(value) if parameters.contains(&value.to_id()))
                && !matches!(assignment.right.unwrap_parens(), Expr::Lit(_))
        {
            return None;
        }
        write_spans.insert(assignment.span.lo.0);
    }
    Some(FreshRecordProof {
        object: object.span.lo.0,
        local: binding.id.to_id(),
        write_spans,
    })
}
