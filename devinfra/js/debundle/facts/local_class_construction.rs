//! Source-derived proof for a narrow implicit local class constructor.
//!
//! Shape and identity premises are collected structurally; the prepared native
//! index validates field intrinsics before purity or synchronous execution.
//! Purity additionally requires an exact, eager, zero-argument construction
//! after the declaration and a unique class value in whole-chunk flow.

use super::*;
use crate::analysis_inputs::ProofDependencies;
use crate::facts::semantic_inference::{SemanticFacts, SemanticKey};
use crate::purity::PURE_BUILTIN_NEW_NO_ARGS;
use std::sync::Arc;

#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassConstructionShapes {
    pub(super) classes: BTreeMap<u32, BTreeSet<Id>>,
    pub(super) constructions: BTreeMap<u32, u32>,
}

impl LocalClassConstructionShapes {
    pub(crate) fn analyze(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        flow: &ChunkValueFlow,
        dynamic_scope: DynamicScopeFact,
    ) -> Self {
        if dynamic_scope.present {
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
            let mut native_fields = BTreeSet::new();
            if safe_implicit_class(&class.class, shadowed, &mut native_fields) {
                let key = class.class.span.lo.0;
                proof.classes.insert(key, native_fields);
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
                    proof
                        .constructions
                        .insert(construction.span.lo.0, class_key);
                }
            }
        }
        proof
    }

    pub(crate) fn contains_class(&self, class: u32) -> bool {
        self.classes.contains_key(&class)
    }
}

/// Validated class judgments shared by purity, execution and heap ordering.
/// Structural shapes alone never construct this type.
#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassConstructionProof {
    facts: Arc<SemanticFacts>,
}

impl LocalClassConstructionProof {
    pub(super) fn new(facts: Arc<SemanticFacts>) -> Self {
        Self { facts }
    }

    pub(crate) fn contains_class(&self, class: u32) -> bool {
        self.dependencies_for_class(class).is_some()
    }

    pub(crate) fn dependencies_for_class(&self, class: u32) -> Option<&ProofDependencies> {
        self.facts.dependencies(&SemanticKey::LocalClass(class))
    }

    pub(crate) fn dependencies_for_construction(&self, span: Span) -> Option<&ProofDependencies> {
        self.facts
            .class_for_construction(span)
            .and_then(|class| self.dependencies_for_class(class))
    }

    pub(crate) fn inference_use_for_class(
        &self,
        class: u32,
    ) -> Option<crate::reports::schema::InferenceUse> {
        self.facts.inference_use(&SemanticKey::LocalClass(class))
    }

    pub(crate) fn heap_proof(&self) -> LocalClassHeapProof {
        LocalClassHeapProof {
            facts: Arc::clone(&self.facts),
        }
    }
}

/// Exact stable construction sites whose initialization only creates fresh
/// state. The same derivation validates their field allocations; callee and
/// argument evaluation remain separate effects.
#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassHeapProof {
    facts: Arc<SemanticFacts>,
}

impl LocalClassHeapProof {
    pub(crate) fn dependencies_for_construction(&self, span: Span) -> Option<&ProofDependencies> {
        let class = self.facts.class_for_construction(span)?;
        self.facts.dependencies(&SemanticKey::LocalClass(class))
    }
}

fn safe_implicit_class(
    class: &Class,
    shadowed: &BTreeSet<&'static str>,
    native_fields: &mut BTreeSet<Id>,
) -> bool {
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return false;
    }
    class.body.iter().all(|member| match member {
        ClassMember::Method(method) => {
            method.kind != MethodKind::Setter
                && !matches!(method.key, PropName::Computed(_))
                && method.function.decorators.is_empty()
                && method
                    .function
                    .params
                    .iter()
                    .all(|param| param.decorators.is_empty())
        }
        ClassMember::PrivateMethod(method) => {
            method.function.decorators.is_empty()
                && method
                    .function
                    .params
                    .iter()
                    .all(|param| param.decorators.is_empty())
        }
        ClassMember::ClassProp(prop) => {
            !prop.is_static
                && prop.decorators.is_empty()
                && matches!(prop.key, PropName::Ident(_) | PropName::Str(_))
                && prop
                    .value
                    .as_deref()
                    .is_none_or(|value| safe_field_value(value, shadowed, native_fields))
        }
        ClassMember::PrivateProp(prop) => {
            !prop.is_static
                && prop.decorators.is_empty()
                && prop
                    .value
                    .as_deref()
                    .is_none_or(|value| safe_field_value(value, shadowed, native_fields))
        }
        ClassMember::Empty(_) => true,
        _ => false,
    })
}

fn safe_field_value(
    value: &Expr,
    shadowed: &BTreeSet<&'static str>,
    native_fields: &mut BTreeSet<Id>,
) -> bool {
    match value.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
        | Expr::Arrow(_) => true,
        // Unary `void` evaluates its operand first, so admit only the inert
        // numeric literal form used to spell an absent field value.
        Expr::Unary(unary)
            if unary.op == UnaryOp::Void
                && matches!(
                    unary.arg.unwrap_parens(),
                    Expr::Lit(Lit::Num(number)) if number.value == 0.0
                ) =>
        {
            true
        }
        Expr::Fn(function) => {
            function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|param| param.decorators.is_empty())
        }
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
            let safe = matches!(callee.sym.as_ref(), "Set" | "Map" | "WeakSet" | "WeakMap")
                && PURE_BUILTIN_NEW_NO_ARGS.contains(&callee.sym.as_ref())
                && !shadowed.contains(callee.sym.as_ref())
                && construction.args.as_ref().is_none_or(Vec::is_empty);
            if safe {
                native_fields.insert(callee.to_id());
            }
            safe
        }
        _ => false,
    }
}
