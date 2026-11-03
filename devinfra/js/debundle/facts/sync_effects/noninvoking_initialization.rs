//! Whole-source-unit coverage for a small, non-invoking initializer subset.

use super::value_model::{AbstractValue, CallSite, ValueExpr, Values};
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::abstract_domain::value::{Kind, PrimKinds, Value};
use crate::facts::item_views::{SplitDeclaratorProvenance, TopLevelItemView};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum NativeConstructorKind {
    Map,
    Set,
    TextEncoder,
}

impl NativeConstructorKind {
    pub(super) fn global_name(self) -> &'static str {
        match self {
            Self::Map => "Map",
            Self::Set => "Set",
            Self::TextEncoder => "TextEncoder",
        }
    }
}

#[derive(Clone)]
enum Leaf {
    Primitive(PrimKinds),
    Function(u32),
    Class(u32),
    Binding(Id),
    NativeConstructor {
        callee: Id,
        call_site: CallSite,
        kind: NativeConstructorKind,
    },
    Allocation {
        allocation: u32,
        reads: BTreeSet<Id>,
    },
}

/// Collected from the original owner before ValueExpr can erase syntax.
/// This says nothing about executing a function's lazy body.
#[derive(Clone)]
pub(super) struct OriginalInitializer {
    owner: SourceUnitOrdinal,
    binding: Id,
    initializer_span: Span,
    leaf: Leaf,
}

pub(super) struct EvaluatedInitializer {
    owner: SourceUnitOrdinal,
    initializer_span: Span,
}

/// A candidate exists only while one dedicated root initializer is evaluated.
/// Its matching transfer is recorded by that root's native New dispatch.
#[derive(Clone, Copy)]
pub(super) struct NativeConstructorCandidate {
    owner: SourceUnitOrdinal,
    call_site: CallSite,
    kind: NativeConstructorKind,
}

/// Evidence that the exact root New dispatch selected and applied the native
/// no-argument constructor row. This is not a value identity or a heap-shape fact.
#[derive(Clone, Copy)]
pub(super) struct NativeConstructorTransfer {
    owner: SourceUnitOrdinal,
    call_site: CallSite,
    kind: NativeConstructorKind,
}

impl NativeConstructorTransfer {
    pub(super) fn applied(candidate: NativeConstructorCandidate) -> Self {
        Self {
            owner: candidate.owner,
            call_site: candidate.call_site,
            kind: candidate.kind,
        }
    }

    fn matches(self, candidate: NativeConstructorCandidate) -> bool {
        self.owner == candidate.owner
            && self.call_site == candidate.call_site
            && self.kind == candidate.kind
    }
}

impl NativeConstructorCandidate {
    pub(super) fn kind(self) -> NativeConstructorKind {
        self.kind
    }

    pub(super) fn matches_dispatch(self, call_site: CallSite, kind: NativeConstructorKind) -> bool {
        self.call_site == call_site && self.kind == kind
    }
}

/// Terminal evidence for this source owner only. Never composed through a
/// child ledger, generic Evaluate result, callee outcome or invocation memo.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct CompletedInitialization {
    owner: SourceUnitOrdinal,
}

impl CompletedInitialization {
    pub(crate) fn covers(&self, owner: SourceUnitOrdinal) -> bool {
        self.owner == owner
    }
}

fn refusal_for_non_variable_owner(item: &ModuleItem) -> OwnerInitializationCoverageRefusalReason {
    if matches!(
        item,
        ModuleItem::ModuleDecl(
            ModuleDecl::ExportDecl(_)
                | ModuleDecl::ExportNamed(_)
                | ModuleDecl::ExportDefaultDecl(_)
                | ModuleDecl::ExportDefaultExpr(_)
                | ModuleDecl::ExportAll(_)
                | ModuleDecl::TsExportAssignment(_)
                | ModuleDecl::TsNamespaceExport(_)
        )
    ) {
        OwnerInitializationCoverageRefusalReason::ExportedOwner
    } else {
        OwnerInitializationCoverageRefusalReason::UnsupportedOwnerKind
    }
}

