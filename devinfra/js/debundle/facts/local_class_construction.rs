//! Source-derived proof for a narrow implicit local class constructor.
//!
//! Shape and identity premises are collected structurally; the prepared native
//! index validates field intrinsics before purity or synchronous execution.
//! Purity additionally requires an exact, eager, zero-argument construction
//! after the declaration and a unique class value in whole-chunk flow.

use super::*;
use crate::analysis_inputs::ProofDependencies;
use crate::purity::PURE_BUILTIN_NEW_NO_ARGS;

#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassConstructionProof {
    classes: BTreeMap<u32, BTreeSet<Id>>,
    dependencies: BTreeMap<u32, ProofDependencies>,
    constructions: BTreeMap<u32, u32>,
}

impl LocalClassConstructionProof {
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
                proof.dependencies.insert(key, ProofDependencies::default());
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

    pub(crate) fn dependencies_for_class(&self, class: u32) -> Option<&ProofDependencies> {
        self.dependencies.get(&class)
    }

    pub(crate) fn dependencies_for_construction(&self, span: Span) -> Option<&ProofDependencies> {
        self.constructions
            .get(&span.lo.0)
            .and_then(|class| self.dependencies_for_class(*class))
    }

    pub(crate) fn with_native_dependencies(
        &self,
        native_constructor_dependencies: impl Fn(&Id) -> Option<ProofDependencies>,
    ) -> Self {
        let mut classes = BTreeMap::new();
        let mut dependencies = BTreeMap::new();
        for (class, fields) in &self.classes {
            let mut required = ProofDependencies::default();
            let mut valid = true;
            for field in fields {
                let Some(field_dependencies) = native_constructor_dependencies(field) else {
                    valid = false;
                    break;
                };
                required.extend(&field_dependencies);
            }
            if valid {
                classes.insert(*class, fields.clone());
                dependencies.insert(*class, required);
            }
        }
        Self {
            constructions: self
                .constructions
                .iter()
                .filter(|(_, class)| classes.contains_key(class))
                .map(|(span, class)| (*span, *class))
                .collect(),
            classes,
            dependencies,
        }
    }
    pub(crate) fn heap_proof(
        &self,
        native_constructor_is_heap_local: impl Fn(&Id) -> bool,
    ) -> LocalClassHeapProof {
        let classes: BTreeSet<_> = self
            .classes
            .iter()
            .filter(|(_, fields)| fields.iter().all(&native_constructor_is_heap_local))
            .map(|(class, _)| *class)
            .collect();
        LocalClassHeapProof {
            constructions: self
                .constructions
                .iter()
                .filter(|(_, class)| classes.contains(class))
                .map(|(span, class)| (*span, self.dependencies[class].clone()))
                .collect(),
        }
    }
}

/// Exact stable construction sites whose initialization only creates fresh
/// state. Callee and argument evaluation are separate effects.
#[derive(Debug, Clone, Default)]
pub(crate) struct LocalClassHeapProof {
    constructions: BTreeMap<u32, ProofDependencies>,
}

impl LocalClassHeapProof {
    pub(crate) fn contains_construction(&self, span: Span) -> bool {
        self.dependencies_for_construction(span).is_some()
    }

    pub(crate) fn dependencies_for_construction(&self, span: Span) -> Option<&ProofDependencies> {
        self.constructions.get(&span.lo.0)
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

#[cfg(test)]
mod heap_dependency_tests {
    use super::*;
    use crate::analysis_inputs::{ProofDependencies, ValueInputRef};
    use crate::reports::schema::NativeIntegrityGuard;

    fn heap_fact(
        source: &str,
        binding: &str,
        dependencies_for_set: Option<ProofDependencies>,
    ) -> crate::facts::StatementFacts {
        let module = raw_js_test_support::parse(source);
        let body = top_level_item_views(&module.body);
        let flow = ChunkValueFlow::analyze(&body);
        let shadowed = compute_shadowed_globals(&body);
        let construction_proof = LocalClassConstructionProof::analyze(
            &body,
            &shadowed,
            &flow,
            DynamicScopeFact::analyze(&body),
        )
        .with_native_dependencies(|field| {
            if field.0.as_ref() == "Set" {
                dependencies_for_set.clone()
            } else {
                Some(ProofDependencies::default())
            }
        });
        let heap_proof = construction_proof.heap_proof(|field| field.0.as_ref() == "Set");
        let mut facts = analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None).facts;
        super::super::array_content_effects::annotate_array_content_effects(
            &body,
            &flow,
            &heap_proof,
            &mut facts,
        );
        facts
            .into_iter()
            .find(|fact| {
                fact.declared
                    .iter()
                    .any(|declared| declared.0.as_ref() == binding)
            })
            .unwrap_or_else(|| panic!("expected a source unit declaring {binding}"))
    }

    #[test]
    fn local_class_heap_dependency_is_reported_only_when_consumed() {
        let dependency = ValueInputRef {
            defining_chunk: "static/provider".to_string(),
            input_index: 2,
        };
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([dependency.clone()]),
        };
        let native_backed = heap_fact(
            "class Registry { entries = new Set(); } const instance = new Registry();",
            "instance",
            Some(dependencies),
        );
        assert!(!native_backed.array_contents.opaque.eager);
        assert_eq!(
            native_backed.heap_native_integrity_uses,
            vec![crate::reports::schema::NativeIntegrityUse {
                guard: NativeIntegrityGuard::LocalClassConstruction,
                value_input_refs: BTreeSet::from([dependency]),
            }]
        );

        let source_only = heap_fact(
            "class PlainStore { ready = 0; } const plainStore = new PlainStore();",
            "plainStore",
            Some(ProofDependencies {
                value_inputs: BTreeSet::from([ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                }]),
            }),
        );
        assert!(!source_only.array_contents.opaque.eager);
        assert!(source_only.heap_native_integrity_uses.is_empty());

        let unchecked_native = heap_fact(
            "class UncheckedStore { entries = new Set(); } const uncheckedStore = new UncheckedStore();",
            "uncheckedStore",
            None,
        );
        assert!(unchecked_native.array_contents.opaque.eager);
        assert!(unchecked_native.heap_native_integrity_uses.is_empty());
    }
}
