//! Source-derived proof for a narrow implicit local class constructor.
//!
//! The class-shape certificate is shared by purity and synchronous execution.
//! Purity additionally requires an exact, eager, zero-argument construction
//! after the declaration and a unique class value in whole-chunk flow.

use super::*;
use crate::purity::PURE_BUILTIN_NEW_NO_ARGS;

#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassConstructionProof {
    classes: BTreeSet<u32>,
    constructions: BTreeSet<u32>,
}

impl LocalClassConstructionProof {
    pub(crate) fn analyze(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        flow: &ChunkValueFlow,
        set_iterable_proof: &SetIterableProof,
    ) -> Self {
        if set_iterable_proof.has_dynamic_scope() {
            return Self::default();
        }
        let mut proof = Self::default();
        let mut declarations = BTreeMap::<Id, (u32, usize)>::new();
        for (ordinal, item) in body.iter().enumerate() {
            let class = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => class,
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    let Decl::Class(class) = &export.decl else {
                        continue;
                    };
                    class
                }
                _ => continue,
            };
            if safe_implicit_class(&class.class, shadowed) {
                let key = class.class.span.lo.0;
                proof.classes.insert(key);
                declarations.insert(class.ident.to_id(), (key, ordinal));
            }
        }

        for (ordinal, item) in body.iter().enumerate() {
            let Some(var) = var_decl_of_item(item.as_module_item()) else {
                continue;
            };
            for declarator in &var.decls {
                let Some(Expr::New(construction)) =
                    declarator.init.as_deref().map(Expr::unwrap_parens)
                else {
                    continue;
                };
                let Expr::Ident(callee) = construction.callee.unwrap_parens() else {
                    continue;
                };
                let Some(&(class_key, declared_at)) = declarations.get(&callee.to_id()) else {
                    continue;
                };
                if ordinal <= declared_at
                    || construction
                        .args
                        .as_ref()
                        .is_some_and(|args| !args.is_empty())
                {
                    continue;
                }
                // Monotone value flow includes every assignment and unknown
                // alternative. A same-named local shadow or rebound binding
                // cannot establish this unique class origin.
                let values: Vec<_> = flow
                    .values_of_binding(&callee.to_id())
                    .into_iter()
                    .collect();
                if values == [ValueOrigin::Class(class_key)] {
                    proof.constructions.insert(construction.span.lo.0);
                }
            }
        }
        proof
    }

    pub(crate) fn contains_class(&self, span: Span) -> bool {
        self.classes.contains(&span.lo.0)
    }

    pub(crate) fn contains_construction(&self, span: Span) -> bool {
        self.constructions.contains(&span.lo.0)
    }
}

fn safe_implicit_class(class: &Class, shadowed: &BTreeSet<&'static str>) -> bool {
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return false;
    }
    class.body.iter().all(|member| match member {
        ClassMember::Method(method) => {
            method.kind != MethodKind::Setter
                && !matches!(method.key, PropName::Computed(_))
                && method.function.decorators.is_empty()
        }
        ClassMember::PrivateMethod(method) => method.function.decorators.is_empty(),
        ClassMember::ClassProp(prop) => {
            !prop.is_static
                && prop.decorators.is_empty()
                && matches!(prop.key, PropName::Ident(_) | PropName::Str(_))
                && prop
                    .value
                    .as_deref()
                    .is_none_or(|value| safe_field_value(value, shadowed))
        }
        ClassMember::PrivateProp(prop) => {
            !prop.is_static
                && prop.decorators.is_empty()
                && prop
                    .value
                    .as_deref()
                    .is_none_or(|value| safe_field_value(value, shadowed))
        }
        ClassMember::Empty(_) => true,
        _ => false,
    })
}

fn safe_field_value(value: &Expr, shadowed: &BTreeSet<&'static str>) -> bool {
    match value.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
        | Expr::Fn(_)
        | Expr::Arrow(_) => true,
        Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
            matches!(
                unary.arg.unwrap_parens(),
                Expr::Lit(Lit::Num(_) | Lit::Bool(_))
            )
        }
        Expr::New(construction) => {
            let Expr::Ident(callee) = construction.callee.unwrap_parens() else {
                return false;
            };
            matches!(callee.sym.as_ref(), "Set" | "Map" | "WeakSet" | "WeakMap")
                && PURE_BUILTIN_NEW_NO_ARGS.contains(&callee.sym.as_ref())
                && !shadowed.contains(callee.sym.as_ref())
                && construction.args.as_ref().is_none_or(Vec::is_empty)
        }
        _ => false,
    }
}