fn same_simple_binding(left: &Pat, right: &Pat) -> bool {
    let (Pat::Ident(left), Pat::Ident(right)) = (left, right) else {
        return false;
    };
    left.id.to_id() == right.id.to_id()
        && left.id.span == right.id.span
        && left.id.optional == right.id.optional
        && left.type_ann.as_ref().map(|annotation| annotation.span())
            == right.type_ann.as_ref().map(|annotation| annotation.span())
}

fn original_declarator<'source>(
    item: &TopLevelItemView<'source>,
) -> Result<(&'source VarDecl, &'source VarDeclarator), OwnerInitializationCoverageRefusalReason> {
    match item {
        TopLevelItemView::Borrowed(item) => {
            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = item else {
                return Err(refusal_for_non_variable_owner(item));
            };
            let [declarator] = declaration.decls.as_slice() else {
                return Err(OwnerInitializationCoverageRefusalReason::SplitDeclaration);
            };
            Ok((declaration, declarator))
        }
        TopLevelItemView::Owned { item, provenance } => original_split_declarator(item, provenance),
    }
}

fn original_split_declarator<'source>(
    view: &ModuleItem,
    provenance: &SplitDeclaratorProvenance<'source>,
) -> Result<(&'source VarDecl, &'source VarDeclarator), OwnerInitializationCoverageRefusalReason> {
    use OwnerInitializationCoverageRefusalReason::SplitDeclaration;

    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(original))) = provenance.parent_item else {
        return Err(SplitDeclaration);
    };
    if original.decls.len() <= 1 {
        return Err(SplitDeclaration);
    }
    let Some(declarator) = original.decls.get(provenance.declarator_index) else {
        return Err(SplitDeclaration);
    };
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(single))) = view else {
        return Err(SplitDeclaration);
    };
    let [single_declarator] = single.decls.as_slice() else {
        return Err(SplitDeclaration);
    };
    let same_initializer_span = single_declarator.init.as_ref().map(|init| init.span())
        == declarator.init.as_ref().map(|init| init.span());
    if single.kind != original.kind
        || single.ctxt != original.ctxt
        || single.declare != original.declare
        || single_declarator.span != declarator.span
        || !same_simple_binding(&single_declarator.name, &declarator.name)
        || !same_initializer_span
    {
        return Err(SplitDeclaration);
    }
    Ok((original, declarator))
}

fn ordinary_parameter(parameter: &Pat) -> bool {
    matches!(parameter, Pat::Ident(binding)
        if binding.type_ann.is_none() && !binding.id.optional)
}

fn javascript_parameter(parameter: &Pat) -> bool {
    match parameter {
        Pat::Ident(_) => ordinary_parameter(parameter),
        Pat::Array(array) => {
            array.type_ann.is_none()
                && !array.optional
                && array.elems.iter().flatten().all(javascript_parameter)
        }
        Pat::Object(object) => {
            object.type_ann.is_none()
                && !object.optional
                && object.props.iter().all(|property| match property {
                    ObjectPatProp::KeyValue(property) => javascript_parameter(&property.value),
                    ObjectPatProp::Assign(property) => {
                        property.key.type_ann.is_none() && !property.key.id.optional
                    }
                    ObjectPatProp::Rest(rest) => {
                        rest.type_ann.is_none() && javascript_parameter(&rest.arg)
                    }
                })
        }
        Pat::Assign(assignment) => javascript_parameter(&assignment.left),
        Pat::Rest(rest) => rest.type_ann.is_none() && javascript_parameter(&rest.arg),
        Pat::Expr(_) | Pat::Invalid(_) => false,
    }
}

