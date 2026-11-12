//! AST collection for synchronous execution: callables, roots, writes, and source order.

mod callback_arguments;
mod prototype_properties;

use super::callable_body_facts::CallableBodyFacts;
use super::local_operations::{DeferredCallableCreation, LocalBinding, LocalBody};
use super::namespace_wrapper::{self, NamespaceWrapperCandidate};
use super::noninvoking_initialization::{InitializationSourceIdentity, OriginalInitializer};
use super::strict_provider;
use super::value_model::{
    ArrayElement, ArrayIteration, CallSite, ClassSurface, ClassValueFacts, DiscardedHeritageCall,
    FromEntriesMappedProof, LexicalClassHeritage, MemberKey, NamespaceValueFacts,
    OwnPropertyDescriptorSyntax, OwnPropertyShape, ValueExpr, infer_namespace_aliases,
    object_data_property_name, static_prop, static_property_name,
};
use crate::analysis_inputs::{
    AnalysisInputs, BundleGlobalNames, HostBehaviorKey, InferredSetConstruction,
    InferredSetOperation, NamespaceWrapperSummary, NativeInputKind, NativeIntegrity,
    NativeIntrinsic, NativeOperationInputEvidence, NoAccessorInstallRef, ProgramPremiseKey,
    ProofDependencies, ResolvedBindingValueInput, ResolvedNativeOperationInput,
    ResolvedNoAccessorInstall, SelectedBindingValueInput, SelectedNativeOperationInput,
    ValueInputEvidence, ValueInputRef,
};
use crate::execution_evidence::SourceLocation;
use crate::facts::abstract_domain::builtins::{
    self, ArgSel, IntrinsicOp, Invocation, Row, iteration_row,
};
use crate::facts::abstract_domain::integrity::{
    IntegrityFlags, IntrinsicIntact, IntrinsicSlot, PrototypeWrites,
};
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::at_init_fallback::{
    is_no_sync_callback_member_call, member_row_invokes_no_argument,
};
use crate::facts::callable_inference::{
    CallableInferenceInputs, FunctionBindingFacts, NamespaceWrapperCandidateFacts,
};
use crate::facts::collector::UnresolvedCallSourceCollector;
use crate::facts::global_object::{unshadowed_global_names, unshadowed_global_object_aliases};
use crate::facts::item_views::{TopLevelItemView, collect_declared_names, var_decl_of_item};
use crate::facts::lazy_boundary::visit_eager_member_parts;
use crate::facts::local_effect_targets::{MetadataDecorateCall, metadata_decorate_call};
use crate::facts::semantic_inference::{
    DescriptorCopyCallCandidate as SemanticDescriptorCopyCallCandidate, NativeBindingContext,
    NativeCallee, NativePremise, NativePremises, SemanticFact, SemanticFacts, SemanticKey,
    SemanticRules, native_premise,
};
use crate::facts::standard_globals::is_predefined_global;
use crate::facts::statement_facts::{
    CallbackFactoryCallArgumentWitness, CallbackFactoryWriteWitness,
};
use crate::facts::value_flow::ChunkValueFlow;
use crate::facts::{
    LocalClassConstructionProof, LocalClassConstructionShapes,
    OwnerInitializationCoverageRefusalReason, SetIterableShapes, StableArraySpreadShapes,
};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::reports::schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativePrototypeMutation,
    NativePrototypeMutationKind, NativeReceiverPath, NativeWriteSlot, RecordFactoryRejectionReason,
};
use analysis_model::PrimKinds;
use source_identity::SourceUnitOrdinal;
use spec::{BuiltinGlobalRoot, ReentryFreeOperation};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

fn map_entry_array_span(new_expr: &NewExpr) -> Option<u32> {
    if !matches!(new_expr.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym == "Map") {
        return None;
    }
    let [argument] = new_expr.args.as_deref().unwrap_or_default() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Array(entries) = argument.expr.unwrap_parens() else {
        return None;
    };
    for entry in &entries.elems {
        let Some(entry) = entry else {
            return None;
        };
        if entry.spread.is_some() {
            return None;
        }
        let Expr::Array(pair) = entry.expr.unwrap_parens() else {
            return None;
        };
        if pair.elems.len() != 2
            || pair.elems.iter().any(|element| {
                element
                    .as_ref()
                    .is_none_or(|element| element.spread.is_some())
            })
        {
            return None;
        }
    }
    Some(entries.span.lo.0)
}

fn from_entries_mapped_proof(
    call: &CallExpr,
    plain_array_bindings: &BTreeSet<String>,
) -> Option<FromEntriesMappedProof> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(from_entries) = callee.unwrap_parens() else {
        return None;
    };
    if !matches!(from_entries.obj.unwrap_parens(), Expr::Ident(object) if object.sym == "Object")
        || !matches!(&from_entries.prop, MemberProp::Ident(property) if property.sym == "fromEntries")
    {
        return None;
    }
    let [argument] = call.args.as_slice() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Call(map_call) = argument.expr.unwrap_parens() else {
        return None;
    };
    let Callee::Expr(map_callee) = &map_call.callee else {
        return None;
    };
    let Expr::Member(map_member) = map_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&map_member.prop, MemberProp::Ident(method) if method.sym == "map")
        || map_call.args.len() != 1
        || map_call.args[0].spread.is_some()
    {
        return None;
    }
    let Expr::Ident(source) = map_member.obj.unwrap_parens() else {
        return None;
    };
    if !plain_array_bindings.contains(source.sym.as_ref()) {
        return None;
    }
    let Expr::Arrow(mapper) = map_call.args[0].expr.unwrap_parens() else {
        return None;
    };
    if mapper.is_async
        || mapper.params.len() != 1
        || mapper.type_params.is_some()
        || mapper.return_type.is_some()
    {
        return None;
    }
    let Pat::Ident(parameter) = &mapper.params[0] else {
        return None;
    };
    let ArrowFunctionBody::Expr(body) = mapper.body.as_ref() else {
        return None;
    };
    let Expr::Array(pair) = body.unwrap_parens() else {
        return None;
    };
    if pair.elems.len() != 2
        || pair.elems.iter().any(|element| {
            element
                .as_ref()
                .is_none_or(|element| element.spread.is_some())
        })
    {
        return None;
    }
    let [Some(key), Some(value)] = pair.elems.as_slice() else {
        return None;
    };
    let Expr::Member(key_member) = key.expr.unwrap_parens() else {
        return None;
    };
    let MemberProp::Ident(key_property) = &key_member.prop else {
        return None;
    };
    if !matches!(key_member.obj.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
        || !matches!(value.expr.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
    {
        return None;
    }
    Some(FromEntriesMappedProof {
        source: ValueExpr::Binding(source.to_id()),
        key: key_property.sym.to_string(),
        map_call_site: map_call.span.into(),
        object_intrinsic: match from_entries.obj.unwrap_parens() {
            Expr::Ident(object) => object.to_id(),
            _ => unreachable!("checked Object.fromEntries receiver"),
        },
    })
}

enum FreshValueStep {
    Allocate {
        binding: Id,
        constructor: Id,
    },
    Factory {
        binding: Id,
        callee: ValueExpr,
    },
    Literal {
        binding: Id,
        span: u32,
        own_fields: BTreeSet<String>,
    },
    Write {
        receiver: Id,
        property: String,
    },
}

enum FreshValue {
    Instance(u32),
    Factory,
    Literal(BTreeSet<String>),
}

fn fresh_literal_own_fields(object: &ObjectLit) -> Option<BTreeSet<String>> {
    let mut fields = BTreeSet::new();
    for property in &object.props {
        let (name, value) = match property {
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                    (object_data_property_name(&pair.key)?, pair.value.as_ref())
                }
                Prop::Shorthand(ident) => {
                    if ident.sym == "__proto__" || !fields.insert(ident.sym.to_string()) {
                        return None;
                    }
                    continue;
                }
                _ => return None,
            },
            PropOrSpread::Spread(_) => return None,
        };
        if name == "__proto__"
            || !matches!(value.unwrap_parens(), Expr::Ident(_)) && !value_without_user_code(value)
            || !fields.insert(name)
        {
            return None;
        }
    }
    Some(fields)
}

fn primitive_constant(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_)) => true,
        Expr::Unary(unary) if unary.op != UnaryOp::Delete => primitive_constant(&unary.arg),
        Expr::Bin(binary) if !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf) => {
            primitive_constant(&binary.left) && primitive_constant(&binary.right)
        }
        _ => false,
    }
}

fn primitive_literal(expression: &Expr) -> bool {
    matches!(
        expression.unwrap_parens(),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_))
    )
}

#[derive(Clone, Copy)]
enum LiteralRecordPrimitive {
    BigInt,
    Other,
}

fn literal_record_primitive(expression: &Expr) -> Option<LiteralRecordPrimitive> {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::BigInt(_)) => Some(LiteralRecordPrimitive::BigInt),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_)) => {
            Some(LiteralRecordPrimitive::Other)
        }
        Expr::Unary(unary) => {
            let operand = literal_record_primitive(&unary.arg)?;
            match unary.op {
                UnaryOp::Delete => None,
                UnaryOp::Plus if matches!(operand, LiteralRecordPrimitive::BigInt) => None,
                UnaryOp::Minus | UnaryOp::Tilde => Some(operand),
                UnaryOp::Bang | UnaryOp::Void | UnaryOp::TypeOf | UnaryOp::Plus => {
                    Some(LiteralRecordPrimitive::Other)
                }
            }
        }
        _ => None,
    }
}

/// Syntax of a direct annotated record-factory call's single argument. Binding
/// reads, member reads and template substitutions are only candidates here:
/// the interpreter proves their values at the call before consulting the
/// author premise.
pub(super) type RecordFactoryArgument =
    Result<RecordFactoryRecord, (RecordFactoryRejectionReason, Option<String>)>;

#[derive(Debug, Clone, Default)]
pub(super) struct RecordFactoryRecord {
    /// Template substitutions, by outermost record property, whose `ToString`
    /// must see a proven primitive.
    pub(super) template_substitutions: Vec<(String, ValueExpr)>,
}

fn record_factory_argument(arguments: &[ExprOrSpread]) -> RecordFactoryArgument {
    let [argument] = arguments else {
        return Err((RecordFactoryRejectionReason::NotObjectLiteral, None));
    };
    if argument.spread.is_some() || !matches!(argument.expr.unwrap_parens(), Expr::Object(_)) {
        return Err((RecordFactoryRejectionReason::NotObjectLiteral, None));
    }
    let mut record = RecordFactoryRecord::default();
    record_factory_value(&argument.expr, None, &mut record)?;
    Ok(record)
}

fn record_factory_value(
    expression: &Expr,
    property: Option<&str>,
    record: &mut RecordFactoryRecord,
) -> Result<(), (RecordFactoryRejectionReason, Option<String>)> {
    let reject = |reason, name: Option<&str>| Err((reason, property.or(name).map(str::to_owned)));
    let expression = expression.unwrap_parens();
    if literal_record_primitive(expression).is_some() {
        return Ok(());
    }
    match expression {
        Expr::Ident(_) => Ok(()),
        Expr::Object(object) => {
            let mut names = BTreeSet::new();
            for entry in &object.props {
                let PropOrSpread::Prop(entry) = entry else {
                    return reject(RecordFactoryRejectionReason::Spread, None);
                };
                let (key, value) = match entry.as_ref() {
                    Prop::KeyValue(pair) => (&pair.key, Some(pair.value.as_ref())),
                    // Defining a method allocates a function; no code runs.
                    Prop::Method(method) => (&method.key, None),
                    Prop::Shorthand(binding) => {
                        let name = binding.sym.to_string();
                        if name == "__proto__" {
                            return reject(RecordFactoryRejectionReason::PrototypeKey, None);
                        }
                        if !names.insert(name.clone()) {
                            return reject(RecordFactoryRejectionReason::DuplicateKey, Some(&name));
                        }
                        continue;
                    }
                    Prop::Getter(_) | Prop::Setter(_) => {
                        return reject(RecordFactoryRejectionReason::Accessor, None);
                    }
                    Prop::Assign(_) => {
                        return reject(RecordFactoryRejectionReason::UnsupportedValue, None);
                    }
                };
                if matches!(key, PropName::Computed(_)) {
                    return reject(RecordFactoryRejectionReason::ComputedKey, None);
                }
                let Some(name) = object_data_property_name(key) else {
                    let reason = if static_property_name(key).is_some() {
                        RecordFactoryRejectionReason::PrototypeKey
                    } else {
                        RecordFactoryRejectionReason::ComputedKey
                    };
                    return reject(reason, None);
                };
                if !names.insert(name.clone()) {
                    return reject(RecordFactoryRejectionReason::DuplicateKey, Some(&name));
                }
                if let Some(value) = value {
                    record_factory_value(value, Some(property.unwrap_or(&name)), record)?;
                }
            }
            Ok(())
        }
        Expr::Array(array) => {
            for element in &array.elems {
                match element {
                    Some(element) if element.spread.is_none() => {
                        record_factory_value(&element.expr, property, record)?;
                    }
                    _ => return reject(RecordFactoryRejectionReason::ArrayShape, None),
                }
            }
            Ok(())
        }
        Expr::Arrow(_) => Ok(()),
        Expr::Fn(function)
            if function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|parameter| parameter.decorators.is_empty()) =>
        {
            Ok(())
        }
        Expr::Member(member)
            if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                && static_prop(&member.prop).is_some() =>
        {
            Ok(())
        }
        Expr::Member(_) => reject(RecordFactoryRejectionReason::UnprovenMemberRead, None),
        Expr::Tpl(template) => {
            for substitution in &template.exprs {
                let substitution = substitution.unwrap_parens();
                if literal_record_primitive(substitution).is_some() {
                    continue;
                }
                let candidate = matches!(substitution, Expr::Ident(_))
                    || matches!(substitution, Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                            && static_prop(&member.prop).is_some());
                if !candidate {
                    return reject(RecordFactoryRejectionReason::TemplateSubstitution, None);
                }
                record.template_substitutions.push((
                    property.unwrap_or_default().to_owned(),
                    ValueExpr::from_expr(substitution),
                ));
            }
            Ok(())
        }
        _ => reject(RecordFactoryRejectionReason::UnsupportedValue, None),
    }
}

fn value_without_user_code(expression: &Expr) -> bool {
    if primitive_constant(expression) {
        return true;
    }
    match expression.unwrap_parens() {
        Expr::Arrow(_) => true,
        Expr::Fn(function) => {
            function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|parameter| parameter.decorators.is_empty())
        }
        Expr::Array(array) => array.elems.is_empty(),
        // A11 fixes Array.prototype.values as an ordinary data property.
        // The empty receiver cannot publish the instance being initialized.
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(property) if property.sym == "values")
                && matches!(member.obj.unwrap_parens(), Expr::Array(array) if array.elems.is_empty())
        }
        _ => false,
    }
}

fn fresh_value_step(item: &ModuleItem) -> Option<FreshValueStep> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))
            if declaration.kind == VarDeclKind::Const && declaration.decls.len() == 1 =>
        {
            let declaration = &declaration.decls[0];
            let Pat::Ident(binding) = &declaration.name else {
                return None;
            };
            let initializer = declaration.init.as_deref()?.unwrap_parens();
            if let Expr::Object(object) = initializer {
                return fresh_literal_own_fields(object).map(|own_fields| {
                    FreshValueStep::Literal {
                        binding: binding.id.to_id(),
                        span: object.span.lo.0,
                        own_fields,
                    }
                });
            }
            if let Expr::Call(call) = initializer {
                let [argument] = call.args.as_slice() else {
                    return None;
                };
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                match callee.unwrap_parens() {
                    Expr::Ident(_) => {}
                    Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                            && matches!(member.prop, MemberProp::Ident(_)) => {}
                    _ => return None,
                }
                return (argument.spread.is_none()
                    && matches!(argument.expr.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                    && value_without_user_code(&argument.expr))
                .then(|| FreshValueStep::Factory {
                    binding: binding.id.to_id(),
                    callee: ValueExpr::from_expr(callee),
                });
            }
            let Expr::New(allocation) = initializer else {
                return None;
            };
            let Expr::Ident(constructor) = allocation.callee.unwrap_parens() else {
                return None;
            };
            allocation
                .args
                .as_ref()
                .is_none_or(Vec::is_empty)
                .then(|| FreshValueStep::Allocate {
                    binding: binding.id.to_id(),
                    constructor: constructor.to_id(),
                })
        }
        ModuleItem::Stmt(Stmt::Expr(statement)) => {
            let Expr::Assign(assignment) = statement.expr.unwrap_parens() else {
                return None;
            };
            if assignment.op != AssignOp::Assign || !value_without_user_code(&assignment.right) {
                return None;
            }
            let Some(binding_targets::SimpleWriteTarget::Member(member)) =
                binding_targets::simple_write_target(&assignment.left)
            else {
                return None;
            };
            let Expr::Ident(receiver) = binding_targets::transparent_target_expr(&member.obj)
            else {
                return None;
            };
            let MemberProp::Ident(property) = &member.prop else {
                return None;
            };
            (property.sym != "__proto__").then(|| FreshValueStep::Write {
                receiver: receiver.to_id(),
                property: property.sym.to_string(),
            })
        }
        _ => None,
    }
}

fn fresh_value_writes(
    steps: &[Option<FreshValueStep>],
    facts: &NamespaceValueFacts,
    factory_imports: &BTreeSet<Id>,
    factory_members: &BTreeMap<Id, BTreeSet<String>>,
) -> (BTreeSet<SourceUnitOrdinal>, BTreeSet<SourceUnitOrdinal>) {
    let mut active: Option<(Id, FreshValue)> = None;
    let mut writes = BTreeSet::new();
    let mut instance_field_writes = BTreeSet::new();
    for (ordinal, step) in steps.iter().enumerate() {
        match step {
            Some(FreshValueStep::Allocate {
                binding,
                constructor,
            }) => {
                active = facts
                    .resolve_class(
                        &ValueExpr::Binding(constructor.clone()),
                        &mut BTreeSet::new(),
                    )
                    .filter(|(key, surface)| {
                        *surface == ClassSurface::Constructor
                            && facts.class_allocation_without_user_code(*key)
                    })
                    .map(|(key, _)| (binding.clone(), FreshValue::Instance(key)));
            }
            Some(FreshValueStep::Factory { binding, callee }) => {
                let is_factory = match callee {
                    ValueExpr::Binding(callee) => factory_imports.contains(callee),
                    ValueExpr::Member(object, Some(property), MemberKey::Static, _) => {
                        matches!(object.as_ref(), ValueExpr::Binding(namespace)
                            if factory_members.get(namespace)
                                .is_some_and(|members| members.contains(property)))
                    }
                    _ => false,
                };
                active = is_factory.then(|| (binding.clone(), FreshValue::Factory));
            }
            Some(FreshValueStep::Literal {
                binding,
                span: _,
                own_fields,
            }) => {
                active = Some((binding.clone(), FreshValue::Literal(own_fields.clone())));
            }
            Some(FreshValueStep::Write { receiver, property })
                if active.as_ref().is_some_and(|(binding, value)| {
                    binding == receiver
                        && match value {
                            FreshValue::Instance(key) => facts.has_instance_field(*key, property),
                            FreshValue::Factory => property != "prototype",
                            FreshValue::Literal(own_fields) => own_fields.contains(property),
                        }
                }) =>
            {
                writes.insert(SourceUnitOrdinal(ordinal));
                if active
                    .as_ref()
                    .is_some_and(|(_, value)| matches!(value, FreshValue::Instance(_)))
                {
                    // A constructorless local class defined this own data
                    // field before the adjacent write. An inherited setter
                    // cannot observe the assignment.
                    instance_field_writes.insert(SourceUnitOrdinal(ordinal));
                }
            }
            _ => active = None,
        }
    }
    (writes, instance_field_writes)
}

fn add_class_field(facts: &mut ClassValueFacts, is_static: bool, name: String, value: ValueExpr) {
    let fields = if is_static {
        &mut facts.static_fields
    } else {
        &mut facts.instance_fields
    };
    if fields.insert(name.clone(), value).is_some() {
        fields.insert(name, ValueExpr::Unknown);
    }
}

fn collect_class_value_facts(class: &Class) -> ClassValueFacts {
    let mut heritage_inputs = UnresolvedCallSourceCollector::default();
    if let Some(heritage) = class.super_class.as_deref() {
        heritage.visit_with(&mut heritage_inputs);
    }
    let mut facts = ClassValueFacts {
        super_class: class.super_class.as_deref().map(ValueExpr::from_expr),
        super_class_inputs: heritage_inputs.idents,
        super_class_inputs_incomplete: heritage_inputs.inline_fn,
        discarded_heritage_call: class
            .super_class
            .as_deref()
            .and_then(DiscardedHeritageCall::collect),
        lexical_heritage: class
            .super_class
            .as_deref()
            .and_then(LexicalClassHeritage::collect),
        allocation_without_user_code: class.super_class.is_none() && class.decorators.is_empty(),
        default_has_instance: class.super_class.is_none() && class.decorators.is_empty(),
        ..ClassValueFacts::default()
    };
    if class.super_class.is_some() {
        facts.opaque_instance = true;
    }
    if !class.decorators.is_empty() {
        facts.opaque_instance = true;
        facts.opaque_static = true;
    }
    let mut accessor_names = BTreeSet::new();
    let mut dynamic_instance_keys = !class.decorators.is_empty();
    for member in &class.body {
        facts.default_has_instance &= match member {
            ClassMember::Method(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::PrivateMethod(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::ClassProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::PrivateProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::Constructor(_) | ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::Method(method) if !method.is_static => {
                if let Some(name) = static_property_name(&method.key) {
                    if method.kind == MethodKind::Method && method.function.decorators.is_empty() {
                        facts.ordinary_instance_methods.insert(name);
                    } else {
                        accessor_names.insert(name);
                    }
                } else {
                    dynamic_instance_keys = true;
                }
            }
            ClassMember::ClassProp(property) if !property.is_static => {
                dynamic_instance_keys |= static_property_name(&property.key).is_none()
                    || !property.decorators.is_empty();
            }
            ClassMember::AutoAccessor(_) => dynamic_instance_keys = true,
            _ => {}
        }
        facts.allocation_without_user_code &= match member {
            ClassMember::ClassProp(property) => {
                !property.is_static
                    && property.decorators.is_empty()
                    && static_property_name(&property.key).is_some_and(|name| name != "__proto__")
                    && property
                        .value
                        .as_deref()
                        .is_none_or(value_without_user_code)
            }
            ClassMember::Method(method) => {
                !method.is_static
                    && matches!(method.kind, MethodKind::Method)
                    && method.function.decorators.is_empty()
                    && method
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty())
            }
            ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::ClassProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                let Some(name) = static_property_name(&prop.key) else {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                };
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    name,
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::PrivateProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    format!("#{}", prop.key.name),
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::Method(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::PrivateMethod(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::Constructor(_) => facts.opaque_instance = true,
            ClassMember::AutoAccessor(accessor) => {
                if accessor.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::StaticBlock(_) => facts.opaque_static = true,
            ClassMember::TsIndexSignature(_) => {
                facts.opaque_instance = true;
                facts.opaque_static = true;
            }
            _ => {}
        }
    }
    if dynamic_instance_keys {
        facts.ordinary_instance_methods.clear();
    } else {
        facts
            .ordinary_instance_methods
            .retain(|name| !accessor_names.contains(name));
    }
    facts
}

fn collect_pattern_assignments(
    pattern: &Pat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match pattern {
        Pat::Ident(binding) => assignments.push((binding.id.to_id(), value.clone())),
        Pat::Assign(assign) => {
            let value = ValueExpr::Alternatives(
                [value.clone(), ValueExpr::from_expr(&assign.right)].into(),
            );
            collect_pattern_assignments(&assign.left, &value, assignments);
        }
        Pat::Rest(rest) => {
            collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
        }
        Pat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key).map_or(
                            ValueExpr::Unknown,
                            |name| {
                                ValueExpr::Member(
                                    Arc::new(value.clone()),
                                    Some(name),
                                    MemberKey::Computed(None),
                                    swc_common::DUMMY_SP.into(),
                                )
                            },
                        );
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Arc::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            MemberKey::Computed(None),
                            swc_common::DUMMY_SP.into(),
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(
                                        [projected, ValueExpr::from_expr(default)].into(),
                                    )
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        // Array destructuring invokes the iterator protocol. Retain its bound
        // names but do not infer element identity from a syntax-only array
        // shape; unknown iteration results must reach later publication sinks.
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn collect_assign_target_assignments(
    target: &AssignTargetPat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match target {
        AssignTargetPat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key).map_or(
                            ValueExpr::Unknown,
                            |name| {
                                ValueExpr::Member(
                                    Arc::new(value.clone()),
                                    Some(name),
                                    MemberKey::Computed(None),
                                    swc_common::DUMMY_SP.into(),
                                )
                            },
                        );
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Arc::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            MemberKey::Computed(None),
                            swc_common::DUMMY_SP.into(),
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(
                                        [projected, ValueExpr::from_expr(default)].into(),
                                    )
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        AssignTargetPat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        AssignTargetPat::Invalid(_) => {}
    }
}

fn infer_mutation_receiver_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    aliases: &BTreeSet<Id>,
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    let rules = MutationReceiverAliasRules {
        namespace,
        assignments,
        aliases,
        facts,
    };
    IterativeInference::new(rules)
        .solve(0..assignments.len())
        .into_values()
        .into_keys()
        .collect()
}

struct MutationReceiverAliasRules<'a> {
    namespace: &'a Id,
    assignments: &'a [&'a (Id, ValueExpr)],
    aliases: &'a BTreeSet<Id>,
    facts: &'a NamespaceValueFacts,
}

impl IterativeRules for MutationReceiverAliasRules<'_> {
    type Key = Id;
    type Fact = bool;
    type Rule = usize;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (target, value) = self.assignments[*rule];
        let receiver = if target != self.namespace {
            let mut is_receiver_alias = |binding: &Id| context.read(binding.clone()).is_some();
            value.may_be_namespace_mutation_receiver_with_alias_query(
                self.namespace,
                self.aliases,
                &mut is_receiver_alias,
                self.facts,
            )
        } else {
            false
        };
        if receiver {
            context.propose(target.clone(), true);
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let changed = !*current && contribution;
        *current |= contribution;
        changed
    }
}

fn has_definite_primitive_result(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Regex(_)) => false,
        Expr::Lit(_) | Expr::Tpl(_) | Expr::Unary(_) | Expr::Update(_) => true,
        Expr::Bin(binary) => !matches!(
            binary.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ),
        _ => false,
    }
}

/// Admit only arguments whose evaluation cannot itself trigger caller code.
/// The provider must separately be audited for every admitted argument shape.
/// The call result stays opaque and the ordinary visitor records the effects.
fn no_reentry_primitive_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(literal) => !matches!(literal, Lit::Regex(_)),
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Void | UnaryOp::Bang) => {
            matches!(unary.arg.unwrap_parens(), Expr::Lit(literal) if !matches!(literal, Lit::Regex(_)))
        }
        Expr::Tpl(template) => template.exprs.is_empty(),
        _ => false,
    }
}

fn no_reentry_inert_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Object(object) => object.props.iter().all(|property| {
            let PropOrSpread::Prop(property) = property else {
                return false;
            };
            let Prop::KeyValue(pair) = property.as_ref() else {
                return false;
            };
            let static_key = match &pair.key {
                PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
                PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
                PropName::Num(_) | PropName::BigInt(_) => true,
                PropName::Computed(_) => false,
            };
            static_key && no_reentry_inert_argument(&pair.value)
        }),
        _ => no_reentry_primitive_argument(expression),
    }
}

#[derive(Debug, Clone)]
pub(super) struct Parameter {
    pub(super) binding: Option<Id>,
    pub(super) bindings: BTreeSet<Id>,
    pub(super) default: Option<ValueExpr>,
    /// A pattern parameter's bindings as projections of its incoming value,
    /// bound to `pattern_source()`, and whether some binding receives a part
    /// of it only as unknown.
    pub(super) pattern_assignments: Vec<(Id, ValueExpr)>,
    pub(super) pattern_loses_values: bool,
    /// The pattern destructures an array, so binding it runs the iterator
    /// protocol on the incoming value (or on a part of it).
    pub(super) pattern_iterates: bool,
    /// Some array pattern below the root iterates a part of the incoming
    /// value, or a default, which no source tracks.
    pub(super) pattern_iterates_part: bool,
    /// Defaults below the root that supply a value an array pattern
    /// iterates.
    pub(super) iterated_defaults: Vec<ValueExpr>,
}

/// The binding a pattern parameter's projections read its incoming value
/// from. No source identifier can spell it.
pub(super) fn pattern_source() -> Id {
    ("\u{0}pattern_source".into(), Default::default())
}

/// Whether destructuring `pattern` gives some binding a part of the value
/// only as unknown: an array element, a rest, or a computed key.
fn pattern_loses_values(pattern: &Pat) -> bool {
    match pattern {
        Pat::Ident(_) => false,
        Pat::Assign(assign) => pattern_loses_values(&assign.left),
        Pat::Object(object) => object.props.iter().any(|property| match property {
            ObjectPatProp::KeyValue(property) => {
                static_property_name(&property.key).is_none()
                    || pattern_loses_values(&property.value)
            }
            ObjectPatProp::Assign(_) => false,
            ObjectPatProp::Rest(_) => true,
        }),
        Pat::Array(_) | Pat::Rest(_) | Pat::Expr(_) | Pat::Invalid(_) => true,
    }
}

/// Whether destructuring `pattern` runs the iterator protocol on the value
/// or on a part of it.
fn pattern_iterates(pattern: &Pat) -> bool {
    match pattern {
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => false,
        Pat::Assign(assign) => pattern_iterates(&assign.left),
        Pat::Rest(rest) => pattern_iterates(&rest.arg),
        Pat::Object(object) => object.props.iter().any(|property| match property {
            ObjectPatProp::KeyValue(property) => pattern_iterates(&property.value),
            ObjectPatProp::Assign(_) => false,
            ObjectPatProp::Rest(rest) => pattern_iterates(&rest.arg),
        }),
        Pat::Array(_) => true,
    }
}

/// Whether destructuring `pattern` iterates a part of the value, or a
/// default, rather than the value itself: an array pattern below the root.
fn pattern_iterates_part(pattern: &Pat) -> bool {
    match pattern {
        Pat::Array(array) => array.elems.iter().flatten().any(pattern_iterates),
        Pat::Assign(assign) => pattern_iterates_part(&assign.left),
        Pat::Object(_) | Pat::Rest(_) => pattern_iterates(pattern),
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => false,
    }
}

/// The defaults below the root of `pattern` that supply a value an array
/// pattern iterates: `[[x] = mk()]`, `{ a: [x] = mk() }`.
fn iterated_defaults<'a>(pattern: &'a Pat, defaults: &mut Vec<&'a Expr>) {
    match pattern {
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_iterated_default(element, defaults);
            }
        }
        Pat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        collect_iterated_default(&property.value, defaults)
                    }
                    ObjectPatProp::Rest(rest) => collect_iterated_default(&rest.arg, defaults),
                    ObjectPatProp::Assign(_) => {}
                }
            }
        }
        Pat::Assign(assign) => iterated_defaults(&assign.left, defaults),
        Pat::Rest(rest) => iterated_defaults(&rest.arg, defaults),
        Pat::Ident(_) | Pat::Expr(_) | Pat::Invalid(_) => {}
    }
}

fn collect_iterated_default<'a>(pattern: &'a Pat, defaults: &mut Vec<&'a Expr>) {
    if let Pat::Assign(assign) = pattern
        && pattern_iterates(&assign.left)
    {
        defaults.push(&assign.right);
    }
    iterated_defaults(pattern, defaults);
}

impl Parameter {
    fn from_pattern(pattern: &Pat) -> Self {
        let mut assignments = Vec::new();
        collect_pattern_assignments(pattern, &ValueExpr::Unknown, &mut assignments);
        let bindings = assignments
            .into_iter()
            .map(|(binding, _)| binding)
            .collect();
        let (binding, default, destructured) = match pattern {
            Pat::Ident(binding) => (Some(binding.id.to_id()), None, None),
            Pat::Assign(assign) => (
                match assign.left.as_ref() {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                },
                Some(ValueExpr::from_expr(&assign.right)),
                Some(assign.left.as_ref()),
            ),
            _ => (None, None, Some(pattern)),
        };
        let mut pattern_assignments = Vec::new();
        let pattern_iterates = destructured.is_some_and(pattern_iterates);
        let pattern_iterates_part = destructured.is_some_and(pattern_iterates_part);
        let mut defaults = Vec::new();
        if let Some(destructured) = destructured {
            iterated_defaults(destructured, &mut defaults);
        }
        let iterated_defaults = defaults.into_iter().map(ValueExpr::from_expr).collect();
        let pattern_loses_values = match destructured.filter(|_| binding.is_none()) {
            Some(destructured) => {
                collect_pattern_assignments(
                    destructured,
                    &ValueExpr::Binding(pattern_source()),
                    &mut pattern_assignments,
                );
                pattern_loses_values(destructured)
            }
            None => false,
        };
        Self {
            binding,
            bindings,
            default,
            pattern_assignments,
            pattern_loses_values,
            pattern_iterates,
            pattern_iterates_part,
            iterated_defaults,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct Call {
    pub(super) kind: CallKind,
    pub(super) callee: ValueExpr,
    pub(super) args: Vec<ValueExpr>,
    pub(super) span: Span,
    pub(super) caller: Option<u32>,
    pub(super) owner: SourceUnitOrdinal,
    pub(super) plain_object_sources: bool,
    pub(super) no_sync_arguments: bool,
    pub(super) no_reentry_candidate: bool,
    pub(super) no_reentry_record_candidate: bool,
    pub(super) has_spread: bool,
}

impl Call {
    pub(super) fn member_read(
        owner: SourceUnitOrdinal,
        caller: Option<u32>,
        callee: ValueExpr,
        span: Span,
    ) -> Self {
        Self {
            kind: CallKind::LocalMemberRead,
            callee,
            args: Vec::new(),
            span,
            caller,
            owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct RetainedCallbackCall {
    pub(super) namespace_member: Option<(Id, String)>,
    pub(super) signatures: Vec<spec::RetainedCallbackSignature>,
    pub(super) argument_count: usize,
    pub(super) callback_argument_exprs: BTreeMap<usize, ValueExpr>,
    pub(super) require_single_callback_identity: bool,
}

#[derive(Debug, Clone)]
pub(super) struct IndexedRetainedCallbackCall {
    pub(super) call: RetainedCallbackCall,
    pub(super) owner: SourceUnitOrdinal,
    pub(super) caller: Option<u32>,
}

#[derive(Debug, Clone)]
pub(super) struct DescriptorCopyCallCandidate {
    pub(super) source_object: u32,
    pub(super) signature: spec::DescriptorCopyFactorySignature,
    pub(super) provider_binding: Id,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum CallKind {
    Call,
    New,
    ImplicitCoercion,
    ArraySpread,
    ObjectSpread,
    LocalMemberRead,
    AccessorCalleeLookup,
    OpaqueExecution,
    InProtocol,
    InstanceOfProtocol,
    DeepDestructuringProtocol,
}

/// The builtin row a direct call or construction names, with its callee
/// spelling. Identity and slot integrity are proved separately.
pub(super) fn native_call_identity_key(
    kind: CallKind,
    callee: &ValueExpr,
) -> Option<(&'static Row, NativeCallee)> {
    let invocation = match kind {
        CallKind::Call => Invocation::Call,
        CallKind::New => Invocation::Construct,
        _ => return None,
    };
    let (binding, receiver, member, global_this_qualified) = match callee {
        ValueExpr::Binding(binding) => (binding, binding.0.as_ref(), None, false),
        ValueExpr::Member(receiver, Some(member), MemberKey::Static, _) => {
            match receiver.as_ref() {
                ValueExpr::Binding(binding) if binding.0.as_ref() == "globalThis" => {
                    (binding, member.as_str(), None, true)
                }
                ValueExpr::Binding(binding) => {
                    (binding, binding.0.as_ref(), Some(member.as_str()), false)
                }
                ValueExpr::Member(global, Some(receiver), MemberKey::Static, _) => {
                    let ValueExpr::Binding(binding) = global.as_ref() else {
                        return None;
                    };
                    if binding.0.as_ref() != "globalThis" {
                        return None;
                    }
                    (binding, receiver.as_str(), Some(member.as_str()), true)
                }
                _ => return None,
            }
        }
        _ => return None,
    };
    // A callee outside the census has no identity premise to prove.
    let row =
        builtins::lookup(receiver, member, invocation).filter(|row| row.family().is_some())?;
    Some((
        row,
        NativeCallee {
            binding: binding.clone(),
            global_this_qualified,
        },
    ))
}

/// The selected operation must name the exact original global and, for the
/// registry operation, its direct data property. The global shadowing check
/// remains with the caller's canonical value-flow facts.
fn selected_symbol_binding(callee: &ValueExpr, operation: NativeInputKind) -> Option<&Id> {
    match (operation, callee) {
        (
            NativeInputKind::Operation(spec::NativeOperationKind::Symbol),
            ValueExpr::Binding(binding),
        ) => Some(binding),
        (
            NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor),
            ValueExpr::Member(receiver, Some(property), MemberKey::Static, _),
        ) if property == "for" => {
            let ValueExpr::Binding(binding) = receiver.as_ref() else {
                return None;
            };
            Some(binding)
        }
        _ => None,
    }
}

fn native_operation_call_name(operation: NativeInputKind) -> &'static str {
    match operation {
        NativeInputKind::Operation(spec::NativeOperationKind::TextEncoderEmpty) => {
            "direct zero-argument new TextEncoder() construction"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty) => {
            "direct zero-argument new Map() construction"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::WeakMapEmpty) => {
            "direct zero-argument new WeakMap() construction"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::SetEmpty) => {
            "direct zero-argument new Set() construction"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => {
            "direct Symbol(...) call (unsupported Symbol call shape)"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor) => {
            "direct Symbol.for(...) call (unsupported Symbol call shape)"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve) => {
            "direct Promise.resolve() call (unsupported Promise.resolve call shape)"
        }
        _ => "supported native operation",
    }
}

fn native_operation_mutation_name(operation: NativeInputKind) -> &'static str {
    match operation {
        NativeInputKind::Operation(
            spec::NativeOperationKind::Symbol | spec::NativeOperationKind::SymbolFor,
        ) => "global Symbol replacement or accessor installation",
        NativeInputKind::Operation(spec::NativeOperationKind::TextEncoderEmpty) => {
            "global TextEncoder replacement or accessor installation"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty) => {
            "global Map replacement or accessor installation"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::WeakMapEmpty) => {
            "global WeakMap replacement or accessor installation"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::SetEmpty) => {
            "global Set replacement or accessor installation"
        }
        NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve) => {
            "global Promise replacement or Promise.resolve overwrite, accessor installation, or deletion"
        }
        _ => "native operation mutation",
    }
}

fn shallow_data_pattern(pattern: &Pat) -> bool {
    fn leaf(pattern: &Pat) -> bool {
        matches!(pattern, Pat::Ident(_))
            || matches!(pattern, Pat::Assign(assign) if matches!(assign.left.as_ref(), Pat::Ident(_)))
    }
    match pattern {
        Pat::Object(object) => object.props.iter().all(|property| match property {
            ObjectPatProp::KeyValue(property) => leaf(&property.value),
            ObjectPatProp::Assign(_) => true,
            ObjectPatProp::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
        }),
        Pat::Array(array) => array.elems.iter().flatten().all(|element| match element {
            Pat::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
            other => leaf(other),
        }),
        _ => false,
    }
}

fn data_read_operand(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(_)
        | Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::BigInt(_) | Lit::Bool(_) | Lit::Null(_)) => {
            true
        }
        Expr::Member(member) if matches!(member.prop, MemberProp::Ident(_)) => {
            data_read_operand(&member.obj)
        }
        _ => false,
    }
}

fn plain_assign_source_property(property: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(property) = property else {
        return false;
    };
    let name = match property.as_ref() {
        Prop::KeyValue(pair) => &pair.key,
        Prop::Shorthand(ident) => return ident.sym.as_ref() != "__proto__",
        _ => return false,
    };
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
        PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
        PropName::Num(_) | PropName::BigInt(_) => true,
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => string.value.to_string_lossy() != "__proto__",
            Expr::Lit(Lit::Num(_)) | Expr::Lit(Lit::BigInt(_)) => true,
            _ => false,
        },
    }
}

#[derive(Debug, Clone)]
pub(super) struct CallbackResultWrite {
    pub(super) receiver: ValueExpr,
    pub(super) property_key: ValueExpr,
    pub(super) simple_assignment: bool,
    pub(super) data_read_operands: bool,
    pub(super) property: Option<String>,
    pub(super) value: ValueExpr,
    pub(super) site: CallSite,
}

#[derive(Debug, Clone)]
pub(super) struct MemberValueWrite {
    pub(super) receiver: Id,
    pub(super) property: Option<String>,
    pub(super) value: ValueExpr,
    pub(super) site: CallSite,
    pub(super) owner: SourceUnitOrdinal,
}

#[derive(Debug, Clone)]
pub(super) struct Callable {
    pub(super) owner: SourceUnitOrdinal,
    created_after_parent_await: bool,
    pub(super) params: Vec<Parameter>,
    pub(super) reads: BTreeSet<Id>,
    bare_reads: BTreeSet<Id>,
    pub(super) rebinds: BTreeSet<Id>,
    pub(super) calls: Vec<Call>,
    pub(super) local_assignments: Vec<(Id, ValueExpr)>,
    pub(super) declared_locals: BTreeSet<Id>,
    /// Locals a read can observe before any assignment, as `undefined`: a
    /// declarator without an initializer outside a `for`-`in`/`of` head, and
    /// every `var` (hoisted). `local_assignments` never contributes that value.
    pub(super) undefined_before_assignment: BTreeSet<Id>,
    /// Bindings a `for`-`in`/`of` head of this callable writes. No
    /// `local_assignments` entry records the elements a head binding takes.
    pub(super) head_bindings: BTreeSet<Id>,
    /// Values whose parts reach a binding only as unknown: `for`-`of`
    /// iterables, initializers of destructuring that loses values, thrown
    /// values (a `catch` parameter) and yielded values (a generator's
    /// consumer).
    pub(super) published_sources: Vec<ValueExpr>,
    /// Operands this callable runs the iterator protocol on: `for`-`of`
    /// iterables, array-destructured values, spread operands and `yield*`
    /// operands. Over anything but a proven native iterable, the protocol
    /// calls user code (`[Symbol.iterator]`, `next`).
    pub(super) iterated_sources: Vec<ValueExpr>,
    /// Bindings an update expression (`x++`) of this callable writes; neither
    /// `rebinds` nor `local_assignments` records them.
    pub(super) update_targets: BTreeSet<Id>,
    hoisted_vars: BTreeSet<Id>,
    lexical_declarations: BTreeMap<Id, LocalBinding>,
    pub(super) self_binding: Option<Id>,
    /// Catch parameters and destructured declarations are locals for closure
    /// projection. Keep them separate from `declared_locals`, whose consumers
    /// also include direct callback-factory and class-closure admission.
    pub(super) pattern_locals: BTreeSet<Id>,
    pub(super) body_facts: CallableBodyFacts,
    pub(super) local_functions: BTreeMap<Id, u32>,
    pub(super) literal_captures: BTreeSet<Id>,
    literal_exposures: BTreeSet<Id>,
    pub(super) escaped_bindings: BTreeSet<Id>,
    pub(super) namespace_escape_values: Vec<ValueExpr>,
    pub(super) member_writes: Vec<(Id, String)>,
    pub(super) member_value_writes: Vec<MemberValueWrite>,
    pub(super) callback_result_writes: Vec<CallbackResultWrite>,
    pub(super) dynamic_property_effect: bool,
    pub(super) dynamic_property_receivers: Vec<ValueExpr>,
    /// Receivers of member targets in this body's destructuring assignments.
    pub(super) destructuring_member_receivers: Vec<ValueExpr>,
    pub(super) implicit_capture: bool,
    pub(super) opaque: bool,
    pub(super) generator: bool,
    pub(super) may_fall_through: bool,
}

impl Callable {
    /// The bindings this callable's own scope declares, parameters included.
    pub(super) fn own_bindings(&self) -> BTreeSet<&Id> {
        self.declared_locals
            .iter()
            .chain(&self.pattern_locals)
            .chain(&self.hoisted_vars)
            .chain(self.lexical_declarations.keys())
            .chain(self.local_functions.keys())
            .chain(&self.self_binding)
            .chain(self.params.iter().flat_map(|parameter| &parameter.bindings))
            .collect()
    }

    /// Every binding this callable writes: assignments of any operator and
    /// destructuring targets (`rebinds`), loop heads and updates.
    pub(super) fn writes(&self) -> impl Iterator<Item = &Id> {
        self.rebinds
            .iter()
            .chain(&self.head_bindings)
            .chain(&self.update_targets)
    }

    /// Bindings an enclosing scope declares that this callable writes.
    pub(super) fn captured_writes(&self) -> BTreeSet<&Id> {
        let own = self.own_bindings();
        self.writes()
            .filter(|binding| !own.contains(binding))
            .collect()
    }

    /// Bindings an enclosing scope declares that this callable reads.
    pub(super) fn captured_reads(&self) -> BTreeSet<&Id> {
        let own = self.own_bindings();
        self.reads
            .iter()
            .filter(|binding| !own.contains(binding))
            .collect()
    }

    /// Locals whose value an earlier assignment of this callable reads
    /// although a later one assigns them: a loop, or a call between, can
    /// carry the later value to the earlier read. Returns the readers'
    /// targets and the locals they read.
    pub(super) fn reads_before_assignment(&self) -> (BTreeSet<&Id>, BTreeSet<Id>) {
        let mut readers = BTreeSet::new();
        let mut sources = BTreeSet::new();
        for (position, (target, value)) in self.local_assignments.iter().enumerate() {
            let mut read = BTreeSet::new();
            value.bindings(&mut read);
            for binding in read {
                if self.local_assignments[position..]
                    .iter()
                    .any(|(assigned, _)| *assigned == binding)
                {
                    readers.insert(target);
                    sources.insert(binding);
                }
            }
        }
        (readers, sources)
    }
}

#[derive(Clone, Default)]
pub(super) struct CallablePrefixEffects {
    pub(super) reads: BTreeSet<Id>,
    pub(super) rebinds: BTreeSet<Id>,
}

#[derive(Debug, Clone)]
pub(super) struct LocalConstructor {
    pub(super) key: Option<u32>,
    /// No constructor body or instance fields run during direct allocation.
    pub(super) inert_allocation: bool,
    /// Implicit constructor whose instance fields are checked to initialize
    /// without user code. Unlike inert allocation, the fields still run.
    pub(super) checked_default_fields: bool,
    /// Own data fields written by the checked constructor body.
    pub(super) own_fields: BTreeSet<String>,
    /// Parameter property reads must come from fresh plain data objects.
    pub(super) property_reads: BTreeSet<(usize, String)>,
    /// Unqualified reads must resolve to local bindings, not host getters.
    pub(super) binding_reads: BTreeSet<Id>,
}

fn safe_constructor_value(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<(usize, String)>,
    binding_reads: &mut BTreeSet<Id>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(id) => {
            if !params
                .iter()
                .any(|param| param.as_ref() == Some(&id.to_id()))
            {
                binding_reads.insert(id.to_id());
            }
            true
        }
        Expr::Lit(_) => true,
        Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
            safe_constructor_value(&unary.arg, params, property_reads, binding_reads)
        }
        Expr::Member(member) if matches!(&member.prop, MemberProp::Ident(_)) => {
            let Expr::Ident(id) = member.obj.unwrap_parens() else {
                return false;
            };
            let Some(index) = params
                .iter()
                .position(|param| param.as_ref() == Some(&id.to_id()))
            else {
                return false;
            };
            let MemberProp::Ident(property) = &member.prop else {
                unreachable!()
            };
            property_reads.insert((index, property.sym.to_string()));
            true
        }
        Expr::Bin(bin) if matches!(bin.op, BinaryOp::NullishCoalescing | BinaryOp::EqEqEq) => {
            safe_constructor_value(&bin.left, params, property_reads, binding_reads)
                && safe_constructor_value(&bin.right, params, property_reads, binding_reads)
        }
        _ => false,
    }
}

fn safe_constructor_assignment(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<(usize, String)>,
    binding_reads: &mut BTreeSet<Id>,
    written_properties: &mut BTreeSet<String>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Seq(seq) => seq.exprs.iter().all(|expr| {
            safe_constructor_assignment(
                expr,
                params,
                property_reads,
                binding_reads,
                written_properties,
            )
        }),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let Some(binding_targets::SimpleWriteTarget::Member(target)) =
                binding_targets::simple_write_target(&assign.left)
            else {
                return false;
            };
            let MemberProp::Ident(property) = &target.prop else {
                return false;
            };
            if !matches!(
                binding_targets::transparent_target_expr(&target.obj),
                Expr::This(_)
            ) || property.sym.as_ref() == "__proto__"
                || !safe_constructor_value(&assign.right, params, property_reads, binding_reads)
            {
                return false;
            }
            written_properties.insert(property.sym.to_string());
            true
        }
        _ => false,
    }
}

fn local_constructor(
    class: &Class,
    construction_proof: &LocalClassConstructionShapes,
) -> Option<LocalConstructor> {
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return None;
    }
    let checked_default_fields = construction_proof.contains_class(class.span.lo.0);
    let mut constructor = None;
    let mut has_instance_fields = false;
    for member in &class.body {
        match member {
            ClassMember::Constructor(ctor) if constructor.is_none() => constructor = Some(ctor),
            ClassMember::Method(method)
                if method.kind != MethodKind::Setter
                    && !matches!(method.key, PropName::Computed(_))
                    && method.function.decorators.is_empty() => {}
            ClassMember::PrivateMethod(method) if method.function.decorators.is_empty() => {}
            ClassMember::ClassProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && !matches!(prop.key, PropName::Computed(_))
                    && (checked_default_fields
                        || prop.value.as_deref().is_none_or(|value| {
                            matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                        })) =>
            {
                has_instance_fields = true
            }
            ClassMember::PrivateProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && (checked_default_fields
                        || prop.value.as_deref().is_none_or(|value| {
                            matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                        })) =>
            {
                has_instance_fields = true
            }
            ClassMember::Empty(_) => {}
            _ => return None,
        }
    }
    let Some(ctor) = constructor else {
        return Some(LocalConstructor {
            key: None,
            inert_allocation: !has_instance_fields,
            checked_default_fields,
            own_fields: BTreeSet::new(),
            property_reads: BTreeSet::new(),
            binding_reads: BTreeSet::new(),
        });
    };
    let params: Vec<_> = ctor
        .params
        .iter()
        .map(|param| match param {
            ParamOrTsParamProp::Param(param) if param.decorators.is_empty() => match &param.pat {
                Pat::Ident(binding) => Some(binding.id.to_id()),
                _ => None,
            },
            _ => None,
        })
        .collect();
    if params.iter().any(Option::is_none) {
        return None;
    }
    let mut property_reads = BTreeSet::new();
    let mut binding_reads = BTreeSet::new();
    let mut written_properties = BTreeSet::new();
    for statement in &ctor.body.as_ref()?.stmts {
        let Stmt::Expr(expr) = statement else {
            return None;
        };
        if !safe_constructor_assignment(
            &expr.expr,
            &params,
            &mut property_reads,
            &mut binding_reads,
            &mut written_properties,
        ) {
            return None;
        }
    }
    // A getter with the same name on this class's prototype makes assignment
    // to the fresh instance throw. All other accepted writes create ordinary
    // own data properties under the unmodified built-in prototype premise.
    if class.body.iter().any(|member| {
        matches!(member, ClassMember::Method(method)
            if !method.is_static && method.kind == MethodKind::Getter
                && static_property_name(&method.key)
                    .is_some_and(|name| written_properties.contains(&name)))
    }) {
        return None;
    }
    Some(LocalConstructor {
        key: Some(ctor.span.lo.0),
        inert_allocation: false,
        checked_default_fields: false,
        own_fields: written_properties,
        property_reads,
        binding_reads,
    })
}

impl Default for Callable {
    fn default() -> Self {
        Self {
            owner: SourceUnitOrdinal(0),
            created_after_parent_await: false,
            params: Vec::new(),
            reads: BTreeSet::new(),
            bare_reads: BTreeSet::new(),
            rebinds: BTreeSet::new(),
            calls: Vec::new(),
            local_assignments: Vec::new(),
            declared_locals: BTreeSet::new(),
            undefined_before_assignment: BTreeSet::new(),
            head_bindings: BTreeSet::new(),
            published_sources: Vec::new(),
            iterated_sources: Vec::new(),
            update_targets: BTreeSet::new(),
            hoisted_vars: BTreeSet::new(),
            lexical_declarations: BTreeMap::new(),
            self_binding: None,
            pattern_locals: BTreeSet::new(),
            body_facts: CallableBodyFacts::default(),
            local_functions: BTreeMap::new(),
            literal_captures: BTreeSet::new(),
            literal_exposures: BTreeSet::new(),
            escaped_bindings: BTreeSet::new(),
            namespace_escape_values: Vec::new(),
            member_writes: Vec::new(),
            member_value_writes: Vec::new(),
            callback_result_writes: Vec::new(),
            dynamic_property_effect: false,
            dynamic_property_receivers: Vec::new(),
            destructuring_member_receivers: Vec::new(),
            implicit_capture: false,
            opaque: false,
            generator: false,
            may_fall_through: true,
        }
    }
}

fn sole_return_expression(body: &FunctionBody) -> Option<&Expr> {
    match body.stmts.as_slice() {
        [Stmt::Return(ret)] => ret.arg.as_deref(),
        _ => None,
    }
}

type IntrinsicRoots = BTreeSet<NativeIntrinsic>;

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum NativeReceiverRoot {
    Intrinsic(NativeIntrinsic),
    IntrinsicPrototype(NativeIntrinsic),
    IntrinsicDescendant(NativeIntrinsic),
    ArrayIteratorPrototype,
    SetIteratorPrototype,
    OrdinaryDirect {
        reference: ValueInputRef,
        available_after: SourceUnitOrdinal,
    },
    OrdinaryDescendant {
        reference: ValueInputRef,
        available_after: SourceUnitOrdinal,
    },
    /// The value of a bundle-owned global property: never an intrinsic
    /// (`GlobalNames::bundle`). Its descendants stay unknown.
    BundleGlobal,
}

type NativeReceiverRoots = BTreeSet<NativeReceiverRoot>;

/// Global-object names the native census resolves.
#[derive(Debug, Clone, Default)]
pub(super) struct GlobalNames {
    /// Unshadowed bindings that evaluate to the global object.
    aliases: BTreeSet<&'static str>,
    /// Node's and Bun's `global` is one of `aliases` under a
    /// `predefined_globals` claim.
    node_global: bool,
    /// Bundle-owned globals may be inferred unless this is `unknown`.
    predefined: spec::PredefinedGlobals,
    bundle: BundleGlobals,
}

impl GlobalNames {
    fn contains(&self, name: &str) -> bool {
        self.aliases.contains(name)
    }

    /// The census root a global property of this name holds.
    fn binding_root(&self, name: &str) -> Option<NativeIntrinsic> {
        NativeIntrinsic::for_global_binding(name).or_else(|| {
            (self.node_global && name == "global").then_some(NativeIntrinsic::GlobalObject)
        })
    }

    /// Every property name `binding_root` resolves.
    fn binding_names(&self) -> impl Iterator<Item = &'static str> + '_ {
        NativeIntrinsic::GLOBAL_BINDINGS
            .iter()
            .copied()
            .chain(self.node_global.then_some("global"))
    }
}

/// Global data properties that hold only fresh ordinary objects created by
/// analyzed code (SPEC.md § Bundle-owned globals).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct BundleGlobals {
    names: BTreeSet<String>,
    /// Every property keyed by a `Symbol.for` symbol.
    registered_symbols: bool,
    /// Bindings whose key domain is a `Symbol.for` symbol.
    symbol_keys: BTreeSet<Id>,
    /// `new Map()` and its kin allocate fresh objects.
    fresh_collections: bool,
    /// Premises the set depends on.
    no_accessor_install_callables: BTreeSet<NoAccessorInstallRef>,
}

impl BundleGlobals {
    /// Whether reading this property of the global object yields a
    /// bundle-owned value.
    fn holds(&self, property: Option<&str>, key: &MemberKey) -> bool {
        match (property, key) {
            (Some(name), _) => self.names.contains(name),
            (None, MemberKey::Computed(Some(binding))) => {
                self.registered_symbols && self.symbol_keys.contains(binding)
            }
            (None, _) => false,
        }
    }
}

#[derive(Debug, Clone, Default)]
struct CallableReturnInputs {
    by_binding: BTreeMap<Id, NativeReceiverRoots>,
    by_origin: BTreeMap<u32, NativeReceiverRoots>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum ReceiverFlowKey {
    Assignment(usize),
    Binding(Id),
    BindingSeed(Id),
    ReturnSite(u32, usize),
    Callable(u32),
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum CallableReturnOutcome {
    Safe,
    Descendant(ValueInputRef, SourceUnitOrdinal),
    Intrinsic(NativeIntrinsic),
    Unknown,
}

#[derive(Debug, Clone, Default, Eq, PartialEq)]
struct ReceiverFlowFact {
    roots: NativeReceiverRoots,
    callable_targets: BTreeSet<u32>,
    callable_dependencies: BTreeSet<u32>,
    capture_dependencies: BTreeSet<(ValueInputRef, SourceUnitOrdinal)>,
    outcomes: BTreeSet<CallableReturnOutcome>,
    opaque: bool,
    safe: bool,
    complete: bool,
}

impl ReceiverFlowFact {
    fn merge(&mut self, contribution: Self) -> bool {
        let previous = self.clone();
        self.roots.extend(contribution.roots);
        self.callable_targets.extend(contribution.callable_targets);
        self.callable_dependencies
            .extend(contribution.callable_dependencies);
        self.capture_dependencies
            .extend(contribution.capture_dependencies);
        self.outcomes.extend(contribution.outcomes);
        self.opaque |= contribution.opaque;
        self.safe |= contribution.safe;
        self.complete |= contribution.complete;
        *self != previous
    }
}

#[derive(Debug, Clone, Default)]
struct ReceiverFlowValue {
    fact: ReceiverFlowFact,
    pending: bool,
}

#[derive(Debug, Clone)]
pub(super) struct BindingDeclarationWrite {
    owner: SourceUnitOrdinal,
    /// A simple `var` in a callable's scope. Such a binding is initialized to
    /// undefined on entry; this proves no kind or privacy of its stored value.
    private_var_scope: Option<u32>,
    binding_span: Span,
    initializer_span: Option<Span>,
    pub(super) initializer: Option<ValueExpr>,
    pub(super) direct_top_level: bool,
    pub(super) simple_identifier: bool,
    primitive_literal_initializer: bool,
    /// Retained from the canonical declaration visitor so native-input
    /// validation need not re-traverse the initializer AST.
    /// `Some` is the dense primitive Set array shape; identifiers in the
    /// array are certified against the completed declaration census below.
    set_from_primitive_array_aliases: Option<Vec<Id>>,
    /// Source binding of a direct `new Set([...source, primitive literals])`.
    /// The interval and output schedule are certified separately.
    set_from_set_spread_source: Option<(Id, Span, Span)>,
    /// Exact direct constructions in an object containing only ordinary,
    /// noncomputed data fields initialized by bare zero-argument `new Map`.
    empty_map_object_sites: Option<Vec<Span>>,
    /// Eager computed ordinary method keys of a direct plain class initializer.
    symbol_class_key_sites: Option<Vec<Span>>,
}

fn symbol_class_key_property(property: &str) -> Option<&'static str> {
    if !matches!(property, "asyncIterator" | "asyncDispose" | "dispose") {
        return None;
    }
    builtins::data_property("Symbol", property)
        .filter(|data| matches!(data.value, builtins::PropertyValue::Primitive(_)))
        .map(|data| data.name)
}

/// Eligibility under the primitive-read premise, not a proof that the guard,
/// call or assignment is silent or that the fill successfully installs data.
fn symbol_guarded_fill(statement: &IfStmt) -> Option<(Id, &'static str, Span)> {
    if statement.alt.is_some() {
        return None;
    }
    let Expr::Bin(test) = statement.test.as_ref() else {
        return None;
    };
    if !matches!(test.op, BinaryOp::NotEq | BinaryOp::NotEqEq) {
        return None;
    }
    let Expr::Unary(probe) = test.left.as_ref() else {
        return None;
    };
    if probe.op != UnaryOp::TypeOf
        || !matches!(test.right.as_ref(), Expr::Lit(Lit::Str(value)) if value.value.to_string_lossy() == "symbol")
    {
        return None;
    }
    let Expr::Member(read) = probe.arg.as_ref() else {
        return None;
    };
    let Expr::Ident(symbol) = read.obj.as_ref() else {
        return None;
    };
    let MemberProp::Ident(key) = &read.prop else {
        return None;
    };
    if symbol.sym != "Symbol" {
        return None;
    }
    let property = symbol_class_key_property(key.sym.as_ref())?;
    let Stmt::Try(body) = statement.cons.as_ref() else {
        return None;
    };
    let handler = body.handler.as_ref()?;
    if body.finalizer.is_some() || handler.param.is_some() || !handler.body.stmts.is_empty() {
        return None;
    }
    let [Stmt::Expr(expression)] = body.block.stmts.as_slice() else {
        return None;
    };
    let Expr::Assign(assignment) = expression.expr.as_ref() else {
        return None;
    };
    let Some(binding_targets::SimpleWriteTarget::Member(target)) =
        binding_targets::simple_write_target(&assignment.left)
    else {
        return None;
    };
    if assignment.op != AssignOp::Assign
        || !matches!(binding_targets::transparent_target_expr(&target.obj), Expr::Ident(receiver) if receiver.to_id() == symbol.to_id())
        || !matches!(&target.prop, MemberProp::Ident(key) if key.sym.as_ref() == property)
    {
        return None;
    }
    let Expr::Call(call) = assignment.right.as_ref() else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(callee) = callee.as_ref() else {
        return None;
    };
    let [argument] = call.args.as_slice() else {
        return None;
    };
    (matches!(callee.obj.as_ref(), Expr::Ident(receiver) if receiver.to_id() == symbol.to_id())
        && matches!(&callee.prop, MemberProp::Ident(key) if key.sym == "for")
        && argument.spread.is_none()
        && matches!(argument.expr.as_ref(), Expr::Lit(Lit::Str(value))
            if value.value.to_string_lossy() == format!("Symbol.{property}")))
    .then(|| (symbol.to_id(), property, target.span))
}

fn symbol_class_key_sites(expression: &Expr) -> Option<Vec<Span>> {
    let Expr::Class(expression) = expression else {
        return None;
    };
    let class = &expression.class;
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return None;
    }
    let mut sites = Vec::new();
    for member in &class.body {
        match member {
            ClassMember::Method(method) => {
                if !method.function.decorators.is_empty()
                    || method.function.params.iter().any(|parameter| !parameter.decorators.is_empty())
                {
                    return None;
                }
                if let PropName::Computed(key) = &method.key {
                    if method.kind != MethodKind::Method {
                        return None;
                    }
                    let Expr::Member(read) = key.expr.as_ref() else {
                        return None;
                    };
                    let Expr::Ident(receiver) = read.obj.as_ref() else {
                        return None;
                    };
                    let MemberProp::Ident(property) = &read.prop else {
                        return None;
                    };
                    if receiver.sym != "Symbol"
                        || symbol_class_key_property(property.sym.as_ref()).is_none()
                    {
                        return None;
                    }
                    sites.push(read.span);
                }
            }
            ClassMember::ClassProp(property)
                if property.decorators.is_empty()
                    && !matches!(property.key, PropName::Computed(_)) => {}
            ClassMember::PrivateProp(property) if property.decorators.is_empty() => {}
            ClassMember::PrivateMethod(method)
                if method.function.decorators.is_empty()
                    && method.function.params.iter().all(|parameter| parameter.decorators.is_empty()) => {}
            ClassMember::Constructor(constructor)
                if !matches!(constructor.key, PropName::Computed(_))
                    && constructor.params.iter().all(|parameter| {
                        matches!(parameter, ParamOrTsParamProp::Param(parameter) if parameter.decorators.is_empty())
                    }) => {}
            ClassMember::Empty(_) => {}
            _ => return None,
        }
    }
    (!sites.is_empty()).then_some(sites)
}

fn empty_map_object_sites(expression: &Expr) -> Option<Vec<Span>> {
    let Expr::Object(object) = expression else {
        return None;
    };
    if object.props.is_empty() {
        return None;
    }
    object
        .props
        .iter()
        .map(|property| {
            let PropOrSpread::Prop(property) = property else {
                return None;
            };
            let Prop::KeyValue(property) = property.as_ref() else {
                return None;
            };
            if !matches!(
                property.key,
                PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
            ) || static_property_name(&property.key).as_deref() == Some("__proto__")
            {
                return None;
            }
            let Expr::New(construction) = property.value.as_ref() else {
                return None;
            };
            (matches!(construction.callee.as_ref(), Expr::Ident(binding) if binding.sym == "Map")
                && construction.args.as_ref().is_none_or(Vec::is_empty))
            .then_some(construction.span)
        })
        .collect()
}

fn native_operation_initializers(
    declaration: &BindingDeclarationWrite,
    operation: NativeInputKind,
) -> Option<Vec<(&ValueExpr, &[ValueExpr], CallSite)>> {
    let initializer = declaration.initializer.as_ref()?;
    match initializer {
        ValueExpr::Call(callee, args, site) | ValueExpr::New(callee, args, site) => {
            Some(vec![(callee.as_ref(), args.as_ref(), *site)])
        }
        ValueExpr::ObjectLiteral(_, fields)
            if operation == NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty) =>
        {
            let sites = declaration.empty_map_object_sites.as_ref()?;
            if sites.len() != fields.len() {
                return None;
            }
            fields
                .iter()
                .zip(sites)
                .map(|((_, value), span)| {
                    let ValueExpr::New(callee, args, site) = value else {
                        return None;
                    };
                    (*site == CallSite::from(*span)).then_some((
                        callee.as_ref(),
                        args.as_ref(),
                        *site,
                    ))
                })
                .collect()
        }
        _ => None,
    }
}

fn set_from_primitive_array_aliases(expression: &Expr) -> Option<Vec<Id>> {
    let Expr::New(construction) = expression else {
        return None;
    };
    let Expr::Ident(constructor) = construction.callee.as_ref() else {
        return None;
    };
    let Some([argument]) = construction.args.as_deref() else {
        return None;
    };
    let Expr::Array(array) = argument.expr.as_ref() else {
        return None;
    };
    if constructor.sym != "Set" || argument.spread.is_some() {
        return None;
    }
    let mut aliases = Vec::new();
    for element in &array.elems {
        let element = element.as_ref()?;
        if element.spread.is_some() {
            return None;
        }
        if primitive_literal(&element.expr) {
            continue;
        }
        let Expr::Ident(identifier) = element.expr.unwrap_parens() else {
            return None;
        };
        aliases.push(identifier.to_id());
    }
    Some(aliases)
}

fn set_from_set_spread_source(expression: &Expr) -> Option<(Id, Span, Span)> {
    let Expr::New(construction) = expression else {
        return None;
    };
    let Expr::Ident(constructor) = construction.callee.as_ref() else {
        return None;
    };
    let [argument] = construction.args.as_deref()? else {
        return None;
    };
    let Expr::Array(array) = argument.expr.as_ref() else {
        return None;
    };
    let [Some(first), rest @ ..] = array.elems.as_slice() else {
        return None;
    };
    let Expr::Ident(source) = first.expr.as_ref() else {
        return None;
    };
    (constructor.sym == "Set"
        && argument.spread.is_none()
        && first.spread.is_some()
        && rest.iter().all(|element| {
            element
                .as_ref()
                .is_some_and(|element| element.spread.is_none() && primitive_literal(&element.expr))
        }))
    .then(|| {
        (
            source.to_id(),
            source.span,
            first.spread.expect("checked spread"),
        )
    })
}

fn span_contains(outer: Span, inner: Span) -> bool {
    outer.lo.0 <= inner.lo.0 && inner.hi.0 <= outer.hi.0
}

/// First milestone accepts only a source-local opaque slot/member read. This
/// is a value-shape boundary, not a purity/effect check: the member access is
/// still interpreted normally for calls, getters, throws, and ordering.
fn is_opaque_member_slot(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::Member(base, ..) => match base.as_ref() {
            ValueExpr::Binding(_) => true,
            ValueExpr::Member(..) => is_opaque_member_slot(base),
            _ => false,
        },
        _ => false,
    }
}

/// A return that is statically primitive cannot expose a captured native
/// descendant. This is a value-shape boundary; it does not assert that
/// evaluating the callable is pure or safe.
/// The named native binding a write on a proved global object can replace.
/// Reading an unrelated global slot instead yields an unknown value identity.
/// Any mutation through an unresolved global-slot value can reach a native
/// object. Preserve that uncertainty instead of treating it as the global
/// object itself and discarding an unrelated descendant property name.
fn native_mutation_roots(mut roots: IntrinsicRoots) -> IntrinsicRoots {
    if roots.remove(&NativeIntrinsic::UnknownGlobalValue) {
        roots.insert(NativeIntrinsic::GlobalObject);
    }
    roots
}

struct ReceiverRootProjection {
    prior_roots: IntrinsicRoots,
    effective_roots: IntrinsicRoots,
    affected_roots: IntrinsicRoots,
    value_input_refs: BTreeSet<ValueInputRef>,
    /// A bundle-owned global value was discharged.
    bundle_global: bool,
}

fn project_receiver_roots(
    roots: &NativeReceiverRoots,
    owner: SourceUnitOrdinal,
) -> ReceiverRootProjection {
    let mut prior_roots = IntrinsicRoots::new();
    let mut effective_roots = IntrinsicRoots::new();
    let mut value_input_refs = BTreeSet::new();
    let mut bundle_global = false;
    for root in roots {
        match root {
            NativeReceiverRoot::Intrinsic(root)
            | NativeReceiverRoot::IntrinsicPrototype(root)
            | NativeReceiverRoot::IntrinsicDescendant(root) => {
                prior_roots.insert(*root);
                effective_roots.insert(*root);
            }
            NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
            }
            NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            } if owner > *available_after => {
                // The input discharges this prior unknown-global possibility
                // only for the exact captured receiver, preserving the fact
                // dependency even when the resulting mutation roots are empty.
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                value_input_refs.insert(reference.clone());
            }
            NativeReceiverRoot::OrdinaryDirect { .. } => {
                // The initializer or stable copy has not completed at this
                // observation. Preserve the prior opaque behavior.
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
            }
            NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                if owner > *available_after {
                    value_input_refs.insert(reference.clone());
                }
            }
            // A bundle-owned global's value is discharged by the host claim.
            NativeReceiverRoot::BundleGlobal => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                bundle_global = true;
            }
        }
    }
    let affected_roots = native_mutation_roots(effective_roots.clone());
    ReceiverRootProjection {
        prior_roots,
        effective_roots,
        affected_roots,
        value_input_refs,
        bundle_global,
    }
}

fn native_write_slots(
    roots: &NativeReceiverRoots,
    key: &PropertyKeyDomain,
) -> BTreeSet<NativeWriteSlot> {
    let keys: Vec<_> = match key {
        PropertyKeyDomain::FiniteStrings(values) => values
            .iter()
            .cloned()
            .map(NativePropertyKey::String)
            .collect(),
        PropertyKeyDomain::Affixed { prefix, suffix } => vec![NativePropertyKey::Affixed {
            prefix: prefix.clone(),
            suffix: suffix.clone(),
        }],
        PropertyKeyDomain::SymbolIterator => vec![NativePropertyKey::SymbolIterator],
        PropertyKeyDomain::Symbol => vec![NativePropertyKey::OtherSymbol],
        PropertyKeyDomain::Unknown => vec![NativePropertyKey::Unknown],
    };
    let roots = if roots.is_empty() {
        BTreeSet::from([NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        )])
    } else {
        roots.clone()
    };
    roots
        .iter()
        .flat_map(|root| {
            let receiver = match root {
                NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                    NativeReceiverPath::GlobalObject
                }
                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                | NativeReceiverRoot::OrdinaryDirect { .. }
                | NativeReceiverRoot::OrdinaryDescendant { .. }
                | NativeReceiverRoot::BundleGlobal => NativeReceiverPath::Unknown,
                NativeReceiverRoot::Intrinsic(root) => NativeReceiverPath::Constructor(*root),
                NativeReceiverRoot::IntrinsicPrototype(root) => {
                    NativeReceiverPath::Prototype(*root)
                }
                NativeReceiverRoot::IntrinsicDescendant(root) => {
                    NativeReceiverPath::Descendant(*root)
                }
                NativeReceiverRoot::ArrayIteratorPrototype => {
                    NativeReceiverPath::ArrayIteratorPrototype
                }
                NativeReceiverRoot::SetIteratorPrototype => {
                    NativeReceiverPath::SetIteratorPrototype
                }
            };
            keys.iter().cloned().map(move |key| NativeWriteSlot {
                receiver: receiver.clone(),
                key,
            })
        })
        .collect()
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum NativeSlotOverlap {
    Known(SourceUnitOrdinal),
    Opaque,
    Absent,
}

/// The Array-spread operation input supplies this one narrow native-iterator
/// premise: an iterator from a fresh array uses the native iterator protocol.
/// It can identify a write to that iterator's `next` slot, but does not prove
/// Object or Symbol identity.
#[derive(Clone, Copy)]
struct ArraySpreadNativeIteratorPremise;

fn array_spread_protocol_slot_overlap<'a>(
    witnesses: impl Iterator<Item = &'a NativeMutationWitness>,
    slot: &NativeWriteSlot,
    source_mutated: &IntrinsicRoots,
    global_array_binding_mutated: bool,
    premise_authorized_next_writes: &BTreeSet<ArrayIteratorNextWriteId>,
    _premise: ArraySpreadNativeIteratorPremise,
) -> NativeSlotOverlap {
    let (expected_dependencies, independently_required, premise_allowed) =
        match (&slot.receiver, &slot.key) {
            (
                NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                NativePropertyKey::SymbolIterator,
            ) => (
                IntrinsicRoots::from([NativeIntrinsic::Symbol]),
                IntrinsicRoots::from([NativeIntrinsic::Symbol]),
                IntrinsicRoots::new(),
            ),
            (NativeReceiverPath::ArrayIteratorPrototype, NativePropertyKey::String(property))
                if property == "next" =>
            {
                (
                    IntrinsicRoots::from([
                        NativeIntrinsic::Object,
                        NativeIntrinsic::Array,
                        NativeIntrinsic::Symbol,
                    ]),
                    IntrinsicRoots::from([NativeIntrinsic::Object, NativeIntrinsic::Symbol]),
                    // The selected Array-spread premise supplies only the Array
                    // iterator receiver identity; Object.getPrototypeOf and the
                    // Symbol.iterator key still require independent source proof.
                    IntrinsicRoots::from([NativeIntrinsic::Array]),
                )
            }
            _ => return NativeSlotOverlap::Absent,
        };
    let mut opaque = false;
    for witness in witnesses {
        if witness.unreachable_branch.is_some() || !witness.target_slots.contains(slot) {
            continue;
        }
        let is_array_iterator_next = matches!(
            (&slot.receiver, &slot.key),
            (
                NativeReceiverPath::ArrayIteratorPrototype,
                NativePropertyKey::String(property)
            ) if property == "next"
        );
        if is_array_iterator_next
            && !premise_authorized_next_writes.contains(&array_iterator_next_write_id(witness))
        {
            // The premise discharges only the receiver part of this merged
            // dependency set. A conditional key dependency on Array remains
            // unresolved unless this exact write was independently eligible
            // for the premise-relative closure.
            opaque = true;
            continue;
        }
        // The expected sets distinguish the canonical receiver dependency
        // from any uncertainty in a computed property key or alternative
        // receiver path. Do not discharge arbitrary roots from the premise.
        if witness.target_slots.len() != 1
            || witness.key_dependencies != expected_dependencies
            || !witness.value_input_refs.is_empty()
            || (matches!(
                (&slot.receiver, &slot.key),
                (
                    NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                    NativePropertyKey::SymbolIterator
                )
            ) && global_array_binding_mutated)
            || source_mutated.contains(&NativeIntrinsic::GlobalObject)
            || !independently_required.is_disjoint(source_mutated)
            || !witness
                .key_dependencies
                .intersection(source_mutated)
                .all(|dependency| premise_allowed.contains(dependency))
        {
            opaque = true;
            continue;
        }
        return NativeSlotOverlap::Known(witness.statement_ordinal);
    }
    if opaque {
        NativeSlotOverlap::Opaque
    } else {
        NativeSlotOverlap::Absent
    }
}

type ArrayIteratorNextWriteId = (String, SourceUnitOrdinal, u32, u32);

fn array_iterator_next_write_id(witness: &NativeMutationWitness) -> ArrayIteratorNextWriteId {
    (
        witness.chunk_id.clone(),
        witness.statement_ordinal,
        witness.span.lo.0,
        witness.span.hi.0,
    )
}

fn exact_array_iterator_next_candidate(witness: &NativeMutationWitness) -> bool {
    witness.unreachable_branch.is_none()
        && witness.value_input_refs.is_empty()
        && witness.target_slots.len() == 1
        && witness.target_slots.contains(&NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        })
        && witness.key_dependencies
            == IntrinsicRoots::from([
                NativeIntrinsic::Object,
                NativeIntrinsic::Array,
                NativeIntrinsic::Symbol,
            ])
}

fn source_slot_overlap<'a>(
    witnesses: impl Iterator<Item = &'a NativeMutationWitness>,
    slot: &NativeWriteSlot,
) -> NativeSlotOverlap {
    let mut opaque = false;
    for witness in witnesses {
        // A direct constructor/prototype path is exact relative to the
        // caller's native-identity premise. Descendant and cross-path aliases
        // may reach that same object, but remain opaque here.
        if matches!(
            &slot.receiver,
            NativeReceiverPath::GlobalObject
                | NativeReceiverPath::Constructor(_)
                | NativeReceiverPath::Prototype(_)
        ) && witness.target_slots.contains(slot)
            && matches!(
                &slot.key,
                NativePropertyKey::String(_) | NativePropertyKey::SymbolIterator
            )
            && witness.key_dependencies.is_empty()
        {
            return NativeSlotOverlap::Known(witness.statement_ordinal);
        }
        opaque |= witness.target_slots.iter().any(|candidate| {
            !witness.key_dependencies.is_empty()
                || candidate.key == slot.key
                || match &slot.key {
                    NativePropertyKey::String(name) => candidate.key.may_be_string(name),
                    _ => candidate.key == NativePropertyKey::Unknown,
                }
        });
    }
    if opaque {
        NativeSlotOverlap::Opaque
    } else {
        NativeSlotOverlap::Absent
    }
}

/// The own-data property of the unshadowed global `Object` an exact member
/// read names, as the `%Object.<property>%` slot spells it.
pub(super) fn object_own_data_read_property(
    value: &ValueExpr,
    is_declared: impl Fn(&Id) -> bool,
) -> Option<&'static str> {
    let [
        IntrinsicSlot::Global(NativeIntrinsic::Object),
        IntrinsicSlot::Static(NativeIntrinsic::Object, property),
    ] = native_own_data_read_slots(value, is_declared)?
    else {
        return None;
    };
    builtins::data_property("Object", property)
        .filter(|data| data.value == builtins::PropertyValue::Function)
        .map(|data| data.name)
}

fn symbol_well_known_read_property(
    value: &ValueExpr,
    is_declared: impl Fn(&Id) -> bool,
) -> Option<&'static str> {
    let [
        IntrinsicSlot::Global(NativeIntrinsic::Symbol),
        IntrinsicSlot::Static(NativeIntrinsic::Symbol, property),
    ] = native_own_data_read_slots(value, is_declared)?
    else {
        return None;
    };
    symbol_class_key_property(property)
}

/// Slot obligations for an exact native own-data lookup. Receiver and key
/// evaluation remain with the indexed operation's caller.
pub(super) fn native_own_data_read_slots(
    value: &ValueExpr,
    is_declared: impl Fn(&Id) -> bool,
) -> Option<[IntrinsicSlot; 2]> {
    let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = value else {
        return None;
    };
    let ValueExpr::Binding(binding) = receiver.as_ref() else {
        return None;
    };
    if is_declared(binding) {
        return None;
    }
    let family = NativeIntrinsic::for_global_binding(binding.0.as_ref())?;
    let property = builtins::own_data_property_name(binding.0.as_ref(), property)?;
    Some([
        IntrinsicSlot::Global(family),
        IntrinsicSlot::Static(family, property),
    ])
}

fn receiver_uses_intrinsic_alias(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
) -> bool {
    match value {
        ValueExpr::Binding(id) => aliases.contains_key(id),
        ValueExpr::Member(base, ..) => receiver_uses_intrinsic_alias(base, aliases),
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|option| receiver_uses_intrinsic_alias(option, aliases))
        }
        _ => false,
    }
}

/// The global-object property whose value the receiver is read from, by
/// spelling: `location` for `window.location.href = url` or
/// `location.href = url`.
fn receiver_global_property(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
) -> Option<String> {
    let global_object = |id: &Id| {
        (!declared.contains(id) && globals.contains(id.0.as_ref()))
            || aliases.get(id).is_some_and(|roots| {
                roots.contains(&NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::GlobalObject,
                ))
            })
    };
    match value {
        ValueExpr::Binding(id)
            if !declared.contains(id)
                && !global_object(id)
                && globals.binding_root(id.0.as_ref()).is_none() =>
        {
            Some(id.0.to_string())
        }
        ValueExpr::Member(base, property, _, _) => match base.as_ref() {
            ValueExpr::Binding(id) if global_object(id) => property.clone(),
            base => receiver_global_property(base, aliases, globals, declared),
        },
        _ => None,
    }
}

fn intrinsic_roots(
    value: &ValueExpr,
    roots: &BTreeMap<Id, NativeReceiverRoots>,
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
    ordinary_inputs: &BTreeMap<Id, (ValueInputRef, SourceUnitOrdinal)>,
    callable_return_inputs: &CallableReturnInputs,
) -> NativeReceiverRoots {
    let intrinsic = |root| NativeReceiverRoot::Intrinsic(root);
    match value {
        ValueExpr::Binding(id) if ordinary_inputs.contains_key(id) => {
            let (reference, available_after) = &ordinary_inputs[id];
            BTreeSet::from([NativeReceiverRoot::OrdinaryDirect {
                reference: reference.clone(),
                available_after: *available_after,
            }])
        }
        ValueExpr::ArrayIteratorPrototype(expression) => {
            let mut bindings = BTreeSet::new();
            expression.bindings(&mut bindings);
            if bindings.iter().any(|binding| {
                declared.contains(binding) && matches!(binding.0.as_ref(), "Object" | "Symbol")
            }) {
                BTreeSet::from([intrinsic(NativeIntrinsic::UnknownGlobalValue)])
            } else {
                BTreeSet::from([NativeReceiverRoot::ArrayIteratorPrototype])
            }
        }
        ValueExpr::SetIteratorPrototype(_) => {
            BTreeSet::from([NativeReceiverRoot::SetIteratorPrototype])
        }
        // The spelling of a local binding is not an intrinsic identity.
        // Retain source-only aliases, but never manufacture a native root for
        // a shadowed constructor or global-object name.
        ValueExpr::Binding(id) if declared.contains(id) => {
            roots.get(id).cloned().unwrap_or_default()
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Error" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Error)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Function" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Function)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Object" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Object)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Array" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Array)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Math" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Math)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "RegExp" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::RegExp)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Symbol" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Symbol)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Promise" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Promise)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Map" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Map)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Set" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Set)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakMap" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::WeakMap)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakSet" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::WeakSet)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "FinalizationRegistry" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::FinalizationRegistry)])
        }
        ValueExpr::Binding(id) if globals.contains(id.0.as_ref()) => {
            BTreeSet::from([intrinsic(NativeIntrinsic::GlobalObject)])
        }
        ValueExpr::Binding(id) => roots.get(id).cloned().unwrap_or_default(),
        ValueExpr::Alloc(Kind::Function, _) => NativeReceiverRoots::new(),
        ValueExpr::ObjectLiteral(_, fields) => {
            // A fresh container is not native merely because it stores a
            // native value. Preserve this value-input extension only when a
            // field actually carries the selected ordinary input.
            let mut references = BTreeSet::new();
            for (_, field) in fields.iter() {
                for root in intrinsic_roots(
                    field,
                    roots,
                    globals,
                    declared,
                    ordinary_inputs,
                    callable_return_inputs,
                ) {
                    match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => {
                            references.insert((reference, available_after));
                        }
                        NativeReceiverRoot::Intrinsic(_)
                        | NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => {}
                    }
                }
            }
            let mut out = NativeReceiverRoots::new();
            if !references.is_empty() {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                out.extend(references.into_iter().map(|(reference, available_after)| {
                    NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    }
                }));
            }
            out
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            let options = value.result_alternatives().expect("alternative expression");
            let mut combined = NativeReceiverRoots::new();
            let mut unknown_arm = false;
            let mut ordinary_refs = BTreeSet::new();
            for option in options {
                let option_roots = intrinsic_roots(
                    option,
                    roots,
                    globals,
                    declared,
                    ordinary_inputs,
                    callable_return_inputs,
                );
                ordinary_refs.extend(option_roots.iter().filter_map(|root| match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => Some((reference.clone(), *available_after)),
                    NativeReceiverRoot::Intrinsic(_)
                    | NativeReceiverRoot::IntrinsicPrototype(_)
                    | NativeReceiverRoot::IntrinsicDescendant(_)
                    | NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype
                    | NativeReceiverRoot::BundleGlobal => None,
                }));
                if option_roots.is_empty()
                    && !matches!(
                        option,
                        ValueExpr::Undefined
                            | ValueExpr::NonCallable(_)
                            | ValueExpr::Primitive(_)
                            | ValueExpr::StrictEquality(..)
                            | ValueExpr::LogicalNot(_)
                            | ValueExpr::Void(_)
                    )
                {
                    unknown_arm = true;
                }
                combined.extend(option_roots);
            }
            if unknown_arm {
                for (reference, available_after) in ordinary_refs {
                    combined.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                    combined.insert(NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    });
                }
            }
            combined
        }
        ValueExpr::Member(base, property, key, _) => {
            let base_roots = intrinsic_roots(
                base,
                roots,
                globals,
                declared,
                ordinary_inputs,
                callable_return_inputs,
            );
            let mut out = NativeReceiverRoots::new();
            for root in base_roots {
                match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => {
                        out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                        out.insert(NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        });
                    }
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                        if globals.bundle.holds(property.as_deref(), key) {
                            out.insert(NativeReceiverRoot::BundleGlobal);
                        } else {
                            out.insert(intrinsic(
                                property
                                    .as_deref()
                                    .and_then(|property| globals.binding_root(property))
                                    .unwrap_or(NativeIntrinsic::UnknownGlobalValue),
                            ));
                        }
                    }
                    NativeReceiverRoot::BundleGlobal => {
                        out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                    }
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => {
                        out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                    }
                    NativeReceiverRoot::Intrinsic(root) => {
                        out.insert(if property.as_deref() == Some("prototype") {
                            NativeReceiverRoot::IntrinsicPrototype(root)
                        } else {
                            NativeReceiverRoot::IntrinsicDescendant(root)
                        });
                        // These paths can cross from one intrinsic family to
                        // another; computed keys can name either one. Preserve
                        // the known family and add an opaque root so writes
                        // through aliases conservatively invalidate all native
                        // proofs that may depend on the resolved object.
                        if property
                            .as_deref()
                            .is_none_or(|property| matches!(property, "__proto__" | "constructor"))
                        {
                            out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                        }
                    }
                    NativeReceiverRoot::IntrinsicPrototype(root)
                    | NativeReceiverRoot::IntrinsicDescendant(root) => {
                        out.insert(NativeReceiverRoot::IntrinsicDescendant(root));
                        if property
                            .as_deref()
                            .is_none_or(|property| matches!(property, "__proto__" | "constructor"))
                        {
                            out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                        }
                    }
                    NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype => {
                        out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                    }
                }
            }
            out
        }
        ValueExpr::Call(callee, args, _) | ValueExpr::New(callee, args, _) => {
            let mut input_roots = NativeReceiverRoots::new();
            let mut ordinary_refs = BTreeSet::new();
            let mut has_callable_return_roots = false;
            for value in std::iter::once(callee.as_ref()).chain(args.iter()) {
                let value_roots = intrinsic_roots(
                    value,
                    roots,
                    globals,
                    declared,
                    ordinary_inputs,
                    callable_return_inputs,
                );
                ordinary_refs.extend(value_roots.iter().filter_map(|root| match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => Some((reference.clone(), *available_after)),
                    NativeReceiverRoot::Intrinsic(_)
                    | NativeReceiverRoot::IntrinsicPrototype(_)
                    | NativeReceiverRoot::IntrinsicDescendant(_)
                    | NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype
                    | NativeReceiverRoot::BundleGlobal => None,
                }));
                input_roots.extend(value_roots);
            }
            let object_get_prototype_of = match callee.as_ref() {
                ValueExpr::Member(receiver, Some(property), MemberKey::Static, _)
                    if property == "getPrototypeOf" =>
                {
                    intrinsic_roots(
                        receiver,
                        roots,
                        globals,
                        declared,
                        ordinary_inputs,
                        callable_return_inputs,
                    )
                    .contains(&NativeReceiverRoot::Intrinsic(NativeIntrinsic::Object))
                }
                _ => false,
            };
            if matches!(value, ValueExpr::Call(..)) && object_get_prototype_of {
                // A prototype query can cross intrinsic families or reach a
                // builtin through an ordinary input. Keep its result opaque
                // and carry the possible native roots through result aliases;
                // a later write must not evade the completed census.
                input_roots.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                return input_roots;
            }
            if let ValueExpr::Binding(binding) = callee.as_ref()
                && let Some(return_roots) = callable_return_inputs.by_binding.get(binding)
            {
                has_callable_return_roots = true;
                ordinary_refs.extend(return_roots.iter().filter_map(|root| match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => Some((reference.clone(), *available_after)),
                    NativeReceiverRoot::Intrinsic(_)
                    | NativeReceiverRoot::IntrinsicPrototype(_)
                    | NativeReceiverRoot::IntrinsicDescendant(_)
                    | NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype
                    | NativeReceiverRoot::BundleGlobal => None,
                }));
                input_roots.extend(return_roots.iter().cloned());
            } else if let ValueExpr::Alloc(Kind::Function, function) = callee.as_ref()
                && let Some(return_roots) = callable_return_inputs.by_origin.get(function)
            {
                has_callable_return_roots = true;
                ordinary_refs.extend(return_roots.iter().filter_map(|root| match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => Some((reference.clone(), *available_after)),
                    NativeReceiverRoot::Intrinsic(_)
                    | NativeReceiverRoot::IntrinsicPrototype(_)
                    | NativeReceiverRoot::IntrinsicDescendant(_)
                    | NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype
                    | NativeReceiverRoot::BundleGlobal => None,
                }));
                input_roots.extend(return_roots.iter().cloned());
            }
            // Preserve baseline opaque call/construct behavior unless an
            // author input participates. Native constructor aliases and
            // instances do not inherit their constructor's identity.
            if ordinary_refs.is_empty() {
                return if has_callable_return_roots {
                    input_roots
                } else {
                    NativeReceiverRoots::new()
                };
            }
            let mut out = input_roots;
            out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
            out.extend(
                ordinary_refs
                    .into_iter()
                    .map(
                        |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        },
                    ),
            );
            out
        }
        _ => NativeReceiverRoots::new(),
    }
}

fn read_intrinsic_alias_inputs(
    context: &mut impl FactContext<Id, NativeReceiverRoots>,
    value: &ValueExpr,
) -> BTreeMap<Id, NativeReceiverRoots> {
    let mut mentioned = BTreeSet::new();
    value.bindings(&mut mentioned);
    read_intrinsic_aliases(context, mentioned)
}

fn read_intrinsic_aliases(
    context: &mut impl FactContext<Id, NativeReceiverRoots>,
    bindings: impl IntoIterator<Item = Id>,
) -> BTreeMap<Id, NativeReceiverRoots> {
    bindings
        .into_iter()
        .filter_map(|binding| {
            context
                .read(binding.clone())
                .cloned()
                .map(|roots| (binding, roots))
        })
        .collect()
}

struct RawIntrinsicAliasRules<'a> {
    assignments: &'a [&'a (Id, ValueExpr)],
    globals: &'a GlobalNames,
    declared: &'a BTreeSet<Id>,
}

impl IterativeRules for RawIntrinsicAliasRules<'_> {
    type Key = Id;
    type Fact = NativeReceiverRoots;
    type Rule = usize;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (target, value) = self.assignments[*rule];
        let aliases = read_intrinsic_alias_inputs(context, value);
        let roots = intrinsic_roots(
            value,
            &aliases,
            self.globals,
            self.declared,
            &BTreeMap::new(),
            &CallableReturnInputs::default(),
        );
        if !roots.is_empty() {
            context.propose(target.clone(), roots);
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

fn close_raw_intrinsic_aliases(
    assignments: &[&(Id, ValueExpr)],
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
) -> BTreeMap<Id, NativeReceiverRoots> {
    IterativeInference::new(RawIntrinsicAliasRules {
        assignments,
        globals,
        declared,
    })
    .solve(0..assignments.len())
    .into_values()
}

#[cfg(test)]
mod iterative_alias_closure_tests {
    use super::{
        GlobalNames, MemberKey, NamespaceValueFacts, NativeReceiverRoot, ValueExpr,
        close_prepublication_derived_bindings, close_raw_intrinsic_aliases,
        infer_mutation_receiver_aliases,
    };
    use crate::analysis_inputs::NativeIntrinsic;
    use std::collections::{BTreeMap, BTreeSet};
    use std::sync::Arc;
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    #[test]
    fn receiver_alias_watchers_propagate_backward_assignment_chains() {
        let namespace = id("namespace");
        let first = id("first");
        let second = id("second");
        let assignments = [
            (first.clone(), ValueExpr::Binding(second.clone())),
            (second.clone(), ValueExpr::Binding(namespace.clone())),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = infer_mutation_receiver_aliases(
            &namespace,
            &assignment_refs,
            &BTreeSet::new(),
            &NamespaceValueFacts::default(),
        );

        assert_eq!(aliases, BTreeSet::from([first, second]));
    }

    #[test]
    fn receiver_alias_watchers_include_projected_data_members() {
        let namespace = id("namespace");
        let holder = id("holder");
        let candidate = id("candidate");
        let projected = id("projected");
        let assignments = [
            (
                projected.clone(),
                ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(holder.clone())),
                    Some("member".to_owned()),
                    MemberKey::Static,
                    swc_common::DUMMY_SP.into(),
                ),
            ),
            (
                holder.clone(),
                ValueExpr::ObjectLiteral(
                    7,
                    [
                        (
                            Some("owner".to_owned()),
                            ValueExpr::Binding(namespace.clone()),
                        ),
                        (
                            Some("member".to_owned()),
                            ValueExpr::Binding(candidate.clone()),
                        ),
                    ]
                    .into(),
                ),
            ),
            (candidate.clone(), ValueExpr::Unknown),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();
        let facts = NamespaceValueFacts::from_assignments(
            BTreeMap::new(),
            BTreeSet::from([7]),
            &assignment_refs,
        );

        let aliases =
            infer_mutation_receiver_aliases(&namespace, &assignment_refs, &BTreeSet::new(), &facts);

        assert_eq!(aliases, BTreeSet::from([candidate, holder, projected]));
    }

    #[test]
    fn raw_intrinsic_aliases_remain_source_only_and_follow_late_roots() {
        let array = id("Array");
        let first = id("first");
        let second = id("second");
        let assignments = [
            (first.clone(), ValueExpr::Binding(second.clone())),
            (second.clone(), ValueExpr::Binding(array.clone())),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = close_raw_intrinsic_aliases(
            &assignment_refs,
            &GlobalNames::default(),
            &BTreeSet::new(),
        );

        assert_eq!(
            aliases.get(&first),
            Some(&BTreeSet::from([NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::Array
            )]))
        );
        assert_eq!(
            aliases.get(&second),
            Some(&BTreeSet::from([NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::Array
            )]))
        );
        assert!(!aliases.contains_key(&array));
    }

    #[test]
    fn raw_intrinsic_aliases_do_not_turn_shadowed_names_into_native_roots() {
        let local_set = id("Set");
        let alias = id("alias");
        let assignments = [(alias.clone(), ValueExpr::Binding(local_set.clone()))];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = close_raw_intrinsic_aliases(
            &assignment_refs,
            &GlobalNames::default(),
            &BTreeSet::from([local_set]),
        );
        assert!(!aliases.contains_key(&alias));
    }

    #[test]
    fn prepublication_derived_bindings_wake_backward_assignment_chains() {
        let alias = id("alias");
        let holder = id("holder");
        let projected = id("projected");
        let unrelated = id("unrelated");
        let assignments = [
            (
                projected.clone(),
                ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(holder.clone())),
                    Some("member".to_owned()),
                    MemberKey::Static,
                    swc_common::DUMMY_SP.into(),
                ),
            ),
            (
                holder.clone(),
                ValueExpr::ObjectLiteral(
                    9,
                    [
                        (Some("member".to_owned()), ValueExpr::Binding(alias.clone())),
                        (
                            Some("other".to_owned()),
                            ValueExpr::Binding(unrelated.clone()),
                        ),
                    ]
                    .into(),
                ),
            ),
        ];

        assert_eq!(
            close_prepublication_derived_bindings(&alias, &assignments),
            BTreeSet::from([alias, holder, projected])
        );
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum ReceiverFlowRule {
    Assignment(usize),
    Binding(Id),
    ReturnSite(u32, usize),
    Callable(u32),
}

struct ReceiverFlowRules<'a> {
    assignments: &'a [&'a (Id, ValueExpr)],
    assignment_indices: &'a BTreeMap<Id, BTreeSet<usize>>,
    index: &'a ExecutionIndex,
    declared: &'a BTreeSet<Id>,
    pending_bindings: &'a BTreeSet<Id>,
}

fn receiver_member_roots(
    base_roots: &NativeReceiverRoots,
    property: Option<&str>,
) -> NativeReceiverRoots {
    let mut out = NativeReceiverRoots::new();
    for root in base_roots {
        match root {
            NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype
            | NativeReceiverRoot::BundleGlobal => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
            NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            }
            | NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
                out.insert(NativeReceiverRoot::OrdinaryDescendant {
                    reference: reference.clone(),
                    available_after: *available_after,
                });
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    property
                        .and_then(NativeIntrinsic::for_global_binding)
                        .unwrap_or(NativeIntrinsic::UnknownGlobalValue),
                ));
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
            NativeReceiverRoot::Intrinsic(root) => {
                out.insert(if property == Some("prototype") {
                    NativeReceiverRoot::IntrinsicPrototype(*root)
                } else {
                    NativeReceiverRoot::IntrinsicDescendant(*root)
                });
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                }
            }
            NativeReceiverRoot::IntrinsicPrototype(root)
            | NativeReceiverRoot::IntrinsicDescendant(root) => {
                out.insert(NativeReceiverRoot::IntrinsicDescendant(*root));
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                }
            }
        }
    }
    out
}

fn evaluate_receiver_value(
    value: &ValueExpr,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    match value {
        ValueExpr::ArrayIteratorPrototype(_) | ValueExpr::SetIteratorPrototype(_) => {
            result.fact.roots = intrinsic_roots(
                value,
                &BTreeMap::new(),
                &rules.index.census_globals,
                rules.declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            result.fact.complete = true;
            return result;
        }
        ValueExpr::Binding(binding) => {
            if let Some((reference, available_after)) = rules.index.ordinary_input_refs.get(binding)
            {
                result
                    .fact
                    .roots
                    .insert(NativeReceiverRoot::OrdinaryDirect {
                        reference: reference.clone(),
                        available_after: *available_after,
                    });
                result
                    .fact
                    .capture_dependencies
                    .insert((reference.clone(), *available_after));
                return result;
            }
            let builtin_roots = intrinsic_roots(
                value,
                &BTreeMap::new(),
                &rules.index.census_globals,
                rules.declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            if !builtin_roots.is_empty() {
                result.fact.roots = builtin_roots;
                result.fact.complete = true;
                return result;
            }
            if let Some(fact) = context.read(ReceiverFlowKey::Binding(binding.clone())) {
                result.fact = fact.clone();
                result.pending = !fact.complete;
            } else if rules.pending_bindings.contains(binding) {
                result.pending = true;
            } else {
                result.fact.opaque = true;
                result.fact.complete = true;
            }
            let targets = result.fact.callable_targets.clone();
            read_callable_capture_dependencies(&targets, context, &mut result.fact);
        }
        ValueExpr::Alloc(Kind::Function, function) => {
            result.fact.callable_targets.insert(*function);
            result.fact.complete = true;
            read_callable_capture_dependencies(
                &BTreeSet::from([*function]),
                context,
                &mut result.fact,
            );
        }
        ValueExpr::Alloc(..) => {
            result.fact.opaque = true;
            result.fact.complete = true;
        }
        ValueExpr::ObjectLiteral(_, fields) => {
            let mut ordinary_refs = BTreeSet::new();
            result.fact.safe = true;
            result.fact.complete = true;
            for (_, field) in fields.iter() {
                let field = evaluate_receiver_value(field, context, rules);
                result.pending |= field.pending;
                result.fact.safe &= field.fact.safe;
                result.fact.opaque |= field.fact.opaque;
                result
                    .fact
                    .callable_targets
                    .extend(field.fact.callable_targets);
                result
                    .fact
                    .callable_dependencies
                    .extend(field.fact.callable_dependencies);
                result
                    .fact
                    .capture_dependencies
                    .extend(field.fact.capture_dependencies);
                result.fact.complete &= field.fact.complete;
                for root in field.fact.roots {
                    match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => {
                            ordinary_refs.insert((reference, available_after));
                        }
                        NativeReceiverRoot::Intrinsic(_)
                        | NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => {}
                    }
                }
            }
            if !ordinary_refs.is_empty() {
                result.fact.safe = false;
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
            result.fact.roots.extend(ordinary_refs.into_iter().map(
                |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                    reference,
                    available_after,
                },
            ));
            // A returned object containing a callable can expose that callable's
            // later native return through a projection. This bounded model does
            // not inspect arbitrary object protocols, so keep that escape opaque.
            if !result.fact.callable_targets.is_empty() {
                result.fact.safe = false;
                result.fact.opaque = true;
            }
        }
        ValueExpr::Undefined | ValueExpr::NonCallable(_) => {
            result.fact.safe = true;
            result.fact.complete = true;
        }
        ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => {
            result.fact.safe = true;
            result.fact.complete = true;
        }
        ValueExpr::Member(base, property, _, _) => {
            let base = evaluate_receiver_value(base, context, rules);
            result.pending = base.pending;
            // A primitive or fresh-container receiver does not prove that its
            // inherited property is primitive or fresh. Keep arbitrary member
            // reads opaque; captured-rooted reads retain descendant taint via
            // `receiver_member_roots` below.
            result.fact.safe = false;
            result.fact.opaque = base.fact.opaque;
            result.fact.callable_dependencies = base.fact.callable_dependencies;
            result.fact.capture_dependencies = base.fact.capture_dependencies;
            result.fact.complete = base.fact.complete;
            result.fact.roots = receiver_member_roots(&base.fact.roots, property.as_deref());
            if base.fact.roots.is_empty() || !base.fact.callable_targets.is_empty() {
                result.fact.opaque = true;
            }
            if result.fact.opaque {
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            let options = value.result_alternatives().expect("alternative expression");
            result.fact.safe = true;
            result.fact.complete = true;
            for option in options {
                let option = evaluate_receiver_value(option, context, rules);
                result.pending |= option.pending;
                result.fact.safe &= option.fact.safe;
                result.fact.opaque |= option.fact.opaque;
                result.fact.roots.extend(option.fact.roots);
                result
                    .fact
                    .callable_targets
                    .extend(option.fact.callable_targets);
                result
                    .fact
                    .callable_dependencies
                    .extend(option.fact.callable_dependencies);
                result
                    .fact
                    .capture_dependencies
                    .extend(option.fact.capture_dependencies);
                result.fact.complete &= option.fact.complete;
            }
            if result.fact.opaque {
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
        }
        ValueExpr::Call(callee, args, _) => {
            let callee_value = evaluate_receiver_value(callee, context, rules);
            result.pending = callee_value.pending;
            result.fact.complete = callee_value.fact.complete;
            result
                .fact
                .roots
                .extend(callee_value.fact.roots.iter().cloned());
            result.fact.opaque |= callee_value.fact.opaque;
            result
                .fact
                .callable_dependencies
                .extend(callee_value.fact.callable_dependencies.iter().copied());
            result
                .fact
                .capture_dependencies
                .extend(callee_value.fact.capture_dependencies.iter().cloned());
            let mut ordinary_refs = BTreeSet::new();
            for root in &result.fact.roots {
                if let NativeReceiverRoot::OrdinaryDirect {
                    reference,
                    available_after,
                }
                | NativeReceiverRoot::OrdinaryDescendant {
                    reference,
                    available_after,
                } = root
                {
                    ordinary_refs.insert((reference.clone(), *available_after));
                }
            }
            for argument in args.iter() {
                let argument = evaluate_receiver_value(argument, context, rules);
                result.pending |= argument.pending;
                result.fact.complete &= argument.fact.complete;
                result.fact.opaque |= argument.fact.opaque;
                result
                    .fact
                    .callable_dependencies
                    .extend(argument.fact.callable_dependencies.iter().copied());
                result
                    .fact
                    .capture_dependencies
                    .extend(argument.fact.capture_dependencies.iter().cloned());
                result
                    .fact
                    .roots
                    .extend(argument.fact.roots.iter().cloned());
                for root in argument.fact.roots {
                    if let NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } = root
                    {
                        ordinary_refs.insert((reference, available_after));
                    }
                }
            }
            if !ordinary_refs.is_empty() {
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
                result.fact.roots.extend(ordinary_refs.iter().map(
                    |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                        reference: reference.clone(),
                        available_after: *available_after,
                    },
                ));
            }
            if callee_value.fact.callable_targets.is_empty() {
                result.pending |= callee_value.pending;
                if !callee_value.pending {
                    result.fact.opaque = true;
                }
            } else {
                for function in callee_value.fact.callable_targets {
                    result.fact.callable_dependencies.insert(function);
                    if let Some(summary) = context.read(ReceiverFlowKey::Callable(function)) {
                        for outcome in &summary.outcomes {
                            match outcome {
                                CallableReturnOutcome::Safe => result.fact.safe = true,
                                CallableReturnOutcome::Descendant(reference, available_after) => {
                                    result
                                        .fact
                                        .capture_dependencies
                                        .insert((reference.clone(), *available_after));
                                    result.fact.roots.insert(
                                        NativeReceiverRoot::OrdinaryDescendant {
                                            reference: reference.clone(),
                                            available_after: *available_after,
                                        },
                                    );
                                }
                                CallableReturnOutcome::Intrinsic(root) => {
                                    result
                                        .fact
                                        .roots
                                        .insert(NativeReceiverRoot::Intrinsic(*root));
                                }
                                CallableReturnOutcome::Unknown => result.fact.opaque = true,
                            }
                        }
                        result.fact.opaque |= summary.opaque;
                        result
                            .fact
                            .capture_dependencies
                            .extend(summary.capture_dependencies.iter().cloned());
                        result
                            .fact
                            .callable_dependencies
                            .extend(summary.callable_dependencies.iter().copied());
                        result.fact.complete &= summary.complete;
                        result.pending |= !summary.complete;
                    } else {
                        result.fact.complete = false;
                        result.pending = true;
                    }
                }
            }
            result.fact.complete &= !result.pending;
            if result.fact.opaque {
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
        }
        ValueExpr::New(callee, args, _) => {
            let callee = evaluate_receiver_value(callee, context, rules);
            result.pending = callee.pending;
            result.fact.complete = callee.fact.complete;
            result.fact.roots.extend(callee.fact.roots);
            result
                .fact
                .capture_dependencies
                .extend(callee.fact.capture_dependencies);
            result.fact.opaque = true;
            for argument in args.iter() {
                let argument = evaluate_receiver_value(argument, context, rules);
                result.pending |= argument.pending;
                result.fact.complete &= argument.fact.complete;
                result
                    .fact
                    .callable_dependencies
                    .extend(argument.fact.callable_dependencies);
                result
                    .fact
                    .capture_dependencies
                    .extend(argument.fact.capture_dependencies);
                result.fact.roots.extend(argument.fact.roots);
            }
            result.fact.complete &= !result.pending;
            let ordinary_refs: BTreeSet<_> = result
                .fact
                .roots
                .iter()
                .filter_map(|root| match root {
                    NativeReceiverRoot::OrdinaryDirect {
                        reference,
                        available_after,
                    }
                    | NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    } => Some((reference.clone(), *available_after)),
                    NativeReceiverRoot::Intrinsic(_)
                    | NativeReceiverRoot::IntrinsicPrototype(_)
                    | NativeReceiverRoot::IntrinsicDescendant(_)
                    | NativeReceiverRoot::ArrayIteratorPrototype
                    | NativeReceiverRoot::SetIteratorPrototype
                    | NativeReceiverRoot::BundleGlobal => None,
                })
                .collect();
            if !ordinary_refs.is_empty() {
                result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
                result.fact.roots.extend(ordinary_refs.into_iter().map(
                    |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    },
                ));
            }
        }
        ValueExpr::Unknown => {
            result.fact.opaque = true;
            result.fact.complete = true;
        }
    }
    result
}

fn read_callable_capture_dependencies(
    targets: &BTreeSet<u32>,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    fact: &mut ReceiverFlowFact,
) {
    for function in targets {
        fact.callable_dependencies.insert(*function);
        if let Some(summary) = context.read(ReceiverFlowKey::Callable(*function)) {
            fact.capture_dependencies
                .extend(summary.capture_dependencies.iter().cloned());
        }
    }
}

impl IterativeRules for ReceiverFlowRules<'_> {
    type Key = ReceiverFlowKey;
    type Fact = ReceiverFlowFact;
    type Rule = ReceiverFlowRule;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            ReceiverFlowRule::Assignment(index) => {
                let (target, value) = self.assignments[*index];
                if self.index.ordinary_input_refs.contains_key(target) {
                    return;
                }
                let evaluated = evaluate_receiver_value(value, context, self);
                let mut fact = evaluated.fact;
                fact.complete = !evaluated.pending;
                let ordinary_refs: BTreeSet<_> = fact
                    .roots
                    .iter()
                    .filter_map(|root| match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => Some((reference.clone(), *available_after)),
                        NativeReceiverRoot::Intrinsic(_)
                        | NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => None,
                    })
                    .collect();
                if let Some(copy_owner) = self.index.stable_top_level_copy_owner(target, value) {
                    fact.roots = fact
                        .roots
                        .into_iter()
                        .map(|root| match root {
                            NativeReceiverRoot::OrdinaryDirect {
                                reference,
                                available_after,
                            } if copy_owner > available_after => {
                                NativeReceiverRoot::OrdinaryDirect {
                                    reference,
                                    available_after: copy_owner,
                                }
                            }
                            NativeReceiverRoot::OrdinaryDirect { .. } => {
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            }
                            NativeReceiverRoot::OrdinaryDescendant {
                                reference,
                                available_after,
                            } if copy_owner > available_after => {
                                NativeReceiverRoot::OrdinaryDescendant {
                                    reference,
                                    available_after,
                                }
                            }
                            NativeReceiverRoot::OrdinaryDescendant { .. } => {
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            }
                            other => other,
                        })
                        .collect();
                } else if !ordinary_refs.is_empty() {
                    fact.roots.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                    fact.roots.extend(ordinary_refs.into_iter().map(
                        |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        },
                    ));
                }
                context.propose(ReceiverFlowKey::Assignment(*index), fact);
            }
            ReceiverFlowRule::Binding(binding) => {
                let mut fact = ReceiverFlowFact::default();
                let mut complete = true;
                let mut has_producer = false;
                if let Some(seed) = context.read(ReceiverFlowKey::BindingSeed(binding.clone())) {
                    fact.merge(seed.clone());
                    complete &= seed.complete;
                    has_producer = true;
                }
                if let Some(indices) = self.assignment_indices.get(binding) {
                    for index in indices {
                        if let Some(producer) = context.read(ReceiverFlowKey::Assignment(*index)) {
                            fact.merge(producer.clone());
                            complete &= producer.complete;
                            has_producer = true;
                        } else {
                            // Keep partial roots and capture dependencies from
                            // ready producers, but do not treat one completed
                            // assignment as proof that every reaching write is
                            // closed.
                            complete = false;
                        }
                    }
                }
                fact.complete = has_producer && complete;
                context.propose(ReceiverFlowKey::Binding(binding.clone()), fact);
            }
            ReceiverFlowRule::ReturnSite(function, site) => {
                let Some(callable) = self.index.callables.get(function) else {
                    return;
                };
                let Some(value) = callable.body_facts.value_flow().returns().get(*site) else {
                    return;
                };
                let evaluated = evaluate_receiver_value(value, context, self);
                let mut outcomes = BTreeSet::new();
                if evaluated.fact.safe {
                    outcomes.insert(CallableReturnOutcome::Safe);
                }
                for root in evaluated.fact.roots {
                    outcomes.insert(match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => CallableReturnOutcome::Descendant(reference, available_after),
                        NativeReceiverRoot::Intrinsic(root) => {
                            CallableReturnOutcome::Intrinsic(root)
                        }
                        // The captured-return summary has no exact native path
                        // guarantee. Keep non-constructor native returns opaque.
                        NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => CallableReturnOutcome::Unknown,
                    });
                }
                if evaluated.fact.opaque || !evaluated.fact.callable_targets.is_empty() {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                if outcomes.is_empty() && !evaluated.pending {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                context.propose(
                    ReceiverFlowKey::ReturnSite(*function, *site),
                    ReceiverFlowFact {
                        callable_dependencies: evaluated.fact.callable_dependencies,
                        capture_dependencies: evaluated.fact.capture_dependencies,
                        outcomes,
                        complete: !evaluated.pending,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
            ReceiverFlowRule::Callable(function) => {
                let Some(callable) = self.index.callables.get(function) else {
                    return;
                };
                let mut outcomes = BTreeSet::new();
                let mut callable_dependencies = BTreeSet::new();
                let mut capture_dependencies = callable
                    .reads
                    .iter()
                    .filter_map(|binding| {
                        self.index.ordinary_input_refs.get(binding).map(
                            |(reference, available_after)| (reference.clone(), *available_after),
                        )
                    })
                    .collect::<BTreeSet<_>>();
                let mut complete = true;
                if callable.generator || self.index.async_callables.contains(function) {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                } else if callable.body_facts.value_flow().returns().is_empty() {
                    outcomes.insert(CallableReturnOutcome::Safe);
                } else {
                    for site in 0..callable.body_facts.value_flow().returns().len() {
                        let Some(site_fact) =
                            context.read(ReceiverFlowKey::ReturnSite(*function, site))
                        else {
                            complete = false;
                            continue;
                        };
                        if site_fact.outcomes.is_empty() && !site_fact.complete {
                            complete = false;
                        }
                        outcomes.extend(site_fact.outcomes.iter().cloned());
                        callable_dependencies
                            .extend(site_fact.callable_dependencies.iter().copied());
                        capture_dependencies.extend(site_fact.capture_dependencies.iter().cloned());
                        complete &= site_fact.complete;
                    }
                    if callable.may_fall_through {
                        outcomes.insert(CallableReturnOutcome::Safe);
                    }
                }
                // Dependencies in returned call expressions are read from the
                // same worklist. Their capture facts wake wrappers and aliases
                // as summaries become available, including recursive SCCs.
                for dependency in &callable_dependencies {
                    if let Some(summary) = context.read(ReceiverFlowKey::Callable(*dependency)) {
                        capture_dependencies.extend(summary.capture_dependencies.iter().cloned());
                    }
                }
                if outcomes.is_empty() && complete {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                context.propose(
                    ReceiverFlowKey::Callable(*function),
                    ReceiverFlowFact {
                        callable_dependencies,
                        capture_dependencies,
                        outcomes,
                        opaque: callable.generator || self.index.async_callables.contains(function),
                        complete,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        current.merge(contribution)
    }
}

struct ReceiverFlowClosure {
    bindings: BTreeMap<Id, NativeReceiverRoots>,
    callable_returns: CallableReturnInputs,
    facts: BTreeMap<ReceiverFlowKey, ReceiverFlowFact>,
}

fn solve_receiver_flow(
    index: &ExecutionIndex,
    assignments: &[&(Id, ValueExpr)],
    flow: Option<&ChunkValueFlow>,
    unresolved_callable_fallbacks: &BTreeMap<u32, ReceiverFlowFact>,
) -> BTreeMap<ReceiverFlowKey, ReceiverFlowFact> {
    let mut producer_bindings = assignments
        .iter()
        .map(|(binding, _)| binding.clone())
        .chain(index.hoisted_bindings.keys().cloned())
        .chain(index.property_key_declarations.iter().cloned())
        .collect::<BTreeSet<_>>();
    producer_bindings.extend(index.ordinary_input_refs.keys().cloned());
    let binding_rules = producer_bindings
        .iter()
        .cloned()
        .map(ReceiverFlowRule::Binding)
        .collect::<Vec<_>>();
    let mut assignment_indices = BTreeMap::<Id, BTreeSet<usize>>::new();
    for (assignment_index, (binding, _)) in assignments.iter().enumerate() {
        if !index.ordinary_input_refs.contains_key(binding) {
            assignment_indices
                .entry(binding.clone())
                .or_default()
                .insert(assignment_index);
        }
    }
    let declared = index.declared_bindings();
    let rules = ReceiverFlowRules {
        assignments,
        assignment_indices: &assignment_indices,
        index,
        declared: &declared,
        pending_bindings: &producer_bindings,
    };
    let mut inference = IterativeInference::new(rules);
    let mut producers = (0..assignments.len())
        .map(ReceiverFlowRule::Assignment)
        .collect::<Vec<_>>();
    producers.extend(binding_rules);
    for (function, callable) in &index.callables {
        producers.extend(
            (0..callable.body_facts.value_flow().returns().len())
                .map(|site| ReceiverFlowRule::ReturnSite(*function, site)),
        );
        producers.push(ReceiverFlowRule::Callable(*function));
    }
    for input in &index.resolved_value_inputs {
        inference.seed(
            ReceiverFlowKey::BindingSeed(input.binding.clone()),
            ReceiverFlowFact {
                roots: BTreeSet::from([NativeReceiverRoot::OrdinaryDirect {
                    reference: input.evidence.reference.clone(),
                    available_after: input.initializer_ordinal,
                }]),
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    for (binding, function) in &index.hoisted_bindings {
        inference.seed(
            ReceiverFlowKey::BindingSeed(binding.clone()),
            ReceiverFlowFact {
                callable_targets: BTreeSet::from([*function]),
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    if let Some(flow) = flow {
        for binding in &index.property_key_declarations {
            let values = flow
                .values_of_binding(binding)
                .into_iter()
                .collect::<Vec<_>>();
            if let [Value::Alloc(Kind::Function, function)] = values.as_slice() {
                inference.seed(
                    ReceiverFlowKey::BindingSeed(binding.clone()),
                    ReceiverFlowFact {
                        callable_targets: BTreeSet::from([*function]),
                        complete: true,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
        }
    }
    for (function, fallback) in unresolved_callable_fallbacks {
        inference.seed(ReceiverFlowKey::Callable(*function), fallback.clone());
    }
    inference.solve_values(producers)
}

fn close_receiver_flow(
    index: &ExecutionIndex,
    assignments: &[&(Id, ValueExpr)],
    flow: Option<&ChunkValueFlow>,
    raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
) -> ReceiverFlowClosure {
    let initial_facts = solve_receiver_flow(index, assignments, flow, &BTreeMap::new());
    let selected_bindings = index
        .resolved_value_inputs
        .iter()
        .map(|input| input.binding.clone())
        .collect::<BTreeSet<_>>();
    let selected_refs = index
        .resolved_value_inputs
        .iter()
        .map(|input| input.evidence.reference.clone())
        .collect::<BTreeSet<_>>();
    let is_capture_connected = |fact: &ReceiverFlowFact| {
        fact.capture_dependencies
            .iter()
            .any(|(reference, _)| selected_refs.contains(reference))
    };
    // Capture connectivity is propagated by ReceiverFlowRules in the same
    // worklist as aliases and return summaries. Unrelated opaque helpers never
    // enter this projection or the conservative fallback below.
    let mut unresolved_callable_fallbacks = BTreeMap::new();
    for (function, callable) in &index.callables {
        let existing = initial_facts.get(&ReceiverFlowKey::Callable(*function));
        if !existing.is_some_and(is_capture_connected) {
            continue;
        }
        if existing.is_some_and(|fact| fact.complete) {
            continue;
        }
        // The first solve has reached quiescence. An absent or incomplete
        // capture-connected summary means its return flow is unresolved
        // (including a recursive bottom cycle), never a safe primitive.
        // Widen that same rule domain with an opaque outcome, retain partial
        // descendants/dependencies, and rerun all binding/callable watchers.
        let mut outcomes = BTreeSet::from([CallableReturnOutcome::Unknown]);
        let mut callable_dependencies = BTreeSet::new();
        let mut capture_dependencies = BTreeSet::new();
        if let Some(fact) = existing {
            outcomes.extend(fact.outcomes.iter().cloned());
            callable_dependencies.extend(fact.callable_dependencies.iter().copied());
            capture_dependencies.extend(fact.capture_dependencies.iter().cloned());
        }
        for site in 0..callable.body_facts.value_flow().returns().len() {
            if let Some(site_fact) =
                initial_facts.get(&ReceiverFlowKey::ReturnSite(*function, site))
            {
                outcomes.extend(site_fact.outcomes.iter().cloned());
                callable_dependencies.extend(site_fact.callable_dependencies.iter().copied());
                capture_dependencies.extend(site_fact.capture_dependencies.iter().cloned());
            }
        }
        unresolved_callable_fallbacks.insert(
            *function,
            ReceiverFlowFact {
                callable_dependencies,
                capture_dependencies,
                outcomes,
                opaque: true,
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    let facts = if unresolved_callable_fallbacks.is_empty() {
        initial_facts
    } else {
        solve_receiver_flow(index, assignments, flow, &unresolved_callable_fallbacks)
    };
    let bindings = facts
        .iter()
        .filter_map(|(key, fact)| match key {
            ReceiverFlowKey::Binding(binding) => {
                let mut roots = raw_aliases.get(binding).cloned().unwrap_or_default();
                let refined_roots = captured_roots(&fact.roots, &selected_refs);
                if selected_bindings.contains(binding) {
                    roots.clear();
                } else {
                    let has_refined_direct_identity = refined_roots.iter().any(|root| {
                        matches!(
                            root,
                            NativeReceiverRoot::OrdinaryDirect { reference, .. }
                                if selected_refs.contains(reference)
                        )
                    });
                    let stable_direct_copy = has_refined_direct_identity
                        && assignments.iter().any(|assignment| {
                            let (target, value) = *assignment;
                            target == binding
                                && index.stable_top_level_copy_owner(target, value).is_some()
                        });
                    if stable_direct_copy {
                        // The raw source-only alias records the original
                        // unknown-global possibility. A stable copy after the
                        // selected initializer is the same ordinary object, so
                        // retain its OrdinaryDirect timing fact without also
                        // charging the discharged raw root. Pre-capture copies
                        // and every descendant projection lack this proof.
                        roots.retain(|root| {
                            !matches!(
                                root,
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            )
                        });
                    }
                }
                roots.extend(refined_roots);
                (!roots.is_empty()).then_some((binding.clone(), roots))
            }
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    let callable_roots = facts
        .iter()
        .filter_map(|(key, fact)| {
            let ReceiverFlowKey::Callable(function) = key else {
                return None;
            };
            if !is_capture_connected(fact) {
                return None;
            }
            let roots = outcomes_to_receiver_roots(&fact.outcomes, &fact.capture_dependencies);
            Some((*function, roots))
        })
        .collect::<BTreeMap<_, _>>();
    let by_origin = callable_roots.clone();
    let by_binding = facts
        .iter()
        .filter_map(|(key, fact)| {
            let ReceiverFlowKey::Binding(binding) = key else {
                return None;
            };
            let roots = fact
                .callable_targets
                .iter()
                .filter_map(|function| callable_roots.get(function))
                .flatten()
                .cloned()
                .collect::<NativeReceiverRoots>();
            (!roots.is_empty()).then_some((binding.clone(), roots))
        })
        .collect();
    ReceiverFlowClosure {
        bindings,
        callable_returns: CallableReturnInputs {
            by_binding,
            by_origin,
        },
        facts,
    }
}

fn captured_roots(
    roots: &NativeReceiverRoots,
    selected_refs: &BTreeSet<ValueInputRef>,
) -> NativeReceiverRoots {
    let has_selected_ref = roots.iter().any(|root| match root {
        NativeReceiverRoot::OrdinaryDirect { reference, .. }
        | NativeReceiverRoot::OrdinaryDescendant { reference, .. } => {
            selected_refs.contains(reference)
        }
        NativeReceiverRoot::Intrinsic(_)
        | NativeReceiverRoot::IntrinsicPrototype(_)
        | NativeReceiverRoot::IntrinsicDescendant(_)
        | NativeReceiverRoot::ArrayIteratorPrototype
        | NativeReceiverRoot::SetIteratorPrototype
        | NativeReceiverRoot::BundleGlobal => false,
    });
    roots
        .iter()
        .filter(|root| match root {
            NativeReceiverRoot::OrdinaryDirect { reference, .. }
            | NativeReceiverRoot::OrdinaryDescendant { reference, .. } => {
                selected_refs.contains(reference)
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => has_selected_ref,
            NativeReceiverRoot::Intrinsic(_)
            | NativeReceiverRoot::IntrinsicPrototype(_)
            | NativeReceiverRoot::IntrinsicDescendant(_)
            | NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype
            | NativeReceiverRoot::BundleGlobal => false,
        })
        .cloned()
        .collect()
}

fn outcomes_to_receiver_roots(
    outcomes: &BTreeSet<CallableReturnOutcome>,
    capture_dependencies: &BTreeSet<(ValueInputRef, SourceUnitOrdinal)>,
) -> NativeReceiverRoots {
    let mut roots = outcomes
        .iter()
        .filter_map(|outcome| match outcome {
            CallableReturnOutcome::Safe => None,
            CallableReturnOutcome::Descendant(reference, available_after) => {
                Some(NativeReceiverRoot::OrdinaryDescendant {
                    reference: reference.clone(),
                    available_after: *available_after,
                })
            }
            CallableReturnOutcome::Intrinsic(root) => Some(NativeReceiverRoot::Intrinsic(*root)),
            CallableReturnOutcome::Unknown => Some(NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::UnknownGlobalValue,
            )),
        })
        .collect::<NativeReceiverRoots>();
    if outcomes.contains(&CallableReturnOutcome::Unknown) {
        roots.extend(
            capture_dependencies
                .iter()
                .map(
                    |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                        reference: reference.clone(),
                        available_after: *available_after,
                    },
                ),
        );
    }
    roots
}

/// Property-key facts affect only the native write census. They make no claim
/// about evaluating the key, calling a factory, or consuming its return value.
#[derive(Debug, Clone)]
enum PropertyKeyExpr {
    String(String),
    /// A canonical array index from a numeric literal: a key, not a string
    /// operand of `+`.
    Index(String),
    /// An untagged template with substitutions: a string between its first
    /// and last literal parts.
    Template {
        prefix: String,
        suffix: String,
    },
    /// `left + right`.
    Concat(Box<PropertyKeyExpr>, Box<PropertyKeyExpr>),
    SymbolIterator(Id),
    Binding(Id),
    SymbolFor(Id),
    EntriesKeys(Id, BTreeSet<String>),
    Unknown,
}

impl PropertyKeyExpr {
    fn from_expr(expression: &Expr) -> Self {
        match expression.unwrap_parens() {
            Expr::Lit(Lit::Str(value)) => Self::String(value.value.to_string_lossy().into_owned()),
            Expr::Lit(Lit::Num(value))
                if value.value >= 0.0
                    && value.value <= u32::MAX as f64
                    && value.value.fract() == 0.0 =>
            {
                Self::Index((value.value as u32).to_string())
            }
            Expr::Tpl(template) => {
                let cooked = |index: usize| {
                    template.quasis[index]
                        .cooked
                        .as_ref()
                        .map(|part| part.to_string_lossy().into_owned())
                };
                let (Some(prefix), Some(suffix)) = (cooked(0), cooked(template.quasis.len() - 1))
                else {
                    return Self::Unknown;
                };
                if template.exprs.is_empty() {
                    Self::String(prefix)
                } else {
                    Self::Template { prefix, suffix }
                }
            }
            Expr::Bin(binary) if binary.op == BinaryOp::Add => Self::Concat(
                Box::new(Self::from_expr(&binary.left)),
                Box::new(Self::from_expr(&binary.right)),
            ),
            // Simple assignment returns its RHS, independent of an inherited
            // setter's return value. Keep coercive and compound forms opaque.
            Expr::Assign(assignment)
                if assignment.op == AssignOp::Assign
                    && matches!(assignment.right.unwrap_parens(), Expr::Lit(_)) =>
            {
                Self::from_expr(&assignment.right)
            }
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Member(member)
                if static_prop(&member.prop).as_deref() == Some("iterator")
                    && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol") =>
            {
                let Expr::Ident(symbol) = member.obj.unwrap_parens() else {
                    unreachable!()
                };
                Self::SymbolIterator(symbol.to_id())
            }
            // `Symbol.for(value)` returns a Symbol whatever its argument
            // converts to; a conversion that throws leaves no key.
            Expr::Call(call) if call.args.len() == 1 && call.args[0].spread.is_none() => {
                match &call.callee {
                    Callee::Expr(callee) => match callee.unwrap_parens() {
                        Expr::Member(member)
                            if static_prop(&member.prop).as_deref() == Some("for") =>
                        {
                            match member.obj.unwrap_parens() {
                                Expr::Ident(ident) if ident.sym == "Symbol" => {
                                    Self::SymbolFor(ident.to_id())
                                }
                                _ => Self::Unknown,
                            }
                        }
                        _ => Self::Unknown,
                    },
                    _ => Self::Unknown,
                }
            }
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone)]
enum PropertyKeyDomain {
    FiniteStrings(BTreeSet<String>),
    /// Strings with this constant prefix and suffix, not both empty.
    Affixed {
        prefix: String,
        suffix: String,
    },
    SymbolIterator,
    Symbol,
    Unknown,
}

#[derive(Debug, Clone)]
struct PropertyKeyEvidence {
    domain: PropertyKeyDomain,
    dependencies: IntrinsicRoots,
}

impl PropertyKeyEvidence {
    fn unknown() -> Self {
        Self {
            domain: PropertyKeyDomain::Unknown,
            dependencies: BTreeSet::new(),
        }
    }
}

fn property_key_evidence<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    declared: &BTreeSet<Id>,
) -> PropertyKeyEvidence {
    let mut expression = expression;
    let mut seen = BTreeSet::new();
    loop {
        match expression {
            PropertyKeyExpr::Binding(binding) => {
                if !seen.insert(binding) {
                    return PropertyKeyEvidence::unknown();
                }
                match assignments.get(binding).map(Vec::as_slice) {
                    Some([value]) => expression = value,
                    _ => return PropertyKeyEvidence::unknown(),
                }
            }
            PropertyKeyExpr::String(key) | PropertyKeyExpr::Index(key) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::FiniteStrings(BTreeSet::from([key.clone()])),
                    dependencies: BTreeSet::new(),
                };
            }
            PropertyKeyExpr::Template { .. } | PropertyKeyExpr::Concat(..) => {
                let Some((prefix, suffix)) = string_affixes(expression, assignments, &mut seen)
                else {
                    return PropertyKeyEvidence::unknown();
                };
                if prefix.is_empty() && suffix.is_empty() {
                    return PropertyKeyEvidence::unknown();
                }
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::Affixed { prefix, suffix },
                    dependencies: BTreeSet::new(),
                };
            }
            PropertyKeyExpr::SymbolIterator(namespace) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::SymbolIterator,
                    dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
                };
            }
            PropertyKeyExpr::SymbolFor(namespace) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::Symbol,
                    dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
                };
            }
            PropertyKeyExpr::EntriesKeys(namespace, keys) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::FiniteStrings(keys.clone()),
                    // Both the outer entries array and each destructured pair use
                    // the Array iterator, so the premise covers both protocols.
                    dependencies: BTreeSet::from([NativeIntrinsic::Object, NativeIntrinsic::Array]),
                };
            }
            _ => return PropertyKeyEvidence::unknown(),
        }
    }
}

/// The constant prefix and suffix of a string-valued expression, or `None`
/// when it may not be a string. A template is a string; `left + right` is a
/// string concatenation when either operand is, whose result starts with a
/// string left operand's prefix and ends with a string right operand's suffix
/// (the other operand's ToPrimitive/ToString may run user code but still
/// yields the concatenation, or throws before any key exists).
fn string_affixes<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    seen: &mut BTreeSet<&'a Id>,
) -> Option<(String, String)> {
    match expression {
        PropertyKeyExpr::String(value) => Some((value.clone(), value.clone())),
        PropertyKeyExpr::Template { prefix, suffix } => Some((prefix.clone(), suffix.clone())),
        PropertyKeyExpr::Binding(binding) if seen.insert(binding) => {
            let affixes = match assignments.get(binding).map(Vec::as_slice) {
                Some([value]) => string_affixes(value, assignments, seen),
                _ => None,
            };
            seen.remove(binding);
            affixes
        }
        PropertyKeyExpr::Concat(left, right) => {
            let left = string_affixes(left, assignments, seen);
            let right = string_affixes(right, assignments, seen);
            if left.is_none() && right.is_none() {
                return None;
            }
            Some((
                left.map(|(prefix, _)| prefix).unwrap_or_default(),
                right.map(|(_, suffix)| suffix).unwrap_or_default(),
            ))
        }
        _ => None,
    }
}

#[derive(Debug)]
pub(super) struct IntrinsicWriteTarget {
    owner: SourceUnitOrdinal,
    activation_callable: Option<u32>,
    /// The constructor whose `this` a direct member operation writes.
    /// Destructuring and reflective calls stay opaque.
    constructor_this: Option<u32>,
    span: Span,
    operation: NativeMutationOperation,
    pub(super) receiver: ValueExpr,
    key: Option<PropertyKeyExpr>,
    additional_keys: Vec<PropertyKeyExpr>,
    /// The value an assignment stores; `None` for every other operation.
    value: Option<ValueExpr>,
}

/// Conservative effects activated before a top-level item or callable body.
/// This deliberately has no within-body ordering: all of a body's writes
/// precede its first modeled native guard.
#[derive(Clone, Default)]
pub(super) struct NativePointEffects {
    pub(super) roots: IntrinsicRoots,
    prototype_site_count: usize,
    prototype_any_key: usize,
    prototype_known_keys: BTreeMap<String, usize>,
}

impl NativePointEffects {
    pub(super) fn extend(&mut self, other: &Self) {
        self.roots.extend(&other.roots);
        self.prototype_site_count += other.prototype_site_count;
        self.prototype_any_key += other.prototype_any_key;
        for (property, count) in &other.prototype_known_keys {
            *self
                .prototype_known_keys
                .entry(property.clone())
                .or_default() += count;
        }
    }

    fn add_prototype_mutation(&mut self, mutation: &NativePrototypeMutation) {
        self.prototype_site_count += 1;
        if let Some(properties) = &mutation.properties {
            for property in properties {
                *self
                    .prototype_known_keys
                    .entry(property.clone())
                    .or_default() += 1;
            }
        } else {
            self.prototype_any_key += 1;
        }
    }

    pub(super) fn blocking_prototype_mutations(&self, property: Option<&str>) -> usize {
        match property {
            Some(property) => {
                self.prototype_any_key
                    + self
                        .prototype_known_keys
                        .get(property)
                        .copied()
                        .unwrap_or(0)
            }
            None => self.prototype_site_count,
        }
    }
}

fn intrinsic_write_target(
    member: &MemberExpr,
    owner: SourceUnitOrdinal,
    receiver_callable: Option<u32>,
    activation_callable: Option<u32>,
    operation: NativeMutationOperation,
) -> IntrinsicWriteTarget {
    let key = match &member.prop {
        MemberProp::Ident(property) => PropertyKeyExpr::String(property.sym.to_string()),
        MemberProp::Computed(property) => PropertyKeyExpr::from_expr(&property.expr),
        _ => PropertyKeyExpr::Unknown,
    };
    IntrinsicWriteTarget {
        owner,
        activation_callable,
        constructor_this: receiver_callable.filter(|_| {
            matches!(
                binding_targets::transparent_target_expr(&member.obj),
                Expr::This(_)
            )
        }),
        span: member.span,
        operation,
        receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(&member.obj)),
        key: Some(key),
        additional_keys: Vec::new(),
        value: None,
    }
}

#[derive(Clone, Default)]
pub(crate) struct NativeWriteCensus {
    unconditional: IntrinsicRoots,
    /// Host claims a receiver discharge consumed.
    host_behavior: BTreeSet<HostBehaviorKey>,
    prototype_mutations: Arc<Vec<NativePrototypeMutation>>,
    witnesses: Arc<Vec<NativeMutationWitness>>,
    conditional: Vec<ConditionalIntrinsicWrite>,
}

/// A premise-relative view used only to decide whether a selected Array-spread
/// input is contradicted by a source-known iterator protocol write. Its root
/// closure never feeds ordinary native-integrity inference.
pub(crate) struct ArraySpreadProtocolIntegrity {
    mutated: IntrinsicRoots,
    global_array_binding_mutated: bool,
    premise_authorized_next_writes: BTreeSet<ArrayIteratorNextWriteId>,
}

impl NativeWriteCensus {
    fn for_array_spread_protocol_query(&self) -> (Self, BTreeSet<ArrayIteratorNextWriteId>) {
        let mut census = self.clone();
        let mut premise_authorized_next_writes = BTreeSet::new();
        for write in &mut census.conditional {
            if !write.dependencies.is_empty()
                || !exact_array_iterator_next_candidate(&write.witness)
            {
                continue;
            }
            // The typed Array-spread premise identifies only the iterator
            // receiver. Keep the original fallback for a failed independent
            // Object/Symbol proof, and retain the Array effect so other
            // conditional writes still see this mutation in the fixed point.
            write.refined = IntrinsicRoots::from([NativeIntrinsic::Array]);
            write.dependencies =
                IntrinsicRoots::from([NativeIntrinsic::Object, NativeIntrinsic::Symbol]);
            premise_authorized_next_writes.insert(array_iterator_next_write_id(&write.witness));
        }
        (census, premise_authorized_next_writes)
    }
}

#[derive(Clone)]
struct ConditionalIntrinsicWrite {
    witness: NativeMutationWitness,
    refined: IntrinsicRoots,
    fallback: IntrinsicRoots,
    dependencies: IntrinsicRoots,
}

struct NativeIntegrityRootRules<'a> {
    writes: Vec<&'a ConditionalIntrinsicWrite>,
}

impl IterativeRules for NativeIntegrityRootRules<'_> {
    type Key = ();
    type Fact = IntrinsicRoots;
    type Rule = usize;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let write = self.writes[*rule];
        let invalid = {
            let Some(mutated) = context.read(()) else {
                return;
            };
            mutated.contains(&NativeIntrinsic::GlobalObject)
                || !write.dependencies.is_disjoint(mutated)
        };
        context.propose(
            (),
            if invalid {
                write.fallback.clone()
            } else {
                write.refined.clone()
            },
        );
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

/// A11 supplies the initial native identities. Every retained write either
/// preserves those identities under its operation premises or invalidates the
/// affected families. Propagate invalidation to a fixed point, including writes
/// in deferred bodies. This is a closed-write invariant, not self-certification
/// from the absence of a fact produced by the same candidate.
pub(crate) fn resolve_native_integrity<'a>(
    censuses: impl IntoIterator<Item = &'a NativeWriteCensus>,
) -> NativeIntegrity {
    let censuses: Vec<_> = censuses.into_iter().collect();
    let mutated = resolve_native_integrity_roots(&censuses);
    let mut witnesses: Vec<_> = censuses
        .iter()
        .flat_map(|census| census.witnesses.iter().cloned())
        .collect();
    for write in censuses.iter().flat_map(|census| &census.conditional) {
        let mut witness = write.witness.clone();
        if witness.unreachable_branch.is_some() {
            witness.affected_roots.clear();
            witness.value_input_refs.clear();
            witness.value_input_evidence.clear();
            witnesses.push(witness);
            continue;
        }
        let invalid = mutated.contains(&NativeIntrinsic::GlobalObject)
            || !write.dependencies.is_disjoint(&mutated);
        witness.affected_roots = if invalid {
            write.fallback.clone()
        } else {
            write.refined.clone()
        };
        if invalid && write.refined != write.fallback {
            witness.invalidated_key_dependencies =
                write.dependencies.intersection(&mutated).copied().collect();
            if mutated.contains(&NativeIntrinsic::GlobalObject) {
                witness
                    .invalidated_key_dependencies
                    .insert(NativeIntrinsic::GlobalObject);
            }
        }
        if !witness.affected_roots.is_empty() || !witness.value_input_refs.is_empty() {
            witnesses.push(witness);
        }
    }
    let mut consumed_value_inputs = BTreeSet::new();
    let mut value_input_evidence = BTreeMap::new();
    for witness in &witnesses {
        if witness.unreachable_branch.is_some() {
            continue;
        }
        consumed_value_inputs.extend(witness.value_input_refs.iter().cloned());
        value_input_evidence.extend(
            witness
                .value_input_evidence
                .iter()
                .cloned()
                .map(|evidence| (evidence.reference.clone(), evidence)),
        );
    }
    NativeIntegrity {
        prototype_mutations: Arc::new(
            censuses
                .iter()
                .flat_map(|census| census.prototype_mutations.iter().cloned())
                .map(|mut mutation| {
                    if !mutation.key_dependencies.is_disjoint(&mutated)
                        || (!mutation.key_dependencies.is_empty()
                            && mutated.contains(&NativeIntrinsic::GlobalObject))
                    {
                        mutation.properties = None;
                        if mutated.contains(&NativeIntrinsic::GlobalObject) {
                            mutation
                                .key_dependencies
                                .insert(NativeIntrinsic::GlobalObject);
                        }
                    }
                    mutation
                })
                .collect(),
        ),
        mutated,
        witnesses: Arc::new(witnesses),
        value_input_evidence,
        consumed_value_inputs,
        consumed_host_behavior: censuses
            .iter()
            .flat_map(|census| census.host_behavior.iter().copied())
            .collect(),
        never_written: BTreeSet::new(),
        never_written_overrides: Vec::new(),
    }
}

/// Applies `program_premises.intrinsics_never_written`: the named families'
/// slots hold whatever the census witnesses, so they leave `mutated` and each
/// witness reaching them joins the audit list. A witness writing such a family
/// on its exact object refutes the premise; every such site is returned.
pub(crate) fn apply_intrinsics_never_written(
    integrity: &mut NativeIntegrity,
    families: &BTreeSet<NativeIntrinsic>,
) -> std::result::Result<(), Vec<String>> {
    use crate::reports::schema::{NeverWrittenOverride, NeverWrittenReach};
    let exact_family = |slot: &NativeWriteSlot| match &slot.receiver {
        NativeReceiverPath::Constructor(family) | NativeReceiverPath::Prototype(family) => {
            families.contains(family).then_some(*family)
        }
        NativeReceiverPath::ArrayIteratorPrototype => families
            .contains(&NativeIntrinsic::Array)
            .then_some(NativeIntrinsic::Array),
        NativeReceiverPath::SetIteratorPrototype => families
            .contains(&NativeIntrinsic::Set)
            .then_some(NativeIntrinsic::Set),
        NativeReceiverPath::GlobalObject => families.iter().copied().find(|family| {
            matches!(&slot.key, NativePropertyKey::String(key)
                if family.global_binding() == Some(key.as_str()))
        }),
        NativeReceiverPath::Descendant(_) | NativeReceiverPath::Unknown => None,
    };
    let mut refutations = Vec::new();
    let mut overrides = Vec::new();
    for (index, witness) in integrity.witnesses.iter().enumerate() {
        if witness.unreachable_branch.is_some() {
            continue;
        }
        let location = || {
            format!(
                "{}{}",
                witness.chunk_id,
                witness
                    .source_location
                    .as_ref()
                    .map_or(String::new(), |location| format!(
                        ":{}:{}",
                        location.start_line,
                        location.start_column.unwrap_or_default()
                    ))
            )
        };
        let exact: BTreeSet<_> = if witness.operation == NativeMutationOperation::BindingAssignment
        {
            witness
                .affected_roots
                .intersection(families)
                .copied()
                .collect()
        } else {
            witness
                .target_slots
                .iter()
                .filter_map(exact_family)
                .collect()
        };
        if !exact.is_empty() {
            refutations.push(format!(
                "{} writes {exact:?} on its exact object",
                location()
            ));
            continue;
        }
        if witness.affected_roots.is_empty() {
            continue;
        }
        let unknown_receiver = witness
            .target_slots
            .iter()
            .any(|slot| slot.receiver == NativeReceiverPath::Unknown)
            || witness.target_slots.is_empty();
        let global_object = witness
            .affected_roots
            .contains(&NativeIntrinsic::GlobalObject);
        let (reach, reached) = if unknown_receiver {
            (NeverWrittenReach::UnknownReceiver, families.clone())
        } else if global_object {
            (NeverWrittenReach::GlobalObject, families.clone())
        } else {
            (
                NeverWrittenReach::Family,
                witness
                    .affected_roots
                    .intersection(families)
                    .copied()
                    .collect(),
            )
        };
        if reached.is_empty() {
            continue;
        }
        overrides.push(NeverWrittenOverride {
            witness: index,
            chunk_id: witness.chunk_id.clone(),
            source_location: witness.source_location.clone(),
            families: reached,
            reach,
        });
    }
    if !refutations.is_empty() {
        return Err(refutations);
    }
    integrity.mutated.retain(|root| !families.contains(root));
    integrity.never_written = families.clone();
    integrity.never_written_overrides = overrides;
    Ok(())
}

fn resolve_native_integrity_roots(censuses: &[&NativeWriteCensus]) -> IntrinsicRoots {
    let mutated: IntrinsicRoots = censuses
        .iter()
        .flat_map(|census| census.unconditional.iter().copied())
        .collect();
    let rules = NativeIntegrityRootRules {
        writes: censuses
            .iter()
            .flat_map(|census| &census.conditional)
            .filter(|write| write.witness.unreachable_branch.is_none())
            .collect(),
    };
    let producer_count = rules.writes.len();
    let mut inference = IterativeInference::new(rules);
    inference.seed((), mutated);
    inference
        .solve(0..producer_count)
        .into_values()
        .remove(&())
        .expect("unconditional native roots seed the shared closure fact")
}

fn source_global_array_binding_write(censuses: &[&NativeWriteCensus]) -> bool {
    let slot = NativeWriteSlot {
        receiver: NativeReceiverPath::GlobalObject,
        key: NativePropertyKey::String("Array".to_owned()),
    };
    censuses.iter().any(|census| {
        census
            .witnesses
            .iter()
            .chain(census.conditional.iter().map(|write| &write.witness))
            .any(|witness| {
                witness.unreachable_branch.is_none() && witness.target_slots.contains(&slot)
            })
    })
}

pub(crate) fn resolve_array_spread_protocol_integrity<'a>(
    censuses: impl IntoIterator<Item = &'a NativeWriteCensus>,
) -> ArraySpreadProtocolIntegrity {
    let source_censuses: Vec<_> = censuses.into_iter().collect();
    let global_array_binding_mutated = source_global_array_binding_write(&source_censuses);
    let mut premise_authorized_next_writes = BTreeSet::new();
    let query_censuses: Vec<_> = source_censuses
        .iter()
        .map(|census| {
            let (query, authorized) = census.for_array_spread_protocol_query();
            premise_authorized_next_writes.extend(authorized);
            query
        })
        .collect();
    let query_refs: Vec<_> = query_censuses.iter().collect();
    ArraySpreadProtocolIntegrity {
        mutated: resolve_native_integrity_roots(&query_refs),
        global_array_binding_mutated,
        premise_authorized_next_writes,
    }
}

#[cfg(test)]
mod native_integrity_closure_tests {
    use super::{
        ArraySpreadNativeIteratorPremise, ConditionalIntrinsicWrite, IntrinsicRoots,
        NativeSlotOverlap, NativeWriteCensus, array_spread_protocol_slot_overlap,
        resolve_array_spread_protocol_integrity, resolve_native_integrity, source_slot_overlap,
    };
    use crate::analysis_inputs::{
        CallbackFactoryEvidence, CallbackObjectArguments, NativeIntrinsic,
    };
    use crate::reports::schema::{
        NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativeReceiverPath,
        NativeUnreachableBranch, NativeWriteSlot,
    };
    use source_identity::SourceUnitOrdinal;
    use std::collections::BTreeSet;
    use swc_common::DUMMY_SP;

    fn conditional_write(
        ordinal: usize,
        dependencies: IntrinsicRoots,
        refined: IntrinsicRoots,
        fallback: IntrinsicRoots,
        unreachable: bool,
    ) -> ConditionalIntrinsicWrite {
        ConditionalIntrinsicWrite {
            witness: NativeMutationWitness {
                chunk_id: "test".to_owned(),
                statement_ordinal: SourceUnitOrdinal(ordinal),
                source_location: None,
                operation: NativeMutationOperation::PropertyAssignment,
                receiver_roots: BTreeSet::new(),
                through_local_alias: false,
                receiver_global: None,
                affected_roots: BTreeSet::new(),
                target_slots: BTreeSet::new(),
                key_dependencies: BTreeSet::new(),
                invalidated_key_dependencies: BTreeSet::new(),
                value_input_refs: BTreeSet::new(),
                value_input_evidence: Vec::new(),
                unreachable_branch: unreachable.then(|| NativeUnreachableBranch {
                    arguments: CallbackObjectArguments {
                        wrapper: CallbackFactoryEvidence::AuthorAssertion {
                            defining_module: "test".to_owned(),
                            export: "wrapper".to_owned(),
                        },
                        object_arguments: BTreeSet::new(),
                    },
                    imported_local: "wrapper".to_owned(),
                    wrapper_call_location: None,
                    callback_location: None,
                    guard_location: None,
                    guard_span: DUMMY_SP,
                    wrapper_call_span: DUMMY_SP,
                    callback_span: DUMMY_SP,
                }),
                span: DUMMY_SP,
            },
            refined,
            fallback,
            dependencies,
        }
    }

    fn array_iterator_next_candidate(ordinal: usize) -> ConditionalIntrinsicWrite {
        let mut candidate = conditional_write(
            ordinal,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
            false,
        );
        candidate.witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        });
        candidate.witness.key_dependencies = IntrinsicRoots::from([
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Symbol,
        ]);
        candidate
    }

    #[test]
    fn slot_query_distinguishes_known_global_write_from_alias_or_key_uncertainty() {
        let target = NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::String("Symbol".to_owned()),
        };
        let mut witness = conditional_write(
            1,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        witness.target_slots.insert(target.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );

        witness.key_dependencies.insert(NativeIntrinsic::Symbol);
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Opaque
        );

        witness.key_dependencies.clear();
        witness.target_slots.clear();
        witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::Descendant(NativeIntrinsic::Set),
            key: NativePropertyKey::String("Symbol".to_owned()),
        });
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Opaque
        );

        let prototype_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Set),
            key: NativePropertyKey::String("add".to_owned()),
        };
        witness.target_slots.clear();
        witness.target_slots.insert(prototype_slot.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &prototype_slot),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );
        witness.target_slots.clear();
        witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::Descendant(NativeIntrinsic::Set),
            key: NativePropertyKey::String("add".to_owned()),
        });
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &prototype_slot),
            NativeSlotOverlap::Opaque
        );
        let wildcard = NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::Unknown,
        };
        witness.target_slots.clear();
        witness.target_slots.insert(wildcard.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &wildcard),
            NativeSlotOverlap::Opaque
        );
    }

    #[test]
    fn array_spread_slot_query_uses_only_independent_symbol_and_object_guards() {
        let premise = ArraySpreadNativeIteratorPremise;
        let query = |witness: &NativeMutationWitness,
                     slot: &NativeWriteSlot,
                     source_mutated: &IntrinsicRoots,
                     global_array_binding_mutated: bool,
                     authorize_next: bool| {
            let authorized = if authorize_next {
                BTreeSet::from([super::array_iterator_next_write_id(witness)])
            } else {
                BTreeSet::new()
            };
            array_spread_protocol_slot_overlap(
                std::iter::once(witness),
                slot,
                source_mutated,
                global_array_binding_mutated,
                &authorized,
                premise,
            )
        };
        let array_iterator_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
            key: NativePropertyKey::SymbolIterator,
        };
        let mut array_iterator_write = conditional_write(
            1,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        array_iterator_write
            .target_slots
            .insert(array_iterator_slot.clone());
        array_iterator_write
            .key_dependencies
            .insert(NativeIntrinsic::Symbol);
        assert_eq!(
            query(
                &array_iterator_write,
                &array_iterator_slot,
                &IntrinsicRoots::new(),
                false,
                false,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );
        for mutated in [
            IntrinsicRoots::from([NativeIntrinsic::Symbol]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
        ] {
            assert_eq!(
                query(
                    &array_iterator_write,
                    &array_iterator_slot,
                    &mutated,
                    false,
                    false,
                ),
                NativeSlotOverlap::Opaque
            );
        }
        assert_eq!(
            query(
                &array_iterator_write,
                &array_iterator_slot,
                &IntrinsicRoots::new(),
                true,
                false,
            ),
            NativeSlotOverlap::Opaque,
            "replacing global Array means Array.prototype no longer identifies the literal-array iterator"
        );

        let iterator_next_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        };
        let mut iterator_next_write = conditional_write(
            2,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        iterator_next_write
            .target_slots
            .insert(iterator_next_slot.clone());
        iterator_next_write.key_dependencies = IntrinsicRoots::from([
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Symbol,
        ]);
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                true,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(2))
        );
        // Array may be invalidated by this very next write; the typed
        // Array-spread premise supplies just this receiver identity. Object
        // and Symbol remain independent and a global-object mutation defeats
        // both slot queries.
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::from([NativeIntrinsic::Array]),
                false,
                true,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(2))
        );
        for mutated in [
            IntrinsicRoots::from([NativeIntrinsic::Object]),
            IntrinsicRoots::from([NativeIntrinsic::Symbol]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
        ] {
            assert_eq!(
                query(
                    &iterator_next_write,
                    &iterator_next_slot,
                    &mutated,
                    false,
                    true,
                ),
                NativeSlotOverlap::Opaque
            );
        }
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                false,
            ),
            NativeSlotOverlap::Opaque,
            "the typed premise cannot discharge a candidate not admitted by the query closure"
        );
        iterator_next_write
            .key_dependencies
            .insert(NativeIntrinsic::Math);
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                true,
            ),
            NativeSlotOverlap::Opaque
        );
    }

    #[test]
    fn array_spread_protocol_view_keeps_candidate_effect_and_fallback_dependencies() {
        let candidate = array_iterator_next_candidate(1);
        let census = NativeWriteCensus {
            conditional: vec![candidate],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&census]);
        assert_eq!(
            protocol.mutated,
            IntrinsicRoots::from([NativeIntrinsic::Array])
        );
        assert!(
            !protocol.global_array_binding_mutated,
            "an iterator-prototype write is not a global Array binding write"
        );

        for independent_mutation in [
            NativeIntrinsic::Object,
            NativeIntrinsic::Symbol,
            NativeIntrinsic::GlobalObject,
        ] {
            let mut contradicted = census.clone();
            contradicted.unconditional.insert(independent_mutation);
            let protocol = resolve_array_spread_protocol_integrity([&contradicted]);
            assert!(
                protocol.mutated.contains(&NativeIntrinsic::GlobalObject),
                "candidate fallback must remain active when {independent_mutation:?} is mutated"
            );
        }

        let mut array_dependent = conditional_write(
            2,
            IntrinsicRoots::from([NativeIntrinsic::Array]),
            IntrinsicRoots::from([NativeIntrinsic::Promise]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
            false,
        );
        array_dependent
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::String("values".to_owned()),
            });
        let with_dependent = NativeWriteCensus {
            conditional: vec![array_iterator_next_candidate(1), array_dependent],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&with_dependent]);
        assert!(protocol.mutated.contains(&NativeIntrinsic::Array));
        assert!(
            protocol.mutated.contains(&NativeIntrinsic::GlobalObject),
            "the candidate's Array effect must still invalidate dependent writes"
        );

        let mut input_dependent = array_iterator_next_candidate(3);
        input_dependent
            .witness
            .value_input_refs
            .insert(crate::analysis_inputs::ValueInputRef {
                defining_chunk: "test".to_owned(),
                input_index: 0,
            });
        let input_census = NativeWriteCensus {
            conditional: vec![input_dependent],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&input_census]);
        assert!(
            !protocol.mutated.contains(&NativeIntrinsic::Array),
            "author-conditioned candidate identity must not inherit the premise"
        );

        let mut computed_key_candidate = array_iterator_next_candidate(4);
        computed_key_candidate
            .dependencies
            .insert(NativeIntrinsic::Array);
        let original_refined = computed_key_candidate.refined.clone();
        let original_fallback = computed_key_candidate.fallback.clone();
        let computed_key_census = NativeWriteCensus {
            conditional: vec![computed_key_candidate],
            ..NativeWriteCensus::default()
        };
        let (query_census, authorized) = computed_key_census.for_array_spread_protocol_query();
        assert_eq!(
            query_census.conditional[0].dependencies,
            IntrinsicRoots::from([NativeIntrinsic::Array]),
            "the premise cannot remove an independently computed-key dependency"
        );
        assert_eq!(query_census.conditional[0].refined, original_refined);
        assert_eq!(query_census.conditional[0].fallback, original_fallback);
        let next_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        };
        assert_eq!(
            array_spread_protocol_slot_overlap(
                std::iter::once(&computed_key_census.conditional[0].witness),
                &next_slot,
                &IntrinsicRoots::new(),
                false,
                &authorized,
                ArraySpreadNativeIteratorPremise,
            ),
            NativeSlotOverlap::Opaque,
            "merged key and receiver dependencies cannot authorize this computed key"
        );

        let mut global_array_write = conditional_write(
            5,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        );
        global_array_write
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Array".to_owned()),
            });
        let mut array_prototype_write = conditional_write(
            6,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        );
        array_prototype_write
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            });
        array_prototype_write
            .witness
            .key_dependencies
            .insert(NativeIntrinsic::Symbol);
        let global_replacement_census = NativeWriteCensus {
            conditional: vec![global_array_write, array_prototype_write],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&global_replacement_census]);
        assert!(protocol.global_array_binding_mutated);
        let array_iterator_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
            key: NativePropertyKey::SymbolIterator,
        };
        assert_eq!(
            array_spread_protocol_slot_overlap(
                std::iter::once(&global_replacement_census.conditional[1].witness),
                &array_iterator_slot,
                &protocol.mutated,
                protocol.global_array_binding_mutated,
                &protocol.premise_authorized_next_writes,
                ArraySpreadNativeIteratorPremise,
            ),
            NativeSlotOverlap::Opaque,
            "Array.prototype writes do not identify literal-array iterators after global Array replacement"
        );
    }

    #[test]
    fn invalidation_watchers_revisit_writes_and_preserve_witnesses() {
        let mut census = NativeWriteCensus::default();
        census.unconditional.insert(NativeIntrinsic::Set);
        // The first producer refines while Function is absent, then must run
        // again after the later producer contributes Function.
        census.conditional.push(conditional_write(
            0,
            BTreeSet::from([NativeIntrinsic::Function]),
            BTreeSet::from([NativeIntrinsic::Math]),
            BTreeSet::from([NativeIntrinsic::Array]),
            false,
        ));
        census.conditional.push(conditional_write(
            1,
            BTreeSet::from([NativeIntrinsic::Set]),
            BTreeSet::from([NativeIntrinsic::Promise]),
            BTreeSet::from([NativeIntrinsic::Function]),
            false,
        ));
        census.conditional.push(conditional_write(
            2,
            BTreeSet::from([NativeIntrinsic::Array]),
            BTreeSet::new(),
            BTreeSet::from([NativeIntrinsic::GlobalObject]),
            true,
        ));

        let integrity = resolve_native_integrity([&census]);

        assert_eq!(
            integrity.mutated,
            BTreeSet::from([
                NativeIntrinsic::Set,
                NativeIntrinsic::Function,
                NativeIntrinsic::Array,
                NativeIntrinsic::Math,
            ])
        );
        let revisited = integrity
            .witnesses
            .iter()
            .find(|witness| witness.statement_ordinal == SourceUnitOrdinal(0))
            .expect("conditional witness remains visible");
        assert_eq!(
            revisited.affected_roots,
            BTreeSet::from([NativeIntrinsic::Array])
        );
        assert_eq!(
            revisited.invalidated_key_dependencies,
            BTreeSet::from([NativeIntrinsic::Function])
        );
        let unreachable = integrity
            .witnesses
            .iter()
            .find(|witness| witness.statement_ordinal == SourceUnitOrdinal(2))
            .expect("unreachable write remains in provenance");
        assert!(unreachable.affected_roots.is_empty());
        assert!(!integrity.mutated.contains(&NativeIntrinsic::GlobalObject));
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct FreshErrorWriteSite {
    owner: SourceUnitOrdinal,
    callable: Option<u32>,
    receiver: Id,
    property: String,
}

struct FreshErrorWriteClaim {
    site: FreshErrorWriteSite,
    callee: Id,
    local_read: Option<Id>,
}

#[derive(Clone)]
pub(super) struct DeferredGlobalPublication {
    root: BuiltinGlobalRoot,
    receiver_id: Id,
    property: String,
    pub(super) value: ValueExpr,
}

fn builtin_global_root(ident: &Ident) -> Option<BuiltinGlobalRoot> {
    match ident.sym.as_ref() {
        "globalThis" => Some(BuiltinGlobalRoot::GlobalThis),
        "window" => Some(BuiltinGlobalRoot::Window),
        _ => None,
    }
}

fn is_global_receiver(expression: &Expr, names: &BTreeSet<&str>) -> bool {
    match binding_targets::transparent_target_expr(expression) {
        Expr::Ident(ident) => names.contains(ident.sym.as_ref()),
        Expr::Member(member) => {
            matches!(
                static_prop(&member.prop).as_deref(),
                Some("globalThis" | "window" | "self" | "frames" | "top")
            ) && is_global_receiver(&member.obj, names)
        }
        _ => false,
    }
}

fn exposes_global_receiver(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, property, _, _) => {
            // A direct global property read is not publication of the global
            // receiver. Projection through any other value can carry one:
            // `({root: globalThis}).root` must stay an escape.
            if matches!(receiver.as_ref(), ValueExpr::Binding(id) if names.contains(id.0.as_ref()))
            {
                property.as_deref().is_some_and(|property| {
                    matches!(
                        property,
                        "globalThis" | "window" | "self" | "frames" | "top"
                    )
                })
            } else {
                exposes_global_receiver(receiver, names)
            }
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| exposes_global_receiver(value, names))
        }
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .any(|(_, value)| exposes_global_receiver(value, names)),
        ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
            .iter()
            .any(|value| exposes_global_receiver(value, names)),
        // Operators can inspect or coerce their operands, but their result
        // cannot be the global object. Calls arising from those operands are
        // checked separately below.
        ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => false,
        _ => false,
    }
}

fn call_mutates_global_slot(call: &Call, names: &BTreeSet<&str>) -> bool {
    matches!(&call.callee, ValueExpr::Member(receiver, Some(property), _, _)
        if matches!(property.as_str(), "__defineSetter__" | "__defineGetter__")
            && exposes_global_receiver(receiver, names))
}

fn call_exposes_global_receiver(
    call: &Call,
    names: &BTreeSet<&str>,
    certified_root_ids: &BTreeSet<Id>,
) -> bool {
    if call_mutates_global_slot(call, names) {
        return true;
    }
    // The retained `in` protocol call models possible proxy/host effects;
    // it does not pass its RHS to a user function. A literal key against a
    // direct, unshadowed builtin global root cannot publish that root. Keep
    // the protocol call itself for ordinary effect and reentry analysis.
    if call.kind == CallKind::InProtocol
        && matches!(call.args.as_slice(),
            [ValueExpr::Binding(receiver), ValueExpr::NonCallable(_)]
                if names.contains(receiver.0.as_ref())
                    && certified_root_ids.contains(receiver))
    {
        return false;
    }
    call.args
        .iter()
        .any(|value| exposes_global_receiver(value, names))
}

fn uses_any_binding(value: &ValueExpr, ids: &BTreeSet<Id>) -> bool {
    let mut bindings = BTreeSet::new();
    value.bindings(&mut bindings);
    !bindings.is_disjoint(ids)
}

struct DerivedBindingRules {
    dependencies: Vec<(Id, Vec<Id>)>,
}

impl IterativeRules for DerivedBindingRules {
    type Key = Id;
    type Fact = ();
    type Rule = usize;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (target, sources) = &self.dependencies[*rule];
        if sources
            .iter()
            .any(|source| context.read(source.clone()).is_some())
        {
            context.propose(target.clone(), ());
        }
    }

    fn merge(_: &Self::Key, _: &mut Self::Fact, _: Self::Fact) -> bool {
        false
    }
}

fn close_prepublication_derived_bindings(
    alias: &Id,
    assignments: &[(Id, ValueExpr)],
) -> BTreeSet<Id> {
    let dependencies = assignments
        .iter()
        .map(|(target, value)| {
            let mut sources = BTreeSet::new();
            value.bindings(&mut sources);
            (target.clone(), sources.into_iter().collect())
        })
        .collect::<Vec<_>>();
    let mut inference = IterativeInference::new(DerivedBindingRules { dependencies });
    inference.seed(alias.clone(), ());
    inference
        .solve(0..assignments.len())
        .into_values()
        .into_keys()
        .collect()
}

fn same_property_or_fresh(value: &ValueExpr, alias: &Id, property: &str) -> bool {
    match value {
        ValueExpr::Member(receiver, Some(read), _, _)
            if read == property
                && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == alias) =>
        {
            true
        }
        ValueExpr::ObjectLiteral(_, properties) => properties.is_empty(),
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .all(|value| same_property_or_fresh(value, alias, property))
        }
        ValueExpr::NonCallable(_)
        | ValueExpr::Undefined
        | ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => true,
        _ => false,
    }
}

fn confined_prepublication_alias(
    index: &ExecutionIndex,
    key: u32,
    alias: &Id,
    first_publication: SourceUnitOrdinal,
    protected: &BTreeSet<String>,
) -> bool {
    let Some(callable) = index.callables.get(&key) else {
        return false;
    };
    let owner = callable.owner;
    if owner >= first_publication
        || index.discarded_sync_iifes[owner.0] != Some(key)
        || callable.opaque
        || !callable.params.is_empty()
        || !callable.body_facts.value_flow().returns().is_empty()
        || callable.reads.iter().any(|id| id.0 == "arguments")
        || callable.bare_reads.contains(alias)
        || callable.rebinds.contains(alias)
        || index.callables.iter().any(|(other_key, other)| {
            other.owner == owner && *other_key != key
                || other.owner != owner && other.reads.contains(alias)
        })
        || callable
            .local_assignments
            .iter()
            .filter(|(id, _)| id == alias)
            .count()
            != 1
    {
        return false;
    }
    let forbidden = |property: &str| {
        protected.contains(property)
            || matches!(
                property,
                "__proto__"
                    | "prototype"
                    | "constructor"
                    | "globalThis"
                    | "window"
                    | "self"
                    | "frames"
                    | "top"
                    | "__defineSetter__"
                    | "__defineGetter__"
            )
    };
    if index.callable_member_reads.get(&key).is_some_and(|reads| {
        reads.iter().any(|(receiver, property)| {
            matches!(receiver, ValueExpr::Binding(id) if id == alias)
                && property.as_deref().is_none_or(&forbidden)
        })
    }) {
        return false;
    }
    let derived = close_prepublication_derived_bindings(alias, &callable.local_assignments);
    for call in &callable.calls {
        if call.kind == CallKind::Call && uses_any_binding(&call.callee, &derived)
            || matches!(call.kind, CallKind::Call | CallKind::New)
                && call.args.iter().any(|arg| uses_any_binding(arg, &derived))
        {
            return false;
        }
        if call.kind == CallKind::New
            && uses_any_binding(&call.callee, &derived)
            && !matches!(&call.callee,
                ValueExpr::Member(receiver, Some(property), _, _)
                    if property == "Error"
                        && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == alias)
                        && call.args.is_empty())
        {
            return false;
        }
    }
    callable.callback_result_writes.iter().all(|write| {
        if let ValueExpr::Binding(id) = &write.receiver
            && id == alias
        {
            let Some(property) = write.property.as_deref() else {
                return false;
            };
            !forbidden(property) && same_property_or_fresh(&write.value, alias, property)
        } else if uses_any_binding(&write.receiver, &derived) {
            // A derived receiver may be an alias, but this entire invocation
            // finishes before publication. Only primitive writes can survive.
            matches!(
                write.value,
                ValueExpr::NonCallable(_)
                    | ValueExpr::Undefined
                    | ValueExpr::Primitive(_)
                    | ValueExpr::StrictEquality(..)
                    | ValueExpr::LogicalNot(_)
                    | ValueExpr::Void(_)
            )
        } else {
            !uses_any_binding(&write.value, &derived)
        }
    })
}

fn deferred_global_assignment(expression: &Expr) -> Option<DeferredGlobalPublication> {
    let Expr::Assign(assignment) = expression.unwrap_parens() else {
        return None;
    };
    let Some(binding_targets::SimpleWriteTarget::Member(member)) =
        binding_targets::simple_write_target(&assignment.left)
    else {
        return None;
    };
    let (Expr::Ident(receiver), MemberProp::Ident(property)) = (
        binding_targets::transparent_target_expr(&member.obj),
        &member.prop,
    ) else {
        return None;
    };
    if assignment.op != AssignOp::Assign
        || !matches!(
            assignment.right.unwrap_parens(),
            Expr::Arrow(_) | Expr::Fn(_) | Expr::Ident(_)
        )
    {
        return None;
    }
    Some(DeferredGlobalPublication {
        root: builtin_global_root(receiver)?,
        receiver_id: receiver.to_id(),
        property: property.sym.to_string(),
        value: ValueExpr::from_expr(&assignment.right),
    })
}

fn typeof_window_guard(expression: &Expr) -> bool {
    let Expr::Bin(guard) = expression.unwrap_parens() else {
        return false;
    };
    let Expr::Unary(left) = guard.left.unwrap_parens() else {
        return false;
    };
    if left.op != UnaryOp::TypeOf
        || !matches!(left.arg.unwrap_parens(), Expr::Ident(id) if id.sym == "window")
    {
        return false;
    }
    matches!((guard.op, guard.right.unwrap_parens()),
        (BinaryOp::Lt, Expr::Lit(Lit::Str(value))) if value.value == "u"
    ) || matches!((guard.op, guard.right.unwrap_parens()),
        (BinaryOp::NotEq | BinaryOp::NotEqEq, Expr::Lit(Lit::Str(value))) if value.value == "undefined"
    )
}

fn deferred_global_publication(item: &ModuleItem) -> Option<DeferredGlobalPublication> {
    let ModuleItem::Stmt(Stmt::Expr(statement)) = item else {
        return None;
    };
    let expression = statement.expr.unwrap_parens();
    if let Expr::Bin(binary) = expression
        && binary.op == BinaryOp::LogicalAnd
        && typeof_window_guard(&binary.left)
    {
        return deferred_global_assignment(&binary.right)
            .filter(|publication| publication.root == BuiltinGlobalRoot::Window);
    }
    deferred_global_assignment(expression)
        .filter(|publication| publication.root == BuiltinGlobalRoot::GlobalThis)
}

fn discarded_synchronous_iife(item: &ModuleItem) -> Option<u32> {
    let ModuleItem::Stmt(Stmt::Expr(statement)) = item else {
        return None;
    };
    let value = match statement.expr.unwrap_parens() {
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Bang | UnaryOp::Void) => {
            unary.arg.unwrap_parens()
        }
        value => value,
    };
    let Expr::Call(call) = value else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = callee.unwrap_parens() else {
        return None;
    };
    (function.ident.is_none()
        && call.args.is_empty()
        && !function.function.is_async
        && !function.function.is_generator
        && function.function.decorators.is_empty()
        && function.function.params.is_empty())
    .then_some(function.function.span.lo.0)
}

struct DestructuringMemberWrite {
    receiver: ValueExpr,
    /// The whole source (including defaults) over-approximates the target value.
    source: ValueExpr,
}

/// A sufficient source-privacy proof, independent of execution/value-flow facts.
/// Every configured root belongs to one potentially aliasing namespace family.
#[derive(Default)]
struct NamespacePrivacy {
    members: BTreeMap<Id, BTreeSet<String>>,
    import_sites: BTreeSet<u32>,
    allowed_read: Option<u32>,
    optional_members: BTreeSet<u32>,
    target_depth: usize,
    invalid: bool,
}

impl NamespacePrivacy {
    fn record_ident(&mut self, ident: &Ident) {
        if self.members.contains_key(&ident.to_id())
            && !self.import_sites.contains(&ident.span.lo.0)
            && self.allowed_read != Some(ident.span.lo.0)
        {
            self.invalid = true;
        }
    }

    fn read_receiver(&self, member: &MemberExpr) -> Option<u32> {
        if self.target_depth == 0
            && !self.optional_members.contains(&member.span.lo.0)
            && let Expr::Ident(receiver) = member.obj.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && self
                .members
                .get(&receiver.to_id())
                .is_some_and(|members| members.contains(property.sym.as_ref()))
        {
            Some(receiver.span.lo.0)
        } else {
            None
        }
    }
}

#[derive(Default)]
struct GlobalMembershipBindings(BTreeSet<Id>);

impl Visit for GlobalMembershipBindings {
    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        self.0.insert(binding.id.to_id());
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        self.0.insert(declaration.ident.to_id());
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        self.0.insert(declaration.ident.to_id());
        declaration.visit_children_with(self);
    }

    fn visit_fn_expr(&mut self, expression: &FnExpr) {
        if let Some(ident) = &expression.ident {
            self.0.insert(ident.to_id());
        }
        expression.visit_children_with(self);
    }

    fn visit_class_expr(&mut self, expression: &ClassExpr) {
        if let Some(ident) = &expression.ident {
            self.0.insert(ident.to_id());
        }
        expression.visit_children_with(self);
    }
}

#[derive(Clone)]
struct GlobalMembershipProbe {
    root: BuiltinGlobalRoot,
    receiver: Id,
    property: String,
    owner: SourceUnitOrdinal,
}

fn global_receiver_path(value: &ValueExpr, names: &BTreeSet<&str>) -> bool {
    match value {
        ValueExpr::Binding(id) => names.contains(id.0.as_ref()),
        ValueExpr::Member(receiver, _, _, _) => global_receiver_path(receiver, names),
        _ => false,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum GlobalMutationPath {
    Unrelated,
    Descendant,
    Identity,
    Prototype,
}

// This classifies syntactic evidence only. A descendant is not proven
// disjoint: its complete alias integrity remains the author's premise.
fn global_mutation_path(
    value: &ValueExpr,
    names: &BTreeSet<&str>,
    bindings: &BTreeSet<Id>,
) -> GlobalMutationPath {
    use GlobalMutationPath::*;
    match value {
        ValueExpr::Binding(id) if !bindings.contains(id) => match id.0.as_ref() {
            name if names.contains(name) => Identity,
            "document" => Descendant,
            "Window" | "EventTarget" | "Object" | "Document" | "HTMLDocument" => Prototype,
            _ => Unrelated,
        },
        ValueExpr::Member(receiver, property, _, _) => {
            match global_mutation_path(receiver, names, bindings) {
                Unrelated => Unrelated,
                Prototype => Prototype,
                Identity | Descendant => match property.as_deref() {
                    Some(
                        "__proto__" | "prototype" | "constructor" | "Window" | "EventTarget"
                        | "Object" | "Document" | "HTMLDocument",
                    ) => Prototype,
                    Some(
                        "globalThis" | "global" | "window" | "self" | "frames" | "top" | "parent"
                        | "defaultView",
                    ) => Identity,
                    _ => Descendant,
                },
            }
        }
        _ => Unrelated,
    }
}

fn stored_global_alias(
    value: &ValueExpr,
    names: &BTreeSet<&str>,
    bindings: &BTreeSet<Id>,
    include_prototypes: bool,
) -> bool {
    match global_mutation_path(value, names, bindings) {
        GlobalMutationPath::Identity => true,
        GlobalMutationPath::Prototype => include_prototypes,
        GlobalMutationPath::Descendant => false,
        GlobalMutationPath::Unrelated => match value {
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
                value
                    .result_alternatives()
                    .expect("alternative expression")
                    .iter()
                    .any(|value| stored_global_alias(value, names, bindings, include_prototypes))
            }
            ValueExpr::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| stored_global_alias(value, names, bindings, include_prototypes)),
            ValueExpr::Member(receiver, _, _, _) => {
                stored_global_alias(receiver, names, bindings, include_prototypes)
            }
            ValueExpr::Call(_, args, _) | ValueExpr::New(_, args, _) => args
                .iter()
                .any(|value| stored_global_alias(value, names, bindings, include_prototypes)),
            _ => false,
        },
    }
}

pub(super) struct ExecutionIndex {
    pub(super) member_owners: BTreeMap<CallSite, SourceUnitOrdinal>,
    pub(super) retained_record_factories: BTreeSet<String>,
    owner: SourceUnitOrdinal,
    current: Option<u32>,
    /// Lexical var scope, distinct from the effect owner inherited by
    /// unindexed accessors and static blocks.
    variable_scope: Option<u32>,
    allowed_local_member_receiver: Option<u32>,
    past_await: bool,
    may_skip_await_depth: u32,
    conditional_depth: u32,
    literal_depth: u32,
    factory_member_value_depth: u32,
    factory_callback_depth: u32,
    pub(super) callables: BTreeMap<u32, Callable>,
    /// Functions whose call cannot inspect or retain any argument.
    inert_argument_functions: BTreeSet<u32>,
    pub(super) callable_spans: BTreeMap<u32, Span>,
    immediate_anonymous_callers: BTreeMap<u32, Option<u32>>,
    inline_callback_entries: BTreeMap<u32, callback_arguments::InlineCallbackEntry>,
    native_guarded_regions: Vec<callback_arguments::GuardedRegion>,
    pub(super) local_constructors: BTreeMap<u32, LocalConstructor>,
    /// Source-covered constructor operations for exported provider summaries.
    pub(super) class_provider_bodies: BTreeMap<u32, (LocalBody, Vec<Id>)>,
    pub(super) local_class_construction_shapes: LocalClassConstructionShapes,
    pub(super) local_class_construction_proof: LocalClassConstructionProof,
    pub(super) semantic_facts: Option<Arc<SemanticFacts>>,
    /// Slot flags of the completed census and validated inputs; set with
    /// `semantic_facts`.
    pub(super) integrity: IntegrityFlags,
    /// Native own-data lookups from the canonical operation index, with the
    /// slots required at each read's source position.
    pub(super) native_own_data_reads: BTreeMap<u32, [IntrinsicSlot; 2]>,
    pub(super) class_values: BTreeMap<u32, ClassValueFacts>,
    pub(super) class_owners: BTreeMap<u32, SourceUnitOrdinal>,
    pub(super) class_bindings: BTreeMap<u32, Id>,
    pub(super) class_spans: BTreeMap<u32, Span>,
    /// The `extends` expression of each class that has one.
    pub(super) class_heritage_spans: BTreeMap<u32, Span>,
    /// Object literals with a getter or setter that reads `this`: a member
    /// read or write that may run it hands it the receiver.
    pub(super) this_reading_accessor_objects: BTreeSet<u32>,
    /// The non-null `__proto__` expression of each object literal with one.
    pub(super) literal_prototypes: BTreeMap<u32, ValueExpr>,
    /// Constructors, methods and accessors of every class: they run as class
    /// members, not as values the class body creates.
    pub(super) class_member_callables: BTreeSet<u32>,
    /// The constructors among `class_member_callables`.
    pub(super) class_constructors: BTreeSet<u32>,
    /// Per class, the spans that run at construction or class creation and
    /// can store values on the instance or the class: the constructor,
    /// field initializers and static blocks.
    pub(super) class_store_regions: BTreeMap<u32, Vec<Span>>,
    /// The getters and setters (static or not) each class declares.
    class_accessors: BTreeMap<u32, Vec<u32>>,
    /// Classes declaring a getter or setter that reads `this`.
    pub(super) this_reading_accessor_classes: BTreeSet<u32>,
    /// Receivers of member targets in destructuring assignments, per
    /// top-level owner; a callable's own are on `Callable`.
    pub(super) top_destructuring_member_receivers: Vec<Vec<ValueExpr>>,
    pub(super) private_empty_set_sites: BTreeMap<u32, (Span, Id)>,
    class_ids: BTreeSet<Id>,
    /// Top-level `const` declarations, exported or not: a store throws.
    pub(super) top_const_bindings: BTreeSet<Id>,
    /// Every binding read or written anywhere inside a callable body,
    /// including after an `await` (unlike `CallableFacts::{reads,rebinds}`).
    pub(super) captured_bindings: BTreeSet<Id>,
    pub(super) other_class_uses: BTreeSet<Id>,
    safe_class_target: Option<Id>,
    pub(super) plain_data_objects: BTreeSet<u32>,
    /// The top-level owner whose source contains each object literal.
    pub(super) object_literal_owners: BTreeMap<u32, SourceUnitOrdinal>,
    pub(super) own_descriptor_shapes: BTreeMap<u32, OwnPropertyShape>,
    descriptor_copy_factories: BTreeMap<Id, crate::analysis_inputs::DescriptorCopyFactorySummary>,
    pub(super) descriptor_copy_calls: BTreeMap<CallSite, DescriptorCopyCallCandidate>,
    pub(super) null_proto_data_objects: BTreeSet<u32>,
    pub(super) exact_object_spread_copies: BTreeSet<u32>,
    pub(super) map_entry_arrays: BTreeSet<u32>,
    pub(super) dense_array_literals: BTreeMap<u32, usize>,
    pub(super) array_elements: BTreeMap<u32, Vec<Option<ArrayElement>>>,
    literal_property_kinds: BTreeMap<u32, Vec<super::value_model::LiteralPropertyKind>>,
    callable_creations: BTreeMap<u32, DeferredCallableCreation>,
    pub(super) method_record_members:
        BTreeMap<u32, Vec<crate::facts::abstract_domain::foreign_call::RecordMember>>,
    pub(super) array_iterations: BTreeMap<CallSite, ArrayIteration>,
    pub(super) async_callables: BTreeSet<u32>,
    pub(super) callable_member_reads: BTreeMap<u32, Vec<(ValueExpr, Option<String>)>>,
    direct_call_members: BTreeSet<CallSite>,
    /// Direct calls of annotated record factories, with the syntax check of
    /// their argument. Semantic admission is checked by the interpreter.
    pub(super) record_factory_calls: BTreeMap<CallSite, RecordFactoryArgument>,
    member_write_sites: BTreeSet<CallSite>,
    pub(super) primitive_data_properties: BTreeSet<(u32, String)>,
    pub(super) from_entries_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    pub(super) mapped_array_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    pub(super) plain_array_bindings: BTreeSet<String>,
    mutated_intrinsic_roots: IntrinsicRoots,
    native_integrity_witnesses: Arc<Vec<NativeMutationWitness>>,
    native_prototype_mutations: Arc<Vec<NativePrototypeMutation>>,
    source_chunk_id: Option<String>,
    pub(super) native_point_effects: BTreeMap<(SourceUnitOrdinal, Option<u32>), NativePointEffects>,
    pub(super) initial_native_point_effects: NativePointEffects,
    pub(super) all_native_point_effects: NativePointEffects,
    pub(super) reassigned_map: bool,
    namespace_privacy: NamespacePrivacy,
    fresh_error_claims: Vec<FreshErrorWriteClaim>,
    pub(super) intrinsic_writes: Vec<IntrinsicWriteTarget>,
    prototype_calls: Vec<Call>,
    prototype_field_writes: Vec<(SourceUnitOrdinal, Option<u32>, Span, PropertyKeyExpr)>,
    pub(super) prototype_callable_reads: BTreeMap<u32, BTreeSet<Id>>,
    /// Per callable, the bindings it or a callable nested in it writes
    /// (assignment and destructuring targets, loop heads, updates). A closure
    /// must capture a cell for each so a nested writer reaches the owner.
    pub(super) nested_callable_writes: BTreeMap<u32, BTreeSet<Id>>,
    prototype_capture_stack: Vec<u32>,
    property_key_assignments: BTreeMap<Id, Vec<PropertyKeyExpr>>,
    pub(super) property_key_declarations: BTreeSet<Id>,
    native_write_census: NativeWriteCensus,
    pub(super) binding_declaration_writes: BTreeMap<Id, Vec<BindingDeclarationWrite>>,
    namespace_wrapper_candidates: BTreeMap<u32, NamespaceWrapperCandidate>,
    /// Reference bounds suffice to reject any use outside a candidate body.
    /// Binding declarations are handled by `visit_binding_ident` separately.
    binding_use_bounds: BTreeMap<Id, (u32, u32)>,
    /// Every identifier reference by source position, sorted: what the
    /// unexecuted remainder of a truncated operation can read.
    ident_references: Vec<(u32, Id)>,
    top_const_declarations: BTreeSet<Id>,
    direct_top_level_var_declaration: bool,
    direct_top_level_const_declaration: bool,
    variable_kind: Option<VarDeclKind>,
    /// Direct primitive literal initializers for simple top-level `const`
    /// bindings. Reassignment and dynamic-scope checks are applied after the
    /// single canonical AST walk is complete.
    pub(super) direct_primitive_const_bindings: BTreeSet<Id>,
    pub(super) reassigned_bindings: BTreeSet<Id>,
    /// Complete target-walker writes for primitive Set operand certification.
    binding_target_writes: BTreeSet<Id>,
    ordinary_input_refs: BTreeMap<Id, (ValueInputRef, SourceUnitOrdinal)>,
    value_input_evidence: BTreeMap<ValueInputRef, ValueInputEvidence>,
    resolved_value_inputs: Vec<ResolvedBindingValueInput>,
    value_input_errors: Vec<String>,
    pub(super) resolved_native_operation_inputs: Vec<ResolvedNativeOperationInput>,
    pub(super) native_operation_input_evidence: BTreeMap<CallSite, NativeOperationInputEvidence>,
    symbol_guarded_fill_sites: BTreeMap<CallSite, (Id, &'static str)>,
    native_operation_input_errors: Vec<String>,
    primitive_native_symbol_calls: BTreeSet<CallSite>,
    native_integrity_value_input_refs: BTreeSet<ValueInputRef>,
    native_integrity_host_behavior: BTreeSet<HostBehaviorKey>,
    /// `program_premises.intrinsics_never_written` families, and whether that
    /// premise overrode a census witness.
    never_written: BTreeSet<NativeIntrinsic>,
    never_written_overrode: bool,
    destructuring_source: Option<ValueExpr>,
    /// Visiting an assignment target: its identifiers are assigned, not
    /// declared.
    assignment_target: bool,
    /// Visiting the declaration of a `for`-`in`/`of` head.
    for_head_declaration: bool,
    destructuring_member_writes: Vec<DestructuringMemberWrite>,
    pub(super) setter_owners: BTreeMap<Option<String>, BTreeSet<SourceUnitOrdinal>>,
    hoisted_nested_functions: BTreeSet<u32>,
    pub(super) owner_callable_prefix_effects: Vec<Option<CallablePrefixEffects>>,
    pub(super) uncovered_callable_effect_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) top_calls: Vec<Vec<Call>>,
    /// Per top-level owner, what `Callable::published_sources` lists for a
    /// callable: values whose parts reach a binding only as unknown.
    pub(super) top_published_sources: BTreeMap<SourceUnitOrdinal, Vec<ValueExpr>>,
    /// Per top-level owner, what `Callable::iterated_sources` lists for a
    /// callable.
    pub(super) top_iterated_sources: BTreeMap<SourceUnitOrdinal, Vec<ValueExpr>>,
    original_statement_spans: Vec<Span>,
    /// Ordinary CallExpr syntax at lexical top level; optional calls use a
    /// conservative Call record too, but do not satisfy this witness shape.
    ordinary_top_call_sites: BTreeSet<CallSite>,
    bare_constructor_sites: BTreeSet<CallSite>,
    pub(super) literal_true_second_arguments: BTreeSet<CallSite>,
    pub(super) top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    pub(super) fresh_instance_field_writes: BTreeSet<SourceUnitOrdinal>,
    top_dynamic_property_receivers: Vec<Vec<ValueExpr>>,
    pub(super) top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    pub(super) ordered_initializers: BTreeMap<SourceUnitOrdinal, LocalBody>,
    /// Top-level `receiver.property = value` and `binding = value` statements
    /// lowered from their exact source expression. Lowering selects nothing: `roots/operands`
    /// admits one.
    pub(super) ordered_statements: BTreeMap<SourceUnitOrdinal, LocalBody>,
    /// Canonical owner/binding/initializer identity, independent of the
    /// legacy initializer leaf and allocation admission.
    pub(super) initialization_source_identities:
        BTreeMap<SourceUnitOrdinal, InitializationSourceIdentity>,
    pub(super) noninvoking_initializers: BTreeMap<SourceUnitOrdinal, OriginalInitializer>,
    pub(super) initialization_coverage_refusals:
        Vec<Option<OwnerInitializationCoverageRefusalReason>>,
    pub(super) top_definite_initializers: Vec<BTreeSet<Id>>,
    /// The sole direct top-level `let`/`const` identifier declared by an owner.
    /// A lexical read of it before this owner runs cannot create its initializer's callbacks.
    pub(super) single_top_lexical_binding: Vec<Option<Id>>,
    pub(super) const_call_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) top_member_value_writes: Vec<Vec<MemberValueWrite>>,
    pub(super) top_callback_result_writes: Vec<Vec<CallbackResultWrite>>,
    pub(super) top_exported_values: Vec<Vec<ValueExpr>>,
    pub(super) top_metadata_decorations: Vec<Option<MetadataDecorateCall>>,
    pub(super) deferred_global_candidates: Vec<Option<DeferredGlobalPublication>>,
    discarded_sync_iifes: Vec<Option<u32>>,
    pub(super) deferred_global_certificates: BTreeSet<SourceUnitOrdinal>,
    deferred_global_requested: bool,
    global_guards_requested: bool,
    global_membership_candidates: BTreeMap<CallSite, GlobalMembershipProbe>,
    pub(super) non_reentrant_global_membership: BTreeSet<CallSite>,
    global_slot_mentions: BTreeMap<String, usize>,
    dynamic_global_member: bool,
    global_alias_written: bool,
    pub(super) imported_bindings: BTreeSet<Id>,
    pub(super) hoisted_bindings: BTreeMap<Id, u32>,
    pub(super) direct_top_level_functions: BTreeSet<Id>,
    /// Exact sole-return calls in top-level function declarations, collected
    /// by the canonical structural visitor alongside callable/call facts.
    direct_return_calls: BTreeMap<Id, Span>,
    pub(super) exported_hoisted_functions: BTreeSet<u32>,
    pub(super) global_object_names: BTreeSet<&'static str>,
    /// Global names for the native census; the bundle-owned properties are
    /// filled in once the census resolves them.
    census_globals: GlobalNames,
    pub(super) hoisted_function_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) exported_declaration_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) exported_bindings: BTreeSet<Id>,
    /// Local binding of each local export name; `default` is not a binding.
    pub(super) exported_names: BTreeMap<String, Id>,
    /// Source units holding `export default`.
    pub(super) default_export_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) dynamic_scope_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) opaque_top: BTreeSet<SourceUnitOrdinal>,
    no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    pub(super) callback_factory_imports: BTreeSet<Id>,
    pub(super) callback_factory_summaries: BTreeMap<Id, crate::analysis_inputs::CallableSummary>,
    /// The existing canonical call-shape check accepted one syntactic inline
    /// callback. Lowered values cannot establish this source prerequisite.
    pub(super) direct_inline_callback_calls: BTreeMap<CallSite, Id>,
    pub(super) foreign_call_imports: BTreeMap<Id, crate::analysis_inputs::ForeignCallSummary>,
    /// The derived summary of an import whose callback-factory evidence is
    /// all source-inferred. The evidence keeps the call's timing and result
    /// consumers; the summary is the receipt for the provider body's effects.
    pub(super) callback_factory_call_summaries:
        BTreeMap<Id, crate::analysis_inputs::ForeignCallSummary>,
    pub(super) inline_callback_factory_imports: BTreeSet<Id>,
    pub(super) callback_factory_members: BTreeMap<Id, BTreeSet<String>>,
    /// Union used only for namespace privacy/mutation guards. Admission of the
    /// old and stronger contracts remains separate.
    pub(super) audited_factory_members: BTreeMap<Id, BTreeSet<String>>,
    pub(super) rejected_namespace_member_writes:
        BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    pub(super) rejected_namespace_dynamic_property_writes:
        BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    pub(super) rejected_namespace_call_arguments: BTreeMap<Id, CallbackFactoryCallArgumentWitness>,
    retained_callback_factory_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    retained_callback_constructor_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    no_reentry_callback_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    no_reentry_statement_calls: BTreeMap<(u32, u32), spec::RetainedCallbackSignature>,
    retained_callback_factory_members:
        BTreeMap<Id, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub(super) retained_callback_calls: BTreeMap<CallSite, IndexedRetainedCallbackCall>,
    pub(super) retained_constructor_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    pub(super) no_reentry_callback_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    /// Synthetic getter reads for a source-selected member callee. The map
    /// lets runtime timing suppress only foreign-callback promotion when the
    /// exact outer call's callback identities satisfy its contract.
    pub(super) no_reentry_accessor_calls: BTreeMap<CallSite, CallSite>,
    /// The ordinary call whose callee is evaluated by a synthetic lookup.
    pub(super) accessor_callee_calls: BTreeMap<CallSite, Call>,
    pub(super) no_reentry_exports: BTreeSet<Id>,
    pub(super) host_async_local_storage_imports: BTreeSet<Id>,
    pub(super) host_promise_resolve: bool,
    pub(super) no_reentry_members: BTreeMap<Id, BTreeSet<String>>,
    pub(super) no_reentry_record_members: BTreeMap<Id, BTreeSet<String>>,
    pub(super) returned_record_summaries:
        BTreeMap<Id, crate::analysis_inputs::ReturnedRecordSummary>,
    pub(super) namespace_wrapper_summaries: BTreeMap<Id, NamespaceWrapperSummary>,
}

impl Default for ExecutionIndex {
    fn default() -> Self {
        Self {
            member_owners: BTreeMap::new(),
            retained_record_factories: BTreeSet::new(),
            owner: SourceUnitOrdinal(0),
            current: None,
            variable_scope: None,
            allowed_local_member_receiver: None,
            past_await: false,
            may_skip_await_depth: 0,
            conditional_depth: 0,
            literal_depth: 0,
            factory_member_value_depth: 0,
            factory_callback_depth: 0,
            callables: BTreeMap::new(),
            inert_argument_functions: BTreeSet::new(),
            callable_spans: BTreeMap::new(),
            immediate_anonymous_callers: BTreeMap::new(),
            inline_callback_entries: BTreeMap::new(),
            native_guarded_regions: Vec::new(),
            local_constructors: BTreeMap::new(),
            class_provider_bodies: BTreeMap::new(),
            local_class_construction_shapes: LocalClassConstructionShapes::default(),
            local_class_construction_proof: LocalClassConstructionProof::default(),
            semantic_facts: None,
            integrity: IntegrityFlags::default(),
            native_own_data_reads: BTreeMap::new(),
            class_values: BTreeMap::new(),
            class_owners: BTreeMap::new(),
            class_bindings: BTreeMap::new(),
            class_spans: BTreeMap::new(),
            class_heritage_spans: BTreeMap::new(),
            class_member_callables: BTreeSet::new(),
            class_constructors: BTreeSet::new(),
            class_store_regions: BTreeMap::new(),
            class_accessors: BTreeMap::new(),
            this_reading_accessor_classes: BTreeSet::new(),
            top_destructuring_member_receivers: Vec::new(),
            this_reading_accessor_objects: BTreeSet::new(),
            literal_prototypes: BTreeMap::new(),
            private_empty_set_sites: BTreeMap::new(),
            class_ids: BTreeSet::new(),
            top_const_bindings: BTreeSet::new(),
            captured_bindings: BTreeSet::new(),
            other_class_uses: BTreeSet::new(),
            safe_class_target: None,
            plain_data_objects: BTreeSet::new(),
            object_literal_owners: BTreeMap::new(),
            own_descriptor_shapes: BTreeMap::new(),
            descriptor_copy_factories: BTreeMap::new(),
            descriptor_copy_calls: BTreeMap::new(),
            null_proto_data_objects: BTreeSet::new(),
            exact_object_spread_copies: BTreeSet::new(),
            map_entry_arrays: BTreeSet::new(),
            dense_array_literals: BTreeMap::new(),
            array_elements: BTreeMap::new(),
            literal_property_kinds: BTreeMap::new(),
            callable_creations: BTreeMap::new(),
            method_record_members: BTreeMap::new(),
            array_iterations: BTreeMap::new(),
            async_callables: BTreeSet::new(),
            callable_member_reads: BTreeMap::new(),
            direct_call_members: BTreeSet::new(),
            record_factory_calls: BTreeMap::new(),
            member_write_sites: BTreeSet::new(),
            primitive_data_properties: BTreeSet::new(),
            from_entries_map_calls: BTreeMap::new(),
            mapped_array_map_calls: BTreeMap::new(),
            plain_array_bindings: BTreeSet::new(),
            mutated_intrinsic_roots: IntrinsicRoots::new(),
            native_integrity_witnesses: Arc::default(),
            native_prototype_mutations: Arc::default(),
            source_chunk_id: None,
            native_point_effects: BTreeMap::new(),
            initial_native_point_effects: NativePointEffects::default(),
            all_native_point_effects: NativePointEffects::default(),
            reassigned_map: false,
            namespace_privacy: NamespacePrivacy::default(),
            fresh_error_claims: Vec::new(),
            intrinsic_writes: Vec::new(),
            prototype_calls: Vec::new(),
            prototype_field_writes: Vec::new(),
            prototype_callable_reads: BTreeMap::new(),
            nested_callable_writes: BTreeMap::new(),
            prototype_capture_stack: Vec::new(),
            property_key_assignments: BTreeMap::new(),
            property_key_declarations: BTreeSet::new(),
            native_write_census: NativeWriteCensus::default(),
            binding_declaration_writes: BTreeMap::new(),
            namespace_wrapper_candidates: BTreeMap::new(),
            binding_use_bounds: BTreeMap::new(),
            ident_references: Vec::new(),
            top_const_declarations: BTreeSet::new(),
            direct_top_level_var_declaration: false,
            direct_top_level_const_declaration: false,
            variable_kind: None,
            direct_primitive_const_bindings: BTreeSet::new(),
            reassigned_bindings: BTreeSet::new(),
            binding_target_writes: BTreeSet::new(),
            ordinary_input_refs: BTreeMap::new(),
            value_input_evidence: BTreeMap::new(),
            resolved_value_inputs: Vec::new(),
            value_input_errors: Vec::new(),
            resolved_native_operation_inputs: Vec::new(),
            native_operation_input_evidence: BTreeMap::new(),
            symbol_guarded_fill_sites: BTreeMap::new(),
            native_operation_input_errors: Vec::new(),
            primitive_native_symbol_calls: BTreeSet::new(),
            native_integrity_value_input_refs: BTreeSet::new(),
            native_integrity_host_behavior: BTreeSet::new(),
            never_written: BTreeSet::new(),
            never_written_overrode: false,
            destructuring_source: None,
            assignment_target: false,
            for_head_declaration: false,
            destructuring_member_writes: Vec::new(),
            setter_owners: BTreeMap::new(),
            hoisted_nested_functions: BTreeSet::new(),
            owner_callable_prefix_effects: Vec::new(),
            uncovered_callable_effect_owners: BTreeSet::new(),
            top_calls: Vec::new(),
            top_published_sources: BTreeMap::new(),
            top_iterated_sources: BTreeMap::new(),
            original_statement_spans: Vec::new(),
            ordinary_top_call_sites: BTreeSet::new(),
            bare_constructor_sites: BTreeSet::new(),
            literal_true_second_arguments: BTreeSet::new(),
            top_property_writes: Vec::new(),
            fresh_instance_field_writes: BTreeSet::new(),
            top_dynamic_property_receivers: Vec::new(),
            top_assignments: Vec::new(),
            ordered_initializers: BTreeMap::new(),
            ordered_statements: BTreeMap::new(),
            initialization_source_identities: BTreeMap::new(),
            noninvoking_initializers: BTreeMap::new(),
            initialization_coverage_refusals: Vec::new(),
            top_definite_initializers: Vec::new(),
            single_top_lexical_binding: Vec::new(),
            const_call_owners: BTreeSet::new(),
            top_member_value_writes: Vec::new(),
            top_callback_result_writes: Vec::new(),
            top_exported_values: Vec::new(),
            top_metadata_decorations: Vec::new(),
            deferred_global_candidates: Vec::new(),
            discarded_sync_iifes: Vec::new(),
            deferred_global_certificates: BTreeSet::new(),
            deferred_global_requested: false,
            global_guards_requested: false,
            global_membership_candidates: BTreeMap::new(),
            non_reentrant_global_membership: BTreeSet::new(),
            global_slot_mentions: BTreeMap::new(),
            dynamic_global_member: false,
            global_alias_written: false,
            imported_bindings: BTreeSet::new(),
            hoisted_bindings: BTreeMap::new(),
            direct_top_level_functions: BTreeSet::new(),
            direct_return_calls: BTreeMap::new(),
            exported_hoisted_functions: BTreeSet::new(),
            global_object_names: BTreeSet::new(),
            census_globals: GlobalNames::default(),
            hoisted_function_owners: BTreeSet::new(),
            exported_declaration_owners: BTreeSet::new(),
            exported_bindings: BTreeSet::new(),
            exported_names: BTreeMap::new(),
            default_export_owners: BTreeSet::new(),
            dynamic_scope_owners: BTreeSet::new(),
            opaque_top: BTreeSet::new(),
            no_sync_callback_members: BTreeMap::new(),
            foreign_call_imports: BTreeMap::new(),
            callback_factory_call_summaries: BTreeMap::new(),
            callback_factory_imports: BTreeSet::new(),
            callback_factory_summaries: BTreeMap::new(),
            direct_inline_callback_calls: BTreeMap::new(),
            inline_callback_factory_imports: BTreeSet::new(),
            callback_factory_members: BTreeMap::new(),
            audited_factory_members: BTreeMap::new(),
            rejected_namespace_member_writes: BTreeMap::new(),
            rejected_namespace_dynamic_property_writes: BTreeMap::new(),
            rejected_namespace_call_arguments: BTreeMap::new(),
            retained_callback_factory_imports: BTreeMap::new(),
            retained_callback_constructor_imports: BTreeMap::new(),
            no_reentry_callback_imports: BTreeMap::new(),
            no_reentry_statement_calls: BTreeMap::new(),
            retained_callback_factory_members: BTreeMap::new(),
            retained_callback_calls: BTreeMap::new(),
            retained_constructor_calls: BTreeMap::new(),
            no_reentry_callback_calls: BTreeMap::new(),
            no_reentry_accessor_calls: BTreeMap::new(),
            accessor_callee_calls: BTreeMap::new(),
            no_reentry_exports: BTreeSet::new(),
            host_async_local_storage_imports: BTreeSet::new(),
            host_promise_resolve: false,
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            returned_record_summaries: BTreeMap::new(),
            namespace_wrapper_summaries: BTreeMap::new(),
        }
    }
}

/// Whether `domain` may be the string `name`.
fn key_domain_may_name(domain: &PropertyKeyDomain, name: &str) -> bool {
    match domain {
        PropertyKeyDomain::FiniteStrings(keys) => keys.contains(name),
        PropertyKeyDomain::Affixed { prefix, suffix } => NativePropertyKey::Affixed {
            prefix: prefix.clone(),
            suffix: suffix.clone(),
        }
        .may_be_string(name),
        PropertyKeyDomain::SymbolIterator | PropertyKeyDomain::Symbol => false,
        PropertyKeyDomain::Unknown => true,
    }
}

/// The own keys `Object.assign` copies from `sources` when each is an object
/// literal of plain data properties and methods with static keys: no spread,
/// accessor, or `__proto__` in any spelling.
fn object_literal_assign_keys(sources: &[ExprOrSpread]) -> Option<Vec<String>> {
    let mut keys = Vec::new();
    for source in sources {
        let (None, Expr::Object(object)) = (&source.spread, source.expr.unwrap_parens()) else {
            return None;
        };
        for property in &object.props {
            let PropOrSpread::Prop(property) = property else {
                return None;
            };
            let key = match property.as_ref() {
                Prop::KeyValue(pair) => static_property_name(&pair.key),
                Prop::Shorthand(ident) => Some(ident.sym.to_string()),
                Prop::Method(method) => static_property_name(&method.key),
                Prop::Assign(_) | Prop::Getter(_) | Prop::Setter(_) => None,
            };
            let key = key.filter(|key| key != "__proto__")?;
            if !keys.contains(&key) {
                keys.push(key);
            }
        }
    }
    Some(keys)
}

const KEYED_COLLECTIONS: [&str; 4] = ["Map", "Set", "WeakMap", "WeakSet"];

const LEGACY_ACCESSOR_METHODS: [&str; 2] = ["__defineGetter__", "__defineSetter__"];

/// One global data property a bundle-owned value may sit in: a name, or any
/// property keyed by a `Symbol.for` symbol.
#[derive(Clone, Copy)]
enum BundleSlot<'a> {
    Name(&'a str),
    RegisteredSymbol,
}

struct BundleGlobalContext<'a> {
    aliases: &'a BTreeMap<Id, NativeReceiverRoots>,
    globals: &'a GlobalNames,
    declared: &'a BTreeSet<Id>,
    /// Top-level bindings that only ever hold fresh objects or primitives.
    fresh_objects: &'a BTreeSet<Id>,
}

impl BundleGlobalContext<'_> {
    fn roots(&self, value: &ValueExpr) -> NativeReceiverRoots {
        intrinsic_roots(
            value,
            self.aliases,
            self.globals,
            self.declared,
            &BTreeMap::new(),
            &CallableReturnInputs::default(),
        )
    }

    /// Whether storing `value` into `slot` keeps it bundle-owned: a fresh
    /// object (inline or through a binding that only holds one), a primitive,
    /// or the value of a bundle-owned property, the slot's own current value
    /// included.
    fn keeps(&self, value: &ValueExpr, slot: BundleSlot<'_>) -> bool {
        match value {
            ValueExpr::ObjectLiteral(..)
            | ValueExpr::Primitive(_)
            | ValueExpr::StrictEquality(..)
            | ValueExpr::LogicalNot(_)
            | ValueExpr::Void(_)
            | ValueExpr::Undefined
            | ValueExpr::NonCallable(_) => true,
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
                let options = value.result_alternatives().expect("alternative expression");
                options.iter().all(|option| self.keeps(option, slot))
            }
            // A keyed collection the program never replaces allocates a fresh
            // object whatever its argument.
            ValueExpr::New(callee, _, _)
                if self.globals.bundle.fresh_collections
                    && matches!(callee.as_ref(), ValueExpr::Binding(id)
                        if !self.declared.contains(id)
                            && KEYED_COLLECTIONS.contains(&id.0.as_ref())) =>
            {
                true
            }
            ValueExpr::Binding(id) if self.fresh_objects.contains(id) => true,
            // A sloppy read of the undeclared global itself.
            ValueExpr::Binding(id)
                if !self.declared.contains(id)
                    && matches!(slot, BundleSlot::Name(name) if *id.0 == *name) =>
            {
                true
            }
            _ => {
                let roots = self.roots(value);
                !roots.is_empty()
                    && roots
                        .iter()
                        .all(|root| *root == NativeReceiverRoot::BundleGlobal)
            }
        }
    }
}

impl ExecutionIndex {
    /// A unique top-level function identity with no source writes or dynamic
    /// scope; earlier statements cannot silently replace this class base.
    pub(super) fn stable_heritage_function(
        &self,
        binding: &Id,
        flow: &ChunkValueFlow,
    ) -> Option<u32> {
        let &key = self.hoisted_bindings.get(binding)?;
        (self.dynamic_scope_owners.is_empty()
            && self.direct_top_level_functions.contains(binding)
            && !self.reassigned_bindings.contains(binding)
            && !self.binding_target_writes.contains(binding)
            && !self.binding_declaration_writes.contains_key(binding)
            && flow
                .values_of_binding(binding)
                .iter()
                .eq([Value::Alloc(Kind::Function, key)]))
        .then_some(key)
    }

    /// The callee's exact body and parameters cannot observe or publish any
    /// argument; this does not waive the call's normal evaluation effects.
    pub(super) fn inert_heritage_callee(&self, binding: &Id, flow: &ChunkValueFlow) -> bool {
        self.stable_heritage_function(binding, flow)
            .is_some_and(|key| self.inert_argument_functions.contains(&key))
    }

    /// A closure can resolve module bindings and unresolved globals when it is
    /// later invoked. Creating it does not read those values into the factory
    /// activation, so they are not retained provider captures. Enclosing
    /// lexical cells still require the local transfer or checked private-var
    /// capture projection to represent them.
    pub(super) fn deferred_module_reference(&self, binding: &Id) -> bool {
        self.imported_bindings.contains(binding)
            || self.hoisted_bindings.contains_key(binding)
            || self.class_ids.contains(binding)
            || self
                .binding_declaration_writes
                .get(binding)
                .is_some_and(|writes| {
                    !writes.is_empty() && writes.iter().all(|write| write.direct_top_level)
                })
            || !self.property_key_declarations.contains(binding)
    }

    pub(super) fn private_var_binding(&self, binding: &Id) -> bool {
        if !self.dynamic_scope_owners.is_empty() || self.imported_bindings.contains(binding) {
            return false;
        }
        let Some(writes) = self.binding_declaration_writes.get(binding) else {
            return false;
        };
        let Some(scope) = writes.first().and_then(|write| write.private_var_scope) else {
            return false;
        };
        let Some(callable) = self.callables.get(&scope) else {
            return false;
        };
        // Parameter initialization has a different TDZ and, with defaults,
        // can create closures before the body environment exists. A var
        // redeclaration must not turn such a parameter into this proof.
        if callable.params.iter().any(|parameter| {
            parameter.binding.is_none()
                || parameter.default.is_some()
                || parameter.binding.as_ref() == Some(binding)
        }) {
            return false;
        }
        writes
            .iter()
            .all(|write| write.private_var_scope == Some(scope) && write.simple_identifier)
    }

    pub(super) fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        if self.array_elements.contains_key(&allocation) {
            return None;
        }
        self.own_descriptor_shapes.get(&allocation)
    }

    pub(super) fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::value_model::LiteralPropertyKind {
        self.literal_property_kinds
            .get(&allocation)
            .and_then(|kinds| kinds.get(position))
            .cloned()
            .unwrap_or(super::value_model::LiteralPropertyKind::Opaque)
    }

    pub(super) fn callable_creation(&self, callable: u32) -> Option<DeferredCallableCreation> {
        self.callable_creations.get(&callable).copied()
    }

    pub(super) fn callable_home_object(&self, callable: u32) -> Option<u32> {
        self.callable_creations
            .get(&callable)
            .and_then(|creation| creation.home_object())
    }

    pub(super) fn allocation_layout(
        &self,
        allocation: u32,
    ) -> super::allocation_transfer::LiteralLayout {
        use super::allocation_transfer::LiteralLayout;
        if self.exact_object_spread_copies.contains(&allocation) {
            return LiteralLayout::Unsupported;
        }
        if let Some(elements) = self.array_elements.get(&allocation) {
            return LiteralLayout::Array(elements.clone());
        }
        LiteralLayout::Record
    }

    /// Simple top-level declared bindings every write of which stores a fresh
    /// value (`fresh_value`); every write is modeled because the modeled
    /// writes number as many as the binding's recorded writes. Before the
    /// first one they are `undefined` or in TDZ.
    fn fresh_object_bindings(&self) -> BTreeSet<Id> {
        let mut values: BTreeMap<&Id, Vec<&ValueExpr>> = BTreeMap::new();
        for (binding, value) in self.bundle_assignments() {
            values.entry(binding).or_default().push(value);
        }
        self.binding_declaration_writes
            .iter()
            .filter(|(binding, declarations)| {
                matches!(declarations.as_slice(), [declaration]
                    if declaration.direct_top_level && declaration.simple_identifier)
                    && values.get(binding).is_some_and(|stored| {
                        self.property_key_assignments
                            .get(*binding)
                            .is_some_and(|writes| writes.len() == stored.len())
                            && stored.iter().all(|value| self.fresh_value(value))
                    })
            })
            .map(|(binding, _)| binding.clone())
            .collect()
    }

    /// An object literal, a primitive, or the result of calling an inline
    /// function whose every own `return` is one.
    fn fresh_value(&self, value: &ValueExpr) -> bool {
        let inert = |value: &ValueExpr| {
            matches!(
                value,
                ValueExpr::ObjectLiteral(..)
                    | ValueExpr::Primitive(_)
                    | ValueExpr::LogicalNot(_)
                    | ValueExpr::Undefined
            )
        };
        inert(value)
            || matches!(value, ValueExpr::Call(callee, _, _)
                if matches!(callee.as_ref(), ValueExpr::Alloc(Kind::Function, start)
                    if self
                        .callables
                        .get(start)
                        .is_some_and(|callable| callable.body_facts.value_flow().returns().iter().all(inert))))
    }

    fn bundle_assignments(&self) -> Vec<&(Id, ValueExpr)> {
        self.top_assignments
            .iter()
            .flatten()
            .chain(
                self.callables
                    .values()
                    .flat_map(|callable| callable.local_assignments.iter()),
            )
            .collect()
    }

    /// Stable bindings whose key domain is a `Symbol.for` symbol.
    fn symbol_key_bindings(&self, declared: &BTreeSet<Id>) -> BTreeSet<Id> {
        self.property_key_assignments
            .keys()
            .filter(|binding| {
                matches!(
                    property_key_evidence(
                        &PropertyKeyExpr::Binding((*binding).clone()),
                        &self.property_key_assignments,
                        declared,
                    )
                    .domain,
                    PropertyKeyDomain::Symbol
                )
            })
            .cloned()
            .collect()
    }

    /// The whole-program bundle-owned set as this module reads it.
    fn bundle_globals(&self, names: &BundleGlobalNames, declared: &BTreeSet<Id>) -> BundleGlobals {
        BundleGlobals {
            names: names.names.clone(),
            registered_symbols: names.registered_symbols,
            symbol_keys: self.symbol_key_bindings(declared),
            fresh_collections: names.fresh_collections,
            no_accessor_install_callables: names.no_accessor_install_callables.clone(),
        }
    }

    /// Every non-standard global property this module stores into the global
    /// object, the start of the greatest fixed point (SPEC.md § Bundle-owned
    /// globals).
    pub(crate) fn bundle_global_candidates(&self) -> BundleGlobalNames {
        if self.census_globals.predefined.is_unknown() {
            return BundleGlobalNames::default();
        }
        let declared = self.declared_bindings();
        let assignments = self.bundle_assignments();
        let aliases = close_raw_intrinsic_aliases(&assignments, &self.census_globals, &declared);
        let fresh_objects = self.fresh_object_bindings();
        let context = BundleGlobalContext {
            aliases: &aliases,
            globals: &self.census_globals,
            declared: &declared,
            fresh_objects: &fresh_objects,
        };
        let mut candidates = BundleGlobalNames {
            fresh_collections: true,
            ..BundleGlobalNames::default()
        };
        for write in &self.intrinsic_writes {
            if write.value.is_none()
                || !context
                    .roots(&write.receiver)
                    .contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::GlobalObject,
                    ))
            {
                continue;
            }
            let Some(key) = &write.key else {
                continue;
            };
            match property_key_evidence(key, &self.property_key_assignments, &declared).domain {
                PropertyKeyDomain::FiniteStrings(keys) => {
                    candidates.names.extend(keys.into_iter().filter(|name| {
                        !is_predefined_global(name, self.census_globals.predefined)
                            && self.census_globals.binding_root(name).is_none()
                    }));
                }
                PropertyKeyDomain::Symbol => candidates.registered_symbols = true,
                _ => {}
            }
        }
        // A non-standard property only read here is a candidate too: with no
        // store it stays absent or a named property.
        let mut reads = BTreeSet::new();
        for expr in self
            .intrinsic_writes
            .iter()
            .flat_map(|write| std::iter::once(&write.receiver).chain(&write.value))
            .chain(self.prototype_calls.iter().map(|call| &call.callee))
            .chain(assignments.iter().map(|(_, value)| value))
        {
            self.global_property_reads(expr, &context, &mut reads);
        }
        candidates.names.extend(reads);
        candidates
    }

    /// Non-standard property names `expr` reads from the global object.
    fn global_property_reads(
        &self,
        expr: &ValueExpr,
        context: &BundleGlobalContext<'_>,
        reads: &mut BTreeSet<String>,
    ) {
        match expr {
            ValueExpr::Member(base, property, _, _) => {
                if let Some(name) = property
                    && !reads.contains(name)
                    && !is_predefined_global(name, self.census_globals.predefined)
                    && self.census_globals.binding_root(name).is_none()
                    && context.roots(base).contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::GlobalObject,
                    ))
                {
                    reads.insert(name.clone());
                }
                self.global_property_reads(base, context, reads);
            }
            ValueExpr::Call(callee, arguments, _) | ValueExpr::New(callee, arguments, _) => {
                self.global_property_reads(callee, context, reads);
                for argument in arguments.iter() {
                    self.global_property_reads(argument, context, reads);
                }
            }
            ValueExpr::ObjectLiteral(_, properties) => {
                for (_, value) in properties.iter() {
                    self.global_property_reads(value, context, reads);
                }
            }
            ValueExpr::ArrayIteratorPrototype(inner) | ValueExpr::SetIteratorPrototype(inner) => {
                self.global_property_reads(inner, context, reads);
            }
            ValueExpr::Primitive(values)
            | ValueExpr::Alternatives(values)
            | ValueExpr::Sequence(values) => {
                for value in values.iter() {
                    self.global_property_reads(value, context, reads);
                }
            }
            ValueExpr::StrictEquality(left, right) => {
                self.global_property_reads(left, context, reads);
                self.global_property_reads(right, context, reads);
            }
            ValueExpr::Void(child) | ValueExpr::LogicalNot(child) => {
                self.global_property_reads(child, context, reads)
            }
            ValueExpr::Conditional { test, alternatives } => {
                self.global_property_reads(test, context, reads);
                for alternative in alternatives.iter() {
                    self.global_property_reads(alternative, context, reads);
                }
            }
            ValueExpr::Binding(_)
            | ValueExpr::Alloc(..)
            | ValueExpr::Undefined
            | ValueExpr::NonCallable(_)
            | ValueExpr::Unknown => {}
        }
    }

    /// The `candidates` this module's writes keep bundle-owned: a greatest
    /// fixed point dropping a property some census write could store anything
    /// else into, and every property once a global-chain write has an unknown
    /// key or may change a prototype chain. A key derived through `Symbol.for`
    /// is exact while no write may replace `%Symbol%` or `%Symbol.for%`.
    pub(crate) fn bundle_global_survivors(
        &self,
        candidates: &BundleGlobalNames,
        premises: &[ResolvedNoAccessorInstall],
    ) -> BundleGlobalNames {
        if self.census_globals.predefined.is_unknown() {
            return BundleGlobalNames::default();
        }
        let declared = self.declared_bindings();
        let assignments = self.bundle_assignments();
        let evidence = |key: &PropertyKeyExpr| {
            property_key_evidence(key, &self.property_key_assignments, &declared)
        };
        let fresh_objects = self.fresh_object_bindings();
        let mut current = candidates.clone();
        let mut symbol_for_intact = true;
        loop {
            let mut globals = self.census_globals.clone();
            globals.bundle = self.bundle_globals(&current, &declared);
            let aliases = close_raw_intrinsic_aliases(&assignments, &globals, &declared);
            let context = BundleGlobalContext {
                aliases: &aliases,
                globals: &globals,
                declared: &declared,
                fresh_objects: &fresh_objects,
            };
            let mut next = current.clone();
            next.no_accessor_install_callables.clear();
            let mut next_symbol_for_intact = true;
            for write in &self.intrinsic_writes {
                let roots = context.roots(&write.receiver);
                if roots
                    .iter()
                    .all(|root| *root == NativeReceiverRoot::BundleGlobal)
                {
                    continue;
                }
                // A keyless reflective write (`Object.assign`,
                // `Object.setPrototypeOf`) may change any property or the
                // prototype chain.
                let Some(key) = &write.key else {
                    return BundleGlobalNames::default();
                };
                // A delete stores nothing: on a prototype it leaves an
                // inherited read absent. A delete that may name a property of
                // the global object itself still drops that property.
                if write.operation == NativeMutationOperation::Delete {
                    if roots.contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::GlobalObject,
                    )) {
                        let domain = evidence(key).domain;
                        next.names
                            .retain(|name| !key_domain_may_name(&domain, name));
                        if matches!(
                            domain,
                            PropertyKeyDomain::Symbol | PropertyKeyDomain::Unknown
                        ) {
                            next.registered_symbols = false;
                        }
                    }
                    continue;
                }
                for evidence in std::iter::once(key)
                    .chain(&write.additional_keys)
                    .map(evidence)
                {
                    let exact = evidence.dependencies.is_empty()
                        || evidence.dependencies == BTreeSet::from([NativeIntrinsic::Symbol])
                            && symbol_for_intact;
                    let domain = if exact {
                        evidence.domain
                    } else {
                        PropertyKeyDomain::Unknown
                    };
                    if matches!(domain, PropertyKeyDomain::Unknown)
                        || key_domain_may_name(&domain, "__proto__")
                    {
                        return BundleGlobalNames::default();
                    }
                    if key_domain_may_name(&domain, "for") || key_domain_may_name(&domain, "Symbol")
                    {
                        next_symbol_for_intact = false;
                    }
                    if KEYED_COLLECTIONS
                        .iter()
                        .any(|name| key_domain_may_name(&domain, name))
                    {
                        next.fresh_collections = false;
                    }
                    let stores = |slot: BundleSlot<'_>| {
                        write.operation == NativeMutationOperation::PropertyAssignment
                            && write
                                .value
                                .as_ref()
                                .is_some_and(|value| context.keeps(value, slot))
                    };
                    next.names.retain(|name| {
                        !key_domain_may_name(&domain, name) || stores(BundleSlot::Name(name))
                    });
                    if matches!(domain, PropertyKeyDomain::Symbol)
                        && !stores(BundleSlot::RegisteredSymbol)
                    {
                        next.registered_symbols = false;
                    }
                }
            }
            for (binding, value) in &assignments {
                if declared.contains(binding) {
                    continue;
                }
                // A sloppy assignment to an undeclared name writes the global
                // property.
                if next.names.contains(binding.0.as_ref())
                    && !context.keeps(value, BundleSlot::Name(binding.0.as_ref()))
                {
                    next.names.remove(binding.0.as_ref());
                }
                if binding.0.as_ref() == "Symbol" {
                    next_symbol_for_intact = false;
                }
                if KEYED_COLLECTIONS.contains(&binding.0.as_ref()) {
                    next.fresh_collections = false;
                }
            }
            // A legacy accessor method can install a getter that runs on a later
            // read. A computed method key counts whenever its domain may spell
            // one, unless an author premise covers its callable.
            let installs_accessor = |property: &Option<String>, key: &MemberKey| {
                let domain = match (property, key) {
                    (Some(name), _) => {
                        PropertyKeyDomain::FiniteStrings(BTreeSet::from([name.clone()]))
                    }
                    (None, MemberKey::Computed(Some(binding))) => {
                        let evidence = evidence(&PropertyKeyExpr::Binding(binding.clone()));
                        if evidence.dependencies.is_empty()
                            || evidence.dependencies == BTreeSet::from([NativeIntrinsic::Symbol])
                                && symbol_for_intact
                        {
                            evidence.domain
                        } else {
                            PropertyKeyDomain::Unknown
                        }
                    }
                    (None, _) => PropertyKeyDomain::Unknown,
                };
                LEGACY_ACCESSOR_METHODS
                    .iter()
                    .any(|method| key_domain_may_name(&domain, method))
            };
            for call in &self.prototype_calls {
                let ValueExpr::Member(base, property, key, _) = &call.callee else {
                    continue;
                };
                if call.kind != CallKind::Call
                    || !installs_accessor(property, key)
                    || context
                        .roots(base)
                        .iter()
                        .all(|root| *root == NativeReceiverRoot::BundleGlobal)
                {
                    continue;
                }
                let premise = premises.iter().find(|premise| {
                    property.is_none() && premise.function_span.contains(call.span)
                });
                let Some(premise) = premise else {
                    return BundleGlobalNames::default();
                };
                next.no_accessor_install_callables
                    .insert(premise.reference.clone());
            }
            if !next_symbol_for_intact {
                next.registered_symbols = false;
            }
            if next == current && next_symbol_for_intact == symbol_for_intact {
                return next;
            }
            current = next;
            symbol_for_intact = next_symbol_for_intact;
        }
    }
}

pub(super) struct SemanticShapeInputs<'a> {
    pub(super) classes: LocalClassConstructionShapes,
    pub(super) sets: SetIterableShapes,
    pub(super) array_spreads: &'a StableArraySpreadShapes,
    pub(super) callables: CallableInferenceInputs,
}

impl ExecutionIndex {
    fn declared_bindings(&self) -> BTreeSet<Id> {
        self.property_key_declarations
            .iter()
            .chain(self.hoisted_bindings.keys())
            .chain(self.imported_bindings.iter())
            .chain(self.class_ids.iter())
            .cloned()
            .collect()
    }

    pub(crate) fn validated_native_operation_inputs(&self) -> Vec<ResolvedNativeOperationInput> {
        self.resolved_native_operation_inputs.clone()
    }

    fn apply_native_integrity(&mut self, integrity: &NativeIntegrity) {
        self.mutated_intrinsic_roots = integrity.mutated.clone();
        self.native_integrity_witnesses = integrity.witnesses.clone();
        self.native_integrity_value_input_refs = integrity.consumed_value_inputs.clone();
        self.native_integrity_host_behavior = integrity.consumed_host_behavior.clone();
        self.never_written = integrity.never_written.clone();
        self.never_written_overrode = !integrity.never_written_overrides.is_empty();
        self.integrity = self.integrity_flags();
    }

    /// Literal operands are primitive without executing source. An identifier
    /// operand is admitted only when the completed census proves the same
    /// canonical binding has one earlier direct primitive initializer, with
    /// no dynamic-scope access or recorded write. This establishes primitive
    /// type, not an exact value; ordinary reads still carry order and TDZ.
    fn has_stable_primitive_set_array(&self, declaration: &BindingDeclarationWrite) -> bool {
        if !self.dynamic_scope_owners.is_empty() {
            return false;
        }
        declaration
            .set_from_primitive_array_aliases
            .as_ref()
            .is_some_and(|aliases| {
                aliases.iter().all(|binding| {
                    !self.reassigned_bindings.contains(binding)
                        && !self.binding_target_writes.contains(binding)
                        && matches!(
                            self.binding_declaration_writes.get(binding).map(Vec::as_slice),
                            Some([source]) if source.direct_top_level
                                && source.simple_identifier
                                && source.primitive_literal_initializer
                                && source.owner < declaration.owner
                        )
                })
            })
    }

    /// Source-only construction proofs. Every protocol premise comes from the
    /// completed census; this operation must run before its roots seed flow.
    fn infer_set_constructions(
        &self,
        integrity: &IntegrityFlags,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        use crate::analysis_provenance::InferenceRule;
        use IntrinsicSlot::{ArrayIteratorPrototype, Global, Prototype, SetIteratorPrototype};
        use NativeIntrinsic::{Array, Set};
        if !self.dynamic_scope_owners.is_empty() {
            return BTreeMap::new();
        }
        let declarations: BTreeMap<_, _> = self
            .binding_declaration_writes
            .iter()
            .filter_map(|(binding, writes)| {
                let [write] = writes.as_slice() else {
                    return None;
                };
                (write.direct_top_level
                    && write.simple_identifier
                    && !self.reassigned_bindings.contains(binding))
                .then_some((write.owner, (binding, write)))
            })
            .collect();
        let set_binding = ("Set".into(), swc_common::SyntaxContext::empty());
        if self.declared_bindings().iter().any(|id| id.0 == "Set")
            || self.property_key_declarations.contains(&set_binding)
        {
            return BTreeMap::new();
        }
        let slots = [
            Global(Set),
            Prototype(Set),
            Prototype(Array),
            ArrayIteratorPrototype,
        ];
        let mut proofs: BTreeMap<u32, InferredSetConstruction> = BTreeMap::new();
        let mut sites_by_binding = BTreeMap::new();
        for (owner, (binding, declaration)) in &declarations {
            let Some(ValueExpr::New(callee, _, site)) = &declaration.initializer else {
                continue;
            };
            if !matches!(callee.as_ref(), ValueExpr::Binding(id) if id.0 == "Set") {
                continue;
            }
            let operation = if self.has_stable_primitive_set_array(declaration) {
                InferredSetOperation::PrimitiveArray
            } else if let Some((source, source_span, spread_token_span)) =
                &declaration.set_from_set_spread_source
            {
                let Some(seed_site) = sites_by_binding.get(source) else {
                    continue;
                };
                let Some(seed) = proofs.get(seed_site) else {
                    continue;
                };
                if !matches!(seed.operation, InferredSetOperation::PrimitiveArray) {
                    continue;
                }
                let Some((seed_owner, _)) = declarations.iter().find(|(_, (id, _))| *id == source)
                else {
                    continue;
                };
                if seed_owner >= owner {
                    continue;
                }
                let mut interval_bindings = Vec::new();
                let mut dependencies = ProofDependencies::default();
                let mut valid = true;
                for ordinal in seed_owner.0..owner.0 {
                    let Some((id, write)) = declarations.get(&SourceUnitOrdinal(ordinal)) else {
                        valid = false;
                        break;
                    };
                    let Some(proof) = sites_by_binding.get(*id).and_then(|site| proofs.get(site))
                    else {
                        valid = false;
                        break;
                    };
                    if !self.has_stable_primitive_set_array(write)
                        || self.original_statement_spans[ordinal]
                            != self.original_statement_spans[owner.0]
                    {
                        valid = false;
                        break;
                    }
                    interval_bindings.push((*id).clone());
                    dependencies.extend(&proof.dependencies);
                }
                if !valid {
                    continue;
                }
                interval_bindings.push((*binding).clone());
                InferredSetOperation::ProtectedSpread {
                    source: source.clone(),
                    interval: Box::new(crate::analysis_inputs::StableSetIterableUse {
                        source_span: *source_span,
                        spread_token_span: *spread_token_span,
                        interval_bindings,
                        dependencies,
                    }),
                }
            } else {
                continue;
            };
            let required_slots = match operation {
                InferredSetOperation::PrimitiveArray => slots.to_vec(),
                InferredSetOperation::ProtectedSpread { .. } => {
                    [slots.as_slice(), &[SetIteratorPrototype]].concat()
                }
            };
            let Some(mut premise) = native_premise(
                integrity,
                &required_slots,
                None,
                InferenceRule::PrimitiveSetIteration,
            ) else {
                continue;
            };
            let mut operation = operation;
            if let InferredSetOperation::ProtectedSpread { interval, .. } = &mut operation {
                premise.dependencies.extend(&interval.dependencies);
                interval.dependencies = premise.dependencies.clone();
            }
            sites_by_binding.insert((*binding).clone(), site.allocation_id());
            proofs.insert(
                site.allocation_id(),
                InferredSetConstruction {
                    binding: (*binding).clone(),
                    operation,
                    dependencies: premise.dependencies,
                    inference: premise.inference_use,
                },
            );
        }
        proofs
    }

    /// Build one immutable context from the canonical source index and the
    /// completed native-write census. Callable candidates are input facts;
    /// their returned-record constructors participate in the same native
    /// identity and integrity proofs as local-class fields.
    fn completed_native_rules(&self) -> SemanticRules {
        SemanticRules {
            native: NativePremises {
                integrity: self.integrity.clone(),
                bindings: NativeBindingContext::default(),
            },
            dynamic_scope: !self.dynamic_scope_owners.is_empty(),
            ..SemanticRules::default()
        }
    }

    /// The author and host premises the completed census consumed.
    pub(super) fn native_integrity_dependencies(&self) -> ProofDependencies {
        let bundle_globals = self
            .native_integrity_host_behavior
            .contains(&HostBehaviorKey::PredefinedGlobals);
        ProofDependencies {
            value_inputs: self.native_integrity_value_input_refs.clone(),
            host_behavior: self.native_integrity_host_behavior.clone(),
            no_accessor_install_callables: if bundle_globals {
                self.census_globals
                    .bundle
                    .no_accessor_install_callables
                    .clone()
            } else {
                BTreeSet::new()
            },
            program_premises: if self.never_written_overrode {
                BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten])
            } else {
                BTreeSet::new()
            },
            ..Default::default()
        }
    }

    /// The completed census as slot flags, with the `IntrinsicIntact` axioms
    /// of every validated native input.
    pub(super) fn integrity_flags(&self) -> IntegrityFlags {
        IntegrityFlags::new(
            self.dynamic_scope_owners.is_empty(),
            self.mutated_intrinsic_roots.clone(),
            &self.native_integrity_witnesses,
            self.native_integrity_dependencies(),
            self.intrinsic_axioms(),
            PrototypeWrites::new(
                self.native_prototype_mutations
                    .iter()
                    .map(|mutation| mutation.properties.as_ref()),
            ),
        )
        .with_never_written(self.never_written.clone())
    }

    /// Each validated input asserts the slots its operation reads, at the
    /// exact site its consumer queries: the call for a callee identity, the
    /// constructor for a Set, the member read for an own-data read and the
    /// initializer for an array spread.
    fn intrinsic_axioms(&self) -> BTreeMap<u32, IntrinsicIntact> {
        use IntrinsicSlot::{
            ArrayIteratorPrototype, Global, Prototype, SetIteratorPrototype, Static,
        };
        use NativeIntrinsic::{Array, Object, Promise, Set, Symbol, WeakMap};
        use spec::{NativeCallIdentityKind as Callee, NativeOperationKind as Operation};
        const SET_FROM_ARRAY: &[IntrinsicSlot] = &[
            Global(Set),
            Prototype(Set),
            Prototype(Array),
            ArrayIteratorPrototype,
        ];
        self.resolved_native_operation_inputs
            .iter()
            .filter_map(|input| {
                let (site, slots): (Span, Vec<IntrinsicSlot>) = match input.evidence.operation {
                    // The builtin `Symbol` / `Symbol.for` rows at the call. A
                    // zero-argument or literal-string `Symbol` call, or a
                    // literal-string `Symbol.for` call, also makes the source
                    // unit pure (`symbol_literal_proof`).
                    NativeInputKind::Operation(Operation::Symbol) => {
                        (input.call_span, vec![Global(Symbol)])
                    }
                    NativeInputKind::Operation(Operation::SymbolFor) => {
                        (input.call_span, vec![Global(Symbol), Static(Symbol, "for")])
                    }
                    // The host Promise timing check, only with the matching
                    // `reentry_free_operations` entry.
                    NativeInputKind::Operation(Operation::PromiseResolve) => (
                        input.call_span,
                        vec![Global(Promise), Static(Promise, "resolve")],
                    ),
                    // A fresh Set value-flow origin and Set-content allocation
                    // cell at the constructor, and constructor purity. A
                    // spread additionally needs the certified seed interval
                    // (`certify_stable_set_spreads`). Later Set methods and
                    // iteration keep their own checks.
                    NativeInputKind::Operation(Operation::SetFromPrimitiveArray) => {
                        (input.call_span, SET_FROM_ARRAY.to_vec())
                    }
                    NativeInputKind::Operation(Operation::SetFromSetSpread) => (
                        input.call_span,
                        [SET_FROM_ARRAY, &[SetIteratorPrototype]].concat(),
                    ),
                    // Timing, purity and heap ordering of this construction;
                    // the platform result has no ordinary-object certificate.
                    NativeInputKind::Operation(Operation::TextEncoderEmpty) => {
                        (input.call_span, vec![Global(NativeIntrinsic::TextEncoder)])
                    }
                    NativeInputKind::Operation(Operation::MapEmpty) => {
                        (input.call_span, vec![Global(NativeIntrinsic::Map)])
                    }
                    NativeInputKind::Operation(Operation::WeakMapEmpty) => {
                        (input.call_span, vec![Global(WeakMap)])
                    }
                    NativeInputKind::Operation(Operation::SetEmpty) => {
                        (input.call_span, vec![Global(Set)])
                    }
                    // Purity, timing and heap ordering at the read; calling the
                    // read function keeps ordinary call analysis.
                    NativeInputKind::Operation(Operation::ObjectOwnDataFunctionRead) => {
                        let [declaration] = self
                            .binding_declaration_writes
                            .get(&input.binding)?
                            .as_slice()
                        else {
                            return None;
                        };
                        let property = object_own_data_read_property(
                            declaration.initializer.as_ref()?,
                            |id| self.declared_bindings().contains(id),
                        )?;
                        (
                            input.initializer_span,
                            vec![Global(Object), Static(Object, property)],
                        )
                    }
                    NativeInputKind::Operation(Operation::SymbolWellKnownClassKeys) => {
                        let [declaration] = self
                            .binding_declaration_writes
                            .get(&input.binding)?
                            .as_slice()
                        else {
                            return None;
                        };
                        let property =
                            self.symbol_class_key_read_property(declaration, input.call_span)?;
                        (
                            input.call_span,
                            vec![Global(Symbol), Static(Symbol, property)],
                        )
                    }
                    // A primitive own-data read supplies no original native identity.
                    NativeInputKind::Operation(Operation::SymbolPrimitiveClassKeys) => return None,
                    // The spread of the co-located primitive-array interval.
                    NativeInputKind::Operation(Operation::ArraySpreadFromPrimitiveArrays) => (
                        input.initializer_span,
                        vec![Prototype(Array), ArrayIteratorPrototype],
                    ),
                    // The `Object.freeze` row at that call. A fresh plain-data
                    // literal argument also drops the call's opaque heap
                    // barrier; the returned value gains no value-flow origin.
                    NativeInputKind::CallIdentity(Callee::ObjectFreeze) => (
                        input.call_span,
                        vec![Global(Object), Static(Object, "freeze")],
                    ),
                    // The empty-collection premise of the local-class proof;
                    // `Set.prototype` methods are not asserted.
                    NativeInputKind::CallIdentity(Callee::SetEmptyPrivateField) => {
                        (input.call_span, vec![Global(Set)])
                    }
                };
                Some((
                    site.lo.0,
                    IntrinsicIntact {
                        slots: slots.into_iter().collect(),
                        reference: input.evidence.reference.clone(),
                    },
                ))
            })
            .collect()
    }

    pub(crate) fn prepare_semantic_facts(
        &self,
        flow: &ChunkValueFlow,
        shadowed: &BTreeSet<&'static str>,
        shapes: SemanticShapeInputs<'_>,
        source_facts: Option<&SemanticFacts>,
    ) -> Arc<SemanticFacts> {
        let SemanticShapeInputs {
            classes,
            sets,
            array_spreads,
            callables,
        } = shapes;
        let mut collection_bindings = BTreeMap::new();
        let constructors = classes
            .classes
            .values()
            .flatten()
            .map(|(binding, _)| binding.clone())
            .chain(
                callables
                    .namespace_wrappers
                    .values()
                    .map(|candidate| candidate.weak_map.clone()),
            );
        for binding in constructors {
            let Some(row) = builtins::lookup(binding.0.as_ref(), None, Invocation::Construct)
            else {
                continue;
            };
            if row.allocates.is_some_and(Kind::is_keyed_collection) {
                collection_bindings.insert(binding, row);
            }
        }
        // Rules read binding identity only for these candidate callees.
        let declared_bindings = collection_bindings
            .keys()
            .chain(
                callables
                    .namespace_wrappers
                    .values()
                    .map(|candidate| &candidate.object),
            )
            .filter(|binding| flow.is_declared(binding))
            .cloned()
            .collect();
        let completed = self.completed_native_rules();
        Arc::new(SemanticFacts::solve_with_source(
            SemanticRules {
                classes,
                sets,
                array_spreads: array_spreads.by_initializer.keys().copied().collect(),
                collection_bindings,
                native: NativePremises {
                    bindings: NativeBindingContext {
                        declared_bindings,
                        shadowed_globals: shadowed.clone(),
                        global_object_names: self.global_object_names.clone(),
                    },
                    ..completed.native
                },
                descriptor_copy_providers: self.descriptor_copy_factories.clone(),
                descriptor_copy_calls: self
                    .descriptor_copy_calls
                    .iter()
                    .filter_map(|(site, candidate)| {
                        let shape = self.own_descriptor_shapes.get(&candidate.source_object)?;
                        let data_keys = shape
                            .properties
                            .iter()
                            .filter_map(|(key, descriptor)| {
                                matches!(descriptor, OwnPropertyDescriptorSyntax::Data)
                                    .then_some(key.clone())
                            })
                            .collect();
                        let accessor_keys = shape
                            .properties
                            .iter()
                            .filter_map(|(key, descriptor)| {
                                matches!(descriptor, OwnPropertyDescriptorSyntax::Accessor { .. })
                                    .then_some(key.clone())
                            })
                            .collect();
                        Some((
                            site.source_span().lo.0,
                            SemanticDescriptorCopyCallCandidate {
                                source_object: candidate.source_object,
                                provider_binding: candidate.provider_binding.clone(),
                                signature: candidate.signature.clone(),
                                data_keys,
                                accessor_keys,
                            },
                        ))
                    })
                    .collect(),
                callables,
                dynamic_scope: completed.dynamic_scope,
                ..SemanticRules::default()
            },
            source_facts,
        ))
    }

    fn validate_value_inputs(
        &mut self,
        selected: &[SelectedBindingValueInput],
        resolved: &[ResolvedBindingValueInput],
        raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
        declared: &BTreeSet<Id>,
    ) {
        for input in selected {
            let request = &input.evidence.spec_location;
            if !self.dynamic_scope_owners.is_empty() {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because provider analysis contains dynamic-scope contamination",
                    input.selected_binding
                ));
                continue;
            }
            let candidates: Vec<_> = self
                .binding_declaration_writes
                .iter()
                .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
                .flat_map(|(binding, writes)| {
                    writes.iter().filter_map(move |write| {
                        (write.simple_identifier
                            && write.direct_top_level
                            && span_contains(input.selected_statement_span, write.binding_span))
                        .then_some((binding.clone(), write.clone()))
                    })
                })
                .collect();
            let [(binding, declaration)] = candidates.as_slice() else {
                self.value_input_errors.push(format!(
                    "{request}.source_match selected `{}`, but the canonical execution index found {} direct simple top-level declarations in that source item; exactly one is required",
                    input.selected_binding,
                    candidates.len()
                ));
                continue;
            };
            let binding = (*binding).clone();
            let declaration = (*declaration).clone();
            if self
                .binding_declaration_writes
                .get(&binding)
                .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because its canonical binding has repeated or pattern declaration sites",
                    input.selected_binding
                ));
                continue;
            }
            if self
                .property_key_assignments
                .get(&binding)
                .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because the canonical write index found a possible reassignment, update, destructuring write, or deferred write",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializer) = declaration.initializer.as_ref() else {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because the selected declaration has no initializer",
                    input.selected_binding
                ));
                continue;
            };
            let check_known_identity =
                matches!(initializer, ValueExpr::Binding(_) | ValueExpr::Member(..));
            if check_known_identity {
                let roots = intrinsic_roots(
                    initializer,
                    raw_aliases,
                    &self.census_globals,
                    declared,
                    &BTreeMap::new(),
                    &CallableReturnInputs::default(),
                );
                let contradictions: BTreeSet<_> = roots
                    .iter()
                    .filter_map(|root| match root {
                        NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => None,
                        NativeReceiverRoot::Intrinsic(root)
                        | NativeReceiverRoot::IntrinsicPrototype(root)
                        | NativeReceiverRoot::IntrinsicDescendant(root) => Some(*root),
                        NativeReceiverRoot::OrdinaryDirect { .. }
                        | NativeReceiverRoot::OrdinaryDescendant { .. }
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => None,
                    })
                    .collect();
                if !contradictions.is_empty() {
                    self.value_input_errors.push(format!(
                        "{request} contradicts recognized native/global identity for `{}`: {contradictions:?}",
                        input.selected_binding
                    ));
                    continue;
                }
            }
            if !is_opaque_member_slot(initializer) {
                self.value_input_errors.push(format!(
                    "{request} uses an unsupported initializer category for `{}`; this version accepts only opaque binding-rooted member reads",
                    input.selected_binding
                ));
                continue;
            }
            let raw_roots = intrinsic_roots(
                initializer,
                raw_aliases,
                &self.census_globals,
                declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            if !raw_roots.iter().any(|root| {
                matches!(
                    root,
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                )
            }) {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because source-only analysis did not establish an existing unknown-global receiver root",
                    input.selected_binding
                ));
                continue;
            }
            if self.ordinary_input_refs.contains_key(&binding) {
                self.value_input_errors.push(format!(
                    "{request} duplicates a value input for canonical binding `{}`",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializer_span) = declaration.initializer_span else {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because its initializer has no source span",
                    input.selected_binding
                ));
                continue;
            };
            let mut evidence = input.evidence.clone();
            evidence.selected_binding = input.selected_binding.clone();
            self.ordinary_input_refs.insert(
                binding.clone(),
                (evidence.reference.clone(), declaration.owner),
            );
            self.value_input_evidence
                .insert(evidence.reference.clone(), evidence.clone());
            self.resolved_value_inputs.push(ResolvedBindingValueInput {
                evidence,
                binding,
                declaration_span: declaration.binding_span,
                initializer_span,
                initializer_ordinal: declaration.owner,
            });
        }
        for input in resolved {
            let request = &input.evidence.spec_location;
            if !self.dynamic_scope_owners.is_empty() {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because provider analysis contains dynamic-scope contamination",
                    input.evidence.selected_binding
                ));
                continue;
            }
            let Some(writes) = self.binding_declaration_writes.get(&input.binding) else {
                self.value_input_errors.push(format!(
                    "{request} no longer resolves to one canonical declaration in this prepared AST"
                ));
                continue;
            };
            let [declaration] = writes.as_slice() else {
                self.value_input_errors.push(format!(
                    "{request} no longer resolves to one canonical declaration in this prepared AST"
                ));
                continue;
            };
            if !declaration.direct_top_level
                || !declaration.simple_identifier
                || declaration.binding_span != input.declaration_span
                || declaration.initializer_span != Some(input.initializer_span)
                || declaration.owner != input.initializer_ordinal
                || input.binding.0.as_ref() != input.evidence.selected_binding
                || self
                    .property_key_assignments
                    .get(&input.binding)
                    .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} no longer identifies the same stable simple top-level binding and complete write summary"
                ));
                continue;
            }
            let Some(initializer) = declaration.initializer.as_ref() else {
                self.value_input_errors.push(format!(
                    "{request} no longer has the source initializer used by the value input"
                ));
                continue;
            };
            if !is_opaque_member_slot(initializer) {
                self.value_input_errors.push(format!(
                    "{request} now uses an unsupported initializer category; expected a binding-rooted opaque member read"
                ));
                continue;
            }
            let roots = intrinsic_roots(
                initializer,
                raw_aliases,
                &self.census_globals,
                declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            let contradiction = roots.iter().any(|root| {
                matches!(
                    root,
                    NativeReceiverRoot::Intrinsic(root)
                        | NativeReceiverRoot::IntrinsicPrototype(root)
                        | NativeReceiverRoot::IntrinsicDescendant(root)
                        if *root != NativeIntrinsic::UnknownGlobalValue
                )
            });
            if contradiction {
                self.value_input_errors.push(format!(
                    "{request} contradicts a recognized native/global initializer in this prepared AST"
                ));
                continue;
            }
            if !roots.iter().any(|root| {
                matches!(
                    root,
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                )
            }) {
                self.value_input_errors.push(format!(
                    "{request} cannot refine this binding because source-only analysis did not establish an existing unknown-global receiver root"
                ));
                continue;
            }
            if self.ordinary_input_refs.contains_key(&input.binding) {
                self.value_input_errors.push(format!(
                    "{request} duplicates a captured value input for canonical binding `{}`",
                    input.evidence.selected_binding
                ));
                continue;
            }
            self.ordinary_input_refs.insert(
                input.binding.clone(),
                (input.evidence.reference.clone(), input.initializer_ordinal),
            );
            self.value_input_evidence
                .insert(input.evidence.reference.clone(), input.evidence.clone());
            self.resolved_value_inputs.push(input.clone());
        }
    }

    fn validate_native_operation_inputs(
        &mut self,
        selected: &[SelectedNativeOperationInput],
        resolved: &[ResolvedNativeOperationInput],
        array_spreads: &StableArraySpreadShapes,
    ) {
        if selected.is_empty() && resolved.is_empty() {
            return;
        }
        if !self.dynamic_scope_owners.is_empty() {
            for input in selected {
                self.native_operation_input_errors.push(format!(
                    "{} cannot assert native operation identity because provider analysis contains dynamic-scope contamination",
                    input.evidence.spec_location
                ));
            }
            for input in resolved {
                self.native_operation_input_errors.push(format!(
                    "{} cannot assert native operation identity because provider analysis contains dynamic-scope contamination",
                    input.evidence.spec_location
                ));
            }
            return;
        }
        for input in selected {
            let request = &input.evidence.spec_location;
            if input.evidence.operation
                == NativeInputKind::Operation(
                    spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                )
            {
                self.validate_selected_array_spread_input(input, array_spreads);
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::ObjectOwnDataFunctionRead)
            {
                self.validate_selected_object_read(input);
                continue;
            }
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(
                    spec::NativeOperationKind::SymbolWellKnownClassKeys
                        | spec::NativeOperationKind::SymbolPrimitiveClassKeys
                )
            ) {
                match self.resolve_symbol_class_keys(input) {
                    Ok(inputs) => {
                        for input in inputs {
                            self.native_operation_input_evidence
                                .insert(input.call_span.into(), input.evidence.clone());
                            self.resolved_native_operation_inputs.push(input);
                        }
                    }
                    Err(error) => self.native_operation_input_errors.push(error),
                }
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::SetEmptyPrivateField)
            {
                if let Some(owner) = self.known_global_set_mutation() {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified global Set slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let Some((binding, declaration_span, call)) = self.private_empty_set_field_call(
                    &input.selected_binding,
                    input.selected_statement_span,
                ) else {
                    self.native_operation_input_errors.push(format!(
                        "{request} requires one stable non-derived implicit-constructor class with exactly one direct zero-argument unshadowed private-field new Set site"
                    ));
                    continue;
                };
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(call.span.into(), evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding,
                        selected_statement_span: input.selected_statement_span,
                        declaration_span,
                        initializer_span: call.span,
                        call_span: call.span,
                        initializer_ordinal: call.owner,
                        spread_source: None,
                        spread_source_span: None,
                        spread_token_span: None,
                    });
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            {
                if let Some(owner) = self.known_object_freeze_mutation() {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified required Object.freeze slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let candidates: Vec<_> = self
                    .hoisted_bindings
                    .keys()
                    .filter(|binding| binding.0.as_ref() == input.selected_binding)
                    .filter(|binding| {
                        self.hoisted_bindings
                            .get(*binding)
                            .and_then(|key| self.callable_spans.get(key))
                            .is_some_and(|span| span_contains(input.selected_statement_span, *span))
                    })
                    .collect();
                let [binding] = candidates.as_slice() else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select exactly one direct top-level function declaration"
                    ));
                    continue;
                };
                let Some(call) =
                    self.direct_freeze_return_call(binding, input.selected_statement_span)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} requires a nonasync top-level function whose sole statement directly returns one unshadowed Object.freeze call"
                    ));
                    continue;
                };
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(call.span.into(), evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: (*binding).clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: self.callable_spans[&self.hoisted_bindings[*binding]],
                        initializer_span: call.span,
                        call_span: call.span,
                        initializer_ordinal: call.owner,
                        spread_source: None,
                        spread_source_span: None,
                        spread_token_span: None,
                    });
                continue;
            }
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
                    | NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
            ) {
                let spread = input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread);
                let known_mutation = if spread {
                    self.known_set_spread_protocol_mutation()
                } else {
                    self.known_set_protocol_mutation()
                };
                if let Some(owner) = known_mutation {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified required native slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let candidates: Vec<_> = self
                    .binding_declaration_writes
                    .iter()
                    .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
                    .flat_map(|(binding, writes)| {
                        writes.iter().filter_map(move |write| {
                            (write.simple_identifier
                                && write.direct_top_level
                                && span_contains(input.selected_statement_span, write.binding_span))
                            .then_some((binding.clone(), write.clone()))
                        })
                    })
                    .collect();
                let [(binding, declaration)] = candidates.as_slice() else {
                    self.native_operation_input_errors.push(format!(
                        "{request}.source_match selected `{}`, but the canonical execution index found {} direct simple top-level declarations in that source item; exactly one is required",
                        input.selected_binding,
                        candidates.len()
                    ));
                    continue;
                };
                let Some(ValueExpr::New(callee, _, call_site)) = declaration.initializer.as_ref()
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select a direct supported Set constructor initializer"
                    ));
                    continue;
                };
                let ValueExpr::Binding(constructor) = callee.as_ref() else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select a direct uncomputed Set identifier"
                    ));
                    continue;
                };
                if !(if spread {
                    declaration.set_from_set_spread_source.is_some()
                } else {
                    self.has_stable_primitive_set_array(declaration)
                }) || constructor.0.as_ref() != "Set"
                    || self.property_key_declarations.contains(constructor)
                    || self.imported_bindings.contains(constructor)
                {
                    let shape = if spread {
                        "a direct unshadowed new Set([...source, primitive literals]) operation with one dense nonspread array literal"
                    } else {
                        "a direct unshadowed new Set([primitive literals or earlier stable primitive bindings]) operation with one dense nonspread array literal"
                    };
                    self.native_operation_input_errors
                        .push(format!("{request} requires {shape}"));
                    continue;
                }
                let Some(call) = self.top_calls[declaration.owner.0]
                    .iter()
                    .find(|call| CallSite::from(call.span) == *call_site)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer resolves to a direct top-level constructor site"
                    ));
                    continue;
                };
                if call.kind != CallKind::New || call.has_spread || call.caller.is_some() {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select an ordinary direct nonspread constructor site"
                    ));
                    continue;
                }
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(*call_site, evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: binding.clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: declaration.binding_span,
                        initializer_span: declaration.initializer_span.unwrap_or(call.span),
                        call_span: call.span,
                        initializer_ordinal: declaration.owner,
                        spread_source: declaration
                            .set_from_set_spread_source
                            .as_ref()
                            .map(|(binding, _, _)| binding.clone()),
                        spread_source_span: declaration
                            .set_from_set_spread_source
                            .as_ref()
                            .map(|(_, span, _)| *span),
                        spread_token_span: declaration
                            .set_from_set_spread_source
                            .as_ref()
                            .map(|(_, _, span)| *span),
                    });
                continue;
            }
            if let Some(owner) =
                self.known_global_native_operation_mutation(input.evidence.operation)
            {
                self.native_operation_input_errors.push(format!(
                    "{request} is contradicted by a positively identified {} (source unit {})",
                    native_operation_mutation_name(input.evidence.operation),
                    owner.0
                ));
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor)
                && let Some(owner) = self.known_symbol_for_mutation()
            {
                self.native_operation_input_errors.push(format!(
                    "{request} is contradicted by a positively identified Symbol.for replacement or accessor installation (source unit {})",
                    owner.0
                ));
                continue;
            }
            let candidates: Vec<_> = self
                .binding_declaration_writes
                .iter()
                .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
                .flat_map(|(binding, writes)| {
                    writes.iter().filter_map(move |write| {
                        (write.simple_identifier
                            && write.direct_top_level
                            && span_contains(input.selected_statement_span, write.binding_span))
                        .then_some((binding.clone(), write.clone()))
                    })
                })
                .collect();
            let [(binding, declaration)] = candidates.as_slice() else {
                self.native_operation_input_errors.push(format!(
                    "{request}.source_match selected `{}`, but the canonical execution index found {} direct simple top-level declarations in that source item; exactly one is required",
                    input.selected_binding,
                    candidates.len()
                ));
                continue;
            };
            let binding = (*binding).clone();
            let declaration = (*declaration).clone();
            if declaration.initializer.is_none() {
                self.native_operation_input_errors.push(format!(
                    "{request} selected `{}` without an initializer",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializers) =
                native_operation_initializers(&declaration, input.evidence.operation)
            else {
                let object_shape = if input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty)
                {
                    ", or an object containing only direct empty Map data fields"
                } else {
                    ""
                };
                self.native_operation_input_errors.push(format!(
                    "{request} must select a direct top-level binding initialized by a {}{object_shape}",
                    native_operation_call_name(input.evidence.operation)
                ));
                continue;
            };
            if declaration.empty_map_object_sites.is_some()
                && self.binding_declaration_writes[&binding].len() != 1
            {
                self.native_operation_input_errors.push(format!(
                    "{request} must select one unambiguous canonical object binding declaration"
                ));
                continue;
            }
            for (callee, args, call_site) in initializers {
                let Some(call) = self.top_calls[declaration.owner.0]
                    .iter()
                    .find(|call| CallSite::from(call.span) == call_site)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer resolves to a direct top-level call site"
                    ));
                    continue;
                };
                if input.evidence.operation.empty_constructor_name().is_some()
                    && declaration.empty_map_object_sites.is_none()
                    && declaration.initializer_span != Some(call.span)
                {
                    self.native_operation_input_errors.push(format!("{request} must select the direct constructor initializer without a sequence or assignment wrapper"));
                    continue;
                }
                if let Some(reason) = self.native_operation_call_shape_error(
                    input.evidence.operation,
                    callee,
                    args,
                    call_site,
                    call,
                ) {
                    self.native_operation_input_errors
                        .push(format!("{request} {reason}"));
                    continue;
                }
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(call_site, evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: binding.clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: declaration.binding_span,
                        initializer_span: declaration.initializer_span.unwrap_or(call.span),
                        call_span: call.span,
                        initializer_ordinal: declaration.owner,
                        spread_source: None,
                        spread_source_span: None,
                        spread_token_span: None,
                    });
            }
        }

        for input in resolved {
            let request = &input.evidence.spec_location;
            if input.evidence.operation
                == NativeInputKind::Operation(
                    spec::NativeOperationKind::ArraySpreadFromPrimitiveArrays,
                )
            {
                self.validate_resolved_array_spread_input(input, array_spreads);
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::ObjectOwnDataFunctionRead)
            {
                self.validate_resolved_object_read(input);
                continue;
            }
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(
                    spec::NativeOperationKind::SymbolWellKnownClassKeys
                        | spec::NativeOperationKind::SymbolPrimitiveClassKeys
                )
            ) {
                let selected = SelectedNativeOperationInput {
                    evidence: input.evidence.clone(),
                    selected_binding: input.evidence.selected_binding.clone(),
                    selected_statement_span: input.selected_statement_span,
                };
                match self.resolve_symbol_class_keys(&selected) {
                    Ok(inputs) if inputs.iter().any(|candidate| {
                        candidate.binding == input.binding
                            && candidate.declaration_span == input.declaration_span
                            && candidate.initializer_span == input.initializer_span
                            && candidate.call_span == input.call_span
                            && candidate.initializer_ordinal == input.initializer_ordinal
                            && input.spread_source.is_none()
                            && input.spread_source_span.is_none()
                            && input.spread_token_span.is_none()
                    }) => {
                        self.native_operation_input_evidence
                            .insert(input.call_span.into(), input.evidence.clone());
                        self.resolved_native_operation_inputs.push(input.clone());
                    }
                    Ok(_) => self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same exact well-known Symbol class-key read"
                    )),
                    Err(error) => self.native_operation_input_errors.push(error),
                }
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::SetEmptyPrivateField)
            {
                if let Some(owner) = self.known_global_set_mutation() {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified global Set slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let is_same = self
                    .private_empty_set_field_call(
                        &input.evidence.selected_binding,
                        input.selected_statement_span,
                    )
                    .is_some_and(|(binding, declaration_span, call)| {
                        binding == input.binding
                            && declaration_span == input.declaration_span
                            && call.span == input.call_span
                            && call.span == input.initializer_span
                            && call.owner == input.initializer_ordinal
                    });
                if !is_same {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same stable private-field new Set site"
                    ));
                    continue;
                }
                self.native_operation_input_evidence
                    .insert(input.call_span.into(), input.evidence.clone());
                self.resolved_native_operation_inputs.push(input.clone());
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            {
                if let Some(owner) = self.known_object_freeze_mutation() {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified required Object.freeze slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let is_same = self
                    .direct_freeze_return_call(&input.binding, input.selected_statement_span)
                    .is_some_and(|call| {
                        call.span == input.call_span
                            && call.owner == input.initializer_ordinal
                            && input.declaration_span
                                == self.callable_spans[&self.hoisted_bindings[&input.binding]]
                            && input.initializer_span == call.span
                            && input.binding.0.as_ref() == input.evidence.selected_binding
                    });
                if !is_same {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same direct unshadowed Object.freeze return call"
                    ));
                    continue;
                }
                self.native_operation_input_evidence
                    .insert(input.call_span.into(), input.evidence.clone());
                self.resolved_native_operation_inputs.push(input.clone());
                continue;
            }
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
                    | NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
            ) {
                let spread = input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread);
                let known_mutation = if spread {
                    self.known_set_spread_protocol_mutation()
                } else {
                    self.known_set_protocol_mutation()
                };
                if let Some(owner) = known_mutation {
                    self.native_operation_input_errors.push(format!(
                        "{request} is contradicted by a positively identified required native slot write in source unit {}",
                        owner.0
                    ));
                    continue;
                }
                let Some([declaration]) = self
                    .binding_declaration_writes
                    .get(&input.binding)
                    .map(Vec::as_slice)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer resolves to one canonical binding declaration"
                    ));
                    continue;
                };
                let Some(ValueExpr::New(callee, _, call_site)) = declaration.initializer.as_ref()
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer selects a direct Set constructor initializer"
                    ));
                    continue;
                };
                let is_same = declaration.direct_top_level
                    && declaration.simple_identifier
                    && (if spread {
                        declaration.set_from_set_spread_source.as_ref().is_some_and(
                            |(binding, source_span, token_span)| {
                                input.spread_source.as_ref() == Some(binding)
                                    && input.spread_source_span == Some(*source_span)
                                    && input.spread_token_span == Some(*token_span)
                            },
                        )
                    } else {
                        self.has_stable_primitive_set_array(declaration)
                            && input.spread_source.is_none()
                            && input.spread_source_span.is_none()
                            && input.spread_token_span.is_none()
                    })
                    && span_contains(input.selected_statement_span, declaration.binding_span)
                    && declaration.binding_span == input.declaration_span
                    && declaration.initializer_span == Some(input.initializer_span)
                    && declaration.owner == input.initializer_ordinal
                    && input.binding.0.as_ref() == input.evidence.selected_binding
                    && *call_site == CallSite::from(input.call_span)
                    && matches!(callee.as_ref(), ValueExpr::Binding(constructor)
                        if constructor.0.as_ref() == "Set"
                            && !self.property_key_declarations.contains(constructor)
                            && !self.imported_bindings.contains(constructor));
                if !is_same {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same direct unshadowed Set initializer"
                    ));
                    continue;
                }
                self.native_operation_input_evidence
                    .insert(*call_site, input.evidence.clone());
                self.resolved_native_operation_inputs.push(input.clone());
                continue;
            }
            if let Some(owner) =
                self.known_global_native_operation_mutation(input.evidence.operation)
            {
                self.native_operation_input_errors.push(format!(
                    "{request} is contradicted by a positively identified {} (source unit {})",
                    native_operation_mutation_name(input.evidence.operation),
                    owner.0
                ));
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SymbolFor)
                && let Some(owner) = self.known_symbol_for_mutation()
            {
                self.native_operation_input_errors.push(format!(
                    "{request} is contradicted by a positively identified Symbol.for replacement or accessor installation (source unit {})",
                    owner.0
                ));
                continue;
            }
            let Some([declaration]) = self
                .binding_declaration_writes
                .get(&input.binding)
                .map(Vec::as_slice)
            else {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer resolves to one canonical binding declaration"
                ));
                continue;
            };
            let Some((callee, args, call_site)) =
                native_operation_initializers(declaration, input.evidence.operation).and_then(
                    |initializers| {
                        initializers
                            .into_iter()
                            .find(|(_, _, site)| *site == CallSite::from(input.call_span))
                    },
                )
            else {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer selects the same direct native initializer site"
                ));
                continue;
            };
            let Some(call) = self.top_calls[declaration.owner.0]
                .iter()
                .find(|call| CallSite::from(call.span) == call_site)
            else {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer resolves to a direct top-level call site"
                ));
                continue;
            };
            if input.evidence.operation.empty_constructor_name().is_some()
                && declaration.empty_map_object_sites.is_none()
                && declaration.initializer_span != Some(call.span)
            {
                self.native_operation_input_errors.push(format!("{request} must select the direct constructor initializer without a sequence or assignment wrapper"));
                continue;
            }
            if let Some(reason) = self.native_operation_call_shape_error(
                input.evidence.operation,
                callee,
                args,
                call_site,
                call,
            ) {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer identifies the selected operation: {reason}"
                ));
                continue;
            }
            let is_same = declaration.direct_top_level
                && declaration.simple_identifier
                && span_contains(input.selected_statement_span, declaration.binding_span)
                && declaration.binding_span == input.declaration_span
                && declaration.initializer_span == Some(input.initializer_span)
                && declaration.owner == input.initializer_ordinal
                && input.binding.0.as_ref() == input.evidence.selected_binding
                && call_site == CallSite::from(input.call_span);
            if !is_same {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer identifies the same direct unshadowed native initializer"
                ));
                continue;
            }
            self.native_operation_input_evidence
                .insert(call_site, input.evidence.clone());
            self.resolved_native_operation_inputs.push(input.clone());
        }

        let original_len = self.resolved_native_operation_inputs.len();
        let mut references = BTreeMap::new();
        let mut sites = BTreeSet::new();
        self.resolved_native_operation_inputs.retain(|input| {
            let same_initializer = references.get(&input.evidence.reference).is_none_or(
                |previous: &ResolvedNativeOperationInput| {
                    matches!(
                        input.evidence.operation,
                        NativeInputKind::Operation(
                            spec::NativeOperationKind::MapEmpty
                                | spec::NativeOperationKind::SymbolWellKnownClassKeys
                                | spec::NativeOperationKind::SymbolPrimitiveClassKeys
                        )
                    ) && previous.evidence.operation == input.evidence.operation
                        && previous.binding == input.binding
                        && previous.selected_statement_span == input.selected_statement_span
                        && previous.declaration_span == input.declaration_span
                        && previous.initializer_span == input.initializer_span
                        && previous.initializer_ordinal == input.initializer_ordinal
                        && input.initializer_span != input.call_span
                },
            );
            if !same_initializer || !sites.insert(CallSite::from(input.call_span)) {
                return false;
            }
            references.insert(input.evidence.reference.clone(), input.clone());
            true
        });
        if self.resolved_native_operation_inputs.len() != original_len {
            self.native_operation_input_errors.push(
                "native_operation_inputs contain conflicting assertion references or duplicate call sites"
                    .to_owned(),
            );
        }
    }

    fn symbol_class_key_read_property(
        &self,
        declaration: &BindingDeclarationWrite,
        span: Span,
    ) -> Option<&'static str> {
        if !declaration.symbol_class_key_sites.as_ref()?.contains(&span) {
            return None;
        }
        let call = self.top_calls[declaration.owner.0].iter().find(|call| {
            call.kind == CallKind::LocalMemberRead && call.caller.is_none() && call.span == span
        })?;
        symbol_well_known_read_property(&call.callee, |id| self.declared_bindings().contains(id))
    }

    fn resolve_symbol_class_keys(
        &self,
        input: &SelectedNativeOperationInput,
    ) -> Result<Vec<ResolvedNativeOperationInput>, String> {
        let request = &input.evidence.spec_location;
        let candidates: Vec<_> = self
            .binding_declaration_writes
            .iter()
            .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
            .flat_map(|(binding, writes)| {
                writes.iter().filter_map(move |declaration| {
                    (declaration.direct_top_level
                        && declaration.simple_identifier
                        && span_contains(input.selected_statement_span, declaration.binding_span))
                    .then_some((binding, declaration))
                })
            })
            .collect();
        let [(binding, declaration)] = candidates.as_slice() else {
            return Err(format!(
                "{request} must select exactly one direct simple top-level class-variable declaration"
            ));
        };
        if self.binding_declaration_writes[*binding].len() != 1 {
            return Err(format!(
                "{request} requires one unambiguous canonical class binding declaration"
            ));
        }
        let Some(sites) = declaration.symbol_class_key_sites.as_ref() else {
            return Err(format!(
                "{request} requires a direct class initializer without extends, decorators or static blocks, whose computed keys are ordinary methods using only direct Symbol.asyncIterator, Symbol.asyncDispose or Symbol.dispose reads"
            ));
        };
        let initializer_span = declaration
            .initializer_span
            .ok_or_else(|| format!("{request} has no exact class initializer span"))?;
        sites.iter().map(|span| {
            let property = self.symbol_class_key_read_property(declaration, *span)
                .ok_or_else(|| format!("{request} requires exact unshadowed bare-global Symbol class-key reads"))?;
            let contradiction = if input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SymbolPrimitiveClassKeys) {
                self.known_primitive_symbol_read_contradiction(property)
            } else {
                self.known_native_read_contradiction(NativeIntrinsic::Symbol, property)
            };
            if let Some(owner) = contradiction {
                return Err(format!("{request} is contradicted by a known Symbol binding or Symbol.{property} write in source unit {}", owner.0));
            }
            let mut evidence = input.evidence.clone();
            evidence.selected_binding = input.selected_binding.clone();
            Ok(ResolvedNativeOperationInput {
                evidence,
                binding: (*binding).clone(),
                selected_statement_span: input.selected_statement_span,
                declaration_span: declaration.binding_span,
                initializer_span,
                call_span: *span,
                initializer_ordinal: declaration.owner,
                spread_source: None,
                spread_source_span: None,
                spread_token_span: None,
            })
        }).collect()
    }

    fn validate_selected_object_read(&mut self, input: &SelectedNativeOperationInput) {
        let request = &input.evidence.spec_location;
        let candidates: Vec<_> = self
            .binding_declaration_writes
            .iter()
            .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
            .flat_map(|(binding, writes)| {
                writes.iter().filter_map(move |write| {
                    (write.simple_identifier
                        && write.direct_top_level
                        && span_contains(input.selected_statement_span, write.binding_span))
                    .then_some((binding.clone(), write.clone()))
                })
            })
            .collect();
        let [(binding, declaration)] = candidates.as_slice() else {
            self.native_operation_input_errors.push(format!(
                "{request} must select exactly one direct simple top-level declaration of `{}`",
                input.selected_binding
            ));
            return;
        };
        let Some(property) = declaration
            .initializer
            .as_ref()
            .and_then(|initializer| {
                object_own_data_read_property(initializer, |id| {
                    self.declared_bindings().contains(id)
                })
            })
            .map(str::to_owned)
        else {
            self.native_operation_input_errors.push(format!(
                "{request} must select an unshadowed static Object own-data function read initializer"
            ));
            return;
        };
        if let Some(owner) =
            self.known_native_read_contradiction(NativeIntrinsic::Object, &property)
        {
            self.native_operation_input_errors.push(format!(
                "{request} is contradicted by a known Object binding or Object.{property} write in source unit {}", owner.0
            ));
            return;
        }
        let Some(span) = declaration.initializer_span else {
            self.native_operation_input_errors
                .push(format!("{request} has no exact initializer span"));
            return;
        };
        let mut evidence = input.evidence.clone();
        evidence.selected_binding = input.selected_binding.clone();
        self.native_operation_input_evidence
            .insert(CallSite::from(span), evidence.clone());
        self.resolved_native_operation_inputs
            .push(ResolvedNativeOperationInput {
                evidence,
                binding: binding.clone(),
                selected_statement_span: input.selected_statement_span,
                declaration_span: declaration.binding_span,
                initializer_span: span,
                call_span: span,
                initializer_ordinal: declaration.owner,
                spread_source: None,
                spread_source_span: None,
                spread_token_span: None,
            });
    }

    fn validate_resolved_object_read(&mut self, input: &ResolvedNativeOperationInput) {
        let request = &input.evidence.spec_location;
        let Some([declaration]) = self
            .binding_declaration_writes
            .get(&input.binding)
            .map(Vec::as_slice)
        else {
            self.native_operation_input_errors.push(format!(
                "{request} no longer resolves to one canonical binding declaration"
            ));
            return;
        };
        let Some(property) = declaration
            .initializer
            .as_ref()
            .and_then(|initializer| {
                object_own_data_read_property(initializer, |id| {
                    self.declared_bindings().contains(id)
                })
            })
            .map(str::to_owned)
        else {
            self.native_operation_input_errors.push(format!(
                "{request} no longer selects an unshadowed static Object own-data function read"
            ));
            return;
        };
        if !declaration.direct_top_level
            || !declaration.simple_identifier
            || !span_contains(input.selected_statement_span, declaration.binding_span)
            || declaration.binding_span != input.declaration_span
            || declaration.initializer_span != Some(input.initializer_span)
            || input.call_span != input.initializer_span
            || declaration.owner != input.initializer_ordinal
            || input.binding.0.as_ref() != input.evidence.selected_binding
        {
            self.native_operation_input_errors.push(format!(
                "{request} no longer identifies the same exact Object own-data member read"
            ));
            return;
        }
        if let Some(owner) =
            self.known_native_read_contradiction(NativeIntrinsic::Object, &property)
        {
            self.native_operation_input_errors.push(format!(
                "{request} is contradicted by a known Object binding or Object.{property} write in source unit {}", owner.0
            ));
            return;
        }
        self.native_operation_input_evidence
            .insert(CallSite::from(input.call_span), input.evidence.clone());
        self.resolved_native_operation_inputs.push(input.clone());
    }

    fn source_slot_overlap(&self, slot: &NativeWriteSlot) -> NativeSlotOverlap {
        source_slot_overlap(
            self.native_write_census
                .witnesses
                .iter()
                .chain(
                    self.native_write_census
                        .conditional
                        .iter()
                        .map(|write| &write.witness),
                )
                .chain(self.native_integrity_witnesses.iter()),
            slot,
        )
    }

    fn native_operation_call_shape_error(
        &self,
        operation: NativeInputKind,
        callee: &ValueExpr,
        args: &[ValueExpr],
        call_site: CallSite,
        call: &Call,
    ) -> Option<&'static str> {
        if let Some(constructor) = operation.empty_constructor_name() {
            if call.kind != CallKind::New
                || call.has_spread
                || call.caller.is_some()
                || !args.is_empty()
                || !self.bare_constructor_sites.contains(&call_site)
            {
                return Some("must select direct zero-argument native construction without spread");
            }
            let ValueExpr::Binding(binding) = callee else {
                return Some("must select the direct bare global constructor");
            };
            if binding.0.as_ref() != constructor || self.declared_bindings().contains(binding) {
                return Some(
                    "must select the original unshadowed global constructor binding matching the requested operation",
                );
            }
            return None;
        }
        if call.kind != CallKind::Call || call.has_spread || call.caller.is_some() {
            return Some(match operation {
                NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => {
                    "must select a nonoptional, nonspread, direct top-level Symbol call"
                }
                NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve) => {
                    "must select a nonoptional, nonspread, direct top-level Promise.resolve() call"
                }
                _ => "must select an ordinary direct top-level native call",
            });
        }
        if !self.ordinary_top_call_sites.contains(&call_site) {
            return Some("must select an ordinary direct top-level call");
        }
        match operation {
            NativeInputKind::Operation(
                spec::NativeOperationKind::Symbol | spec::NativeOperationKind::SymbolFor,
            ) => {
                let Some(symbol) = selected_symbol_binding(callee, operation) else {
                    return Some(
                        "must select a direct uncomputed Symbol identifier call or Symbol.for dot call",
                    );
                };
                if symbol.0.as_ref() != "Symbol" {
                    return Some("operation does not match the selected direct Symbol initializer");
                }
                if self.declared_bindings().contains(symbol) {
                    return Some(
                        "selects a shadowed Symbol binding; only the original unshadowed global Symbol binding is supported",
                    );
                }
                if !self.primitive_native_symbol_calls.contains(&call_site) || args.len() > 1 {
                    return Some(
                        "has an unsupported Symbol call shape; use one ordinary direct call with zero or one syntactically primitive argument and no spread",
                    );
                }
                let Some((row, _)) = native_call_identity_key(CallKind::Call, callee) else {
                    return Some("does not resolve through the exact native Symbol call row");
                };
                (row.family() == Some(NativeIntrinsic::Symbol))
                    .then_some(())
                    .is_none()
                    .then_some("selected an unsupported native Symbol call row")
            }
            NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve) => {
                let ValueExpr::Member(receiver, Some(member), MemberKey::Static, _) = callee else {
                    return Some("must select a direct uncomputed Promise.resolve() call");
                };
                let ValueExpr::Binding(promise) = receiver.as_ref() else {
                    return Some("must select a direct uncomputed Promise.resolve() call");
                };
                if member != "resolve" || promise.0.as_ref() != "Promise" {
                    return Some(
                        "operation does not match the selected direct Promise.resolve() initializer",
                    );
                }
                if self.declared_bindings().contains(promise) {
                    return Some(
                        "selects a shadowed Promise binding; only the original unshadowed global Promise binding is supported",
                    );
                }
                if !args.is_empty() || !call.no_reentry_candidate {
                    return Some(
                        "has an unsupported Promise.resolve call shape; use one ordinary direct zero-argument call with no spread",
                    );
                }
                None
            }
            _ => Some("operation does not match the selected native call"),
        }
    }

    fn known_global_native_operation_mutation(
        &self,
        operation: NativeInputKind,
    ) -> Option<SourceUnitOrdinal> {
        if let Some(constructor) = operation.empty_constructor_name() {
            return match self.source_slot_overlap(&NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String(constructor.to_owned()),
            }) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            };
        }
        match operation {
            NativeInputKind::Operation(
                spec::NativeOperationKind::Symbol | spec::NativeOperationKind::SymbolFor,
            ) => self.known_global_symbol_mutation(),
            NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve) => [
                NativeWriteSlot {
                    receiver: NativeReceiverPath::GlobalObject,
                    key: NativePropertyKey::String("Promise".to_owned()),
                },
                NativeWriteSlot {
                    receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Promise),
                    key: NativePropertyKey::String("resolve".to_owned()),
                },
            ]
            .iter()
            .find_map(|slot| match self.source_slot_overlap(slot) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            }),
            _ => None,
        }
    }

    fn known_global_symbol_mutation(&self) -> Option<SourceUnitOrdinal> {
        let overlap = self.source_slot_overlap(&NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::String("Symbol".to_owned()),
        });
        match overlap {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        }
    }

    fn known_symbol_for_mutation(&self) -> Option<SourceUnitOrdinal> {
        match self.source_slot_overlap(&NativeWriteSlot {
            receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Symbol),
            key: NativePropertyKey::String("for".to_owned()),
        }) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        }
    }

    fn known_global_set_mutation(&self) -> Option<SourceUnitOrdinal> {
        match self.source_slot_overlap(&NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::String("Set".to_owned()),
        }) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        }
    }

    fn known_object_freeze_mutation(&self) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Object".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                key: NativePropertyKey::String("freeze".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| match self.source_slot_overlap(slot) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        })
    }

    fn known_native_read_contradiction(
        &self,
        family: NativeIntrinsic,
        property: &str,
    ) -> Option<SourceUnitOrdinal> {
        for slot in [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String(family.global_binding()?.to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Constructor(family),
                key: NativePropertyKey::String(property.to_owned()),
            },
        ] {
            if let NativeSlotOverlap::Known(owner) = self.source_slot_overlap(&slot) {
                return Some(owner);
            }
        }
        None
    }

    /// Check this chunk's original source witnesses. Whole-program admission
    /// applies this same check to every analyzed chunk with its own AST sites.
    /// The raw census remains unchanged, including every eligible fill.
    fn known_primitive_symbol_read_contradiction(
        &self,
        property: &str,
    ) -> Option<SourceUnitOrdinal> {
        for slot in [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Symbol".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Symbol),
                key: NativePropertyKey::String(property.to_owned()),
            },
        ] {
            let witnesses = self
                .native_write_census
                .witnesses
                .iter()
                .chain(
                    self.native_write_census
                        .conditional
                        .iter()
                        .map(|write| &write.witness),
                )
                .filter(|witness| {
                    let allowed = slot.receiver
                        == NativeReceiverPath::Constructor(NativeIntrinsic::Symbol)
                        && witness.operation == NativeMutationOperation::PropertyAssignment
                        && self
                            .symbol_guarded_fill_sites
                            .get(&CallSite::from(witness.span))
                            .is_some_and(|(symbol, key)| {
                                *key == property && !self.declared_bindings().contains(symbol)
                            });
                    !allowed
                });
            if let NativeSlotOverlap::Known(owner) = source_slot_overlap(witnesses, &slot) {
                return Some(owner);
            }
        }
        None
    }

    fn known_descriptor_copy_mutation(
        &self,
        assigned_keys: &BTreeSet<String>,
    ) -> Option<SourceUnitOrdinal> {
        let slots = ["Object"]
            .into_iter()
            .map(|key| NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String(key.to_owned()),
            })
            .chain(
                [
                    "getOwnPropertyDescriptors",
                    "defineProperties",
                    "assign",
                    "prototype",
                ]
                .into_iter()
                .map(|key| NativeWriteSlot {
                    receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                    key: NativePropertyKey::String(key.to_owned()),
                }),
            )
            .chain(assigned_keys.iter().map(|key| NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Object),
                key: NativePropertyKey::String(key.clone()),
            }));
        slots
            .into_iter()
            .find_map(|slot| match self.source_slot_overlap(&slot) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            })
    }

    pub(super) fn direct_freeze_return_call(
        &self,
        binding: &Id,
        selected_span: Span,
    ) -> Option<Call> {
        if !self.direct_top_level_functions.contains(binding) {
            return None;
        }
        let key = *self.hoisted_bindings.get(binding)?;
        let declaration = *self.callable_spans.get(&key)?;
        if !span_contains(selected_span, declaration) || self.async_callables.contains(&key) {
            return None;
        }
        let callable = self.callables.get(&key)?;
        if callable.generator {
            return None;
        }
        let site = CallSite::from(*self.direct_return_calls.get(binding)?);
        let call = callable
            .calls
            .iter()
            .find(|call| call.kind == CallKind::Call && CallSite::from(call.span) == site)?;
        if call.kind != CallKind::Call
            || call.caller != Some(key)
            || call.has_spread
            || CallSite::from(call.span) != site
            || call.args.len() != 1
        {
            return None;
        }
        let ValueExpr::Member(receiver, Some(member), MemberKey::Static, _) = &call.callee else {
            return None;
        };
        let ValueExpr::Binding(object) = receiver.as_ref() else {
            return None;
        };
        if object.0.as_ref() != "Object"
            || member != "freeze"
            || self.declared_bindings().contains(object)
        {
            return None;
        }
        let (row, _) = native_call_identity_key(CallKind::Call, &call.callee)?;
        (row.op == IntrinsicOp::ObjectFreeze).then(|| call.clone())
    }

    fn private_empty_set_field_call(
        &self,
        selected_binding: &str,
        selected_span: Span,
    ) -> Option<(Id, Span, Call)> {
        let candidates: Vec<_> = self
            .class_bindings
            .iter()
            .filter(|(key, binding)| {
                binding.0.as_ref() == selected_binding
                    && self
                        .class_spans
                        .get(key)
                        .is_some_and(|span| span_contains(selected_span, *span))
            })
            .collect();
        let [(key, binding)] = candidates.as_slice() else {
            return None;
        };
        let key = **key;
        let binding = *binding;
        if self.reassigned_bindings.contains(binding) {
            return None;
        }
        let (site, set_binding) = self.private_empty_set_sites.get(&key)?;
        if self.declared_bindings().contains(set_binding) {
            return None;
        }
        let call = self
            .callables
            .values()
            .flat_map(|callable| &callable.calls)
            .find(|call| call.span == *site)?;
        if call.kind != CallKind::New
            || call.has_spread
            || !call.args.is_empty()
            || !matches!(&call.callee, ValueExpr::Binding(binding) if binding == set_binding)
            || call.owner != *self.class_owners.get(&key)?
        {
            return None;
        }
        Some((binding.clone(), self.class_spans[&key], call.clone()))
    }
    fn known_array_iterator_protocol_mutation_with(
        &self,
        integrity: &ArraySpreadProtocolIntegrity,
    ) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::ArrayIteratorPrototype,
                key: NativePropertyKey::String("next".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| {
            match array_spread_protocol_slot_overlap(
                self.native_write_census.witnesses.iter().chain(
                    self.native_write_census
                        .conditional
                        .iter()
                        .map(|write| &write.witness),
                ),
                slot,
                &integrity.mutated,
                integrity.global_array_binding_mutated,
                &integrity.premise_authorized_next_writes,
                ArraySpreadNativeIteratorPremise,
            ) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            }
        })
    }

    fn validate_selected_array_spread_input(
        &mut self,
        input: &SelectedNativeOperationInput,
        shapes: &StableArraySpreadShapes,
    ) {
        let request = &input.evidence.spec_location;
        let candidates: Vec<_> = self
            .binding_declaration_writes
            .iter()
            .filter(|(binding, writes)| {
                binding.0.as_ref() == input.selected_binding
                    && writes.len() == 1
                    && writes[0].direct_top_level
                    && writes[0].simple_identifier
                    && span_contains(input.selected_statement_span, writes[0].binding_span)
                    && writes[0]
                        .initializer_span
                        .is_some_and(|span| shapes.by_initializer.contains_key(&span.lo.0))
            })
            .collect();
        let [(binding, writes)] = candidates.as_slice() else {
            self.native_operation_input_errors.push(format!(
                "{request} must select exactly one direct const initializer with an inert same-declaration primitive-array spread"
            ));
            return;
        };
        let declaration = &writes[0];
        let initializer_span = declaration
            .initializer_span
            .expect("candidate has initializer");
        let binding = (*binding).clone();
        let Some(shape) = shapes.by_initializer.get(&initializer_span.lo.0) else {
            unreachable!("candidate has source shape")
        };
        if shape.consumer != declaration.owner
            || !self.array_elements.contains_key(&initializer_span.lo.0)
        {
            self.native_operation_input_errors.push(format!(
                "{request} no longer resolves to the source-checked array initializer"
            ));
            return;
        }
        let mut evidence = input.evidence.clone();
        evidence.selected_binding = input.selected_binding.clone();
        self.native_operation_input_evidence
            .insert(CallSite::from(initializer_span), evidence.clone());
        self.resolved_native_operation_inputs
            .push(ResolvedNativeOperationInput {
                evidence,
                binding,
                selected_statement_span: input.selected_statement_span,
                declaration_span: declaration.binding_span,
                initializer_span,
                call_span: initializer_span,
                initializer_ordinal: declaration.owner,
                spread_source: None,
                spread_source_span: None,
                spread_token_span: None,
            });
    }

    fn validate_resolved_array_spread_input(
        &mut self,
        input: &ResolvedNativeOperationInput,
        shapes: &StableArraySpreadShapes,
    ) {
        let request = &input.evidence.spec_location;
        let same_source = self
            .binding_declaration_writes
            .get(&input.binding)
            .is_some_and(|writes| {
                matches!(writes.as_slice(), [declaration]
                if declaration.direct_top_level
                    && declaration.simple_identifier
                    && declaration.owner == input.initializer_ordinal
                    && declaration.binding_span == input.declaration_span
                    && declaration.initializer_span == Some(input.initializer_span)
                    && input.call_span == input.initializer_span
                    && shapes.by_initializer.get(&input.initializer_span.lo.0)
                        .is_some_and(|shape| shape.consumer == declaration.owner))
            });
        if !same_source {
            self.native_operation_input_errors.push(format!(
                "{request} no longer identifies the exact source-checked array spread"
            ));
            return;
        }
        self.native_operation_input_evidence.insert(
            CallSite::from(input.initializer_span),
            input.evidence.clone(),
        );
        self.resolved_native_operation_inputs.push(input.clone());
    }

    fn known_set_protocol_mutation(&self) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Set".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Set),
                key: NativePropertyKey::String("add".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::ArrayIteratorPrototype,
                key: NativePropertyKey::String("next".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| match self.source_slot_overlap(slot) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        })
    }

    fn known_set_spread_protocol_mutation(&self) -> Option<SourceUnitOrdinal> {
        self.known_set_protocol_mutation().or_else(|| {
            [
                NativeWriteSlot {
                    receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Set),
                    key: NativePropertyKey::SymbolIterator,
                },
                NativeWriteSlot {
                    receiver: NativeReceiverPath::SetIteratorPrototype,
                    key: NativePropertyKey::String("next".to_owned()),
                },
            ]
            .iter()
            .find_map(|slot| match self.source_slot_overlap(slot) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            })
        })
    }

    fn validate_captured_callable_returns(
        &mut self,
        assignments: &[&(Id, ValueExpr)],
        flow: Option<&ChunkValueFlow>,
        raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
    ) {
        if self.resolved_value_inputs.is_empty() {
            return;
        }
        let closure = close_receiver_flow(self, assignments, flow, raw_aliases);
        let mut rejected = Vec::new();
        for input in &self.resolved_value_inputs {
            let unsupported = self.callables.iter().any(|(key, callable)| {
                callable.reads.contains(&input.binding)
                    && (callable.generator
                        || self.async_callables.contains(key)
                        || closure
                            .facts
                            .get(&ReceiverFlowKey::Callable(*key))
                            .is_none_or(|fact| {
                                fact.outcomes.is_empty()
                                    || fact.outcomes.contains(&CallableReturnOutcome::Unknown)
                            }))
            });
            if unsupported {
                rejected.push((
                    input.binding.clone(),
                    input.evidence.reference.clone(),
                    format!(
                        "{} cannot refine `{}` because a capturing synchronous return path is unsupported, unresolved, or opaque",
                        input.evidence.spec_location, input.evidence.selected_binding
                    ),
                ));
            }
        }
        for (binding, reference, message) in rejected {
            self.value_input_errors.push(message);
            self.ordinary_input_refs.remove(&binding);
            self.value_input_evidence.remove(&reference);
            self.resolved_value_inputs
                .retain(|input| input.binding != binding);
        }
    }

    fn stable_top_level_copy_owner(
        &self,
        binding: &Id,
        initializer: &ValueExpr,
    ) -> Option<SourceUnitOrdinal> {
        if !matches!(initializer, ValueExpr::Binding(_))
            || self
                .property_key_assignments
                .get(binding)
                .is_none_or(|writes| writes.len() != 1)
        {
            return None;
        }
        let [declaration] = self.binding_declaration_writes.get(binding)?.as_slice() else {
            return None;
        };
        let same_direct_binding = matches!(
            (declaration.initializer.as_ref(), initializer),
            (Some(ValueExpr::Binding(declared)), ValueExpr::Binding(candidate))
                if declared == candidate
        );
        (declaration.direct_top_level && declaration.simple_identifier && same_direct_binding)
            .then_some(declaration.owner)
    }

    pub(super) fn collect(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        flow: Option<&ChunkValueFlow>,
        plain_array_bindings: &BTreeSet<String>,
        construction_proof: &LocalClassConstructionShapes,
        array_spreads: &StableArraySpreadShapes,
    ) -> Self {
        let host_operations = &hints.assumptions.reentry_free_operations;
        let global_guards_requested = !hints.assumptions.deferred_global_publications.is_empty()
            || !hints.assumptions.non_reentrant_global_membership.is_empty();
        // A standard host defines `global` only as Node's global object.
        let node_global = !hints.assumptions.predefined_globals.is_unknown()
            && unshadowed_global_names(body, &["global"]).contains("global");
        let mut index = Self {
            source_chunk_id: hints.current_chunk_id.clone(),
            global_guards_requested,
            local_class_construction_shapes: construction_proof.clone(),
            top_calls: vec![Vec::new(); body.len()],
            original_statement_spans: body
                .iter()
                .map(|item| match item {
                    TopLevelItemView::Borrowed(item) => item.span(),
                    TopLevelItemView::Owned { provenance, .. } => provenance.parent_item.span(),
                })
                .collect(),
            owner_callable_prefix_effects: vec![None; body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
            top_dynamic_property_receivers: vec![Vec::new(); body.len()],
            top_destructuring_member_receivers: vec![Vec::new(); body.len()],
            top_assignments: vec![Vec::new(); body.len()],
            ordered_initializers: BTreeMap::new(),
            initialization_source_identities: BTreeMap::new(),
            initialization_coverage_refusals: vec![None; body.len()],
            top_definite_initializers: vec![BTreeSet::new(); body.len()],
            single_top_lexical_binding: vec![None; body.len()],
            top_member_value_writes: vec![Vec::new(); body.len()],
            top_callback_result_writes: vec![Vec::new(); body.len()],
            top_exported_values: vec![Vec::new(); body.len()],
            top_metadata_decorations: body
                .iter()
                .map(|item| {
                    metadata_decorate_call(item.as_module_item(), &hints.assumptions.known_effects)
                })
                .collect(),
            deferred_global_candidates: vec![None; body.len()],
            discarded_sync_iifes: if !global_guards_requested {
                Vec::new()
            } else {
                vec![None; body.len()]
            },
            deferred_global_requested: !hints.assumptions.deferred_global_publications.is_empty(),
            global_object_names: unshadowed_global_object_aliases(body),
            census_globals: GlobalNames {
                aliases: unshadowed_global_object_aliases(body)
                    .into_iter()
                    .chain(node_global.then_some("global"))
                    .collect(),
                node_global,
                predefined: hints.assumptions.predefined_globals,
                bundle: BundleGlobals::default(),
            },
            plain_array_bindings: plain_array_bindings.clone(),
            no_sync_callback_members: hints.assumptions.no_sync_callback_members.clone(),
            retained_record_factories: hints.assumptions.retained_record_factories.clone(),
            no_reentry_statement_calls: hints.assumptions.no_reentry_statement_calls.clone(),
            host_promise_resolve: host_operations.contains(&ReentryFreeOperation::PromiseResolve),
            native_integrity_value_input_refs: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_value_inputs.clone())
                .unwrap_or_default(),
            native_integrity_host_behavior: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_host_behavior.clone())
                .unwrap_or_default(),
            ..Self::default()
        };
        let mut fresh_steps = Vec::with_capacity(body.len());
        let mut max_source_position = 0u32;
        for item in body {
            max_source_position = max_source_position.max(item.as_module_item().span().hi.0);
            // Seed all candidate roots before visiting any item, including imports
            // textually after a use. Other imported aliases remain an author audit.
            if (!hints.assumptions.namespace_independent_members.is_empty()
                || !hints.assumptions.no_reentry_members.is_empty()
                || !hints.assumptions.no_reentry_record_members.is_empty())
                && let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item()
            {
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    let name = local.sym.as_ref();
                    if hints
                        .assumptions
                        .callback_factory_members
                        .contains_key(name)
                        || hints
                            .assumptions
                            .retained_callback_factory_members
                            .contains_key(name)
                        || hints.assumptions.no_reentry_members.contains_key(name)
                        || hints
                            .assumptions
                            .no_reentry_record_members
                            .contains_key(name)
                        || hints
                            .assumptions
                            .namespace_independent_members
                            .contains_key(name)
                    {
                        index.namespace_privacy.members.insert(
                            local.to_id(),
                            hints
                                .assumptions
                                .namespace_independent_members
                                .get(name)
                                .cloned()
                                .unwrap_or_default(),
                        );
                        index.namespace_privacy.import_sites.insert(local.span.lo.0);
                    }
                }
            }
            let declaration = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration))) => Some(declaration),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Class(declaration) = &export.decl {
                        Some(declaration)
                    } else {
                        None
                    }
                }
                _ => None,
            };
            if let Some(declaration) = declaration {
                let id = declaration.ident.to_id();
                index
                    .class_bindings
                    .insert(declaration.class.span.lo.0, id.clone());
                index.class_ids.insert(id);
            }
            if let Some(declaration) = var_decl_of_item(item.as_module_item())
                && declaration.kind == VarDeclKind::Const
            {
                for declarator in &declaration.decls {
                    index
                        .top_const_bindings
                        .extend(binding_targets::binding_names(&declarator.name));
                }
            }
        }
        for (ordinal, item) in body.iter().enumerate() {
            let owner = SourceUnitOrdinal(ordinal);
            if let TopLevelItemView::Borrowed(ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))) =
                item
                && !declaration.declare
                && let [declarator] = declaration.decls.as_slice()
                && matches!(declarator.name, Pat::Ident(_))
                && let Some(expression) = declarator.init.as_deref()
                && !matches!(expression.unwrap_parens(), Expr::Ident(_))
                && let Some(body) = LocalBody::from_initializer(expression)
                // Only a top-level class declaration can satisfy the kernel's
                // construct admission; other `new` roots keep the flat lane.
                && body
                    .construct_callees()
                    .iter()
                    .all(|callee| index.class_ids.contains(callee))
            {
                debug_assert_eq!(body.root_span(), Some(expression.span()));
                index.ordered_initializers.insert(owner, body);
            }
            if let TopLevelItemView::Borrowed(ModuleItem::Stmt(Stmt::Expr(statement))) = item
                && let Some(body) = LocalBody::from_expression_statement(&statement.expr)
                && body.root_store_statement().is_some()
            {
                index.ordered_statements.insert(owner, body);
            }
            let source_identity = InitializationSourceIdentity::collect(item, owner);
            if let Ok((identity, _)) = &source_identity {
                index
                    .initialization_source_identities
                    .insert(owner, identity.clone());
            }
            // A split comma-list owner keeps its original declaration/span
            // identity. The ordinary composed-initializer path below cannot
            // admit a call leaf, so admit only the one exact audited root call
            // that was independently selected from the original source.
            if !hints.assumptions.initializer_call_effect_inputs.is_empty()
                && matches!(
                    item,
                    TopLevelItemView::Owned {
                        item: ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))),
                        ..
                    }
                )
                && let Some(declaration) = var_decl_of_item(item.as_module_item())
                && !declaration.declare
                && let [declarator] = declaration.decls.as_slice()
                && let Pat::Ident(binding) = &declarator.name
                && let Some(expression) = declarator.init.as_deref()
                && let Some(input) = hints
                    .assumptions
                    .initializer_call_effect_inputs
                    .iter()
                    .find(|input| input.binding == binding.id.to_id())
                && source_identity.as_ref().is_ok_and(|(identity, _)| {
                    identity.matches(owner, &binding.id.to_id(), expression.span())
                })
                && let Some(body) = LocalBody::from_initializer(expression)
                && let Some((callee, call_span)) =
                    body.single_identifier_call_with_function_argument_initializer()
                && input.callee == *callee
                && input.call_span == call_span
            {
                index.ordered_initializers.insert(owner, body);
            }
            match OriginalInitializer::collect(item, owner, source_identity) {
                Ok(initializer) => {
                    if matches!(
                        item,
                        TopLevelItemView::Owned {
                            item: ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))),
                            ..
                        }
                    ) && matches!(
                        initializer.composed_expression(),
                        Some(ValueExpr::Binding(_))
                    ) && let Some(declaration) = var_decl_of_item(item.as_module_item())
                        && !declaration.declare
                        && let [declarator] = declaration.decls.as_slice()
                        && let Pat::Ident(binding) = &declarator.name
                        && let Some(expression) = declarator.init.as_deref()
                        && let Some(body) = LocalBody::from_initializer(expression)
                        && body.root_span() == Some(expression.span())
                        && index
                            .initialization_source_identities
                            .get(&owner)
                            .is_some_and(|identity| {
                                identity.matches(owner, &binding.id.to_id(), expression.span())
                            })
                    {
                        index.ordered_initializers.insert(owner, body);
                    }
                    index.noninvoking_initializers.insert(owner, initializer);
                }
                Err(reason) => index.initialization_coverage_refusals[ordinal] = Some(reason),
            }
            fresh_steps.push(fresh_value_step(item.as_module_item()));
            if index.global_guards_requested {
                index.deferred_global_candidates[ordinal] =
                    deferred_global_publication(item.as_module_item());
                index.discarded_sync_iifes[ordinal] =
                    discarded_synchronous_iife(item.as_module_item());
            }
            index.owner = SourceUnitOrdinal(ordinal);
            index.direct_top_level_var_declaration = matches!(
                item.as_module_item(),
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(_)))
                    | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                        decl: Decl::Var(_),
                        ..
                    }))
            );
            index.direct_top_level_const_declaration = var_decl_of_item(item.as_module_item())
                .is_some_and(|declaration| declaration.kind == VarDeclKind::Const);
            index.past_await = false;
            index.may_skip_await_depth = 0;
            index.conditional_depth = 0;
            index.literal_depth = 0;
            index.factory_member_value_depth = 0;
            index.factory_callback_depth = 0;
            index.collect_uninitialized_lexical_bindings(item.as_module_item());
            index.collect_export_roots(item.as_module_item());
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item() {
                let host_async_local_storage = host_operations
                    .contains(&ReentryFreeOperation::AsyncLocalStorageConstruct)
                    && !import.type_only
                    && matches!(&import.phase, ImportPhase::Evaluation)
                    && import.with.is_none()
                    && matches!(
                        import.src.value.as_str(),
                        Some("async_hooks" | "node:async_hooks")
                    );
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    index.imported_bindings.insert(local.to_id());
                    if host_async_local_storage
                        && let ImportSpecifier::Named(named) = specifier
                        && !named.is_type_only
                        && named
                            .imported
                            .as_ref()
                            .map(|name| match name {
                                ModuleExportName::Ident(id) => id.sym.as_ref(),
                                ModuleExportName::Str(string) => {
                                    string.value.as_str().unwrap_or("")
                                }
                            })
                            .unwrap_or(named.local.sym.as_ref())
                            == "AsyncLocalStorage"
                    {
                        index.host_async_local_storage_imports.insert(local.to_id());
                    }
                    if let Some(record) = hints
                        .imported_callables
                        .get(local.sym.as_ref())
                        .and_then(|summary| summary.returned_record.as_ref())
                    {
                        index
                            .returned_record_summaries
                            .insert(local.to_id(), record.clone());
                    }
                    if let Some(wrapper) = hints
                        .imported_callables
                        .get(local.sym.as_ref())
                        .and_then(|summary| summary.namespace_wrapper.as_ref())
                    {
                        index
                            .namespace_wrapper_summaries
                            .insert(local.to_id(), wrapper.clone());
                    }
                    // A narrower summary or an author contract keeps its own
                    // consumer and use reports; the derived summary fills in
                    // only where nothing else describes the call.
                    if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                        && let Some(call) = summary.foreign_call.as_ref()
                        && summary.returned_record.is_none()
                        && summary.namespace_wrapper.is_none()
                        && summary.descriptor_copy_factory.is_none()
                        && !hints
                            .assumptions
                            .names_imported_callable(local.sym.as_ref())
                    {
                        if summary.supports_callback_factory() {
                            // An author-asserted factory keeps its contract's
                            // consumers alone.
                            if summary.callback_factory_evidence().iter().all(|evidence| {
                                matches!(
                                    evidence,
                                    crate::analysis_inputs::CallbackFactoryEvidence::InferredImmutableWrapper { .. }
                                )
                            }) {
                                index
                                    .callback_factory_call_summaries
                                    .insert(local.to_id(), call.clone());
                            }
                        } else {
                            index
                                .foreign_call_imports
                                .insert(local.to_id(), call.clone());
                        }
                    }
                    if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                        && summary.supports_callback_factory()
                    {
                        index.callback_factory_imports.insert(local.to_id());
                        index
                            .callback_factory_summaries
                            .insert(local.to_id(), summary.clone());
                    }
                    if let Some(summary) = hints
                        .imported_callables
                        .get(local.sym.as_ref())
                        .and_then(|summary| summary.descriptor_copy_factory.as_ref())
                    {
                        index
                            .descriptor_copy_factories
                            .insert(local.to_id(), summary.clone());
                    }
                    if hints
                        .assumptions
                        .inline_callback_factories
                        .contains(local.sym.as_ref())
                    {
                        index.inline_callback_factory_imports.insert(local.to_id());
                    }
                    if hints
                        .assumptions
                        .no_reentry_exports
                        .contains(local.sym.as_ref())
                    {
                        index.no_reentry_exports.insert(local.to_id());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .no_reentry_callback_exports
                        .get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_callback_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .retained_callback_factories
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_factory_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(signatures) = hints
                        .assumptions
                        .retained_callback_constructors
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_constructor_imports
                            .insert(local.to_id(), signatures.clone());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .retained_callback_factory_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .retained_callback_factory_members
                            .insert(local.to_id(), members.clone());
                        index
                            .audited_factory_members
                            .entry(local.to_id())
                            .or_default()
                            .extend(members.keys().cloned());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .callback_factory_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .callback_factory_members
                            .insert(local.to_id(), members.clone());
                        index
                            .audited_factory_members
                            .entry(local.to_id())
                            .or_default()
                            .extend(members.iter().cloned());
                    }
                    if let Some(members) =
                        hints.assumptions.no_reentry_members.get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_members
                            .insert(local.to_id(), members.clone());
                    }
                    if let Some(members) = hints
                        .assumptions
                        .no_reentry_record_members
                        .get(local.sym.as_ref())
                    {
                        index
                            .no_reentry_record_members
                            .insert(local.to_id(), members.clone());
                    }
                }
            }
            match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
                    index
                        .direct_top_level_functions
                        .insert(function.ident.to_id());
                }
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Fn(function) = &export.decl {
                        index
                            .direct_top_level_functions
                            .insert(function.ident.to_id());
                    }
                }
                _ => {}
            }
            item.as_module_item().visit_with(&mut index);
            index.direct_top_level_var_declaration = false;
            index.direct_top_level_const_declaration = false;
        }
        for (offset, proof) in index.array_iterations.values_mut().enumerate() {
            proof.result = u32::try_from(offset)
                .ok()
                .and_then(|offset| max_source_position.checked_add(offset)?.checked_add(1));
        }
        // An accessor fallback may run any callable owned by this item,
        // including a nested function installed as the setter. Union each
        // retained synchronous prefix instead of using whole lazy bodies.
        for callable in index.callables.values() {
            let effects = index.owner_callable_prefix_effects[callable.owner.0]
                .get_or_insert_with(CallablePrefixEffects::default);
            if !callable.created_after_parent_await {
                effects.reads.extend(callable.reads.iter().cloned());
            }
        }
        let owned_namespace_assignments = index
            .top_assignments
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.local_assignments.iter()),
            )
            .cloned()
            .collect::<Vec<_>>();
        let namespace_assignments = owned_namespace_assignments.iter().collect::<Vec<_>>();
        let namespace_value_facts = NamespaceValueFacts::from_assignments(
            index.class_values.clone(),
            fresh_steps
                .iter()
                .filter_map(|step| match step {
                    Some(FreshValueStep::Literal { span, .. }) => Some(*span),
                    _ => None,
                })
                .collect(),
            &namespace_assignments,
        );
        // Track aliases to native and explicitly captured ordinary values in
        // one receiver domain. The first pass is source-only and is used to
        // reject recognized contradictions before any author input is seeded.
        let declared = index.declared_bindings();
        if !index.census_globals.predefined.is_unknown() {
            index.census_globals.bundle =
                index.bundle_globals(&hints.assumptions.bundle_globals, &declared);
        }
        let raw_intrinsic_aliases =
            close_raw_intrinsic_aliases(&namespace_assignments, &index.census_globals, &declared);
        index.validate_value_inputs(
            &hints.selected_value_inputs,
            &hints.assumptions.captured_value_inputs,
            &raw_intrinsic_aliases,
            &declared,
        );
        index.validate_captured_callable_returns(
            &namespace_assignments,
            flow,
            &raw_intrinsic_aliases,
        );
        let (callable_return_inputs, intrinsic_aliases) = if index.resolved_value_inputs.is_empty()
        {
            (
                CallableReturnInputs::default(),
                raw_intrinsic_aliases.clone(),
            )
        } else {
            let receiver_closure =
                close_receiver_flow(&index, &namespace_assignments, flow, &raw_intrinsic_aliases);
            (receiver_closure.callable_returns, receiver_closure.bindings)
        };
        let mut mutated_intrinsics = IntrinsicRoots::new();
        let mut unconditional_witnesses = Vec::new();
        let mut binding_write_sites = BTreeSet::new();
        for (owner, assignments) in index
            .top_assignments
            .iter()
            .enumerate()
            .map(|(owner, values)| (SourceUnitOrdinal(owner), values))
            .chain(
                index
                    .callables
                    .values()
                    .map(|callable| (callable.owner, &callable.local_assignments)),
            )
        {
            for (id, _) in assignments {
                match id.0.as_ref() {
                    "TextEncoder" => {
                        mutated_intrinsics.insert(NativeIntrinsic::TextEncoder);
                    }
                    "Error" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Error);
                    }
                    "Function" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Function);
                    }
                    "Object" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Object);
                    }
                    "Array" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Array);
                    }
                    "Math" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Math);
                    }
                    "RegExp" => {
                        mutated_intrinsics.insert(NativeIntrinsic::RegExp);
                    }
                    "Symbol" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Symbol);
                    }
                    "Promise" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Promise);
                    }
                    "Map" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Map);
                    }
                    "Set" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Set);
                    }
                    "WeakMap" => {
                        mutated_intrinsics.insert(NativeIntrinsic::WeakMap);
                    }
                    "WeakSet" => {
                        mutated_intrinsics.insert(NativeIntrinsic::WeakSet);
                    }
                    "FinalizationRegistry" => {
                        mutated_intrinsics.insert(NativeIntrinsic::FinalizationRegistry);
                    }
                    _ => {}
                }
                // Rebinding an unshadowed global-object alias
                // (`globalThis = other`) leaves every write through that name,
                // earlier or later, on an unknown receiver.
                let root = index
                    .census_globals
                    .binding_root(id.0.as_ref())
                    .filter(|root| {
                        *root != NativeIntrinsic::GlobalObject
                            || index.census_globals.contains(id.0.as_ref())
                    });
                if root == Some(NativeIntrinsic::GlobalObject) {
                    mutated_intrinsics.insert(NativeIntrinsic::GlobalObject);
                }
                if let Some(root) = root
                    && binding_write_sites.insert((owner, root))
                {
                    unconditional_witnesses.push(NativeMutationWitness {
                        chunk_id: String::new(),
                        statement_ordinal: owner,
                        source_location: None,
                        operation: NativeMutationOperation::BindingAssignment,
                        receiver_roots: BTreeSet::from([root]),
                        through_local_alias: false,
                        receiver_global: None,
                        affected_roots: BTreeSet::from([root]),
                        target_slots: if root == NativeIntrinsic::GlobalObject {
                            BTreeSet::from([NativeWriteSlot {
                                receiver: NativeReceiverPath::Unknown,
                                key: NativePropertyKey::Unknown,
                            }])
                        } else if index.property_key_declarations.contains(id) {
                            BTreeSet::new()
                        } else {
                            BTreeSet::from([NativeWriteSlot {
                                receiver: NativeReceiverPath::GlobalObject,
                                key: NativePropertyKey::String(id.0.to_string()),
                            }])
                        },
                        key_dependencies: BTreeSet::new(),
                        invalidated_key_dependencies: BTreeSet::new(),
                        value_input_refs: BTreeSet::new(),
                        value_input_evidence: Vec::new(),
                        unreachable_branch: None,
                        span: body[owner.0].as_module_item().span(),
                    });
                }
            }
        }
        if !index.dynamic_scope_owners.is_empty() {
            mutated_intrinsics.insert(NativeIntrinsic::GlobalObject);
        }
        index.native_write_census.unconditional = mutated_intrinsics;
        index.native_write_census.witnesses = Arc::new(unconditional_witnesses);
        for write in &index.intrinsic_writes {
            // Slot evidence is source-only: author-conditioned receiver
            // refinements may narrow family roots, but cannot certify that
            // their own native-operation premise was not contradicted.
            let source_roots = intrinsic_roots(
                &write.receiver,
                &raw_intrinsic_aliases,
                &index.census_globals,
                &declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            let key_evidence = write
                .key
                .as_ref()
                .map(|key| property_key_evidence(key, &index.property_key_assignments, &declared));
            let additional_key_evidence: Vec<_> = write
                .additional_keys
                .iter()
                .map(|key| property_key_evidence(key, &index.property_key_assignments, &declared))
                .collect();
            let mut target_slots = native_write_slots(
                &source_roots,
                &key_evidence
                    .as_ref()
                    .map_or(PropertyKeyDomain::Unknown, |evidence| {
                        evidence.domain.clone()
                    }),
            );
            for evidence in &additional_key_evidence {
                target_slots.extend(native_write_slots(&source_roots, &evidence.domain));
            }
            let receiver_roots = intrinsic_roots(
                &write.receiver,
                &intrinsic_aliases,
                &index.census_globals,
                &declared,
                &index.ordinary_input_refs,
                &callable_return_inputs,
            );
            let projection = project_receiver_roots(&receiver_roots, write.owner);
            if projection.bundle_global {
                index
                    .native_write_census
                    .host_behavior
                    .insert(HostBehaviorKey::PredefinedGlobals);
            }
            let input_evidence = projection
                .value_input_refs
                .iter()
                .filter_map(|reference| index.value_input_evidence.get(reference).cloned())
                .collect();
            let roots = projection.effective_roots.clone();
            let witness = NativeMutationWitness {
                chunk_id: String::new(),
                statement_ordinal: write.owner,
                source_location: None,
                operation: write.operation,
                receiver_roots: projection.prior_roots,
                through_local_alias: receiver_uses_intrinsic_alias(
                    &write.receiver,
                    &intrinsic_aliases,
                ),
                receiver_global: receiver_global_property(
                    &write.receiver,
                    &intrinsic_aliases,
                    &index.census_globals,
                    &declared,
                ),
                affected_roots: projection.affected_roots,
                target_slots,
                key_dependencies: key_evidence
                    .as_ref()
                    .into_iter()
                    .chain(&additional_key_evidence)
                    .flat_map(|evidence| &evidence.dependencies)
                    .copied()
                    .chain(
                        source_roots
                            .contains(&NativeReceiverRoot::ArrayIteratorPrototype)
                            .then_some([
                                NativeIntrinsic::Object,
                                NativeIntrinsic::Array,
                                NativeIntrinsic::Symbol,
                            ])
                            .into_iter()
                            .flatten(),
                    )
                    .chain(
                        source_roots
                            .contains(&NativeReceiverRoot::SetIteratorPrototype)
                            .then_some([
                                NativeIntrinsic::Object,
                                NativeIntrinsic::Set,
                                NativeIntrinsic::Symbol,
                            ])
                            .into_iter()
                            .flatten(),
                    )
                    .collect(),
                invalidated_key_dependencies: BTreeSet::new(),
                value_input_refs: projection.value_input_refs,
                value_input_evidence: input_evidence,
                unreachable_branch: None,
                span: write.span,
            };
            let Some(_) = &write.key else {
                if !witness.affected_roots.is_empty() || !witness.value_input_refs.is_empty() {
                    Arc::make_mut(&mut index.native_write_census.witnesses).push(witness);
                }
                index
                    .native_write_census
                    .unconditional
                    .extend(native_mutation_roots(roots));
                continue;
            };
            let evidence = key_evidence.expect("write with key has key evidence");
            let fallback = native_mutation_roots(roots.clone());
            let mut refined: IntrinsicRoots = roots
                .iter()
                .filter(|root| **root != NativeIntrinsic::GlobalObject)
                .copied()
                .collect();
            if roots.contains(&NativeIntrinsic::GlobalObject) {
                match &evidence.domain {
                    PropertyKeyDomain::FiniteStrings(keys) => {
                        for key in keys {
                            refined.extend(index.census_globals.binding_root(key));
                        }
                    }
                    PropertyKeyDomain::Affixed { prefix, suffix } => {
                        let key = NativePropertyKey::Affixed {
                            prefix: prefix.clone(),
                            suffix: suffix.clone(),
                        };
                        refined.extend(
                            index
                                .census_globals
                                .binding_names()
                                .filter(|name| key.may_be_string(name))
                                .filter_map(|name| index.census_globals.binding_root(name)),
                        );
                    }
                    PropertyKeyDomain::Symbol | PropertyKeyDomain::SymbolIterator => {}
                    PropertyKeyDomain::Unknown => {
                        refined.extend(&fallback);
                    }
                }
            }
            index
                .native_write_census
                .conditional
                .push(ConditionalIntrinsicWrite {
                    witness,
                    refined: native_mutation_roots(refined),
                    fallback,
                    dependencies: evidence.dependencies,
                });
        }
        index.native_write_census.prototype_mutations =
            Arc::new(index.collect_prototype_mutations(
                &raw_intrinsic_aliases,
                &namespace_assignments,
                &declared,
            ));
        let proofs = index.native_branch_proofs(&hints.imported_callables);
        for (write, proof) in index.native_write_census.conditional.iter_mut().zip(proofs) {
            write.witness.unreachable_branch = proof;
        }
        let mut censuses = vec![&index.native_write_census];
        let external = NativeWriteCensus {
            host_behavior: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_host_behavior.clone())
                .unwrap_or_default(),
            prototype_mutations: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.prototype_mutations.clone())
                .unwrap_or_default(),
            witnesses: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.witnesses.clone())
                .unwrap_or_default(),
            unconditional: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.mutated.clone())
                .unwrap_or_default(),
            conditional: Vec::new(),
        };
        censuses.push(&external);
        let mutated_intrinsics = if let Some(integrity) = &hints.native_integrity {
            index.native_integrity_witnesses = integrity.witnesses.clone();
            index.native_prototype_mutations = integrity.prototype_mutations.clone();
            index.never_written = integrity.never_written.clone();
            index.never_written_overrode = !integrity.never_written_overrides.is_empty();
            let mut mutated = resolve_native_integrity_roots(&censuses);
            mutated.retain(|root| !integrity.never_written.contains(root));
            mutated
        } else {
            let integrity = resolve_native_integrity(censuses);
            index.native_integrity_host_behavior = integrity.consumed_host_behavior;
            index.native_integrity_witnesses = integrity.witnesses;
            index.native_prototype_mutations = integrity.prototype_mutations;
            integrity.mutated
        };
        index.mutated_intrinsic_roots = mutated_intrinsics.clone();
        index.populate_native_point_effects(&external);
        let mut claim_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
        if !index.fresh_error_claims.is_empty()
            && !index.audited_factory_members.is_empty()
            && flow.is_some()
        {
            // A11 supplies the standard Error allocation/prototype behavior. Reject
            // visible replacement of that intrinsic, including writes through local
            // aliases of a known global object. This is not a general effect proof.
            let intrinsic_mutated = mutated_intrinsics.contains(&NativeIntrinsic::Error)
                || mutated_intrinsics.contains(&NativeIntrinsic::GlobalObject);
            if !intrinsic_mutated
                && index.dynamic_scope_owners.is_empty()
                && let Some(flow) = flow
            {
                for claim in &index.fresh_error_claims {
                    if !flow.is_declared(&claim.callee)
                        && claim
                            .local_read
                            .as_ref()
                            .is_none_or(|id| flow.is_declared(id))
                    {
                        *claim_counts.entry(claim.site.clone()).or_default() += 1;
                    }
                }
            }
            let mut write_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
            let mut count_writes = |owner, callable, writes: &[CallbackResultWrite]| {
                for write in writes {
                    if let (ValueExpr::Binding(receiver), Some(property)) =
                        (&write.receiver, &write.property)
                    {
                        *write_counts
                            .entry(FreshErrorWriteSite {
                                owner,
                                callable,
                                receiver: receiver.clone(),
                                property: property.clone(),
                            })
                            .or_default() += 1;
                    }
                }
            };
            for (owner, writes) in index.top_callback_result_writes.iter().enumerate() {
                count_writes(SourceUnitOrdinal(owner), None, writes);
            }
            for (key, callable) in &index.callables {
                count_writes(callable.owner, Some(*key), &callable.callback_result_writes);
            }
            claim_counts.retain(|site, count| write_counts.get(site) == Some(count));
        }
        let certified = |owner, callable, receiver: &Id, property: &str| {
            claim_counts.contains_key(&FreshErrorWriteSite {
                owner,
                callable,
                receiver: receiver.clone(),
                property: property.to_owned(),
            })
        };
        let certified_callback_write = |owner, callable, write: &CallbackResultWrite| matches!((&write.receiver, &write.property), (ValueExpr::Binding(receiver), Some(property)) if certified(owner, callable, receiver, property));
        // A namespace member is an audited target only while its binding is
        // unchanged. Any local write could replace a member with an eager
        // callback invoker before the apparent factory call.
        // Total source privacy proves that this family cannot reach any unrelated
        // Unknown/This slot. It grants no execution or callback timing effects.
        let private_namespace_family = !index.namespace_privacy.members.is_empty()
            && !index.namespace_privacy.invalid
            && index.dynamic_scope_owners.is_empty();
        let namespace_member_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_dynamic_property_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_call_argument_witnesses =
            RefCell::new(BTreeMap::<Id, CallbackFactoryCallArgumentWitness>::new());
        let keep_namespace = |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<_>| {
            if private_namespace_family {
                return true;
            }
            let aliases =
                infer_namespace_aliases(id, &namespace_assignments, &namespace_value_facts);
            let receiver_aliases = infer_mutation_receiver_aliases(
                id,
                &namespace_assignments,
                &aliases,
                &namespace_value_facts,
            );
            let is_namespace_or_alias = |binding: &Id| binding == id || aliases.contains(binding);
            let is_mutation_receiver = |receiver: &ValueExpr| {
                receiver.may_be_namespace_mutation_receiver(
                    id,
                    &aliases,
                    &receiver_aliases,
                    &namespace_value_facts,
                )
            };
            // Keep the original conservative admission predicate independent
            // of diagnostic witness extraction below.
            let top_member_write_rejected = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, (receiver, property))| {
                    receiver.as_ref().is_some_and(|receiver| {
                        !property
                            .as_ref()
                            .is_some_and(|property| certified(owner, None, receiver, property))
                            && ((receiver == id
                                && property
                                    .as_ref()
                                    .is_none_or(|property| members.contains(property)))
                                || aliases.contains(receiver)
                                || receiver_aliases.contains(receiver))
                    })
                });
            let top_member_write_witness = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .find_map(|(owner, (receiver, property))| {
                    let receiver = receiver.as_ref()?;
                    let property = property.as_ref()?;
                    (members.contains(property)
                        && !certified(owner, None, receiver, property)
                        && (receiver == id
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver)))
                    .then_some((owner, receiver, property))
                });
            if let Some((statement_ordinal, receiver_binding, property)) = &top_member_write_witness
            {
                let witness = CallbackFactoryWriteWitness {
                    statement_ordinal: *statement_ordinal,
                    receiver_binding: receiver_binding.0.to_string(),
                    property: Some((*property).clone()),
                };
                let mut recorded = namespace_member_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let destructuring_write = index.destructuring_member_writes.iter().any(|write| {
                is_mutation_receiver(&write.receiver)
                    || write
                        .source
                        .may_publish_namespace(id, &aliases, &namespace_value_facts)
            });
            let top_dynamic_write = index
                .top_dynamic_property_receivers
                .iter()
                .flatten()
                .any(is_mutation_receiver);
            // This is a diagnostic for the already rejected guard, not an
            // admission rule. Only a concrete binding receiver supplies an
            // attributable source witness; opaque receiver shapes stay unknown.
            let dynamic_write_witness = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| writes.iter().map(move |write| (owner, write)))
                .find_map(|(owner, (receiver, property))| match (receiver, property) {
                    (Some(binding), None)
                        if binding == id
                            || aliases.contains(binding)
                            || receiver_aliases.contains(binding) =>
                    {
                        Some(CallbackFactoryWriteWitness {
                            statement_ordinal: SourceUnitOrdinal(owner),
                            receiver_binding: binding.0.to_string(),
                            property: None,
                        })
                    }
                    _ => None,
                });
            if let Some(witness) = dynamic_write_witness {
                let mut recorded = namespace_dynamic_property_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let top_callback_result_write = index
                .top_callback_result_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, write)| {
                    (is_mutation_receiver(&write.receiver)
                        && !certified_callback_write(owner, None, write))
                        || write
                            .value
                            .may_publish_namespace(id, &aliases, &namespace_value_facts)
                });
            let callable_write = index.callables.iter().any(|(key, callable)| {
                callable.member_writes.iter().any(|(receiver, property)| {
                    !certified(callable.owner, Some(*key), receiver, property)
                        && ((receiver == id && members.contains(property))
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver))
                }) || callable
                    .dynamic_property_receivers
                    .iter()
                    .any(is_mutation_receiver)
                    || callable.literal_exposures.iter().any(is_namespace_or_alias)
                    || callable.escaped_bindings.iter().any(is_namespace_or_alias)
                    || callable.namespace_escape_values.iter().any(|value| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.member_value_writes.iter().any(|write| {
                        write
                            .value
                            .may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.callback_result_writes.iter().any(|write| {
                        (is_mutation_receiver(&write.receiver)
                            && !certified_callback_write(callable.owner, Some(*key), write))
                            || write.value.may_publish_namespace(
                                id,
                                &aliases,
                                &namespace_value_facts,
                            )
                    })
                    || callable
                        .body_facts
                        .value_flow()
                        .returns()
                        .iter()
                        .any(|value| {
                            value.may_publish_namespace_in_factory_result(
                                id,
                                &aliases,
                                &namespace_value_facts,
                            )
                        })
                    || callable.calls.iter().any(|call| {
                        call.args.iter().any(|value| {
                            value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                        })
                    })
            });
            let top_member_value = index.top_member_value_writes.iter().flatten().any(|write| {
                write
                    .value
                    .may_publish_namespace(id, &aliases, &namespace_value_facts)
            });
            let top_call_argument = index.top_calls.iter().flatten().any(|call| {
                call.args
                    .iter()
                    .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
            });
            if top_call_argument {
                let direct = index
                    .top_calls
                    .iter()
                    .flatten()
                    .filter(|call| {
                        call.kind == CallKind::Call
                            && call.caller.is_none()
                            && !call.has_spread
                            && index.ordinary_top_call_sites.contains(&call.span.into())
                    })
                    .flat_map(|call| {
                        call.args
                                .iter()
                                .enumerate()
                                .filter(|(_, value)| {
                                    matches!(*value, ValueExpr::Binding(binding) if binding == id)
                                })
                                .map(move |(argument_index, _)| CallbackFactoryCallArgumentWitness {
                                    statement_ordinal: call.owner,
                                    call_span: call.span,
                                    argument_index,
                                    namespace_binding: id.0.to_string(),
                                })
                    })
                    .min_by_key(|witness| {
                        (
                            witness.statement_ordinal,
                            witness.call_span.lo.0,
                            witness.argument_index,
                        )
                    });
                if let Some(witness) = direct {
                    namespace_call_argument_witnesses
                        .borrow_mut()
                        .insert(id.clone(), witness);
                }
            }
            let exported_value = index
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
                || index.exported_bindings.iter().any(is_namespace_or_alias);
            !(top_member_write_rejected
                || destructuring_write
                || top_dynamic_write
                || top_callback_result_write
                || callable_write
                || top_member_value
                || top_call_argument
                || exported_value)
        };
        let namespace_family: Vec<_> = index.namespace_privacy.members.keys().cloned().collect();
        let family_audited_members: BTreeSet<_> = if namespace_family.is_empty() {
            BTreeSet::new()
        } else {
            index
                .audited_factory_members
                .values()
                .chain(index.no_reentry_members.values())
                .chain(index.no_reentry_record_members.values())
                .flat_map(|members| members.iter().cloned())
                .collect()
        };
        let factory_imports = index
            .callback_factory_imports
            .union(&index.inline_callback_factory_imports)
            .cloned()
            .collect();
        // Candidate rejection grows to closure. No provisional admission is
        // exported: removing a factory can invalidate fresh-result writes used
        // by another candidate in the next evaluation.
        type Members = BTreeMap<Id, BTreeSet<String>>;
        #[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
        enum Rejected {
            Factory(Id),
            NoReentry(Id),
            NoReentryRecord(Id),
        }
        struct NamespaceRules<'a, Fresh, Keep> {
            factories: &'a Members,
            legacy: &'a Members,
            no_reentry: &'a Members,
            records: &'a Members,
            independent: &'a Members,
            fresh: &'a Fresh,
            keep: &'a Keep,
        }
        impl<Fresh, Keep> IterativeRules for NamespaceRules<'_, Fresh, Keep>
        where
            Fresh: Fn(&Members) -> (BTreeSet<SourceUnitOrdinal>, BTreeSet<SourceUnitOrdinal>),
            Keep: Fn(&Id, &BTreeSet<String>, &BTreeSet<SourceUnitOrdinal>) -> bool,
        {
            type Key = Rejected;
            type Fact = ();
            type Rule = ();
            fn evaluate(&self, _: &(), context: &mut impl FactContext<Self::Key, Self::Fact>) {
                let legacy: Members = self
                    .legacy
                    .iter()
                    .filter(|(id, _)| context.read(Rejected::Factory((*id).clone())).is_none())
                    .map(|(id, members)| (id.clone(), members.clone()))
                    .collect();
                let (writes, _) = (self.fresh)(&legacy);
                for (id, members) in self.factories {
                    let key = Rejected::Factory(id.clone());
                    if context.read(key.clone()).is_none() && !(self.keep)(id, members, &writes) {
                        context.propose(key, ());
                    }
                }
                for id in self.legacy.keys() {
                    if !self.factories.contains_key(id) {
                        context.propose(Rejected::Factory(id.clone()), ());
                    }
                }
                for (candidates, record) in [(self.no_reentry, false), (self.records, true)] {
                    for (id, members) in candidates {
                        let key = if record {
                            Rejected::NoReentryRecord(id.clone())
                        } else {
                            Rejected::NoReentry(id.clone())
                        };
                        if context.read(key.clone()).is_none()
                            && (!self
                                .independent
                                .get(id)
                                .is_some_and(|independent| members.is_subset(independent))
                                || !(self.keep)(id, members, &writes))
                        {
                            context.propose(key, ());
                        }
                    }
                }
            }
            fn merge(_: &Rejected, _: &mut (), _: ()) -> bool {
                false
            }
        }
        let derive_fresh = |legacy: &Members| {
            if index.dynamic_scope_owners.is_empty() {
                fresh_value_writes(
                    &fresh_steps,
                    &namespace_value_facts,
                    &factory_imports,
                    legacy,
                )
            } else {
                (BTreeSet::new(), BTreeSet::new())
            }
        };
        let keep_audited =
            |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<SourceUnitOrdinal>| {
                if namespace_family.is_empty() {
                    keep_namespace(id, members, writes)
                } else {
                    // Imported names do not establish distinct object identities.
                    namespace_family
                        .iter()
                        .all(|root| keep_namespace(root, &family_audited_members, writes))
                }
            };
        let completed = IterativeInference::new(NamespaceRules {
            factories: &index.audited_factory_members,
            legacy: &index.callback_factory_members,
            no_reentry: &index.no_reentry_members,
            records: &index.no_reentry_record_members,
            independent: &index.namespace_privacy.members,
            fresh: &derive_fresh,
            keep: &keep_audited,
        })
        .solve([()]);
        let retained_legacy: Members = index
            .callback_factory_members
            .iter()
            .filter(|(id, _)| completed.get(&Rejected::Factory((*id).clone())).is_none())
            .map(|(id, members)| (id.clone(), members.clone()))
            .collect();
        let (_, instance_field_writes) = derive_fresh(&retained_legacy);
        index.fresh_instance_field_writes = instance_field_writes;
        index
            .audited_factory_members
            .retain(|id, _| completed.get(&Rejected::Factory(id.clone())).is_none());
        index.callback_factory_members = retained_legacy;
        index
            .no_reentry_members
            .retain(|id, _| completed.get(&Rejected::NoReentry(id.clone())).is_none());
        index.no_reentry_record_members.retain(|id, _| {
            completed
                .get(&Rejected::NoReentryRecord(id.clone()))
                .is_none()
        });
        index.rejected_namespace_member_writes = namespace_member_write_witnesses.into_inner();
        index.rejected_namespace_dynamic_property_writes =
            namespace_dynamic_property_write_witnesses.into_inner();
        index.rejected_namespace_call_arguments = namespace_call_argument_witnesses.into_inner();
        if index.deferred_global_requested {
            let first_publication = index
                .deferred_global_candidates
                .iter()
                .position(|candidate| {
                    candidate.as_ref().is_some_and(|candidate| {
                        hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                    })
                })
                .map(SourceUnitOrdinal)
                .unwrap_or(SourceUnitOrdinal(0));
            let protected: BTreeSet<String> = hints
                .assumptions
                .deferred_global_publications
                .values()
                .flat_map(|slots| slots.iter().cloned())
                .collect();
            let certified_root_ids: BTreeSet<Id> = index
                .deferred_global_candidates
                .iter()
                .flatten()
                .filter(|candidate| {
                    let name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    index.global_object_names.contains(name)
                        && hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                })
                .map(|candidate| candidate.receiver_id.clone())
                .collect();
            let global_root_exposed =
                index.global_receiver_exposed(first_publication, &protected, &certified_root_ids);
            if index.dynamic_scope_owners.is_empty()
                && !index.dynamic_global_member
                && !index.global_alias_written
                && !global_root_exposed
            {
                for (owner, candidate) in index.deferred_global_candidates.iter().enumerate() {
                    let Some(candidate) = candidate else { continue };
                    let root_name = match candidate.root {
                        BuiltinGlobalRoot::GlobalThis => "globalThis",
                        BuiltinGlobalRoot::Window => "window",
                    };
                    if !index.global_object_names.contains(root_name)
                        || !hints
                            .assumptions
                            .deferred_global_publications
                            .get(&candidate.root)
                            .is_some_and(|slots| slots.contains(&candidate.property))
                        || index.global_slot_mentions.get(&candidate.property) != Some(&1)
                        || candidate.property == "__proto__"
                        || !index.top_calls[owner].is_empty()
                        || index.top_property_writes[owner].len() != 1
                        || index.top_member_value_writes[owner].len() != 1
                        || index.top_callback_result_writes[owner].len() != 1
                    {
                        continue;
                    }
                    index
                        .deferred_global_certificates
                        .insert(SourceUnitOrdinal(owner));
                }
            }
        }
        if !hints.assumptions.non_reentrant_global_membership.is_empty() {
            let mut bindings = GlobalMembershipBindings::default();
            for item in body {
                item.as_module_item().visit_with(&mut bindings);
            }
            index.global_membership_candidates.retain(|_, probe| {
                !bindings.0.contains(&probe.receiver)
                    && hints
                        .assumptions
                        .non_reentrant_global_membership
                        .get(&probe.root)
                        .is_some_and(|keys| keys.contains(&probe.property))
            });
            let first_probe = index
                .global_membership_candidates
                .values()
                .map(|probe| probe.owner)
                .min()
                .unwrap_or(SourceUnitOrdinal(0));
            let protected = hints
                .assumptions
                .non_reentrant_global_membership
                .values()
                .flat_map(|keys| keys.iter().cloned())
                .collect();
            let roots = index
                .global_membership_candidates
                .values()
                .map(|probe| probe.receiver.clone())
                .collect();
            let prototype_write = index
                .top_callback_result_writes
                .iter()
                .flatten()
                .chain(
                    index
                        .callables
                        .values()
                        .flat_map(|callable| &callable.callback_result_writes),
                )
                .any(|write| {
                    let path = global_mutation_path(
                        &write.receiver,
                        &index.global_object_names,
                        &bindings.0,
                    );
                    // A visible stored back-alias defeats the descendant
                    // distinction even when its receiver is an ordinary object.
                    // Prototype values held by unrelated private objects do
                    // not themselves expose or mutate a global-rooted path.
                    if stored_global_alias(
                        &write.value,
                        &index.global_object_names,
                        &bindings.0,
                        path != GlobalMutationPath::Unrelated,
                    ) {
                        return true;
                    }
                    match write.property.as_deref() {
                        Some("__proto__" | "prototype" | "constructor") => {
                            path != GlobalMutationPath::Unrelated
                                || global_receiver_path(&write.receiver, &index.global_object_names)
                        }
                        None => matches!(
                            path,
                            GlobalMutationPath::Identity | GlobalMutationPath::Prototype
                        ),
                        _ => false,
                    }
                });
            if index.dynamic_scope_owners.is_empty()
                && !index.dynamic_global_member
                && !index.global_alias_written
                && !prototype_write
                && !index
                    .mutated_intrinsic_roots
                    .contains(&NativeIntrinsic::Object)
                && !index.global_receiver_exposed(first_probe, &protected, &roots)
            {
                index
                    .non_reentrant_global_membership
                    .extend(index.global_membership_candidates.keys().copied());
            }
        }
        let primitive_const_candidates = std::mem::take(&mut index.direct_primitive_const_bindings);
        index.direct_primitive_const_bindings = primitive_const_candidates
            .into_iter()
            .filter(|binding| {
                if !index.dynamic_scope_owners.is_empty()
                    || index.reassigned_bindings.contains(binding)
                {
                    return false;
                }
                matches!(
                    index.binding_declaration_writes.get(binding).map(Vec::as_slice),
                    Some([declaration])
                        if declaration.direct_top_level && declaration.simple_identifier
                )
            })
            .collect();
        // Native-operation inputs annotate exact source sites without
        // refining the native census. Array-protocol contradictions are
        // checked later against the completed all-source census.
        index.validate_native_operation_inputs(
            &hints.selected_native_operation_inputs,
            &hints.assumptions.native_operation_inputs,
            array_spreads,
        );
        index.ident_references.sort_unstable();
        index.ident_references.dedup();
        index.this_reading_accessor_objects = index
            .callable_creations
            .values()
            .filter_map(|creation| match *creation {
                DeferredCallableCreation::Getter {
                    callable,
                    home_object,
                }
                | DeferredCallableCreation::Setter {
                    callable,
                    home_object,
                } => index.reads_this(callable).then_some(home_object),
                _ => None,
            })
            .collect();
        index.this_reading_accessor_classes = index
            .class_accessors
            .iter()
            .filter(|(_, accessors)| accessors.iter().any(|accessor| index.reads_this(*accessor)))
            .map(|(class, _)| *class)
            .collect();
        index
    }

    /// Whether `callable`, or an arrow or other callable nested in it, reads
    /// `this`. An unindexed or opaque callable may.
    pub(super) fn reads_this(&self, callable: u32) -> bool {
        let Some(span) = self.callable_spans.get(&callable) else {
            return true;
        };
        self.callables.get(&callable).is_none_or(|body| body.opaque)
            || self
                .callable_spans
                .range(span.lo.0..span.hi.0)
                .any(|(nested, _)| {
                    self.callables
                        .get(nested)
                        .is_some_and(|nested| nested.implicit_capture)
                })
    }

    /// The bindings referenced in `[lo, hi)`.
    pub(super) fn references_within(&self, lo: u32, hi: u32) -> impl Iterator<Item = &Id> {
        let start = self
            .ident_references
            .partition_point(|(position, _)| *position < lo);
        self.ident_references[start..]
            .iter()
            .take_while(move |(position, _)| *position < hi)
            .map(|(_, id)| id)
    }

    /// A source binding used exactly once, at this member read. A second use
    /// could alias, delete, redefine or publish the instance before the read.
    pub(super) fn sole_binding_reference_at(&self, binding: &Id, span: Span) -> bool {
        self.dynamic_scope_owners.is_empty()
            && !self.reassigned_bindings.contains(binding)
            && !self.binding_target_writes.contains(binding)
            && !self.exported_bindings.contains(binding)
            && !self.imported_bindings.contains(binding)
            && self
                .binding_declaration_writes
                .get(binding)
                .is_some_and(|writes| matches!(writes.as_slice(), [write] if write.direct_top_level && write.simple_identifier))
            && self.binding_use_bounds.get(binding).is_some_and(|(first, last)| {
                first == last && span.lo.0 <= *first && *first < span.hi.0
            })
    }

    /// This member operation belongs to a direct module-level initializer,
    /// whose expression is evaluated at most once. A lexical use inside a
    /// callable can execute again after its first result exposes the instance.
    pub(super) fn single_eager_initializer_read(&self, call: &Call) -> bool {
        call.caller.is_none()
            && self.top_calls[call.owner.0].iter().any(|indexed| {
                indexed.kind == CallKind::LocalMemberRead
                    && indexed.span == call.span
                    && indexed.caller.is_none()
            })
            && self
                .binding_declaration_writes
                .values()
                .flatten()
                .any(|write| {
                    write.owner == call.owner
                        && write.direct_top_level
                        && write
                            .initializer_span
                            .is_some_and(|span| span.lo <= call.span.lo && call.span.hi <= span.hi)
                })
    }

    pub(super) fn statement_span(&self, owner: SourceUnitOrdinal) -> Span {
        self.original_statement_spans[owner.0]
    }

    fn populate_native_point_effects(&mut self, external: &NativeWriteCensus) {
        // The structural visitor is the single source for activation. A
        // source-unit ordinal alone cannot distinguish a function declaration
        // from a synchronous invocation of its body.
        let mut activations =
            BTreeMap::<(SourceUnitOrdinal, u32, u32), BTreeSet<Option<u32>>>::new();
        let mut site = |owner: SourceUnitOrdinal, callable: Option<u32>, span: Span| {
            if span != DUMMY_SP {
                activations
                    .entry((owner, span.lo.0, span.hi.0))
                    .or_default()
                    .insert(callable);
            }
        };
        for write in &self.intrinsic_writes {
            site(write.owner, write.activation_callable, write.span);
        }
        for call in &self.prototype_calls {
            site(call.owner, call.caller, call.span);
        }
        for (owner, callable, span, _) in &self.prototype_field_writes {
            site(*owner, *callable, *span);
        }
        let activation = |owner: SourceUnitOrdinal, span: Span| {
            if span == DUMMY_SP {
                return None;
            }
            activations
                .get(&(owner, span.lo.0, span.hi.0))
                .and_then(|candidates| {
                    (candidates.len() == 1).then(|| *candidates.iter().next().unwrap())
                })
        };
        let mut initial = NativePointEffects::default();
        let is_foreign = |chunk_id: &str| self.source_chunk_id.as_deref() != Some(chunk_id);
        for mutation in external
            .prototype_mutations
            .iter()
            .filter(|mutation| is_foreign(&mutation.chunk_id))
        {
            initial.add_prototype_mutation(mutation);
        }
        for witness in external
            .witnesses
            .iter()
            .filter(|witness| is_foreign(&witness.chunk_id))
        {
            if witness.unreachable_branch.is_none() {
                initial.roots.extend(&witness.affected_roots);
            }
        }
        // The supplied census covers this chunk too. Only roots lacking a
        // source witness in that completed census are necessarily active at
        // entry. A foreign witness stays active even when a local write has
        // the same root.
        let witnessed_roots: IntrinsicRoots = external
            .witnesses
            .iter()
            .filter(|witness| witness.unreachable_branch.is_none())
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .collect();
        initial
            .roots
            .extend(external.unconditional.difference(&witnessed_roots).copied());
        let local_witnessed_roots: IntrinsicRoots = self
            .native_write_census
            .witnesses
            .iter()
            .filter(|witness| witness.unreachable_branch.is_none())
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .chain(
                self.native_write_census
                    .conditional
                    .iter()
                    .filter(|write| write.witness.unreachable_branch.is_none())
                    .flat_map(|write| write.fallback.iter().copied()),
            )
            .collect();
        initial.roots.extend(
            self.native_write_census
                .unconditional
                .difference(&local_witnessed_roots)
                .copied(),
        );
        // Dynamic scope is the only unconditional root without a source
        // witness. Keep it active even if another write has the same family.
        if self
            .native_write_census
            .prototype_mutations
            .iter()
            .chain(external.prototype_mutations.iter())
            .any(|mutation| mutation.kind == NativePrototypeMutationKind::DynamicScope)
        {
            initial.roots.insert(NativeIntrinsic::GlobalObject);
        }
        let mut by_activation = BTreeMap::<_, NativePointEffects>::new();
        for witness in self.native_write_census.witnesses.iter() {
            if witness.unreachable_branch.is_some() || witness.affected_roots.is_empty() {
                continue;
            }
            if let Some(callable) = activation(witness.statement_ordinal, witness.span) {
                by_activation
                    .entry((witness.statement_ordinal, callable))
                    .or_default()
                    .roots
                    .extend(&witness.affected_roots);
            } else {
                initial.roots.extend(&witness.affected_roots);
            }
        }
        for write in &self.native_write_census.conditional {
            if write.witness.unreachable_branch.is_some() {
                continue;
            }
            if let Some(callable) = activation(write.witness.statement_ordinal, write.witness.span)
            {
                by_activation
                    .entry((write.witness.statement_ordinal, callable))
                    .or_default()
                    .roots
                    .extend(&write.fallback);
            } else {
                initial.roots.extend(&write.fallback);
            }
        }
        for mutation in self.native_write_census.prototype_mutations.iter() {
            let mut mutation = mutation.clone();
            if !mutation
                .key_dependencies
                .is_disjoint(&self.mutated_intrinsic_roots)
                || (!mutation.key_dependencies.is_empty()
                    && self
                        .mutated_intrinsic_roots
                        .contains(&NativeIntrinsic::GlobalObject))
            {
                mutation.properties = None;
            }
            if let Some(callable) = activation(mutation.statement_ordinal, mutation.span) {
                by_activation
                    .entry((mutation.statement_ordinal, callable))
                    .or_default()
                    .add_prototype_mutation(&mutation);
            } else {
                initial.add_prototype_mutation(&mutation);
            }
        }
        let mut all = initial.clone();
        for effects in by_activation.values() {
            all.extend(effects);
        }
        self.native_point_effects = by_activation;
        self.initial_native_point_effects = initial;
        self.all_native_point_effects = all;
    }

    /// A separate exact-read capability; this never supplies original native
    /// slot integrity or a known Symbol identity/type.
    pub(super) fn primitive_class_key_read_premise(&self, span: Span) -> Option<NativePremise> {
        if !self.dynamic_scope_owners.is_empty() {
            return None;
        }
        self.native_own_data_reads.get(&span.lo.0)?;
        let evidence = self
            .native_operation_input_evidence
            .get(&CallSite::from(span))?;
        if evidence.operation
            != NativeInputKind::Operation(spec::NativeOperationKind::SymbolPrimitiveClassKeys)
        {
            return None;
        }
        let dependencies = ProofDependencies {
            native_operation_inputs: BTreeSet::from([evidence.reference.clone()]),
            ..Default::default()
        };
        Some(NativePremise {
            inference_use: crate::analysis_provenance::InferenceUse {
                conclusion: crate::analysis_provenance::InferenceRule::NativeOwnDataRead,
                derivation_rules: BTreeSet::from([
                    crate::analysis_provenance::InferenceRule::NativeOwnDataRead,
                ]),
                value_input_refs: BTreeSet::new(),
                descriptor_copy_author_refs: BTreeSet::new(),
                argument_input_refs: BTreeSet::new(),
                native_operation_input_refs: dependencies.native_operation_inputs.clone(),
                imported_primitive_array_spread_input_refs: dependencies
                    .imported_primitive_array_spread_inputs
                    .clone(),
            },
            dependencies,
            census: false,
        })
    }

    /// The exact own-data read capability or native slot proof. A primitive
    /// capability says nothing about the slot's original value or stability.
    pub(super) fn native_own_data_read_premise(&self, span: Span) -> Option<NativePremise> {
        if !self.dynamic_scope_owners.is_empty() {
            return None;
        }
        if let Some(premise) = self.primitive_class_key_read_premise(span) {
            return Some(premise);
        }
        let slots = self.native_own_data_reads.get(&span.lo.0)?;
        native_premise(
            &self.integrity,
            slots,
            Some(span.lo.0),
            crate::analysis_provenance::InferenceRule::NativeOwnDataRead,
        )
    }

    pub(crate) fn locate_native_integrity_witnesses(
        &mut self,
        mut locate: impl FnMut(Span) -> Option<crate::execution_evidence::SourceLocation>,
    ) {
        if self
            .native_integrity_witnesses
            .iter()
            .any(|witness| witness.chunk_id.is_empty())
        {
            for witness in Arc::make_mut(&mut self.native_integrity_witnesses) {
                if witness.chunk_id.is_empty() {
                    witness.source_location = locate(witness.span);
                }
            }
        }
        for mutation in Arc::make_mut(&mut self.native_prototype_mutations) {
            if mutation.chunk_id.is_empty() {
                mutation.source_location = locate(mutation.span);
            }
        }
    }

    fn global_receiver_exposed(
        &self,
        first_publication: SourceUnitOrdinal,
        protected: &BTreeSet<String>,
        certified_root_ids: &BTreeSet<Id>,
    ) -> bool {
        self.top_assignments
            .iter()
            .flatten()
            .any(|(_, value)| exposes_global_receiver(value, &self.global_object_names))
            || self
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| exposes_global_receiver(value, &self.global_object_names))
            || self.top_calls.iter().flatten().any(|call| {
                call_exposes_global_receiver(call, &self.global_object_names, certified_root_ids)
            })
            || self.callables.iter().any(|(key, callable)| {
                callable.local_assignments.iter().any(|(id, value)| {
                    exposes_global_receiver(value, &self.global_object_names)
                        && !confined_prepublication_alias(
                            self,
                            *key,
                            id,
                            first_publication,
                            protected,
                        )
                }) || callable
                    .body_facts
                    .value_flow()
                    .returns()
                    .iter()
                    .any(|value| exposes_global_receiver(value, &self.global_object_names))
                    || callable.calls.iter().any(|call| {
                        call_exposes_global_receiver(
                            call,
                            &self.global_object_names,
                            certified_root_ids,
                        )
                    })
            })
    }

    fn collect_uninitialized_lexical_bindings(&mut self, item: &ModuleItem) {
        let Some(declaration) = var_decl_of_item(item) else {
            return;
        };
        if matches!(declaration.kind, VarDeclKind::Let | VarDeclKind::Const)
            && !declaration.declare
            && let [declarator] = declaration.decls.as_slice()
            && let Pat::Ident(binding) = &declarator.name
        {
            self.single_top_lexical_binding[self.owner.0] = Some(binding.id.to_id());
        }
        if declaration.kind == VarDeclKind::Const
            && matches!(declaration.decls.as_slice(), [declarator]
                if matches!(declarator.init.as_deref().map(Expr::unwrap_parens), Some(Expr::Call(call))
                    if matches!(&call.callee, Callee::Expr(callee)
                        if matches!(callee.unwrap_parens(), Expr::Member(member)
                            if matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
                                && matches!(&member.prop, MemberProp::Ident(prop) if prop.sym == "freeze")))))
        {
            self.const_call_owners.insert(self.owner);
        }
        if declaration.kind == VarDeclKind::Var {
            for declarator in &declaration.decls {
                if declarator.init.is_some()
                    && let Pat::Ident(binding) = &declarator.name
                {
                    self.top_definite_initializers[self.owner.0].insert(binding.id.to_id());
                }
            }
            return;
        }
        for declarator in &declaration.decls {
            if let Pat::Ident(binding) = &declarator.name {
                let id = binding.id.to_id();
                self.top_definite_initializers[self.owner.0].insert(id.clone());
                if declarator.init.is_none() {
                    self.top_assignments[self.owner.0].push((id, ValueExpr::Undefined));
                }
            }
        }
    }

    fn invalidate_property_key_pattern(&mut self, pattern: &Pat) {
        let mut bindings = Vec::new();
        collect_pattern_assignments(pattern, &ValueExpr::Unknown, &mut bindings);
        for (binding, _) in bindings {
            self.property_key_assignments
                .entry(binding)
                .or_default()
                .push(PropertyKeyExpr::Unknown);
        }
    }

    fn collect_export_roots(&mut self, item: &ModuleItem) {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => {
                self.hoisted_function_owners.insert(self.owner);
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                for id in collect_declared_names(item) {
                    self.exported_names.insert(id.0.to_string(), id.clone());
                    self.exported_bindings.insert(id);
                }
                if matches!(export.decl, Decl::Fn(_)) {
                    self.hoisted_function_owners.insert(self.owner);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.default_export_owners.insert(self.owner);
                if let DefaultDecl::Fn(function) = &export.decl {
                    self.hoisted_function_owners.insert(self.owner);
                    self.exported_hoisted_functions
                        .insert(function.function.span.lo.0);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.default_export_owners.insert(self.owner);
                self.top_exported_values[self.owner.0].push(ValueExpr::from_expr(&export.expr));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_none() => {
                for specifier in &export.specifiers {
                    if let ExportSpecifier::Named(named) = specifier
                        && let ModuleExportName::Ident(local) = &named.orig
                    {
                        let exported = match named.exported.as_ref().unwrap_or(&named.orig) {
                            ModuleExportName::Ident(ident) => ident.sym.to_string(),
                            ModuleExportName::Str(value) => js_ast::str_value(value),
                        };
                        self.exported_names.insert(exported, local.to_id());
                        self.exported_bindings.insert(local.to_id());
                    }
                }
            }
            _ => {}
        }
    }

    fn with_callable(&mut self, span: Span, params: Vec<Parameter>, visit: impl FnOnce(&mut Self)) {
        let key = span.lo.0;
        self.callable_spans.insert(key, span);
        let created_after_parent_await = self
            .current
            .and_then(|parent| self.callables.get(&parent))
            .is_some_and(|parent| parent.created_after_parent_await)
            || (self.past_await && !self.hoisted_nested_functions.contains(&key));
        let saved_current = self.current.replace(key);
        let saved_variable_scope = self.variable_scope.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let saved_conditional = std::mem::replace(&mut self.conditional_depth, 0);
        let saved_assignment_target = std::mem::replace(&mut self.assignment_target, false);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
            created_after_parent_await,
            params,
            opaque: opaque_params,
            ..Callable::default()
        });
        self.prototype_capture_stack.push(key);
        visit(self);
        self.prototype_capture_stack.pop();
        let callable = self.callables.get_mut(&key).unwrap();
        if let Some(body) = callable.body_facts.take_ordered_plan()
            // Defaults run outside the body var environment. Use the full
            // declaration census, including dead/nested var declarations.
            && ((callable.hoisted_vars.is_empty() && callable.local_functions.is_empty())
                || body.parameters.iter().all(|parameter| parameter.default.is_none()))
        {
            callable.body_facts.set_ordered_plan(Some(
                body.with_declarations(
                    callable.hoisted_vars.iter().cloned(),
                    callable
                        .lexical_declarations
                        .iter()
                        .map(|(binding, kind)| (binding.clone(), *kind))
                        .chain(callable.local_functions.iter().map(|(binding, function)| {
                            (binding.clone(), LocalBinding::HoistedFunction(*function))
                        })),
                ),
            ));
        }
        self.current = saved_current;
        self.variable_scope = saved_variable_scope;
        self.past_await = saved_await;
        self.may_skip_await_depth = saved_may_skip;
        self.conditional_depth = saved_conditional;
        self.assignment_target = saved_assignment_target;
    }

    fn record_call(&mut self, call: Call) {
        // Native property integrity includes deferred and post-await bodies.
        self.prototype_calls.push(call.clone());
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.dynamic_scope_owners.insert(self.owner);
        }
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().calls.push(call);
        } else {
            self.top_calls[self.owner.0].push(call);
        }
    }

    fn record_accessor_callee_lookup(
        &mut self,
        member: &MemberExpr,
        no_reentry_call: Option<CallSite>,
    ) {
        if let Some(no_reentry_call) = no_reentry_call {
            self.no_reentry_accessor_calls
                .insert(CallSite::from(member.span), no_reentry_call);
        }
        self.record_call(Call {
            kind: CallKind::AccessorCalleeLookup,
            callee: ValueExpr::Member(
                Arc::new(ValueExpr::from_expr(&member.obj)),
                static_prop(&member.prop),
                MemberKey::of(&member.prop),
                member.span.into(),
            ),
            args: Vec::new(),
            span: member.span,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        });
    }

    fn record_source_protocol(&mut self, kind: CallKind, source: ValueExpr, span: Span) {
        self.record_call(Call {
            kind,
            callee: ValueExpr::Unknown,
            args: vec![source],
            span,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        });
    }

    fn record_implicit_coercion(&mut self, expression: &Expr, span: Span) {
        if (self.current.is_some() && self.past_await) || has_definite_primitive_result(expression)
        {
            return;
        }
        self.record_implicit_coercion_value(ValueExpr::from_expr(expression), span);
    }

    fn record_implicit_coercion_value(&mut self, argument: ValueExpr, span: Span) {
        if self.current.is_some() && self.past_await {
            return;
        }
        self.record_call(Call {
            kind: CallKind::ImplicitCoercion,
            callee: ValueExpr::Unknown,
            args: vec![argument],
            span,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        });
    }

    fn mark_opaque(&mut self) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().opaque = true;
        } else {
            self.opaque_top.insert(self.owner);
        }
    }

    fn mark_dynamic_property_effect(&mut self, receiver: Option<&Expr>) {
        if self.past_await {
            return;
        }
        if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.dynamic_property_effect = true;
            callable
                .dynamic_property_receivers
                .push(receiver.map_or(ValueExpr::Unknown, ValueExpr::from_expr));
        } else {
            self.top_property_writes[self.owner.0].push((None, None));
            if let Some(receiver) = receiver {
                self.top_dynamic_property_receivers[self.owner.0]
                    .push(ValueExpr::from_expr(receiver));
            }
        }
    }

    fn mark_possible_namespace_escape(&mut self, expression: &Expr) {
        let Some(key) = self.current else {
            return;
        };
        let value = ValueExpr::from_expr(expression);
        let exposed = self
            .audited_factory_members
            .keys()
            .filter(|id| value.carries_namespace(id, &BTreeSet::new()))
            .cloned()
            .collect::<Vec<_>>();
        let callable = self.callables.get_mut(&key).unwrap();
        callable.escaped_bindings.extend(exposed);
        callable.namespace_escape_values.push(value);
    }

    fn is_factory_member_data_value(&self, key: &PropName, expression: &Expr) -> bool {
        if object_data_property_name(key).is_none() {
            return false;
        }
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        matches!(member.obj.unwrap_parens(), Expr::Ident(namespace)
            if self.audited_factory_members.contains_key(&namespace.to_id()))
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn is_callback_factory_member_callee(&self, expression: &Expr) -> bool {
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        let Expr::Ident(namespace) = member.obj.unwrap_parens() else {
            return false;
        };
        self.audited_factory_members
            .contains_key(&namespace.to_id())
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn inline_callback_callee(call: &CallExpr) -> Option<&Expr> {
        if call.args.len() != 1
            || call.args[0].spread.is_some()
            || !matches!(
                call.args[0].expr.unwrap_parens(),
                Expr::Fn(_) | Expr::Arrow(_)
            )
        {
            return None;
        }
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        Some(callee.unwrap_parens())
    }

    fn inline_factory_callback_argument(&self, call: &CallExpr) -> Option<usize> {
        let is_factory = match Self::inline_callback_callee(call)? {
            Expr::Ident(callee) => self.callback_factory_imports.contains(&callee.to_id()),
            Expr::Member(member) => {
                let (Expr::Ident(namespace), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                self.callback_factory_members
                    .get(&namespace.to_id())
                    .is_some_and(|members| members.contains(property.sym.as_ref()))
            }
            _ => false,
        };
        is_factory.then_some(0)
    }

    fn retained_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let (signatures, namespace_member) = match callee.unwrap_parens() {
            Expr::Ident(binding) => (
                self.retained_callback_factory_imports
                    .get(&binding.to_id())?,
                None,
            ),
            Expr::Member(member) => {
                let (Expr::Ident(namespace), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                (
                    self.retained_callback_factory_members
                        .get(&namespace.to_id())?
                        .get(property.sym.as_ref())?,
                    Some((namespace.to_id(), property.sym.to_string())),
                )
            }
            _ => return None,
        };
        Self::retained_callback_invocation(signatures, &call.args, namespace_member)
    }

    fn no_reentry_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        let imported = if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(binding) = callee.unwrap_parens()
        {
            self.no_reentry_callback_imports
                .get(&binding.to_id())
                .and_then(|signatures| {
                    Self::retained_callback_invocation(signatures, &call.args, None)
                })
        } else {
            None
        };
        imported.or_else(|| self.source_no_reentry_callback_call(call))
    }

    fn source_no_reentry_callback_call(&self, call: &CallExpr) -> Option<RetainedCallbackCall> {
        if call.args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let signature = self
            .no_reentry_statement_calls
            .get(&(call.span.lo.0, call.span.hi.0))?;
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let simple_callee = match callee.unwrap_parens() {
            Expr::Ident(_) => true,
            Expr::Member(member) => {
                matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                    && matches!(member.prop, MemberProp::Ident(_))
            }
            _ => false,
        };
        if !simple_callee
            || signature.callback_arguments.iter().any(|position| {
                !call.args.get(*position).is_some_and(|argument| {
                    match argument.expr.unwrap_parens() {
                        Expr::Ident(_) => true,
                        Expr::Fn(function) => {
                            !function.function.is_async && !function.function.is_generator
                        }
                        Expr::Arrow(arrow) => !arrow.is_async,
                        _ => false,
                    }
                })
            })
        {
            return None;
        }
        let mut invocation =
            Self::retained_callback_invocation(std::slice::from_ref(signature), &call.args, None)?;
        invocation.require_single_callback_identity = true;
        Some(invocation)
    }

    fn retained_constructor_call(&self, allocation: &NewExpr) -> Option<RetainedCallbackCall> {
        let Expr::Ident(binding) = allocation.callee.unwrap_parens() else {
            return None;
        };
        Self::retained_callback_invocation(
            self.retained_callback_constructor_imports
                .get(&binding.to_id())?,
            allocation.args.as_deref().unwrap_or_default(),
            None,
        )
    }

    fn retained_callback_invocation(
        signatures: &[spec::RetainedCallbackSignature],
        args: &[ExprOrSpread],
        namespace_member: Option<(Id, String)>,
    ) -> Option<RetainedCallbackCall> {
        if args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let signatures: Vec<_> = signatures
            .iter()
            .filter(|signature| {
                signature.argument_count == args.len()
                    && signature
                        .callback_arguments
                        .iter()
                        .all(|position| *position < args.len())
                    && args.iter().enumerate().all(|(position, argument)| {
                        if signature.callback_arguments.contains(&position) {
                            matches!(
                                argument.expr.unwrap_parens(),
                                Expr::Ident(_) | Expr::Fn(_) | Expr::Arrow(_) | Expr::Call(_)
                            )
                        } else {
                            no_reentry_primitive_argument(&argument.expr)
                        }
                    })
            })
            .cloned()
            .collect();
        let callback_argument_positions: BTreeSet<_> = signatures
            .iter()
            .flat_map(|signature| signature.callback_arguments.iter().copied())
            .collect();
        let callback_argument_exprs = callback_argument_positions
            .into_iter()
            .map(|position| (position, ValueExpr::from_expr(&args[position].expr)))
            .collect();
        (!signatures.is_empty()).then_some(RetainedCallbackCall {
            namespace_member,
            signatures,
            argument_count: args.len(),
            callback_argument_exprs,
            require_single_callback_identity: false,
        })
    }

    fn record_published_source(&mut self, value: &Expr) {
        let value = ValueExpr::from_expr(value);
        match self.current {
            Some(key) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .published_sources
                .push(value),
            None => self
                .top_published_sources
                .entry(self.owner)
                .or_default()
                .push(value),
        }
    }

    /// `arguments` is recorded as unknown: an own `[Symbol.iterator]` write
    /// to it is not tracked.
    fn record_iterated_source(&mut self, value: &Expr) {
        let value = match value.unwrap_parens() {
            Expr::Ident(ident) if ident.sym == "arguments" => ValueExpr::Unknown,
            value => ValueExpr::from_expr(value),
        };
        self.record_iterated_value(value);
    }

    fn record_head_iterated_defaults(&mut self, head: &ForHead) {
        match head {
            ForHead::VarDecl(declaration) => {
                for declarator in &declaration.decls {
                    self.record_iterated_defaults(&declarator.name);
                }
            }
            ForHead::Pat(pattern) => self.record_iterated_defaults(pattern),
            ForHead::UsingDecl(_) => {}
        }
    }

    fn record_iterated_defaults(&mut self, pattern: &Pat) {
        let mut defaults = Vec::new();
        iterated_defaults(pattern, &mut defaults);
        for default in defaults {
            self.record_iterated_source(default);
        }
    }

    fn record_iterated_value(&mut self, value: ValueExpr) {
        match self.current {
            Some(key) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .iterated_sources
                .push(value),
            None => self
                .top_iterated_sources
                .entry(self.owner)
                .or_default()
                .push(value),
        }
    }

    fn record_nested_write(&mut self, binding: Id) {
        for key in &self.prototype_capture_stack {
            self.nested_callable_writes
                .entry(*key)
                .or_default()
                .insert(binding.clone());
        }
    }

    fn record_member_write(&mut self, member: &MemberExpr) {
        if self.past_await {
            return;
        }
        let receiver = match member.obj.unwrap_parens() {
            Expr::Ident(object) => Some(object.to_id()),
            _ => None,
        };
        let property = static_prop(&member.prop);
        match (self.current, receiver, property) {
            (Some(key), Some(receiver), Some(property)) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .member_writes
                .push((receiver, property)),
            (None, receiver, property) => {
                self.top_property_writes[self.owner.0].push((receiver, property))
            }
            _ => self.mark_dynamic_property_effect(Some(&member.obj)),
        }
    }

    fn visit_may_skip<N: VisitWith<Self>>(&mut self, node: &N) {
        let before = self.past_await;
        self.may_skip_await_depth += 1;
        self.conditional_depth += 1;
        node.visit_children_with(self);
        self.conditional_depth -= 1;
        self.may_skip_await_depth -= 1;
        self.past_await = before;
    }
}

impl binding_targets::TargetAccessRecorder for ExecutionIndex {
    fn record_binding_write(&mut self, id: &Id) {
        self.binding_target_writes.insert(id.clone());
        if self.current.is_some() {
            self.captured_bindings.insert(id.clone());
        }
        if !self.past_await
            && let Some(callable) = self.current.and_then(|key| self.callables.get(&key))
            && !callable.created_after_parent_await
        {
            self.owner_callable_prefix_effects[callable.owner.0]
                .get_or_insert_with(CallablePrefixEffects::default)
                .rebinds
                .insert(id.clone());
        }
    }
}

impl Visit for ExecutionIndex {
    fn visit_named_export(&mut self, export: &NamedExport) {
        for specifier in &export.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(ident) = &named.orig
                && self.namespace_privacy.members.contains_key(&ident.to_id())
            {
                self.namespace_privacy.invalid = true;
            }
        }
        // Linkage is captured by collect_export_roots. Specifiers do not run
        // at module initialization or expose a class in a closed module.
    }

    fn visit_class(&mut self, class: &Class) {
        // A derived constructor can receive a native object returned by super.
        // Its public fields therefore do not carry the fresh-this certificate.
        if class.super_class.is_some() {
            for member in &class.body {
                if let ClassMember::ClassProp(prop) = member
                    && !prop.is_static
                {
                    let key = match &prop.key {
                        PropName::Computed(key) => PropertyKeyExpr::from_expr(&key.expr),
                        key => static_property_name(key)
                            .map(PropertyKeyExpr::String)
                            .unwrap_or(PropertyKeyExpr::Unknown),
                    };
                    self.prototype_field_writes
                        .push((self.owner, self.current, prop.span, key));
                }
            }
        }
        self.class_owners.insert(class.span.lo.0, self.owner);
        self.class_spans.insert(class.span.lo.0, class.span);
        self.class_constructors
            .extend(class.body.iter().filter_map(|member| match member {
                ClassMember::Constructor(constructor) => Some(constructor.span.lo.0),
                _ => None,
            }));
        self.class_member_callables
            .extend(class.body.iter().filter_map(|member| match member {
                ClassMember::Constructor(constructor) => Some(constructor.span.lo.0),
                ClassMember::Method(method) => Some(method.function.span.lo.0),
                ClassMember::PrivateMethod(method) => Some(method.function.span.lo.0),
                _ => None,
            }));
        self.class_store_regions.insert(
            class.span.lo.0,
            class
                .body
                .iter()
                .filter_map(|member| match member {
                    ClassMember::Constructor(constructor) => Some(constructor.span),
                    ClassMember::ClassProp(prop) => prop.value.as_ref().map(|value| value.span()),
                    ClassMember::PrivateProp(prop) => prop.value.as_ref().map(|value| value.span()),
                    ClassMember::StaticBlock(block) => Some(block.span),
                    _ => None,
                })
                .collect(),
        );
        self.class_accessors.insert(
            class.span.lo.0,
            class
                .body
                .iter()
                .filter_map(|member| match member {
                    ClassMember::Method(method)
                        if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
                    {
                        Some(method.function.span.lo.0)
                    }
                    ClassMember::PrivateMethod(method)
                        if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
                    {
                        Some(method.function.span.lo.0)
                    }
                    _ => None,
                })
                .collect(),
        );
        if let Some(heritage) = &class.super_class {
            self.class_heritage_spans
                .insert(class.span.lo.0, heritage.span());
        }
        if let Some(site) = LocalClassConstructionShapes::private_empty_set_site(class) {
            self.private_empty_set_sites.insert(class.span.lo.0, site);
        }
        self.class_values
            .insert(class.span.lo.0, collect_class_value_facts(class));
        if let Some(body) = LocalBody::lower_class_constructor(class) {
            self.class_provider_bodies.insert(class.span.lo.0, body);
        }
        if let Some(constructor) = local_constructor(class, &self.local_class_construction_shapes) {
            self.local_constructors.insert(class.span.lo.0, constructor);
        }
        class.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, statement: &WithStmt) {
        self.dynamic_scope_owners.insert(self.owner);
        statement.obj.visit_with(self);
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::from_expr(&statement.obj),
            statement.span,
        );
        statement.body.visit_with(self);
        self.mark_opaque();
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        // Defining a class evaluates computed keys and static initializers,
        // but does not run its constructor or instance field initializers.
        // The ordinary SWC descent would put calls in those deferred bodies
        // into `top_calls`, falsely promoting their callees at class init.
        match member {
            ClassMember::ClassProp(prop) => prop.decorators.visit_with(self),
            ClassMember::PrivateProp(prop) => prop.decorators.visit_with(self),
            ClassMember::AutoAccessor(accessor) => accessor.decorators.visit_with(self),
            _ => {}
        }
        visit_eager_member_parts(self, member);
        match member {
            ClassMember::Method(method) => {
                if method.kind == MethodKind::Setter {
                    self.setter_owners
                        .entry(static_property_name(&method.key))
                        .or_default()
                        .insert(self.owner);
                }
                method.function.visit_with(self);
            }
            ClassMember::PrivateMethod(method) => {
                method.function.visit_with(self);
            }
            ClassMember::Constructor(ctor) => {
                // Keep inspecting the deferred body for nested accessors;
                // property-write promotion still needs their owner. Its
                // calls must stay out of the class owner's top-call list.
                let params = ctor
                    .params
                    .iter()
                    .map(|param| match param {
                        ParamOrTsParamProp::Param(param) => Parameter::from_pattern(&param.pat),
                        ParamOrTsParamProp::TsParamProp(_) => Parameter {
                            binding: None,
                            bindings: BTreeSet::new(),
                            default: None,
                            pattern_assignments: Vec::new(),
                            pattern_loses_values: false,
                            pattern_iterates: false,
                            pattern_iterates_part: false,
                            iterated_defaults: Vec::new(),
                        },
                    })
                    .collect();
                self.with_callable(ctor.span, params, |index| {
                    for parameter in &ctor.params {
                        if let ParamOrTsParamProp::Param(parameter) = parameter {
                            index.invalidate_property_key_pattern(&parameter.pat);
                        }
                    }
                    ctor.visit_children_with(index);
                });
            }
            ClassMember::ClassProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::PrivateProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
                if let Some(value) = &accessor.value {
                    self.with_callable(accessor.span, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            _ => {}
        }
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        self.callable_spans.insert(setter.span.lo.0, setter.span);
        // The Function child has its own canonical callable key. Keep this
        // owner-wide marker conservative: exact descriptor-body activation
        // and owner-local captured-write settlement are separate proofs.
        self.uncovered_callable_effect_owners.insert(self.owner);
        self.setter_owners
            .entry(static_property_name(&setter.key))
            .or_default()
            .insert(self.owner);
        let previous_scope = self.variable_scope.take();
        setter.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.callable_spans.insert(getter.span.lo.0, getter.span);
        // The Function child is indexed separately, while this owner marker
        // conservatively prevents unproved accessor-write settlement.
        self.uncovered_callable_effect_owners.insert(self.owner);
        let previous_scope = self.variable_scope.take();
        getter.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_static_block(&mut self, block: &StaticBlock) {
        let previous_scope = self.variable_scope.take();
        block.visit_children_with(self);
        self.variable_scope = previous_scope;
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        if let Some(prototype) = object.props.iter().find_map(|property| match property {
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair)
                    if matches!(pair.key, PropName::Ident(_) | PropName::Str(_))
                        && static_property_name(&pair.key).as_deref() == Some("__proto__")
                        && !matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Null(_))) =>
                {
                    Some(ValueExpr::from_expr(&pair.value))
                }
                _ => None,
            },
            _ => None,
        }) {
            self.literal_prototypes.insert(object.span.lo.0, prototype);
        }
        if matches!(object.props.as_slice(), [PropOrSpread::Spread(_)]) {
            self.exact_object_spread_copies.insert(object.span.lo.0);
        }
        use super::value_model::LiteralPropertyKind;
        let mut definition_kinds = Vec::new();
        let mut record_members = Some(Vec::new());
        let mut member_names = BTreeSet::new();
        for property in &object.props {
            let member = match property {
                PropOrSpread::Prop(property) => match property.as_ref() {
                    Prop::Method(method) if method.function.decorators.is_empty() => {
                        Some((&method.key, method.function.span.lo.0))
                    }
                    Prop::Getter(getter) => Some((&getter.key, getter.function.span.lo.0)),
                    _ => None,
                },
                _ => None,
            };
            if let Some(members) = record_members.as_mut() {
                if let Some((key, function_start)) = member
                    && matches!(
                        key,
                        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                    )
                    && let Some(name) = object_data_property_name(key)
                    && member_names.insert(name.clone())
                {
                    members.push(crate::facts::abstract_domain::foreign_call::RecordMember {
                        name,
                        function_start,
                    });
                } else {
                    record_members = None;
                }
            }

            let (kind, callable) = match property {
                PropOrSpread::Prop(property) => match property.as_ref() {
                    Prop::KeyValue(_) | Prop::Shorthand(_) => (LiteralPropertyKind::Data, None),
                    Prop::Method(method) => {
                        (LiteralPropertyKind::Method, Some(method.function.span.lo.0))
                    }
                    Prop::Getter(getter) => {
                        (LiteralPropertyKind::Getter, Some(getter.function.span.lo.0))
                    }
                    Prop::Setter(setter) => {
                        (LiteralPropertyKind::Setter, Some(setter.function.span.lo.0))
                    }
                    _ => (LiteralPropertyKind::Opaque, None),
                },
                _ => (LiteralPropertyKind::Opaque, None),
            };
            definition_kinds.push(kind);
            if let Some(callable) = callable {
                let home_object = object.span.lo.0;
                let creation = match kind {
                    LiteralPropertyKind::Method => DeferredCallableCreation::Method {
                        callable,
                        home_object,
                    },
                    LiteralPropertyKind::Getter => DeferredCallableCreation::Getter {
                        callable,
                        home_object,
                    },
                    LiteralPropertyKind::Setter => DeferredCallableCreation::Setter {
                        callable,
                        home_object,
                    },
                    _ => unreachable!("only deferred definitions have a callable"),
                };
                self.callable_creations.insert(callable, creation);
            }
        }
        self.literal_property_kinds
            .insert(object.span.lo.0, definition_kinds);
        if let Some(members) = record_members.filter(|members| !members.is_empty()) {
            self.method_record_members.insert(object.span.lo.0, members);
        }
        let mut descriptor_shape = BTreeMap::new();
        let mut descriptor_creation_order = Vec::new();
        let descriptorable = object.props.iter().all(|property| {
            let PropOrSpread::Prop(property) = property else {
                return false;
            };
            match property.as_ref() {
                Prop::KeyValue(pair) => {
                    let Some(name) = static_property_name(&pair.key) else {
                        return false;
                    };
                    if name == "__proto__" || descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Shorthand(ident) if ident.sym != "__proto__" => {
                    let name = ident.sym.to_string();
                    if descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Method(method)
                    if method.function.decorators.is_empty()
                        && !matches!(method.key, PropName::Computed(_)) =>
                {
                    let Some(name) = static_property_name(&method.key) else {
                        return false;
                    };
                    if name == "__proto__" || descriptor_shape.contains_key(&name) {
                        return false;
                    }
                    descriptor_creation_order.push(name.clone());
                    descriptor_shape.insert(name, OwnPropertyDescriptorSyntax::Data);
                    true
                }
                Prop::Getter(getter)
                    if getter.function.decorators.is_empty()
                        && !matches!(getter.key, PropName::Computed(_)) =>
                {
                    let Some(name) =
                        static_property_name(&getter.key).filter(|name| name != "__proto__")
                    else {
                        return false;
                    };
                    match descriptor_shape.get_mut(&name) {
                        Some(OwnPropertyDescriptorSyntax::Accessor {
                            getter: current_getter,
                            ..
                        }) if current_getter.is_none() => {
                            *current_getter = Some(getter.function.span.lo.0);
                            true
                        }
                        Some(_) => false,
                        None => {
                            descriptor_creation_order.push(name.clone());
                            descriptor_shape.insert(
                                name,
                                OwnPropertyDescriptorSyntax::Accessor {
                                    getter: Some(getter.function.span.lo.0),
                                    setter: None,
                                },
                            );
                            true
                        }
                    }
                }
                Prop::Setter(setter)
                    if setter.function.decorators.is_empty()
                        && !matches!(setter.key, PropName::Computed(_)) =>
                {
                    let Some(name) =
                        static_property_name(&setter.key).filter(|name| name != "__proto__")
                    else {
                        return false;
                    };
                    match descriptor_shape.get_mut(&name) {
                        Some(OwnPropertyDescriptorSyntax::Accessor {
                            setter: current_setter,
                            ..
                        }) if current_setter.is_none() => {
                            *current_setter = Some(setter.function.span.lo.0);
                            true
                        }
                        Some(_) => false,
                        None => {
                            descriptor_creation_order.push(name.clone());
                            descriptor_shape.insert(
                                name,
                                OwnPropertyDescriptorSyntax::Accessor {
                                    getter: None,
                                    setter: Some(setter.function.span.lo.0),
                                },
                            );
                            true
                        }
                    }
                }
                _ => false,
            }
        });
        if descriptorable {
            self.own_descriptor_shapes.insert(
                object.span.lo.0,
                OwnPropertyShape {
                    properties: descriptor_shape,
                    creation_order: descriptor_creation_order,
                },
            );
        }
        let mut null_prototype = 0;
        let static_data = object.props.iter().all(|property| match property {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) => match static_property_name(&pair.key).as_deref() {
                    Some("__proto__")
                        if matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Null(_))) =>
                    {
                        null_prototype += 1;
                        true
                    }
                    Some("__proto__") | None => false,
                    Some(_) => true,
                },
                Prop::Shorthand(id) => id.sym.as_ref() != "__proto__",
                // Ordinary method syntax installs an own data property. Its
                // body is only stored here, so parameter property reads by a
                // local constructor cannot invoke it. Keep computed keys and
                // the special prototype name outside this narrow proof.
                Prop::Method(method) => {
                    !matches!(method.key, PropName::Computed(_))
                        && static_property_name(&method.key).is_some_and(|name| name != "__proto__")
                }
                _ => false,
            },
        });
        self.object_literal_owners
            .insert(object.span.lo.0, self.owner);
        if static_data {
            if null_prototype == 0 {
                self.plain_data_objects.insert(object.span.lo.0);
            } else if null_prototype == 1 {
                self.null_proto_data_objects.insert(object.span.lo.0);
            }
        }
        let mut primitive_property_counts = BTreeMap::<String, usize>::new();
        let mut primitive_property_values = BTreeSet::<String>::new();
        for property in &object.props {
            let PropOrSpread::Prop(property) = property else {
                continue;
            };
            let (name, primitive_value) = match property.as_ref() {
                Prop::KeyValue(pair) => (
                    static_property_name(&pair.key),
                    primitive_literal(&pair.value),
                ),
                Prop::Shorthand(ident) => (Some(ident.sym.to_string()), false),
                Prop::Assign(assign) => (Some(assign.key.sym.to_string()), false),
                Prop::Method(method) => (static_property_name(&method.key), false),
                Prop::Getter(getter) => (static_property_name(&getter.key), false),
                Prop::Setter(setter) => (static_property_name(&setter.key), false),
            };
            if let Some(name) = name {
                *primitive_property_counts.entry(name.clone()).or_default() += 1;
                if primitive_value {
                    primitive_property_values.insert(name);
                }
            }
        }
        if static_data && null_prototype == 0 {
            for (name, count) in primitive_property_counts {
                if count == 1 && primitive_property_values.contains(&name) {
                    self.primitive_data_properties
                        .insert((object.span.lo.0, name));
                }
            }
        }
        self.literal_depth += 1;
        for property in &object.props {
            match property {
                PropOrSpread::Spread(spread) => {
                    spread.expr.visit_with(self);
                    self.record_call(Call {
                        kind: CallKind::ObjectSpread,
                        callee: ValueExpr::Unknown,
                        args: vec![ValueExpr::from_expr(&spread.expr)],
                        span: spread.expr.span(),
                        caller: self.current,
                        owner: self.owner,
                        plain_object_sources: false,
                        no_sync_arguments: false,
                        no_reentry_candidate: false,
                        no_reentry_record_candidate: false,
                        has_spread: false,
                    });
                }
                PropOrSpread::Prop(property)
                    if let Prop::KeyValue(pair) = property.as_ref()
                        && self.is_factory_member_data_value(&pair.key, &pair.value) =>
                {
                    pair.key.visit_with(self);
                    // This one-hop member read is a deferred data value, not
                    // the namespace object itself. The factory contract's
                    // author must establish that it cannot alias the namespace
                    // or mutate a listed factory member; syntax alone cannot
                    // prove the imported member's runtime identity.
                    self.factory_member_value_depth += 1;
                    pair.value.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                _ => property.visit_with(self),
            }
        }
        self.literal_depth -= 1;
    }

    fn visit_array_lit(&mut self, array: &ArrayLit) {
        if array.elems.iter().all(|element| {
            element
                .as_ref()
                .is_some_and(|element| element.spread.is_none())
        }) {
            self.dense_array_literals
                .insert(array.span.lo.0, array.elems.len());
        }
        self.array_elements.insert(
            array.span.lo.0,
            array
                .elems
                .iter()
                .map(|element| {
                    element.as_ref().map(|element| ArrayElement {
                        spread: element.spread,
                        value: ValueExpr::from_expr(&element.expr),
                    })
                })
                .collect(),
        );
        self.literal_depth += 1;
        for element in array.elems.iter().flatten() {
            element.expr.visit_with(self);
            if let Some(span) = element.spread {
                self.record_call(Call {
                    kind: CallKind::ArraySpread,
                    callee: ValueExpr::Unknown,
                    args: vec![ValueExpr::from_expr(&element.expr)],
                    span,
                    caller: self.current,
                    owner: self.owner,
                    plain_object_sources: false,
                    no_sync_arguments: false,
                    no_reentry_candidate: false,
                    no_reentry_record_candidate: false,
                    has_spread: false,
                });
            }
        }
        self.literal_depth -= 1;
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        self.property_key_declarations
            .insert(declaration.ident.to_id());
        self.property_key_assignments
            .entry(declaration.ident.to_id())
            .or_default()
            .push(PropertyKeyExpr::Unknown);
        if let Some(key) = self.current {
            // Declarations are instantiated when the enclosing activation
            // begins, even if their source text follows an await.
            self.hoisted_nested_functions
                .insert(declaration.function.span.lo.0);
            let callable = self.callables.get_mut(&key).unwrap();
            callable
                .local_functions
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Function, declaration.function.span.lo.0),
            ));
        } else {
            self.hoisted_bindings
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
            if let Some(Expr::Call(call)) = declaration
                .function
                .body
                .as_ref()
                .and_then(sole_return_expression)
                .map(Expr::unwrap_parens)
            {
                self.direct_return_calls
                    .insert(declaration.ident.to_id(), call.span);
            }
        }
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        self.property_key_declarations
            .insert(declaration.ident.to_id());
        self.property_key_assignments
            .entry(declaration.ident.to_id())
            .or_default()
            .push(PropertyKeyExpr::Unknown);
        declaration.class.visit_with(self);
        if self.current.is_none() {
            self.top_definite_initializers[self.owner.0].insert(declaration.ident.to_id());
            self.top_assignments[self.owner.0].push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Class, declaration.class.span.lo.0),
            ));
        } else if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable
                .lexical_declarations
                .insert(declaration.ident.to_id(), LocalBinding::Let);
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Alloc(Kind::Class, declaration.class.span.lo.0),
            ));
        }
    }

    fn visit_if_stmt(&mut self, statement: &IfStmt) {
        if self.current.is_none()
            && self.original_statement_spans.get(self.owner.0) == Some(&statement.span)
            && let Some((symbol, property, target)) = symbol_guarded_fill(statement)
        {
            self.symbol_guarded_fill_sites
                .insert(target.into(), (symbol, property));
        }
        statement.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        statement.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        if let Some(else_branch) = &statement.alt {
            self.conditional_depth += 1;
            else_branch.visit_with(self);
            self.conditional_depth -= 1;
            self.past_await &= after_then;
        } else {
            self.past_await = after_test;
        }
    }

    fn visit_cond_expr(&mut self, expression: &CondExpr) {
        self.record_guard(&expression.test, expression.cons.span(), true);
        self.record_guard(&expression.test, expression.alt.span(), false);
        expression.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        expression.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        self.conditional_depth += 1;
        expression.alt.visit_with(self);
        self.conditional_depth -= 1;
        self.past_await &= after_then;
    }

    fn visit_bin_expr(&mut self, expression: &BinExpr) {
        expression.left.visit_with(self);
        let after_left = self.past_await;
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            self.conditional_depth += 1;
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = if expression.op == BinaryOp::InstanceOf {
            match expression.right.unwrap_parens() {
                Expr::Ident(id) => Some(id.to_id()),
                _ => None,
            }
        } else {
            None
        };
        expression.right.visit_with(self);
        self.safe_class_target = previous_target;
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            self.conditional_depth -= 1;
            self.past_await = after_left;
        }
        match expression.op {
            // Strict comparisons, short-circuit operators and `instanceof`
            // do not apply ToPrimitive to their operands. `in` converts only
            // the left operand to a property key. The remaining eager binary
            // operators can convert either object operand.
            BinaryOp::EqEqEq
            | BinaryOp::NotEqEq
            | BinaryOp::InstanceOf
            | BinaryOp::LogicalAnd
            | BinaryOp::LogicalOr
            | BinaryOp::NullishCoalescing => {}
            BinaryOp::In => self.record_implicit_coercion(&expression.left, expression.span),
            _ => {
                self.record_implicit_coercion(&expression.left, expression.span);
                self.record_implicit_coercion(&expression.right, expression.span);
            }
        }
        if self.global_guards_requested
            && expression.op == BinaryOp::In
            && let (Expr::Lit(Lit::Str(property)), Expr::Ident(receiver)) = (
                expression.left.unwrap_parens(),
                expression.right.unwrap_parens(),
            )
            && self.global_object_names.contains(receiver.sym.as_ref())
            && let Some(root) = builtin_global_root(receiver)
            && let Some(property) = property.value.as_str()
        {
            self.global_membership_candidates.insert(
                expression.span.into(),
                GlobalMembershipProbe {
                    root,
                    receiver: receiver.to_id(),
                    property: property.to_owned(),
                    owner: self.owner,
                },
            );
        }
        if matches!(expression.op, BinaryOp::In | BinaryOp::InstanceOf) {
            self.record_call(Call {
                kind: if expression.op == BinaryOp::In {
                    CallKind::InProtocol
                } else {
                    CallKind::InstanceOfProtocol
                },
                callee: ValueExpr::Unknown,
                args: vec![
                    ValueExpr::from_expr(&expression.right),
                    ValueExpr::from_expr(&expression.left),
                ],
                span: expression.span,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
                no_sync_arguments: false,
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                has_spread: false,
            });
        }
    }

    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_labeled_stmt(&mut self, node: &LabeledStmt) {
        node.visit_children_with(self);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        // An array pattern in the head iterates each key, a string.
        let head_iterates = match &node.left {
            ForHead::VarDecl(declaration) => declaration
                .decls
                .iter()
                .any(|declarator| pattern_iterates(&declarator.name)),
            ForHead::Pat(pattern) => pattern_iterates(pattern),
            ForHead::UsingDecl(_) => false,
        };
        if head_iterates {
            self.record_iterated_value(ValueExpr::NonCallable(PrimKinds::STRING));
        }
        self.record_head_iterated_defaults(&node.left);
        self.visit_may_skip(node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.record_published_source(&node.right);
        self.record_iterated_source(&node.right);
        // An array pattern in the head iterates each element, which no
        // source tracks; `Object.entries` yields fresh native pairs.
        let head_iterates = match &node.left {
            ForHead::VarDecl(declaration) => declaration
                .decls
                .iter()
                .any(|declarator| pattern_iterates(&declarator.name)),
            ForHead::Pat(pattern) => pattern_iterates(pattern),
            ForHead::UsingDecl(_) => false,
        };
        let entries = matches!(node.right.unwrap_parens(), Expr::Call(call)
            if matches!(&call.callee, Callee::Expr(callee)
                if matches!(callee.unwrap_parens(), Expr::Member(member)
                    if static_prop(&member.prop).as_deref() == Some("entries")
                        && matches!(member.obj.unwrap_parens(), Expr::Ident(object) if object.sym == "Object"))));
        if head_iterates && !entries {
            self.record_iterated_value(ValueExpr::Unknown);
        }
        self.record_head_iterated_defaults(&node.left);
        if !node.is_await
            && let ForHead::VarDecl(declaration) = &node.left
            && matches!(declaration.kind, VarDeclKind::Let | VarDeclKind::Const)
            && declaration.decls.len() == 1
            && declaration.decls[0].init.is_none()
            && let Pat::Array(pattern) = &declaration.decls[0].name
            && pattern.elems.len() == 2
            && let Some(Pat::Ident(binding)) = &pattern.elems[0]
            && matches!(&pattern.elems[1], Some(Pat::Ident(_)))
            && let Expr::Call(call) = node.right.unwrap_parens()
            && call.args.len() == 1
            && call.args[0].spread.is_none()
            && let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && static_prop(&member.prop).as_deref() == Some("entries")
            && let Expr::Ident(namespace) = member.obj.unwrap_parens()
            && namespace.sym == "Object"
            && let Expr::Object(object) = call.args[0].expr.unwrap_parens()
        {
            let keys: Option<BTreeSet<String>> = object
                .props
                .iter()
                .map(|property| match property {
                    PropOrSpread::Prop(property) => match property.as_ref() {
                        Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                            static_property_name(&pair.key).filter(|key| key != "__proto__")
                        }
                        Prop::Shorthand(ident) => Some(ident.sym.to_string()),
                        _ => None,
                    },
                    _ => None,
                })
                .collect();
            if let Some(keys) = keys {
                self.property_key_assignments
                    .entry(binding.id.to_id())
                    .or_default()
                    .push(PropertyKeyExpr::EntriesKeys(namespace.to_id(), keys));
            }
        }
        self.visit_may_skip(node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        self.visit_may_skip(node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        self.visit_may_skip(node);
    }

    fn visit_fn_expr(&mut self, expression: &FnExpr) {
        if let Some(binding) = &expression.ident {
            self.property_key_declarations.insert(binding.to_id());
            self.property_key_assignments
                .entry(binding.to_id())
                .or_default()
                .push(PropertyKeyExpr::Unknown);
        }
        expression.visit_children_with(self);
        if let Some(binding) = &expression.ident {
            let callable = self
                .callables
                .get_mut(&expression.function.span.lo.0)
                .unwrap();
            callable.self_binding = Some(binding.to_id());
            if let Some(body) = callable.body_facts.take_ordered_plan() {
                callable
                    .body_facts
                    .set_ordered_plan(Some(body.with_declarations(
                        std::iter::empty(),
                        [(
                            binding.to_id(),
                            LocalBinding::NamedSelf(expression.function.span.lo.0),
                        )],
                    )));
            }
        }
    }

    fn visit_class_expr(&mut self, expression: &ClassExpr) {
        if let Some(binding) = &expression.ident {
            self.property_key_declarations.insert(binding.to_id());
        }
        expression.visit_children_with(self);
    }

    fn visit_catch_clause(&mut self, clause: &CatchClause) {
        if let Some(parameter) = &clause.param {
            self.invalidate_property_key_pattern(parameter);
            // The thrown value is untracked; an array pattern iterates it.
            if pattern_iterates(parameter) {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            self.record_iterated_defaults(parameter);
            if let Some(callable) = self.current {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .pattern_locals
                    .extend(Parameter::from_pattern(parameter).bindings);
            }
        }
        clause.visit_children_with(self);
    }

    fn visit_function(&mut self, function: &Function) {
        if !function.is_async
            && !function.is_generator
            && function.decorators.is_empty()
            && function.type_params.is_none()
            && function.return_type.is_none()
            && function
                .params
                .iter()
                .all(|param| param.decorators.is_empty() && matches!(param.pat, Pat::Ident(_)))
            && function
                .body
                .as_ref()
                .is_some_and(|body| match body.stmts.as_slice() {
                    [] => true,
                    [Stmt::Return(ret)] => ret.arg.as_deref().is_none_or(primitive_literal),
                    _ => false,
                })
        {
            self.inert_argument_functions.insert(function.span.lo.0);
        }
        self.callable_creations
            .entry(function.span.lo.0)
            .or_insert(DeferredCallableCreation::Function(function.span.lo.0));
        if let Some(candidate) = namespace_wrapper::candidate(function) {
            self.namespace_wrapper_candidates
                .insert(function.span.lo.0, candidate);
        }
        if function.is_async {
            self.async_callables.insert(function.span.lo.0);
        }
        // Decorator expressions run when the method is defined. Defaults in
        // parameter patterns and the body run only when it is called.
        function.decorators.visit_with(self);
        for param in &function.params {
            param.decorators.visit_with(self);
        }
        let params = function
            .params
            .iter()
            .map(|param| Parameter::from_pattern(&param.pat))
            .collect();
        self.with_callable(function.span, params, |index| {
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .body_facts
                .set_ordered_plan(
                    (!function.is_async
                        && !function.is_generator
                        && function.decorators.is_empty()
                        && function.type_params.is_none()
                        && function.return_type.is_none()
                        && function
                            .params
                            .iter()
                            .all(|param| param.decorators.is_empty()))
                    .then(|| {
                        LocalBody::lower(
                            function.params.iter().map(|parameter| &parameter.pat),
                            function.body.as_ref()?,
                        )
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .body_facts
                .set_direct_return_candidate(
                    (!function.is_async
                        && !function.is_generator
                        && function.decorators.is_empty()
                        && function.params.iter().all(|param| {
                            param.decorators.is_empty() && matches!(param.pat, Pat::Ident(_))
                        }))
                    .then(|| {
                        function
                            .body
                            .as_ref()
                            .and_then(sole_return_expression)
                            .map(|expression| expression.span().into())
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .may_fall_through = !function.body.as_ref().is_some_and(|body| {
                matches!(body.stmts.last(), Some(Stmt::Return(_) | Stmt::Throw(_)))
            });
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .generator = function.is_generator;
            for param in &function.params {
                index.invalidate_property_key_pattern(&param.pat);
                param.pat.visit_with(index);
            }
            if let Some(body) = &function.body {
                body.visit_with(index);
            }
        });
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.callable_creations.insert(
            arrow.span.lo.0,
            DeferredCallableCreation::Arrow(arrow.span.lo.0),
        );
        if arrow.is_async {
            self.async_callables.insert(arrow.span.lo.0);
        }
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span, params, |index| {
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .body_facts
                .set_ordered_plan(
                    (!arrow.is_async
                        && !arrow.is_generator
                        && arrow.type_params.is_none()
                        && arrow.return_type.is_none())
                    .then(|| match arrow.body.as_ref() {
                        ArrowFunctionBody::FunctionBody(body) => {
                            LocalBody::lower(arrow.params.iter(), body)
                        }
                        ArrowFunctionBody::Expr(expression) => {
                            LocalBody::lower_expression(arrow.params.iter(), expression)
                        }
                    })
                    .flatten(),
                );
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .body_facts
                .set_direct_return_candidate(
                    (!arrow.is_async
                        && !arrow.is_generator
                        && arrow
                            .params
                            .iter()
                            .all(|param| matches!(param, Pat::Ident(_))))
                    .then(|| match arrow.body.as_ref() {
                        ArrowFunctionBody::Expr(expression) => Some(expression.span().into()),
                        ArrowFunctionBody::FunctionBody(body) => {
                            sole_return_expression(body).map(|expression| expression.span().into())
                        }
                    })
                    .flatten(),
                );
            for parameter in &arrow.params {
                index.invalidate_property_key_pattern(parameter);
            }
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .may_fall_through = match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(_) => false,
                ArrowFunctionBody::FunctionBody(body) => {
                    !matches!(body.stmts.last(), Some(Stmt::Return(_) | Stmt::Throw(_)))
                }
            };
            arrow.visit_children_with(index);
            if let ArrowFunctionBody::Expr(expr) = arrow.body.as_ref() {
                let callable = index.callables.get_mut(&arrow.span.lo.0).unwrap();
                callable.body_facts.record_return(expr);
            }
        });
    }

    fn visit_ident(&mut self, ident: &Ident) {
        let position = ident.span.lo.0;
        self.binding_use_bounds
            .entry(ident.to_id())
            .and_modify(|(first, last)| {
                *first = (*first).min(position);
                *last = (*last).max(position);
            })
            .or_insert((position, position));
        self.ident_references.push((position, ident.to_id()));
        // A published outer closure can expose a nested closure's capabilities.
        for key in &self.prototype_capture_stack {
            self.prototype_callable_reads
                .entry(*key)
                .or_default()
                .insert(ident.to_id());
        }
        self.namespace_privacy.record_ident(ident);
        let id = ident.to_id();
        if self.class_ids.contains(&id) && self.safe_class_target.as_ref() != Some(&id) {
            self.other_class_uses.insert(id);
        }
        if self.current.is_some() {
            self.captured_bindings.insert(ident.to_id());
        }
        if !self.past_await
            && let Some(key) = self.current
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.reads.insert(ident.to_id());
            if self.global_guards_requested
                && self.discarded_sync_iifes[self.owner.0] == Some(key)
                && self.allowed_local_member_receiver != Some(ident.span.lo.0)
            {
                callable.bare_reads.insert(ident.to_id());
            }
            if self.literal_depth > 0 {
                callable.literal_captures.insert(ident.to_id());
                if self.factory_member_value_depth == 0 {
                    callable.literal_exposures.insert(ident.to_id());
                }
            }
        }
    }

    fn visit_this_expr(&mut self, _: &ThisExpr) {
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().implicit_capture = true;
        }
    }

    fn visit_meta_prop_expr(&mut self, _: &MetaPropExpr) {
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().implicit_capture = true;
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.member_owners.insert(member.span.into(), self.owner);
        if self.global_guards_requested
            && is_global_receiver(&member.obj, &self.global_object_names)
        {
            if let Some(property) = static_prop(&member.prop) {
                *self.global_slot_mentions.entry(property).or_default() += 1;
            } else {
                self.dynamic_global_member = true;
            }
        }
        if !self.member_write_sites.contains(&member.span.into())
            && let Some(key) = self.current
        {
            self.callable_member_reads
                .entry(key)
                .or_default()
                .push((ValueExpr::from_expr(&member.obj), static_prop(&member.prop)));
        }
        let previous = self.namespace_privacy.allowed_read;
        self.namespace_privacy.allowed_read = self.namespace_privacy.read_receiver(member);
        let previous_local_receiver = self.allowed_local_member_receiver;
        if self.global_guards_requested
            && self
                .current
                .is_some_and(|key| self.discarded_sync_iifes[self.owner.0] == Some(key))
        {
            self.allowed_local_member_receiver = match (member.obj.unwrap_parens(), &member.prop) {
                (Expr::Ident(id), MemberProp::Ident(_)) => Some(id.span.lo.0),
                _ => None,
            };
        }
        member.obj.visit_with(self);
        self.allowed_local_member_receiver = previous_local_receiver;
        self.namespace_privacy.allowed_read = previous;
        member.prop.visit_with(self);
        if !self.member_write_sites.contains(&member.span.into())
            && !self.direct_call_members.contains(&member.span.into())
        {
            self.record_call(Call::member_read(
                self.owner,
                self.current,
                ValueExpr::Member(
                    Arc::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    MemberKey::of(&member.prop),
                    member.span.into(),
                ),
                member.span,
            ));
        }
    }

    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        if !self.assignment_target {
            self.property_key_declarations.insert(binding.id.to_id());
        } else {
            self.record_nested_write(binding.id.to_id());
        }
        self.namespace_privacy.record_ident(&binding.id);
    }

    fn visit_assign_target(&mut self, target: &AssignTarget) {
        let saved = std::mem::replace(&mut self.assignment_target, true);
        target.visit_children_with(self);
        self.assignment_target = saved;
    }

    fn visit_var_decl(&mut self, declaration: &VarDecl) {
        let previous = self.variable_kind.replace(declaration.kind);
        declaration.visit_children_with(self);
        self.variable_kind = previous;
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if self.direct_top_level_var_declaration
            && self.current.is_none()
            && self.conditional_depth == 0
            && !self.past_await
            && matches!(declarator.name, Pat::Ident(_))
            && let Some(Expr::Call(call)) = declarator.init.as_deref().map(Expr::unwrap_parens)
            && call.args.len() == 1
            && call.args.iter().all(|arg| arg.spread.is_none())
            && let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(imported) = callee.unwrap_parens()
            && let Expr::Object(object) = call.args[0].expr.unwrap_parens()
            && let Some(summary) = self.descriptor_copy_factories.get(&imported.to_id())
            && let Some(signature) = summary
                .signatures
                .iter()
                .find(|signature| signature.argument_count == 1 && signature.object_argument == 0)
        {
            self.descriptor_copy_calls.insert(
                call.span.into(),
                DescriptorCopyCallCandidate {
                    source_object: object.span.lo.0,
                    signature: signature.clone(),
                    provider_binding: imported.to_id(),
                },
            );
        }
        if self.variable_kind == Some(VarDeclKind::Var)
            && let Some(callable) = self.variable_scope
            && let Pat::Ident(binding) = &declarator.name
        {
            self.callables
                .get_mut(&callable)
                .unwrap()
                .hoisted_vars
                .insert(binding.id.to_id());
        }
        if let Some(callable) = self.current
            && let Pat::Ident(binding) = &declarator.name
            && let Some(kind @ (VarDeclKind::Let | VarDeclKind::Const)) = self.variable_kind
        {
            self.callables
                .get_mut(&callable)
                .unwrap()
                .lexical_declarations
                .insert(
                    binding.id.to_id(),
                    if kind == VarDeclKind::Let {
                        LocalBinding::Let
                    } else {
                        LocalBinding::Const
                    },
                );
        }
        let initializer = declarator
            .init
            .as_ref()
            .map(|value| ValueExpr::from_expr(value));
        let direct_top_level = self.direct_top_level_var_declaration && self.current.is_none();
        if let Pat::Ident(binding) = &declarator.name {
            if self.direct_top_level_const_declaration && direct_top_level {
                self.top_const_declarations.insert(binding.id.to_id());
            }
            if self.direct_top_level_const_declaration
                && direct_top_level
                && declarator.init.as_deref().is_some_and(primitive_literal)
            {
                self.direct_primitive_const_bindings
                    .insert(binding.id.to_id());
            }
            self.binding_declaration_writes
                .entry(binding.id.to_id())
                .or_insert_with(|| Vec::with_capacity(1))
                .push(BindingDeclarationWrite {
                    owner: self.owner,
                    private_var_scope: self
                        .variable_scope
                        .filter(|_| self.variable_kind == Some(VarDeclKind::Var)),
                    binding_span: binding.id.span,
                    initializer_span: declarator.init.as_ref().map(|value| value.span()),
                    initializer: initializer.clone(),
                    direct_top_level,
                    simple_identifier: true,
                    primitive_literal_initializer: declarator
                        .init
                        .as_deref()
                        .is_some_and(primitive_literal),
                    set_from_primitive_array_aliases: declarator
                        .init
                        .as_deref()
                        .and_then(set_from_primitive_array_aliases),
                    set_from_set_spread_source: declarator
                        .init
                        .as_deref()
                        .and_then(set_from_set_spread_source),
                    empty_map_object_sites: declarator
                        .init
                        .as_deref()
                        .and_then(empty_map_object_sites),
                    symbol_class_key_sites: declarator
                        .init
                        .as_deref()
                        .and_then(symbol_class_key_sites),
                });
            self.property_key_assignments
                .entry(binding.id.to_id())
                .or_default()
                .push(
                    declarator
                        .init
                        .as_ref()
                        .map_or(PropertyKeyExpr::Unknown, |value| {
                            PropertyKeyExpr::from_expr(value)
                        }),
                );
        } else {
            if let Some(init) = &declarator.init
                && pattern_loses_values(&declarator.name)
            {
                self.record_published_source(init);
            }
            if let Some(init) = &declarator.init
                && pattern_iterates(&declarator.name)
            {
                self.record_iterated_source(init);
            }
            if pattern_iterates_part(&declarator.name) {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            self.record_iterated_defaults(&declarator.name);
            let mut bindings = Vec::new();
            let pattern_value = initializer.clone().unwrap_or(ValueExpr::Unknown);
            collect_pattern_assignments(&declarator.name, &pattern_value, &mut bindings);
            if let Some(callable) = self.current {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .pattern_locals
                    .extend(bindings.iter().map(|(binding, _)| binding.clone()));
            }
            if self.variable_kind == Some(VarDeclKind::Var)
                && let Some(callable) = self.variable_scope
            {
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .hoisted_vars
                    .extend(bindings.iter().map(|(binding, _)| binding.clone()));
            } else if let Some(callable) = self.current
                && let Some(kind @ (VarDeclKind::Let | VarDeclKind::Const)) = self.variable_kind
            {
                let kind = if kind == VarDeclKind::Let {
                    LocalBinding::Let
                } else {
                    LocalBinding::Const
                };
                self.callables
                    .get_mut(&callable)
                    .unwrap()
                    .lexical_declarations
                    .extend(bindings.iter().map(|(binding, _)| (binding.clone(), kind)));
            }
            for (id, _) in bindings {
                self.binding_declaration_writes
                    .entry(id.clone())
                    .or_insert_with(|| Vec::with_capacity(1))
                    .push(BindingDeclarationWrite {
                        owner: self.owner,
                        private_var_scope: None,
                        binding_span: declarator.name.span(),
                        initializer_span: declarator.init.as_ref().map(|value| value.span()),
                        initializer: initializer.clone(),
                        direct_top_level,
                        simple_identifier: false,
                        primitive_literal_initializer: false,
                        set_from_primitive_array_aliases: None,
                        set_from_set_spread_source: None,
                        empty_map_object_sites: None,
                        symbol_class_key_sites: None,
                    });
                // A for-of declaration has no initializer: its key fact was
                // supplied by the loop protocol, rather than an unknown RHS.
                if declarator.init.is_some() {
                    self.property_key_assignments
                        .entry(id)
                        .or_default()
                        .push(PropertyKeyExpr::Unknown);
                }
            }
        }
        if let Some(key) = self.current
            && let Pat::Ident(binding) = &declarator.name
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .declared_locals
                .insert(binding.id.to_id());
        }
        // A loop head's binding takes the loop's values, which no entry in
        // `local_assignments` records; leave it to the unknown fallback.
        if let Some(key) = self.current
            && !self.for_head_declaration
            && (declarator.init.is_none() || self.variable_kind == Some(VarDeclKind::Var))
        {
            let mut bindings = Vec::new();
            collect_pattern_assignments(&declarator.name, &ValueExpr::Undefined, &mut bindings);
            self.callables
                .get_mut(&key)
                .unwrap()
                .undefined_before_assignment
                .extend(bindings.into_iter().map(|(binding, _)| binding));
        }
        declarator.visit_children_with(self);
        if let Some(init) = &declarator.init {
            let kind = match &declarator.name {
                Pat::Object(_) | Pat::Array(_) if !shallow_data_pattern(&declarator.name) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                Pat::Object(_) => Some(CallKind::ObjectSpread),
                Pat::Array(_) => Some(CallKind::ArraySpread),
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(kind, ValueExpr::from_expr(init), declarator.span);
            }
            let mut assignments = Vec::new();
            collect_pattern_assignments(
                &declarator.name,
                &ValueExpr::from_expr(init),
                &mut assignments,
            );
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .local_assignments
                    .extend(assignments);
            } else {
                self.top_assignments[self.owner.0].extend(assignments);
            }
        }
    }

    fn visit_seq_expr(&mut self, sequence: &SeqExpr) {
        let mut fresh: Option<(Id, Id)> = None;
        for expression in &sequence.exprs {
            let mut continues = false;
            if let Expr::Assign(assignment) = expression.unwrap_parens()
                && assignment.op == AssignOp::Assign
            {
                if let Some(binding_targets::SimpleWriteTarget::Binding(receiver)) =
                    binding_targets::simple_write_target(&assignment.left)
                    && let Expr::Call(call) = assignment.right.unwrap_parens()
                    && let Callee::Expr(callee) = &call.callee
                    && let Expr::Ident(callee) = callee.unwrap_parens()
                    && callee.sym == "Error"
                {
                    fresh = Some((receiver, callee.to_id()));
                    continues = true;
                } else if let Some((receiver, callee)) = &fresh
                    && let Some(binding_targets::SimpleWriteTarget::Member(member)) =
                        binding_targets::simple_write_target(&assignment.left)
                    && matches!(binding_targets::transparent_target_expr(&member.obj), Expr::Ident(id) if id.to_id() == *receiver)
                    && let Some(property) = static_prop(&member.prop)
                    && property != "__proto__"
                    && property != "stack"
                {
                    let local_read = match assignment.right.unwrap_parens() {
                        Expr::Ident(id) => Some(id.to_id()),
                        _ => None,
                    };
                    if local_read.is_some() || value_without_user_code(&assignment.right) {
                        if !self.past_await {
                            self.fresh_error_claims.push(FreshErrorWriteClaim {
                                site: FreshErrorWriteSite {
                                    owner: self.owner,
                                    callable: self.current,
                                    receiver: receiver.clone(),
                                    property,
                                },
                                callee: callee.clone(),
                                local_read,
                            });
                        }
                        continues = true;
                    }
                }
            }
            if !continues {
                fresh = None;
            }
            expression.visit_with(self);
        }
    }

    fn visit_pat(&mut self, pattern: &Pat) {
        if self.assignment_target && matches!(pattern, Pat::Expr(_) | Pat::Invalid(_)) {
            // The shared pattern target walker only certifies binding
            // patterns. Preserve raw owner effects for expression targets.
            self.uncovered_callable_effect_owners.insert(self.owner);
        }
        if let Some(source) = &self.destructuring_source {
            let receiver = match pattern {
                Pat::Expr(expression) => Some(match expression.unwrap_parens() {
                    Expr::Member(member) => {
                        self.member_write_sites.insert(member.span.into());
                        self.intrinsic_writes.push(intrinsic_write_target(
                            member,
                            self.owner,
                            None,
                            self.current,
                            NativeMutationOperation::PropertyAssignment,
                        ));
                        ValueExpr::from_expr(&member.obj)
                    }
                    _ => ValueExpr::Unknown,
                }),
                Pat::Invalid(_) => Some(ValueExpr::Unknown),
                _ => None,
            };
            if let Some(receiver) = receiver {
                match self.current {
                    Some(key) => self
                        .callables
                        .get_mut(&key)
                        .unwrap()
                        .destructuring_member_receivers
                        .push(receiver.clone()),
                    None => {
                        self.top_destructuring_member_receivers[self.owner.0].push(receiver.clone())
                    }
                }
                self.destructuring_member_writes
                    .push(DestructuringMemberWrite {
                        receiver,
                        source: source.clone(),
                    });
            }
        }
        pattern.visit_children_with(self);
    }

    fn visit_assign_pat(&mut self, assignment: &AssignPat) {
        let Some(source) = self.destructuring_source.take() else {
            assignment.visit_children_with(self);
            return;
        };
        self.destructuring_source = Some(ValueExpr::Alternatives(
            [source.clone(), ValueExpr::from_expr(&assignment.right)].into(),
        ));
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = Some(source);
        assignment.right.visit_with(self);
    }

    fn visit_for_head(&mut self, head: &ForHead) {
        let mut assignments = Vec::new();
        match head {
            ForHead::Pat(pattern) => {
                binding_targets::record_pat_write(pattern, self);
                collect_pattern_assignments(pattern, &ValueExpr::Unknown, &mut assignments);
                self.reassigned_bindings
                    .extend(assignments.iter().map(|(binding, _)| binding.clone()));
            }
            ForHead::VarDecl(declaration) => {
                for declarator in &declaration.decls {
                    binding_targets::record_pat_write(&declarator.name, self);
                    collect_pattern_assignments(
                        &declarator.name,
                        &ValueExpr::Unknown,
                        &mut assignments,
                    );
                }
            }
            _ => {}
        }
        if let Some(key) = self.current {
            self.callables
                .get_mut(&key)
                .unwrap()
                .head_bindings
                .extend(assignments.iter().map(|(binding, _)| binding.clone()));
        }
        if let ForHead::Pat(pattern) = head {
            self.invalidate_property_key_pattern(pattern);
        }
        let previous_source = self.destructuring_source.take();
        if matches!(head, ForHead::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::Unknown);
        }
        self.namespace_privacy.target_depth += 1;
        let saved = std::mem::replace(&mut self.assignment_target, matches!(head, ForHead::Pat(_)));
        let saved_declaration = std::mem::replace(
            &mut self.for_head_declaration,
            matches!(head, ForHead::VarDecl(_)),
        );
        head.visit_children_with(self);
        self.for_head_declaration = saved_declaration;
        self.assignment_target = saved;
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
    }

    fn visit_simple_assign_target(&mut self, target: &SimpleAssignTarget) {
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) =
            binding_targets::simple_assign_target(target)
        {
            self.member_write_sites.insert(member.span.into());
        }
        target.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        // Record before visiting operands: an await inside a target or RHS
        // can only make this conservative. Reuse the complete target walker
        // used by raw facts, including patterns and parenthesized targets.
        binding_targets::record_assign_target(&assignment.left, self);
        let simple_target = binding_targets::simple_write_target(&assignment.left);
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.member_write_sites.insert(member.span.into());
        }
        let mut assigned = Vec::new();
        match simple_target.as_ref() {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                assigned.push((binding.clone(), ValueExpr::Unknown));
            }
            Some(binding_targets::SimpleWriteTarget::Member(_)) => {}
            None => {
                if let AssignTarget::Pat(pattern) = &assignment.left {
                    collect_assign_target_assignments(pattern, &ValueExpr::Unknown, &mut assigned);
                }
            }
        }
        self.reassigned_bindings
            .extend(assigned.iter().map(|(binding, _)| binding.clone()));
        match simple_target.as_ref() {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => self
                .property_key_assignments
                .entry(binding.clone())
                .or_default()
                .push(if assignment.op == AssignOp::Assign {
                    PropertyKeyExpr::from_expr(&assignment.right)
                } else {
                    PropertyKeyExpr::Unknown
                }),
            None if matches!(&assignment.left, AssignTarget::Pat(_)) => {
                for (id, _) in &assigned {
                    self.property_key_assignments
                        .entry(id.clone())
                        .or_default()
                        .push(PropertyKeyExpr::Unknown);
                }
            }
            _ => {}
        }
        if self.global_guards_requested {
            self.global_alias_written |= match simple_target.as_ref() {
                Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                    self.global_object_names.contains(binding.0.as_ref())
                }
                Some(binding_targets::SimpleWriteTarget::Member(member)) => {
                    is_global_receiver(&member.obj, &self.global_object_names)
                        && static_prop(&member.prop).is_some_and(|property| {
                            matches!(
                                property.as_str(),
                                "globalThis" | "window" | "self" | "frames" | "top"
                            )
                        })
                }
                None => false,
            };
        }
        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if binding.0.as_ref() == "Map"
        ) {
            self.reassigned_map = true;
        }
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            let right = ValueExpr::from_expr(&assignment.right);
            let value = match assignment.op {
                AssignOp::Assign => right,
                // A logical assignment stores the right side or keeps the
                // property's current value.
                AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign => {
                    ValueExpr::Alternatives(
                        [
                            ValueExpr::Member(
                                Arc::new(ValueExpr::from_expr(
                                    binding_targets::transparent_target_expr(&member.obj),
                                )),
                                static_prop(&member.prop),
                                MemberKey::of(&member.prop),
                                member.span.into(),
                            ),
                            right,
                        ]
                        .into(),
                    )
                }
                _ => ValueExpr::Primitive([right].into()),
            };
            self.intrinsic_writes.push(IntrinsicWriteTarget {
                value: Some(value),
                ..intrinsic_write_target(
                    member,
                    self.owner,
                    self.current,
                    self.current,
                    NativeMutationOperation::PropertyAssignment,
                )
            });
        }
        let before_assignment = self.past_await;
        let previous_source = self.destructuring_source.take();
        if matches!(assignment.left, AssignTarget::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::from_expr(&assignment.right));
        }
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
        assignment.right.visit_with(self);
        if matches!(
            assignment.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
        if !matches!(
            assignment.op,
            AssignOp::Assign | AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            let left = match simple_target.as_ref() {
                Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                    ValueExpr::Binding(binding.clone())
                }
                Some(binding_targets::SimpleWriteTarget::Member(member)) => ValueExpr::Member(
                    Arc::new(ValueExpr::from_expr(
                        binding_targets::transparent_target_expr(&member.obj),
                    )),
                    static_prop(&member.prop),
                    MemberKey::of(&member.prop),
                    member.span.into(),
                ),
                None => ValueExpr::Unknown,
            };
            self.record_implicit_coercion_value(left, assignment.span);
            self.record_implicit_coercion(&assignment.right, assignment.span);
        }
        if let (Some(key), Some(binding_targets::SimpleWriteTarget::Binding(binding))) =
            (self.current, simple_target.as_ref())
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.rebinds.insert(binding.clone());
            callable
                .local_assignments
                .push((binding.clone(), ValueExpr::from_expr(&assignment.right)));
        } else if let Some(binding_targets::SimpleWriteTarget::Binding(binding)) =
            simple_target.as_ref()
        {
            self.top_assignments[self.owner.0]
                .push((binding.clone(), ValueExpr::from_expr(&assignment.right)));
            if self.conditional_depth == 0 {
                self.top_definite_initializers[self.owner.0].insert(binding.clone());
            }
        } else if let Some(binding_targets::SimpleWriteTarget::Member(member)) =
            simple_target.as_ref()
        {
            let write = CallbackResultWrite {
                receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(
                    &member.obj,
                )),
                property_key: match &member.prop {
                    MemberProp::Computed(key) => ValueExpr::from_expr(&key.expr),
                    _ => ValueExpr::NonCallable(PrimKinds::STRING),
                },
                simple_assignment: assignment.op == AssignOp::Assign,
                data_read_operands: data_read_operand(&assignment.right)
                    && match &member.prop {
                        MemberProp::Computed(key) => data_read_operand(&key.expr),
                        _ => true,
                    },
                property: static_prop(&member.prop),
                value: ValueExpr::from_expr(&assignment.right),
                site: CallSite::from(assignment.span),
            };
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .callback_result_writes
                    .push(write);
            } else {
                self.top_callback_result_writes[self.owner.0].push(write);
            }
            if let Expr::Ident(receiver) = binding_targets::transparent_target_expr(&member.obj)
                && let Some(key) = static_prop(&member.prop)
            {
                let write = MemberValueWrite {
                    receiver: receiver.to_id(),
                    property: Some(key),
                    value: ValueExpr::from_expr(&assignment.right),
                    site: assignment.span.into(),
                    owner: self.owner,
                };
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            } else if let Expr::Ident(receiver) =
                binding_targets::transparent_target_expr(&member.obj)
            {
                let write = MemberValueWrite {
                    receiver: receiver.to_id(),
                    property: None,
                    value: ValueExpr::from_expr(&assignment.right),
                    site: assignment.span.into(),
                    owner: self.owner,
                };
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            }
            self.mark_possible_namespace_escape(&assignment.right);
            if assignment.op == AssignOp::Assign {
                self.record_member_write(member);
            } else {
                self.mark_dynamic_property_effect(Some(&member.obj));
            }
        } else if let AssignTarget::Pat(pattern) = &assignment.left {
            let kind = match pattern {
                AssignTargetPat::Array(_) | AssignTargetPat::Object(_) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(
                    kind,
                    ValueExpr::from_expr(&assignment.right),
                    assignment.span,
                );
            }
            let loses_values = match pattern {
                AssignTargetPat::Array(_) | AssignTargetPat::Invalid(_) => true,
                AssignTargetPat::Object(object) => {
                    pattern_loses_values(&Pat::Object(object.clone()))
                }
            };
            if loses_values {
                self.record_published_source(&assignment.right);
            }
            let (iterates, iterates_part) = match pattern {
                AssignTargetPat::Array(array) => {
                    (true, pattern_iterates_part(&Pat::Array(array.clone())))
                }
                AssignTargetPat::Object(object) => {
                    let object = Pat::Object(object.clone());
                    (pattern_iterates(&object), pattern_iterates_part(&object))
                }
                AssignTargetPat::Invalid(_) => (false, false),
            };
            if iterates {
                self.record_iterated_source(&assignment.right);
            }
            if iterates_part {
                self.record_iterated_value(ValueExpr::Unknown);
            }
            match pattern {
                AssignTargetPat::Array(array) => {
                    self.record_iterated_defaults(&Pat::Array(array.clone()))
                }
                AssignTargetPat::Object(object) => {
                    self.record_iterated_defaults(&Pat::Object(object.clone()))
                }
                AssignTargetPat::Invalid(_) => {}
            }
            let mut assignments = Vec::new();
            collect_assign_target_assignments(
                pattern,
                &ValueExpr::from_expr(&assignment.right),
                &mut assignments,
            );
            if let Some(key) = self.current {
                let callable = self.callables.get_mut(&key).unwrap();
                for (binding, value) in assignments {
                    callable.rebinds.insert(binding.clone());
                    callable.local_assignments.push((binding, value));
                }
            } else {
                if self.conditional_depth == 0 {
                    self.top_definite_initializers[self.owner.0]
                        .extend(assignments.iter().map(|(binding, _)| binding.clone()));
                }
                self.top_assignments[self.owner.0].extend(assignments);
            }
            self.mark_dynamic_property_effect(None);
        } else {
            self.mark_dynamic_property_effect(None);
        }
    }

    fn visit_update_expr(&mut self, expression: &UpdateExpr) {
        binding_targets::record_update_target(&expression.arg, self);
        let simple_target = binding_targets::simple_write_target_expr(&expression.arg);
        if let Some(binding_targets::SimpleWriteTarget::Binding(binding)) = simple_target.as_ref() {
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .update_targets
                    .insert(binding.clone());
            }
            self.record_nested_write(binding.clone());
            self.reassigned_bindings.insert(binding.clone());
            self.property_key_assignments
                .entry(binding.clone())
                .or_default()
                .push(PropertyKeyExpr::Unknown);
        }
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.member_write_sites.insert(member.span.into());
        }

        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if matches!(
                    binding.0.as_ref(),
                    "Error" | "Function" | "Object" | "Array" | "Math" | "RegExp" | "Symbol"
                        | "Promise" | "Map" | "Set" | "WeakMap" | "WeakSet" | "FinalizationRegistry"
                )
        ) {
            self.intrinsic_writes.push(IntrinsicWriteTarget {
                owner: self.owner,
                activation_callable: self.current,
                constructor_this: None,
                span: expression.span,
                operation: NativeMutationOperation::Update,
                receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(
                    &expression.arg,
                )),
                key: None,
                additional_keys: Vec::new(),
                value: None,
            });
        }

        if matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(binding))
                if binding.0.as_ref() == "Map"
        ) {
            self.reassigned_map = true;
        }
        self.namespace_privacy.target_depth += 1;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.record_implicit_coercion(&expression.arg, expression.span);
        if let Some(binding_targets::SimpleWriteTarget::Member(member)) = simple_target.as_ref() {
            self.intrinsic_writes.push(intrinsic_write_target(
                member,
                self.owner,
                self.current,
                self.current,
                NativeMutationOperation::Update,
            ));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
        // Keep the source protocol for conservative flat-summary fallback.
        // A complete ordered body instead represents this binding read,
        // ToNumeric and store at execution, where values decide coverage.
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::from_expr(binding_targets::transparent_target_expr(&expression.arg)),
            expression.span,
        );
        let ordered_binding_update = matches!(
            simple_target.as_ref(),
            Some(binding_targets::SimpleWriteTarget::Binding(_))
        ) && self
            .current
            .is_some_and(|key| self.callables[&key].body_facts.ordered_plan().is_some());
        if !ordered_binding_update {
            self.mark_opaque();
        }
    }

    fn visit_unary_expr(&mut self, expression: &UnaryExpr) {
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.member_write_sites.insert(member.span.into());
        }

        let target = usize::from(expression.op == UnaryOp::Delete);
        self.namespace_privacy.target_depth += target;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= target;
        if matches!(
            expression.op,
            UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde
        ) {
            self.record_implicit_coercion(&expression.arg, expression.span);
        }
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.intrinsic_writes.push(intrinsic_write_target(
                member,
                self.owner,
                self.current,
                self.current,
                NativeMutationOperation::Delete,
            ));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        // A generator's return value reaches its consumer through `next()`
        // or `yield*`, as a yielded one does.
        if let (Some(key), Some(argument)) = (self.current, &ret.arg)
            && self.callables[&key].generator
        {
            self.record_published_source(argument);
        }
        ret.visit_children_with(self);
        if ret.arg.is_none()
            && let Some(key) = self.current
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .body_facts
                .record_undefined_return();
        }
        if let (Some(key), Some(arg)) = (self.current, &ret.arg) {
            let callable = self.callables.get_mut(&key).unwrap();
            let value = callable.body_facts.record_return(arg);
            if let ValueExpr::Binding(id) = &value {
                callable.escaped_bindings.insert(id.clone());
            }
        }
    }

    fn visit_yield_expr(&mut self, yielded: &YieldExpr) {
        if let Some(argument) = &yielded.arg {
            self.record_published_source(argument);
            if yielded.delegate {
                self.record_iterated_source(argument);
            }
        }
        yielded.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, element: &ExprOrSpread) {
        if element.spread.is_some() {
            self.record_iterated_source(&element.expr);
        }
        element.visit_children_with(self);
    }

    fn visit_throw_stmt(&mut self, thrown: &ThrowStmt) {
        self.record_published_source(&thrown.arg);
        thrown.visit_children_with(self);
        self.mark_possible_namespace_escape(&thrown.arg);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if self.current.is_none()
            && call.args.len() <= 1
            && call
                .args
                .iter()
                .all(|argument| argument.spread.is_none() && primitive_constant(&argument.expr))
            && matches!(&call.callee, Callee::Expr(callee)
                if matches!(callee.unwrap_parens(), Expr::Ident(identifier) if identifier.sym == "Symbol")
                    || matches!(callee.unwrap_parens(), Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(identifier) if identifier.sym == "Symbol")
                            && matches!(&member.prop, MemberProp::Ident(identifier) if identifier.sym == "for")))
        {
            self.primitive_native_symbol_calls
                .insert(CallSite::from(call.span));
        }
        if let Callee::Expr(callee) = &call.callee {
            if let Expr::Fn(function) = callee.unwrap_parens()
                && function.ident.is_none()
                && !function.function.is_async
                && !function.function.is_generator
            {
                self.immediate_anonymous_callers
                    .insert(function.function.span.lo.0, self.current);
            }
            if let Expr::Ident(import) = callee.unwrap_parens()
                && let [argument] = call.args.as_slice()
                && argument.spread.is_none()
                && let Expr::Fn(function) = argument.expr.unwrap_parens()
                && function.ident.is_none()
            {
                self.inline_callback_entries.insert(
                    function.function.span.lo.0,
                    callback_arguments::InlineCallbackEntry {
                        import: import.to_id(),
                        function: function.function.span,
                        call: call.span,
                    },
                );
            }
        }
        if self.current.is_none() {
            self.ordinary_top_call_sites.insert(call.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(factory) = callee.unwrap_parens()
            && self
                .retained_record_factories
                .contains(factory.sym.as_ref())
        {
            self.record_factory_calls
                .insert(call.span.into(), record_factory_argument(&call.args));
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            self.direct_call_members.insert(member.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && let Some(row) = iteration_row(property.sym.as_ref(), &call.args)
            && let ArgSel::Arg(callback) = row.invokes
        {
            self.array_iterations.insert(
                call.span.into(),
                ArrayIteration {
                    row,
                    source: ValueExpr::from_expr(&member.obj),
                    callback: ValueExpr::from_expr(&call.args[callback].expr),
                    initial: call
                        .args
                        .get(1)
                        .map(|initial| ValueExpr::from_expr(&initial.expr)),
                    result: None,
                },
            );
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object" || id.sym == "Reflect")
            && static_prop(&member.prop).as_deref().is_none_or(|method| {
                matches!(
                    method,
                    "set"
                        | "defineProperty"
                        | "defineProperties"
                        | "assign"
                        | "setPrototypeOf"
                        | "deleteProperty"
                )
            })
            && let Some(argument) = call.args.first()
        {
            let receiver = ValueExpr::from_expr(&argument.expr);
            let method = static_prop(&member.prop);
            let mut target = if matches!(
                method.as_deref(),
                Some("set" | "defineProperty" | "deleteProperty")
            ) {
                IntrinsicWriteTarget {
                    owner: self.owner,
                    activation_callable: self.current,
                    constructor_this: None,
                    span: call.span,
                    operation: NativeMutationOperation::MutationCall,
                    receiver,
                    key: Some(
                        call.args
                            .get(1)
                            .map_or(PropertyKeyExpr::Unknown, |argument| {
                                PropertyKeyExpr::from_expr(&argument.expr)
                            }),
                    ),
                    additional_keys: Vec::new(),
                    value: None,
                }
            } else {
                IntrinsicWriteTarget {
                    owner: self.owner,
                    activation_callable: self.current,
                    constructor_this: None,
                    span: call.span,
                    operation: NativeMutationOperation::MutationCall,
                    receiver,
                    key: None,
                    additional_keys: Vec::new(),
                    value: None,
                }
            };
            let literal_keys = (method.as_deref() == Some("assign"))
                .then(|| object_literal_assign_keys(call.args.get(1..).unwrap_or_default()))
                .flatten();
            if let Some((first, rest)) = literal_keys.as_ref().and_then(|keys| keys.split_first()) {
                target.key = Some(PropertyKeyExpr::String(first.clone()));
                target
                    .additional_keys
                    .extend(rest.iter().cloned().map(PropertyKeyExpr::String));
            } else if matches!(method.as_deref(), Some("assign" | "defineProperties")) {
                let sources = if method.as_deref() == Some("assign") {
                    call.args.get(1..).unwrap_or_default()
                } else {
                    call.args.get(1..2).unwrap_or_default()
                };
                for source in sources {
                    if source.spread.is_some() {
                        continue;
                    }
                    if let ValueExpr::ObjectLiteral(_, properties) =
                        ValueExpr::from_expr(&source.expr)
                    {
                        for (key, _) in properties.iter() {
                            if let Some(key) = key.as_ref().filter(|key| *key != "__proto__") {
                                target
                                    .additional_keys
                                    .push(PropertyKeyExpr::String(key.clone()));
                            }
                        }
                    }
                }
            }
            self.intrinsic_writes.push(target);
        }

        let callback_argument = self.inline_factory_callback_argument(call);
        if self.current.is_none()
            && let Some(Expr::Ident(callee)) = Self::inline_callback_callee(call)
        {
            self.direct_inline_callback_calls
                .insert(call.span.into(), callee.to_id());
        }
        let retained_call = self.retained_callback_call(call);
        let no_reentry_call = self.no_reentry_callback_call(call);
        let no_reentry_accessor_call = no_reentry_call
            .as_ref()
            .is_some_and(|contract| contract.require_single_callback_identity)
            .then(|| CallSite::from(call.span));
        let callback_arguments: BTreeSet<_> = callback_argument
            .into_iter()
            .chain(
                retained_call
                    .iter()
                    .chain(no_reentry_call.iter())
                    .flat_map(|call| call.signatures.iter())
                    .flat_map(|signature| signature.callback_arguments.iter().copied()),
            )
            .collect();
        if let Some(retained_call) = retained_call {
            self.retained_callback_calls.insert(
                call.span.into(),
                IndexedRetainedCallbackCall {
                    call: retained_call,
                    owner: self.owner,
                    caller: self.current,
                },
            );
        }
        if let Some(no_reentry_call) = no_reentry_call {
            self.no_reentry_callback_calls
                .insert(call.span.into(), no_reentry_call);
        }
        let is_factory_member_callee = self.literal_depth > 0
            && self.factory_callback_depth > 0
            && matches!(&call.callee, Callee::Expr(callee)
                if self.is_callback_factory_member_callee(callee));
        if !callback_arguments.is_empty() || is_factory_member_callee {
            match &call.callee {
                Callee::Expr(callee) if is_factory_member_callee => {
                    self.factory_member_value_depth += 1;
                    callee.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                callee => callee.visit_with(self),
            }
            if let Callee::Expr(callee) = &call.callee
                && let Expr::Member(member) = callee.unwrap_parens()
            {
                self.record_accessor_callee_lookup(member, no_reentry_accessor_call);
            }
            for (index, argument) in call.args.iter().enumerate() {
                if callback_arguments.contains(&index) {
                    self.factory_callback_depth += 1;
                    argument.visit_with(self);
                    self.factory_callback_depth -= 1;
                } else {
                    argument.visit_with(self);
                }
            }
            call.type_args.visit_with(self);
        } else if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            // A member callee is evaluated before any arguments. Keep a
            // typed event at that point so an applied getter can invalidate
            // receiver state before nested argument calls are analyzed.
            callee.visit_with(self);
            self.record_accessor_callee_lookup(member, no_reentry_accessor_call);
            for argument in &call.args {
                argument.visit_with(self);
            }
            call.type_args.visit_with(self);
        } else {
            call.visit_children_with(self);
        }
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            self.mark_opaque();
        }
        if let Some(proof) = from_entries_mapped_proof(call, &self.plain_array_bindings) {
            self.mapped_array_map_calls
                .insert(proof.map_call_site, proof.clone());
            self.from_entries_map_calls.insert(call.span.into(), proof);
        }
        if let Callee::Expr(callee) = &call.callee {
            if matches!(call.args.as_slice(), [first, second]
                if first.spread.is_none()
                    && second.spread.is_none()
                    && matches!(second.expr.unwrap_parens(), Expr::Lit(Lit::Bool(boolean)) if boolean.value))
            {
                self.literal_true_second_arguments.insert(call.span.into());
            }
            let indexed_call = Call {
                callee: ValueExpr::from_expr(callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                kind: CallKind::Call,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
                no_reentry_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_primitive_argument(&arg.expr)),
                no_reentry_record_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_inert_argument(&arg.expr))
                    && call
                        .args
                        .iter()
                        .any(|arg| matches!(arg.expr.unwrap_parens(), Expr::Object(_))),
                no_sync_arguments: is_no_sync_callback_member_call(
                    callee,
                    &self.no_sync_callback_members,
                ) || member_row_invokes_no_argument(callee, &call.args),
                caller: self.current,
                owner: self.owner,
                plain_object_sources: call.args.len() > 1
                    && call.args.iter().skip(1).all(|arg| {
                        matches!(arg.expr.unwrap_parens(), Expr::Object(object)
                            if object.props.iter().all(plain_assign_source_property))
                    }),
            };
            if let Expr::Member(member) = callee.unwrap_parens() {
                self.accessor_callee_calls
                    .insert(member.span.into(), indexed_call.clone());
            }
            self.record_call(indexed_call);
        } else {
            self.record_source_protocol(CallKind::OpaqueExecution, ValueExpr::Unknown, call.span);
            self.mark_opaque();
        }
    }

    fn visit_tpl(&mut self, template: &Tpl) {
        for expression in &template.exprs {
            expression.visit_with(self);
            self.record_implicit_coercion(expression, expression.span());
        }
    }

    fn visit_tagged_tpl(&mut self, template: &TaggedTpl) {
        template.tag.visit_with(self);
        for expression in &template.tpl.exprs {
            expression.visit_with(self);
        }
        template.type_params.visit_with(self);
        self.record_source_protocol(
            CallKind::OpaqueExecution,
            ValueExpr::Alternatives(
                std::iter::once(ValueExpr::from_expr(&template.tag))
                    .chain(
                        template
                            .tpl
                            .exprs
                            .iter()
                            .map(|expr| ValueExpr::from_expr(expr)),
                    )
                    .collect(),
            ),
            template.span,
        );
        self.mark_opaque();
    }

    fn visit_opt_chain_expr(&mut self, chain: &OptChainExpr) {
        // Only the optional operation's own lookup is unsupported. Namespace
        // reads in its argument callbacks still receive their ordinary context.
        let member = match chain.base.as_ref() {
            OptChainBase::Member(member) => Some(member),
            OptChainBase::Call(call) => match call.callee.unwrap_parens() {
                Expr::Member(member) => Some(member),
                _ => None,
            },
        };
        if !self.namespace_privacy.members.is_empty()
            && let Some(member) = member
        {
            self.namespace_privacy
                .optional_members
                .insert(member.span.lo.0);
        }
        let before = self.past_await;
        chain.visit_children_with(self);
        self.past_await = before;
        if let OptChainBase::Call(call) = chain.base.as_ref() {
            // Optional invocation can still execute arbitrary callee/receiver
            // protocols and invoke its arguments. Keep that conservative call
            // even though value/short-circuit inference stays opaque. Do not
            // admit the assertions indexed only for ordinary CallExpr syntax.
            self.record_call(Call {
                callee: ValueExpr::from_expr(&call.callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                kind: CallKind::Call,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                no_sync_arguments: false,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
            });
            if call.args.iter().any(|arg| arg.spread.is_some()) {
                // Argument positions and the returned value are unmodeled for
                // spread calls. Preserve the callee and argument capabilities
                // through the opaque path, including callable return values.
                self.record_source_protocol(
                    CallKind::Call,
                    ValueExpr::Alternatives(
                        std::iter::once(ValueExpr::from_expr(&call.callee))
                            .chain(call.args.iter().map(|arg| ValueExpr::from_expr(&arg.expr)))
                            .collect(),
                    ),
                    call.span,
                );
            }
        }
        self.mark_opaque();
    }

    fn visit_new_expr(&mut self, new_expr: &NewExpr) {
        if matches!(new_expr.callee.unwrap_parens(), Expr::Ident(_)) {
            self.bare_constructor_sites.insert(new_expr.span.into());
        }
        if let Some(span) = map_entry_array_span(new_expr) {
            self.map_entry_arrays.insert(span);
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = match new_expr.callee.unwrap_parens() {
            Expr::Ident(id) => Some(id.to_id()),
            _ => None,
        };
        new_expr.callee.visit_with(self);
        self.safe_class_target = previous_target;
        let retained_call = self.retained_constructor_call(new_expr);
        let callback_arguments: BTreeSet<_> = retained_call
            .iter()
            .flat_map(|call| &call.signatures)
            .flat_map(|signature| signature.callback_arguments.iter().copied())
            .collect();
        if let Some(contract) = retained_call {
            self.retained_constructor_calls
                .insert(new_expr.span.into(), contract);
        }
        for (position, argument) in new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .enumerate()
        {
            if callback_arguments.contains(&position) {
                self.factory_callback_depth += 1;
                argument.visit_with(self);
                self.factory_callback_depth -= 1;
            } else {
                argument.visit_with(self);
            }
        }
        if new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|arg| arg.spread.is_some())
        {
            self.mark_opaque();
        }
        self.record_call(Call {
            callee: ValueExpr::from_expr(&new_expr.callee),
            args: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .map(|arg| ValueExpr::from_expr(&arg.expr))
                .collect(),
            kind: CallKind::New,
            span: new_expr.span,
            has_spread: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .any(|arg| arg.spread.is_some()),
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
        });
    }

    fn visit_await_expr(&mut self, await_expr: &AwaitExpr) {
        await_expr.arg.visit_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
/// An imported one-argument call supplies only the first parameter. The
/// returned closure can therefore retain that callback and omitted parameters,
/// but it must not capture another source/host capability that the abstract
/// returned callback graph would omit when the result is invoked later. The
/// factory call itself is a derived foreign call: it runs no other code.
fn deferred_callback_closure(index: &ExecutionIndex, outer: u32, closure: &Range<u32>) -> bool {
    let (Some(outer), Some(inner)) = (
        index.callables.get(&outer),
        index.callables.get(&closure.start),
    ) else {
        return false;
    };
    let Some(callback) = outer
        .params
        .first()
        .and_then(|param| param.binding.as_ref())
    else {
        return false;
    };
    // The factory must return the closure literal directly. A body that binds
    // it first keeps opaque timing; author source policies
    // (`source_export_policy` fixtures) are tested against such a provider.
    if !outer.declared_locals.is_empty()
        || inner.opaque
        || inner.generator
        || inner.implicit_capture
        || index.async_callables.contains(&closure.start)
        || !inner.params.is_empty()
        || inner.dynamic_property_effect
        || !inner.member_writes.is_empty()
        || !inner.member_value_writes.is_empty()
        || !inner.callback_result_writes.is_empty()
        || index
            .callables
            .range((closure.start + 1)..closure.end)
            .next()
            .is_some()
    {
        return false;
    }
    let allowed: BTreeSet<_> = outer
        .params
        .iter()
        .filter_map(|param| param.binding.clone())
        .chain(inner.declared_locals.iter().cloned())
        .collect();
    let local_receivers: BTreeSet<_> = allowed
        .iter()
        .filter(|id| *id != callback)
        .cloned()
        .collect();
    let has_fresh_own_data_property = |value: &ValueExpr, property: &str| {
        matches!(value, ValueExpr::ObjectLiteral(object, fields)
            if index.plain_data_objects.contains(object)
                && fields.iter().any(|(name, _)| name.as_deref() == Some(property)))
    };
    inner.reads.is_subset(&allowed)
        && inner.rebinds.is_subset(&local_receivers)
        && inner
            .local_assignments
            .iter()
            .all(|(id, _)| local_receivers.contains(id))
        && inner
            .calls
            .iter()
            .all(|call| match (&call.kind, &call.callee) {
                (CallKind::Call, ValueExpr::Binding(id)) => id == callback && !call.has_spread,
                (
                    CallKind::LocalMemberRead,
                    ValueExpr::Member(receiver, Some(property), MemberKey::Static, _),
                ) => {
                    match receiver.as_ref() {
                        ValueExpr::Binding(id) if local_receivers.contains(id) => {
                            inner.local_assignments.iter().any(|(assigned, value)| {
                                assigned == id && has_fresh_own_data_property(value, property)
                            }) && inner
                                .local_assignments
                                .iter()
                                .filter(|(assigned, _)| assigned == id)
                                .all(|(_, value)| has_fresh_own_data_property(value, property))
                        }
                        // ValueExpr reduces an assignment expression to its RHS.
                        // The indexed member receiver is still this fresh literal.
                        value @ ValueExpr::ObjectLiteral(..) => {
                            has_fresh_own_data_property(value, property)
                        }
                        _ => false,
                    }
                }
                _ => false,
            })
}

/// A derived foreign call's fresh record result, with each member's provider
/// captures for the report.
pub(crate) struct InferredReturnedRecord {
    pub members: Vec<(String, u32, BTreeSet<String>)>,
    pub inference: crate::analysis_provenance::InferenceUse,
}

pub(crate) struct InferredNamespaceWrapper {
    pub function_start: u32,
    pub inference: crate::analysis_provenance::InferenceUse,
    pub dependencies: ProofDependencies,
}

pub(crate) struct InferredCallableCapabilities {
    /// Exported local → derived call summary
    /// (`crate::facts::abstract_domain::foreign_call`).
    pub foreign_calls: BTreeMap<String, InferredForeignCall>,
    /// Keyed-collection families whose global constructor slot this module's
    /// census leaves intact.
    pub intact_collections: BTreeSet<NativeIntrinsic>,
    pub stable_providers: BTreeSet<String>,
    pub namespace_wrappers: BTreeMap<String, InferredNamespaceWrapper>,
}

pub(crate) struct InferredForeignCall {
    pub derived: crate::facts::abstract_domain::foreign_call::DerivedForeignCall,
    pub declaration_start: u32,
    pub availability: CallableAvailability,
    pub dependencies: ProofDependencies,
    pub record: Option<InferredReturnedRecord>,
    /// The returned closure defers its first argument (`deferred_callback_closure`).
    pub callback_factory: bool,
}

/// Availability is independent of the callable body's invocation/return proof.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum CallableAvailability {
    /// Existing declaration eligibility, kept separate from the new initializer proof.
    Declaration,
    /// The provider has one const callable declaration and only local exports.
    /// Every emitted destination reaching it therefore depends on a leaf: no
    /// assignment can introduce a return import from that initializer.
    IsolatedInitializer,
    UnprovedInitializer,
}

impl CallableAvailability {
    pub(crate) fn available_to_importer(self) -> bool {
        !matches!(self, Self::UnprovedInitializer)
    }
}

fn isolated_callable_initializer(body: &[TopLevelItemView<'_>], binding: &Ident) -> bool {
    let mut declarations = 0;
    for item in body {
        match item.as_module_item() {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                decl: Decl::Var(declaration), ..
            })) => {
                let [declarator] = declaration.decls.as_slice() else { return false; };
                if declaration.kind != VarDeclKind::Const
                    || !matches!(&declarator.name, Pat::Ident(local) if local.id.to_id() == binding.to_id())
                {
                    return false;
                }
                declarations += 1;
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export))
                if export.src.is_none() && export.specifiers.iter().all(|specifier| {
                    matches!(specifier, ExportSpecifier::Named(named)
                        if matches!(&named.orig, ModuleExportName::Ident(local) if local.to_id() == binding.to_id()))
                }) => {}
            _ => return false,
        }
    }
    declarations == 1
}

/// Prepared once for callable inference and the whole-program native census.
/// Value flow remains demand-driven even when every module contributes writes.
pub(crate) struct PreparedCallableCapabilities {
    pub(super) index: ExecutionIndex,
    exported_locals: BTreeSet<String>,
    flow: Option<ChunkValueFlow>,
}

impl PreparedCallableCapabilities {
    /// A negative check for an exact audited imported call. This reads the
    /// already prepared callable index; unresolved calls remain the author's
    /// responsibility rather than becoming inferred absence of effects.
    pub(crate) fn known_external_write_for_local(&self, local: &str) -> Option<String> {
        let index = &self.index;
        let binding = index
            .hoisted_bindings
            .keys()
            .chain(index.top_assignments.iter().flatten().map(|(id, _)| id))
            .find(|id| id.0.as_ref() == local)?;
        let start = direct_callable_binding(index, binding, &mut BTreeSet::new())?;
        let mut pending = vec![start];
        let mut visited = BTreeSet::new();
        while let Some(key) = pending.pop() {
            if !visited.insert(key) {
                continue;
            }
            let callable = &index.callables[&key];
            let known_nonfresh = |receiver: &Id| {
                // A helper parameter or closure capture can still point to
                // the wrapper allocated by its caller. Only an identified
                // module/global binding is a definite contradiction here.
                !callable.declared_locals.contains(receiver)
                    && !callable.pattern_locals.contains(receiver)
                    && !callable
                        .params
                        .iter()
                        .any(|param| param.bindings.contains(receiver))
                    && ((index.global_object_names.contains(receiver.0.as_ref())
                        && !index.callables.values().any(|scope| {
                            scope.declared_locals.contains(receiver)
                                || scope.pattern_locals.contains(receiver)
                                || scope
                                    .params
                                    .iter()
                                    .any(|param| param.bindings.contains(receiver))
                        }))
                        || index.imported_bindings.contains(receiver)
                        || index.hoisted_bindings.contains_key(receiver)
                        || index.top_const_declarations.contains(receiver)
                        || index
                            .top_assignments
                            .iter()
                            .flatten()
                            .any(|(id, _)| id == receiver))
            };
            for (receiver, property) in &callable.member_writes {
                if known_nonfresh(receiver) {
                    return Some(format!("property write {}.{property}", receiver.0));
                }
            }
            for receiver in &callable.dynamic_property_receivers {
                if let ValueExpr::Binding(receiver) = receiver
                    && known_nonfresh(receiver)
                {
                    return Some(format!("computed property write on {}", receiver.0));
                }
            }
            for binding in &callable.rebinds {
                if known_nonfresh(binding) {
                    return Some(format!("outer binding write {}", binding.0));
                }
            }
            for call in &callable.calls {
                if call.kind != CallKind::Call {
                    continue;
                }
                let ValueExpr::Binding(callee) = &call.callee else {
                    continue;
                };
                if let Some(&helper) = callable.local_functions.get(callee) {
                    pending.push(helper);
                } else if let Some(helper) =
                    direct_callable_binding(index, callee, &mut BTreeSet::new())
                {
                    pending.push(helper);
                }
            }
        }
        None
    }

    /// Called with the conservative completed all-source census before any
    /// structural flow. A later refined census cannot add roots retroactively.
    pub(crate) fn inferred_set_constructions(
        &self,
        integrity: &NativeIntegrity,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        // Keep the early proof independent of the late index flags. A11 gives
        // original slot identities; only this completed all-source census (or
        // its validated program premise) may preserve them. Default flags are
        // open/Unknown and no author exact-site axiom is supplied here.
        let dependencies = ProofDependencies {
            value_inputs: integrity.consumed_value_inputs.clone(),
            host_behavior: integrity.consumed_host_behavior.clone(),
            no_accessor_install_callables: if integrity
                .consumed_host_behavior
                .contains(&HostBehaviorKey::PredefinedGlobals)
            {
                self.index
                    .census_globals
                    .bundle
                    .no_accessor_install_callables
                    .clone()
            } else {
                BTreeSet::new()
            },
            program_premises: if integrity.never_written_overrides.is_empty() {
                BTreeSet::new()
            } else {
                BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten])
            },
            ..ProofDependencies::default()
        };
        let flags = IntegrityFlags::new(
            self.index.dynamic_scope_owners.is_empty(),
            integrity.mutated.clone(),
            &integrity.witnesses,
            dependencies,
            BTreeMap::new(),
            PrototypeWrites::new(
                integrity
                    .prototype_mutations
                    .iter()
                    .map(|write| write.properties.as_ref()),
            ),
        )
        .with_never_written(integrity.never_written.clone());
        self.index.infer_set_constructions(&flags)
    }

    pub(crate) fn bundle_global_candidates(&self) -> BundleGlobalNames {
        self.index.bundle_global_candidates()
    }

    pub(crate) fn bundle_global_survivors(
        &self,
        candidates: &BundleGlobalNames,
        premises: &[ResolvedNoAccessorInstall],
    ) -> BundleGlobalNames {
        self.index.bundle_global_survivors(candidates, premises)
    }

    pub(crate) fn value_input_errors(&self) -> &[String] {
        &self.index.value_input_errors
    }

    pub(crate) fn resolved_value_inputs(&self) -> Vec<ResolvedBindingValueInput> {
        self.index.resolved_value_inputs.clone()
    }

    pub(crate) fn native_operation_input_errors(&self) -> &[String] {
        &self.index.native_operation_input_errors
    }

    pub(crate) fn resolved_native_operation_inputs(&self) -> Vec<ResolvedNativeOperationInput> {
        self.index.resolved_native_operation_inputs.clone()
    }

    pub(crate) fn known_global_native_operation_mutation_owner(
        &self,
        operation: NativeInputKind,
    ) -> Option<SourceUnitOrdinal> {
        self.index.known_global_native_operation_mutation(operation)
    }

    pub(crate) fn known_symbol_for_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_symbol_for_mutation()
    }

    pub(crate) fn known_global_set_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_global_set_mutation()
    }

    pub(crate) fn known_object_freeze_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_object_freeze_mutation()
    }

    pub(crate) fn known_descriptor_copy_mutation_owner(
        &self,
        assigned_keys: &BTreeSet<String>,
    ) -> Option<SourceUnitOrdinal> {
        self.index.known_descriptor_copy_mutation(assigned_keys)
    }

    pub(crate) fn known_set_protocol_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_set_protocol_mutation()
    }

    pub(crate) fn known_set_spread_protocol_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_set_spread_protocol_mutation()
    }

    pub(crate) fn known_native_read_contradiction_owner(
        &self,
        operation: NativeInputKind,
        family: NativeIntrinsic,
        property: &str,
    ) -> Option<SourceUnitOrdinal> {
        if operation
            == NativeInputKind::Operation(spec::NativeOperationKind::SymbolPrimitiveClassKeys)
        {
            return self
                .index
                .known_primitive_symbol_read_contradiction(property);
        }
        self.index.known_native_read_contradiction(family, property)
    }

    pub(crate) fn native_input_read_property(
        &self,
        input: &ResolvedNativeOperationInput,
    ) -> Option<(NativeIntrinsic, &'static str)> {
        let [declaration] = self
            .index
            .binding_declaration_writes
            .get(&input.binding)?
            .as_slice()
        else {
            return None;
        };
        match input.evidence.operation {
            NativeInputKind::Operation(spec::NativeOperationKind::ObjectOwnDataFunctionRead) => {
                let property =
                    object_own_data_read_property(declaration.initializer.as_ref()?, |id| {
                        self.index.declared_bindings().contains(id)
                    })?;
                Some((NativeIntrinsic::Object, property))
            }
            NativeInputKind::Operation(
                spec::NativeOperationKind::SymbolWellKnownClassKeys
                | spec::NativeOperationKind::SymbolPrimitiveClassKeys,
            ) => {
                let property = self
                    .index
                    .symbol_class_key_read_property(declaration, input.call_span)?;
                Some((NativeIntrinsic::Symbol, property))
            }
            _ => None,
        }
    }

    pub(crate) fn known_array_iterator_protocol_mutation_owner_in(
        &self,
        integrity: &ArraySpreadProtocolIntegrity,
    ) -> Option<SourceUnitOrdinal> {
        self.index
            .known_array_iterator_protocol_mutation_with(integrity)
    }

    pub(crate) fn locate_value_inputs(
        &mut self,
        source: Option<&(String, js_ast::SourceLineIndex)>,
    ) {
        let locate = |span: Span| {
            let (path, index) = source?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(crate::execution_evidence::SourceLocation {
                source_path: path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        for input in &mut self.index.resolved_value_inputs {
            input.evidence.declaration_location = locate(input.declaration_span);
            input.evidence.initializer_location = locate(input.initializer_span);
            self.index
                .value_input_evidence
                .insert(input.evidence.reference.clone(), input.evidence.clone());
        }
        for witness in Arc::make_mut(&mut self.index.native_write_census.witnesses) {
            witness.value_input_evidence = witness
                .value_input_refs
                .iter()
                .filter_map(|reference| self.index.value_input_evidence.get(reference).cloned())
                .collect();
        }
        for write in &mut self.index.native_write_census.conditional {
            write.witness.value_input_evidence = write
                .witness
                .value_input_refs
                .iter()
                .filter_map(|reference| self.index.value_input_evidence.get(reference).cloned())
                .collect();
        }
    }

    pub(crate) fn locate_native_operation_inputs(
        &mut self,
        source: Option<&(String, js_ast::SourceLineIndex)>,
    ) {
        let locate = |span: Span| {
            let (path, index) = source?;
            let (start_line, end_line, start_column) =
                index.line_range_and_start_column_for_span(span)?;
            Some(crate::execution_evidence::SourceLocation {
                source_path: path.clone(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        for input in &mut self.index.resolved_native_operation_inputs {
            input.evidence.declaration_location = locate(input.declaration_span);
            input.evidence.initializer_location = locate(input.call_span);
            self.index
                .native_operation_input_evidence
                .insert(CallSite::from(input.call_span), input.evidence.clone());
        }
    }

    pub(crate) fn locate_native_writes(
        &mut self,
        chunk_id: &str,
        source: Option<&(String, js_ast::SourceLineIndex)>,
    ) {
        self.index.source_chunk_id = Some(chunk_id.to_owned());
        let source_location = |span| {
            source.and_then(|(path, index)| {
                let (start_line, end_line, start_column) =
                    index.line_range_and_start_column_for_span(span)?;
                Some(SourceLocation {
                    source_path: path.clone(),
                    start_line,
                    end_line,
                    start_column: Some(start_column),
                })
            })
        };
        let locate = |witness: &mut NativeMutationWitness| {
            if witness.receiver_roots.is_empty() {
                return;
            }
            witness.chunk_id = chunk_id.to_owned();
            witness.source_location = source_location(witness.span);
            if let Some(proof) = &mut witness.unreachable_branch {
                proof.guard_location = source_location(proof.guard_span);
                proof.wrapper_call_location = source_location(proof.wrapper_call_span);
                proof.callback_location = source_location(proof.callback_span);
            }
        };
        for witness in Arc::make_mut(&mut self.index.native_write_census.witnesses) {
            locate(witness);
        }
        for write in &mut self.index.native_write_census.conditional {
            locate(&mut write.witness);
        }
        for mutation in Arc::make_mut(&mut self.index.native_write_census.prototype_mutations) {
            mutation.chunk_id = chunk_id.to_owned();
            mutation.source_location = source_location(mutation.span);
        }
    }

    pub(crate) fn native_writes(&self) -> &NativeWriteCensus {
        &self.index.native_write_census
    }

    pub(crate) fn refine_native_branches(
        &mut self,
        summaries: &BTreeMap<String, crate::analysis_inputs::CallableSummary>,
    ) {
        self.index.refine_native_branches(summaries);
    }

    pub(crate) fn callback_object_arguments<'f>(
        &self,
        body: &[TopLevelItemView<'_>],
        structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
    ) -> BTreeMap<String, (u32, BTreeSet<usize>)> {
        // Only source identity is complete here: callback argument coverage
        // helps resolve native writes, so no native construction is admitted.
        let arguments = |call: &InferredForeignCall| {
            callback_arguments::ordinary_callback_arguments(
                &self.index,
                call.declaration_start,
                call.derived.closure.as_ref()?,
            )
            .filter(|_| call.callback_factory)
        };
        infer_foreign_calls(
            &self.index,
            body,
            &self.exported_locals,
            self.flow.as_ref(),
            structural_flow,
            |_| false,
            |call| arguments(call).is_some(),
        )
        .into_iter()
        .filter_map(|(local, call)| Some((local, (call.declaration_start, arguments(&call)?))))
        .collect()
    }
}

fn direct_callable_binding(
    index: &ExecutionIndex,
    binding: &Id,
    visited: &mut BTreeSet<Id>,
) -> Option<u32> {
    if !visited.insert(binding.clone()) || index.reassigned_bindings.contains(binding) {
        return None;
    }
    if let Some(&key) = index.hoisted_bindings.get(binding) {
        return Some(key);
    }
    let assignments = index
        .top_assignments
        .iter()
        .flatten()
        .filter(|(id, _)| id == binding)
        .map(|(_, value)| value)
        .collect::<Vec<_>>();
    match assignments.as_slice() {
        [ValueExpr::Alloc(Kind::Function, key)] if index.callables.contains_key(key) => Some(*key),
        [ValueExpr::Binding(alias)] => direct_callable_binding(index, alias, visited),
        _ => None,
    }
}

pub(crate) fn prepare_callable_capabilities(
    body: &[TopLevelItemView<'_>],
    array_spreads: StableArraySpreadShapes,
    exported_locals: BTreeSet<String>,
    selected_value_inputs: Vec<SelectedBindingValueInput>,
    selected_native_operation_inputs: Vec<SelectedNativeOperationInput>,
    predefined_globals: spec::PredefinedGlobals,
    bundle_globals: BundleGlobalNames,
) -> PreparedCallableCapabilities {
    let flow = (!selected_value_inputs.is_empty()).then(|| ChunkValueFlow::analyze(body));
    PreparedCallableCapabilities {
        index: ExecutionIndex::collect(
            body,
            &AnalysisInputs {
                selected_value_inputs,
                selected_native_operation_inputs,
                assumptions: crate::analysis_inputs::AnalysisAssumptions {
                    predefined_globals,
                    bundle_globals,
                    ..Default::default()
                },
                ..AnalysisInputs::default()
            },
            flow.as_ref(),
            &BTreeSet::new(),
            &LocalClassConstructionShapes::default(),
            &array_spreads,
        ),
        exported_locals,
        flow,
    }
}

/// Consume the same prepared facts after resolving all modules' native writes.
pub(crate) fn infer_callable_capabilities<'f>(
    body: &[TopLevelItemView<'_>],
    prepared: PreparedCallableCapabilities,
    integrity: &NativeIntegrity,
    required_stable_providers: &BTreeSet<String>,
    derive_foreign_calls: bool,
    structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
) -> InferredCallableCapabilities {
    let PreparedCallableCapabilities {
        mut index,
        exported_locals,
        flow: mut prepared_flow,
    } = prepared;
    index.apply_native_integrity(integrity);
    let mut result = InferredCallableCapabilities {
        foreign_calls: BTreeMap::new(),
        intact_collections: BTreeSet::new(),
        stable_providers: BTreeSet::new(),
        namespace_wrappers: BTreeMap::new(),
    };
    let has_candidate = index.hoisted_bindings.iter().any(|(id, key)| {
        required_stable_providers.contains(id.0.as_ref())
            || (exported_locals.contains(id.0.as_ref())
                && index.namespace_wrapper_candidates.contains_key(key))
    });
    if has_candidate {
        let flow = prepared_flow
            .take()
            .unwrap_or_else(|| ChunkValueFlow::analyze(body));
        let shadowed = crate::facts::compute_shadowed_globals(body);
        let callables = callable_inference_inputs(&index, &flow, &exported_locals);
        index.semantic_facts = Some(index.prepare_semantic_facts(
            &flow,
            &shadowed,
            SemanticShapeInputs {
                classes: LocalClassConstructionShapes::default(),
                sets: SetIterableShapes::default(),
                array_spreads: &StableArraySpreadShapes::default(),
                callables,
            },
            None,
        ));
        result.stable_providers = index
            .hoisted_bindings
            .iter()
            .filter(|(binding, _)| required_stable_providers.contains(binding.0.as_ref()))
            .filter(|(binding, function)| {
                index.semantic_facts.as_ref().is_some_and(|facts| {
                    facts
                        .judgment(&SemanticKey::StableProvider {
                            binding: (*binding).clone(),
                            function: **function,
                        })
                        .is_some()
                })
            })
            .map(|(binding, _)| binding.0.to_string())
            .collect();
        result.namespace_wrappers = infer_namespace_wrappers(&index, &exported_locals);
        prepared_flow = Some(flow);
    }
    if derive_foreign_calls {
        result.foreign_calls = infer_foreign_calls(
            &index,
            body,
            &exported_locals,
            prepared_flow.as_ref(),
            structural_flow,
            |family| {
                matches!(
                    index
                        .integrity
                        .require(&[IntrinsicSlot::Global(family)], None),
                    crate::facts::abstract_domain::integrity::Integrity::Intact(_)
                )
            },
            |call| call.availability.available_to_importer() || call.callback_factory,
        );
    }
    result.intact_collections = [
        NativeIntrinsic::Map,
        NativeIntrinsic::Set,
        NativeIntrinsic::WeakMap,
        NativeIntrinsic::WeakSet,
    ]
    .into_iter()
    .filter(|family| {
        matches!(
            index
                .integrity
                .require(&[IntrinsicSlot::Global(*family)], None),
            crate::facts::abstract_domain::integrity::Integrity::Intact(_)
        )
    })
    .collect();
    result
}

/// Exported top-level function and class declarations, and single-declarator
/// top-level function or arrow initializers, with one stable value, summarized
/// by `foreign_call`. Dynamic scope can rebind any of them. `collection_intact`
/// answers the census for an empty keyed collection's constructor; `wanted`
/// selects summaries before value flow checks their binding's stability.
fn infer_foreign_calls<'f>(
    index: &ExecutionIndex,
    body: &[TopLevelItemView<'_>],
    exported_locals: &BTreeSet<String>,
    flow: Option<&ChunkValueFlow>,
    structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
    collection_intact: impl Fn(NativeIntrinsic) -> bool,
    wanted: impl Fn(&InferredForeignCall) -> bool,
) -> BTreeMap<String, InferredForeignCall> {
    if !index.dynamic_scope_owners.is_empty() {
        return BTreeMap::new();
    }
    let declarations: Vec<_> = body
        .iter()
        .filter_map(|item| {
            let declaration = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(declaration)) => declaration,
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => &export.decl,
                _ => return None,
            };
            let (binding, is_class, start, hoisted) = match declaration {
                Decl::Fn(function) => (&function.ident, false, function.function.span.lo.0, true),
                Decl::Class(class) => (&class.ident, true, class.class.span.lo.0, true),
                Decl::Var(declaration) => {
                    let [declarator] = declaration.decls.as_slice() else {
                        return None;
                    };
                    let Pat::Ident(binding) = &declarator.name else {
                        return None;
                    };
                    let (is_class, start) = match declarator.init.as_deref()?.unwrap_parens() {
                        Expr::Fn(function) => (false, function.function.span.lo.0),
                        Expr::Arrow(arrow) => (false, arrow.span.lo.0),
                        _ => return None,
                    };
                    (&binding.id, is_class, start, false)
                }
                _ => return None,
            };
            exported_locals
                .contains(binding.sym.as_ref())
                .then_some((binding, is_class, start, hoisted))
        })
        .collect();
    if declarations.is_empty() {
        return BTreeMap::new();
    }
    let shadowed = crate::facts::compute_shadowed_globals(body);
    let candidates: Vec<_> = declarations
        .into_iter()
        .filter(|(binding, ..)| !index.reassigned_bindings.contains(&binding.to_id()))
        .filter_map(|(binding, is_class, declaration_start, hoisted)| {
            let derived = if is_class {
                strict_provider::derive_class(index, declaration_start, &shadowed)?
            } else {
                // All callable bodies consume one shared source-covered plan.
                // Refusal never retries another evaluator.
                strict_provider::derive(index, declaration_start, &shadowed)?
            };
            if !derived
                .native_constructions
                .iter()
                .all(|family| collection_intact(*family))
            {
                return None;
            }
            Some((
                binding,
                foreign_call(
                    index,
                    derived,
                    declaration_start,
                    hoisted,
                    !hoisted && isolated_callable_initializer(body, binding),
                ),
            ))
        })
        .filter(|(_, call)| wanted(call))
        .collect();
    if candidates.is_empty() {
        return BTreeMap::new();
    }
    // Binding values do not depend on the tracked candidate objects, so the
    // structural flow the purity oracle solves for this body anyway answers
    // this check.
    let flow = match flow {
        Some(flow) => flow,
        None => structural_flow(),
    };
    candidates
        .into_iter()
        .filter(|(binding, call)| {
            let values = flow.values_of_binding(&binding.to_id());
            // An initializer's binding holds `undefined` (or is in its TDZ)
            // until its statement runs; afterwards it must hold only that
            // function.
            if call.availability == CallableAvailability::Declaration {
                values.iter().count() == 1
            } else {
                values
                    .iter()
                    .eq([Value::Alloc(Kind::Function, call.declaration_start)])
            }
        })
        .map(|(binding, call)| (binding.sym.to_string(), call))
        .collect()
}

fn foreign_call(
    index: &ExecutionIndex,
    derived: crate::facts::abstract_domain::foreign_call::DerivedForeignCall,
    declaration_start: u32,
    hoisted: bool,
    isolated_initializer: bool,
) -> InferredForeignCall {
    let mut dependencies = derived.dependencies.clone();
    for family in &derived.native_constructions {
        dependencies.extend(
            &index
                .integrity
                .require(&[IntrinsicSlot::Global(*family)], None)
                .dependencies()
                .expect("derivation checked the census for every construction"),
        );
    }
    let record = derived
        .record
        .as_ref()
        .map(|members| returned_record(index, members, &derived, &dependencies));
    let callback_factory = derived.closure.as_ref().is_some_and(|closure| {
        derived.returns == BTreeSet::from([crate::analysis_inputs::ForeignReturn::FreshAllocation])
            && deferred_callback_closure(index, declaration_start, closure)
    });
    InferredForeignCall {
        declaration_start,
        availability: if hoisted {
            CallableAvailability::Declaration
        } else if isolated_initializer && derived.retained_provider_captures.is_empty() {
            CallableAvailability::IsolatedInitializer
        } else {
            CallableAvailability::UnprovedInitializer
        },
        derived,
        dependencies,
        record,
        callback_factory,
    }
}

/// Report projection of a derived record: each member's free provider
/// bindings, and the rules the derivation used.
fn returned_record(
    index: &ExecutionIndex,
    members: &[crate::facts::abstract_domain::foreign_call::RecordMember],
    derived: &crate::facts::abstract_domain::foreign_call::DerivedForeignCall,
    dependencies: &ProofDependencies,
) -> InferredReturnedRecord {
    use crate::analysis_provenance::{InferenceRule, InferenceUse};
    let members = members
        .iter()
        .map(|member| {
            let callable = &index.callables[&member.function_start];
            let parameters: BTreeSet<_> = callable
                .params
                .iter()
                .filter_map(|param| param.binding.as_ref())
                .collect();
            let captures = callable
                .reads
                .difference(&callable.declared_locals)
                .filter(|binding| !parameters.contains(binding))
                .map(|binding| binding.0.to_string())
                .collect();
            (member.name.clone(), member.function_start, captures)
        })
        .collect();
    let mut derivation_rules =
        BTreeSet::from([InferenceRule::ReturnedRecord, InferenceRule::StableFunction]);
    if !derived.native_constructions.is_empty() {
        derivation_rules.extend([
            InferenceRule::EmptyCollectionConstruction,
            InferenceRule::NativeCallIdentity,
            InferenceRule::NativeIntegrity,
        ]);
    }
    InferredReturnedRecord {
        members,
        inference: InferenceUse {
            conclusion: InferenceRule::ReturnedRecord,
            derivation_rules,
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: dependencies.argument_inputs.clone(),
            native_operation_input_refs: dependencies.native_operation_inputs.clone(),
            imported_primitive_array_spread_input_refs: dependencies
                .imported_primitive_array_spread_inputs
                .clone(),
        },
    }
}

fn namespace_wrapper_inputs(
    index: &ExecutionIndex,
    exported_locals: &BTreeSet<String>,
) -> BTreeMap<u32, NamespaceWrapperCandidateFacts> {
    let mut inferred = BTreeMap::new();
    for (binding, &function_start) in &index.hoisted_bindings {
        if !exported_locals.contains(binding.0.as_ref()) {
            continue;
        }
        let Some(candidate) = index.namespace_wrapper_candidates.get(&function_start) else {
            continue;
        };
        let Some([cache_declaration]) = index
            .binding_declaration_writes
            .get(&candidate.cache)
            .map(Vec::as_slice)
        else {
            continue;
        };
        let Some(ValueExpr::New(weak_map, args, _)) = &cache_declaration.initializer else {
            continue;
        };
        let ValueExpr::Binding(weak_map) = weak_map.as_ref() else {
            continue;
        };
        if !cache_declaration.direct_top_level
            || !cache_declaration.simple_identifier
            || !index.top_const_declarations.contains(&candidate.cache)
            || index.exported_bindings.contains(&candidate.cache)
            || index.reassigned_bindings.contains(&candidate.cache)
            || !args.is_empty()
            || weak_map.0.as_ref() != "WeakMap"
            || index
                .binding_use_bounds
                .get(&candidate.cache)
                .is_none_or(|(first, last)| {
                    *first < function_start || *last >= candidate.function_end
                })
        {
            continue;
        }
        inferred.insert(
            function_start,
            NamespaceWrapperCandidateFacts {
                object: candidate.object.clone(),
                weak_map: weak_map.clone(),
            },
        );
    }
    inferred
}

/// Retain canonical candidates without granting their semantic conclusions.
fn callable_inference_inputs(
    index: &ExecutionIndex,
    flow: &ChunkValueFlow,
    exported_locals: &BTreeSet<String>,
) -> CallableInferenceInputs {
    let mut inputs = CallableInferenceInputs {
        namespace_wrappers: namespace_wrapper_inputs(index, exported_locals),
        ..CallableInferenceInputs::default()
    };
    let rebound: BTreeSet<_> = index
        .top_assignments
        .iter()
        .flatten()
        .map(|(binding, _)| binding.clone())
        .chain(
            index
                .callables
                .values()
                .flat_map(|callable| callable.rebinds.iter().cloned()),
        )
        .collect();
    for binding in index
        .hoisted_bindings
        .keys()
        .filter(|binding| exported_locals.contains(binding.0.as_ref()))
    {
        inputs.functions.insert(
            binding.clone(),
            FunctionBindingFacts {
                values: flow.values_of_binding(binding),
                rebound: rebound.contains(binding),
            },
        );
    }
    inputs
}

fn infer_namespace_wrappers(
    index: &ExecutionIndex,
    exported_locals: &BTreeSet<String>,
) -> BTreeMap<String, InferredNamespaceWrapper> {
    index
        .hoisted_bindings
        .iter()
        .filter_map(|(binding, &function)| {
            if !exported_locals.contains(binding.0.as_ref()) {
                return None;
            }
            let semantic_facts = index.semantic_facts.as_ref()?;
            let key = SemanticKey::NamespaceWrapper {
                binding: binding.clone(),
                function,
            };
            let judgment = semantic_facts.judgment(&key)?;
            let SemanticFact::NamespaceWrapper(function) = judgment.value() else {
                return None;
            };
            Some((
                binding.0.to_string(),
                InferredNamespaceWrapper {
                    function_start: *function,
                    inference: semantic_facts.inference_use(&key)?,
                    dependencies: judgment.dependencies().clone(),
                },
            ))
        })
        .collect()
}

#[cfg(test)]
mod preflow_set_protocol_tests {
    use super::*;
    use raw_js_test_support::parse;

    // This tests the pre-flow prerequisite seam, which the CLI only exposes
    // after flow and execution. It uses the real source/native preparation.
    fn proofs(source: &str) -> BTreeMap<u32, InferredSetConstruction> {
        proofs_with_premises(source, &BTreeSet::new())
    }

    fn proofs_with_premises(
        source: &str,
        families: &BTreeSet<NativeIntrinsic>,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        let module = parse(source);
        let views = crate::facts::top_level_item_views(&module.body);
        let prepared = prepare_callable_capabilities(
            &views,
            StableArraySpreadShapes::default(),
            BTreeSet::new(),
            Vec::new(),
            Vec::new(),
            spec::PredefinedGlobals::Unknown,
            BundleGlobalNames::default(),
        );
        assert!(prepared.flow.is_none());
        assert!(
            prepared
                .index
                .infer_set_constructions(&IntegrityFlags::default())
                .is_empty()
        );
        let mut census = resolve_native_integrity([prepared.native_writes()]);
        apply_intrinsics_never_written(&mut census, families)
            .expect("premise is not source-refuted");
        let proofs = prepared.inferred_set_constructions(&census);
        assert!(prepared.flow.is_none());
        proofs
    }

    #[test]
    fn full_primitive_set_spread_protocol_is_proved_before_flow() {
        let proofs = proofs(
            r#"console.log("before");
var seed = new Set(["a", "b"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);"#,
        );
        assert_eq!(proofs.len(), 3);
        let spread = proofs
            .values()
            .find(|proof| proof.binding.0 == "copied")
            .unwrap();
        let InferredSetOperation::ProtectedSpread { interval, .. } = &spread.operation else {
            panic!("spread needs a protected source interval")
        };
        assert_eq!(
            interval
                .interval_bindings
                .iter()
                .map(|id| id.0.as_ref())
                .collect::<Vec<_>>(),
            ["seed", "sibling", "copied"]
        );
        assert!(spread.dependencies.native_operation_inputs.is_empty());
    }

    #[test]
    fn source_shape_never_substitutes_for_protocol_integrity() {
        for prefix in [
            "Set.prototype.add = function () {};",
            "Set.prototype[Symbol.iterator] = function () {};",
            "Object.getPrototypeOf(new Set()[Symbol.iterator]()).next = function () {};",
            "Array.prototype[Symbol.iterator] = function () {};",
        ] {
            let source =
                format!("{prefix} var seed = new Set(['a']), copied = new Set([...seed, 'b']);");
            assert!(
                !proofs(&source)
                    .values()
                    .any(|proof| proof.binding.0 == "copied"),
                "{prefix}"
            );
        }
    }

    #[test]
    fn preflow_protocol_consumes_the_existing_program_premise() {
        let source = "globalThis[unknownKey] = 1; var seed = new Set(['a']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);";
        assert!(
            !proofs(source)
                .values()
                .any(|proof| proof.binding.0 == "copied")
        );
        let proven = proofs_with_premises(
            source,
            &BTreeSet::from([NativeIntrinsic::Set, NativeIntrinsic::Array]),
        );
        let copied = proven
            .values()
            .find(|proof| proof.binding.0 == "copied")
            .expect("all protocol slots inherit the validated Set/Array families");
        assert!(
            copied
                .dependencies
                .program_premises
                .contains(&ProgramPremiseKey::IntrinsicsNeverWritten)
        );
        assert!(copied.dependencies.native_operation_inputs.is_empty());
    }

    #[test]
    fn source_interval_requires_unique_stable_bindings_and_inert_siblings() {
        for source in [
            "var seed = new Set(['a']); var copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), seed = new Set(['b']), copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), sibling = mutate(seed), copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), copied = new Set([...seed, 'c']); seed = new Set(['b']);",
            "var seed = new Set(['a']), copied = new Set([...seed, callback]);",
        ] {
            assert!(
                !proofs(source)
                    .values()
                    .any(|proof| proof.binding.0 == "copied"),
                "{source}"
            );
        }
    }
}