fn plain_class_member(member: &ClassMember) -> bool {
    match member {
        ClassMember::Method(method) => {
            method.kind == MethodKind::Method
                && !matches!(method.key, PropName::Computed(_))
                && method.function.decorators.is_empty()
                && method.function.body.is_some()
                && method.function.type_params.is_none()
                && method.function.return_type.is_none()
                && method.function.params.iter().all(|parameter| {
                    parameter.decorators.is_empty() && ordinary_parameter(&parameter.pat)
                })
                && !method.is_abstract
                && !method.is_optional
                && !method.is_override
        }
        ClassMember::Constructor(constructor) => {
            !matches!(constructor.key, PropName::Computed(_))
                && constructor.body.is_some()
                && constructor.accessibility.is_none()
                && !constructor.is_optional
                && constructor.params.iter().all(|parameter| match parameter {
                    ParamOrTsParamProp::Param(parameter) => {
                        parameter.decorators.is_empty() && ordinary_parameter(&parameter.pat)
                    }
                    ParamOrTsParamProp::TsParamProp(_) => false,
                })
        }
        ClassMember::ClassProp(property) => {
            !property.is_static
                && !matches!(property.key, PropName::Computed(_))
                && property.decorators.is_empty()
                && property.type_ann.is_none()
                && !property.is_abstract
                && !property.is_optional
                && !property.is_override
                && !property.declare
                && !property.definite
        }
        ClassMember::Empty(_) => true,
        ClassMember::PrivateMethod(_)
        | ClassMember::PrivateProp(_)
        | ClassMember::TsIndexSignature(_)
        | ClassMember::StaticBlock(_)
        | ClassMember::AutoAccessor(_) => false,
    }
}

/// This subset only creates a fresh constructor, prototype, and method
/// functions, then publishes the class through its lexical binding. With no
/// heritage, decorators, computed keys, or static initialization, class
/// evaluation runs no user code and cannot expose those fresh objects before
/// binding initialization. Method bodies and instance field values stay lazy.
fn inert_class_declaration(declaration: &ClassDecl) -> bool {
    let class = &declaration.class;
    !declaration.declare
        && class.decorators.is_empty()
        && class.super_class.is_none()
        && !class.is_abstract
        && class.type_params.is_none()
        && class.super_type_params.is_none()
        && class.implements.is_empty()
        && class.body.iter().all(plain_class_member)
}

/// Static data fields and dense array elements use CreateDataProperty, without
/// invoking receiver accessors. Retain every declarative-cell read, including
/// overwritten duplicate fields. No value-kind or heap-locality premise is needed.
fn allocation_inputs(expression: &Expr, reads: &mut BTreeSet<Id>) -> Option<()> {
    match expression.unwrap_parens() {
        Expr::Ident(binding) => {
            reads.insert(binding.to_id());
        }
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_)) => {}
        Expr::Object(record) => {
            for property in &record.props {
                let PropOrSpread::Prop(property) = property else {
                    return None;
                };
                match property.as_ref() {
                    Prop::Shorthand(binding) => {
                        reads.insert(binding.to_id());
                    }
                    Prop::KeyValue(property) => {
                        let name = match &property.key {
                            PropName::Ident(name) => name.sym.as_ref(),
                            PropName::Str(name) => name.value.as_str()?,
                            _ => return None,
                        };
                        // A colon-form __proto__ field changes the prototype
                        // rather than creating an own data property.
                        if name == "__proto__" {
                            return None;
                        }
                        allocation_inputs(&property.value, reads)?;
                    }
                    _ => return None,
                }
            }
        }
        Expr::Array(array) => {
            for element in &array.elems {
                let element = element.as_ref()?;
                if element.spread.is_some() {
                    return None;
                }
                allocation_inputs(&element.expr, reads)?;
            }
        }
        _ => return None,
    }
    Some(())
}

fn empty_native_constructor(expression: &Expr) -> Option<(Id, CallSite, NativeConstructorKind)> {
    let Expr::New(construction) = expression.unwrap_parens() else {
        return None;
    };
    if construction.type_args.is_some()
        || construction
            .args
            .as_ref()
            .is_some_and(|arguments| !arguments.is_empty())
    {
        return None;
    }
    let Expr::Ident(callee) = construction.callee.unwrap_parens() else {
        return None;
    };
    let kind = match callee.sym.as_ref() {
        "Map" => NativeConstructorKind::Map,
        "Set" => NativeConstructorKind::Set,
        "TextEncoder" => NativeConstructorKind::TextEncoder,
        _ => return None,
    };
    Some((callee.to_id(), CallSite::from(construction.span), kind))
}

impl OriginalInitializer {
    pub(super) fn collect(
        item: &TopLevelItemView<'_>,
        owner: SourceUnitOrdinal,
    ) -> Result<Self, OwnerInitializationCoverageRefusalReason> {
        if let TopLevelItemView::Borrowed(ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration)))) =
            item
        {
            if !inert_class_declaration(declaration) {
                return Err(OwnerInitializationCoverageRefusalReason::UnsupportedOwnerKind);
            }
            return Ok(Self {
                owner,
                binding: declaration.ident.to_id(),
                initializer_span: declaration.class.span,
                leaf: Leaf::Class(declaration.class.span.lo.0),
            });
        }
        // For split views, recover only the exact original child named by the
        // canonical view's parent/index provenance. The child's own evaluation
        // remains one source-unit owner; sibling initializers are separate
        // owners and keep separate effect records.
        let (declaration, declarator) = original_declarator(item)?;
        let Pat::Ident(binding) = &declarator.name else {
            return Err(OwnerInitializationCoverageRefusalReason::UnsupportedBindingShape);
        };
        if declaration.declare || declarator.definite || !ordinary_parameter(&declarator.name) {
            return Err(OwnerInitializationCoverageRefusalReason::UnsupportedBindingShape);
        }
        let Some(initializer) = declarator.init.as_deref() else {
            return Err(OwnerInitializationCoverageRefusalReason::MissingInitializer);
        };
        let leaf = match initializer.unwrap_parens() {
            Expr::New(_) => {
                let Some((callee, call_site, kind)) = empty_native_constructor(initializer) else {
                    return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
                };
                Leaf::NativeConstructor {
                    callee,
                    call_site,
                    kind,
                }
            }
            Expr::Ident(binding) => Leaf::Binding(binding.to_id()),
            Expr::Lit(Lit::Regex(regex)) => {
                // ECMA-262 evaluates a validated literal through RegExpCreate
                // with its source text; this does not resolve the mutable
                // global RegExp binding or invoke source-defined code. Later
                // reads and calls on the RegExp remain outside this proof.
                Leaf::Allocation {
                    allocation: regex.span.lo.0,
                    reads: BTreeSet::new(),
                }
            }
            Expr::Object(_) | Expr::Array(_) => {
                let mut reads = BTreeSet::new();
                allocation_inputs(initializer, &mut reads)
                    .ok_or(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer)?;
                Leaf::Allocation {
                    allocation: initializer.unwrap_parens().span().lo.0,
                    reads,
                }
            }
            Expr::Lit(literal) => Leaf::Primitive(match literal {
                Lit::Str(_) => PrimKinds::STRING,
                Lit::Num(_) => PrimKinds::NUMBER,
                Lit::Bool(_) => PrimKinds::BOOLEAN,
                Lit::Null(_) => PrimKinds::NULL,
                Lit::BigInt(_) => PrimKinds::BIGINT,
                _ => {
                    return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
                }
            }),
            Expr::Fn(expression)
                if expression.function.body.is_some()
                    && expression.function.type_params.is_none()
                    && expression.function.return_type.is_none()
                    && expression.function.decorators.is_empty()
                    && expression.function.params.iter().all(|parameter| {
                        parameter.decorators.is_empty() && javascript_parameter(&parameter.pat)
                    }) =>
            {
                // Parameters, including defaults and destructuring, execute
                // only on invocation. Async and generator bodies are lazy too.
                Leaf::Function(expression.function.span.lo.0)
            }
            Expr::Arrow(expression)
                if expression.type_params.is_none()
                    && expression.return_type.is_none()
                    && expression.params.iter().all(javascript_parameter) =>
            {
                Leaf::Function(expression.span.lo.0)
            }
            _ => {
                return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
            }
        };
        Ok(Self {
            owner,
            binding: binding.id.to_id(),
            initializer_span: initializer.span(),
            leaf,
        })
    }

    pub(super) fn matches_binding(&self, owner: SourceUnitOrdinal, binding: &Id) -> bool {
        self.owner == owner && self.binding == *binding
    }

    pub(super) fn reads_are_initialized(&self, mut initialized: impl FnMut(&Id) -> bool) -> bool {
        match &self.leaf {
            Leaf::Binding(binding) => initialized(binding),
            Leaf::Allocation { reads, .. } => reads.iter().all(initialized),
            Leaf::Primitive(_)
            | Leaf::Function(_)
            | Leaf::Class(_)
            | Leaf::NativeConstructor { .. } => true,
        }
    }

    pub(super) fn native_constructor_candidate(
        &self,
        expression: &ValueExpr,
    ) -> Option<NativeConstructorCandidate> {
        let Leaf::NativeConstructor {
            callee: expected_callee,
            call_site,
            kind,
        } = &self.leaf
        else {
            return None;
        };
        let ValueExpr::New(callee, arguments, evaluated_site) = expression else {
            return None;
        };
        if *call_site != *evaluated_site
            || !arguments.is_empty()
            || !matches!(callee.as_ref(), ValueExpr::Binding(binding) if binding == expected_callee)
        {
            return None;
        }
        Some(NativeConstructorCandidate {
            owner: self.owner,
            call_site: *call_site,
            kind: *kind,
        })
    }

    /// The dedicated root Evaluate transfer must actually finish with its
    /// expected value. Every source cell read must be initialized before that
    /// transfer; imported and global reads cannot supply this proof.
    /// Generic lowering or an empty effect ledger is no proof.
    pub(super) fn evaluated(
        &self,
        expression: &ValueExpr,
        values: &Values,
        initialized_reads: bool,
        native_constructor_transfer: Option<NativeConstructorTransfer>,
    ) -> Option<EvaluatedInitializer> {
        if values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable)) {
            return None;
        }
        let matches = match (&self.leaf, expression, values.first()?) {
            (Leaf::Binding(expected), ValueExpr::Binding(lowered), _) => {
                expected == lowered && initialized_reads
            }
            (Leaf::NativeConstructor { .. }, _, AbstractValue::Value(Value::Top)) => self
                .native_constructor_candidate(expression)
                .zip(native_constructor_transfer)
                .is_some_and(|(candidate, transfer)| {
                    values.len() == 1 && transfer.matches(candidate)
                }),
            (
                Leaf::Allocation { allocation, .. },
                ValueExpr::ObjectLiteral(lowered, _),
                AbstractValue::Value(Value::Alloc(Kind::Object, evaluated)),
            ) => {
                values.len() == 1
                    && allocation == lowered
                    && allocation == evaluated
                    && initialized_reads
            }
            (
                Leaf::Class(expected),
                ValueExpr::Alloc(Kind::Class, lowered),
                AbstractValue::Value(Value::Alloc(Kind::Class, evaluated)),
            ) => {
                values.len() == 1
                    && expected == lowered
                    && expected == evaluated
                    && initialized_reads
            }
            (
                Leaf::Primitive(expected),
                ValueExpr::NonCallable(lowered),
                AbstractValue::Value(Value::Prim(evaluated)),
            ) => values.len() == 1 && expected == lowered && expected == evaluated,
            (
                Leaf::Function(expected),
                ValueExpr::Alloc(Kind::Function, lowered),
                AbstractValue::Closure(evaluated, _),
            ) => values.len() == 1 && expected == lowered && expected == evaluated,
            _ => false,
        };
        matches.then_some(EvaluatedInitializer {
            owner: self.owner,
            initializer_span: self.initializer_span,
        })
    }

    pub(super) fn complete(
        &self,
        evaluated: EvaluatedInitializer,
        owner: SourceUnitOrdinal,
        initialized: &BTreeSet<Id>,
    ) -> Option<CompletedInitialization> {
        (self.owner == owner
            && evaluated.owner == owner
            && evaluated.initializer_span == self.initializer_span
            && initialized.contains(&self.binding))
        .then_some(CompletedInitialization { owner })
    }
}
