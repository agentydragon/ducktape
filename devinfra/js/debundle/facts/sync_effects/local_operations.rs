//! Source-covered local operations collected before value lowering erases writes.

use super::effect_coverage::{
    EffectCoverageFragment, LocalBodyIdentity, OperationStep, RootEffectIdentity,
    SourceOperationSite,
};
use super::property_inference::DescriptorAttributes;
use super::value_model::{
    CallSite, LiteralPropertyKind, PropertyDefinition, ValueExpr, object_data_property_name,
    static_property_name,
};
use crate::analysis_inputs::ForeignInvocation;
use crate::facts::abstract_domain::value::Kind;
use analysis_model::PrimKinds;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum LocalBinding {
    Parameter,
    Var,
    Let,
    Const,
    /// Created on entry to its handler, rather than in the activation prologue.
    CatchParameter,
    /// These activation values must already be established by the caller.
    /// Source identity alone does not instantiate a function value.
    NamedSelf(u32),
    HoistedFunction(u32),
}

#[derive(Debug, Clone)]
pub(super) enum LocalCallee {
    Binding(Id),
    /// Evaluated through the same creation transfer as a standalone closure,
    /// before any argument. Source identity supplies no invocation coverage.
    Creation(DeferredCallableCreation),
    Member {
        receiver: Arc<LocalExpression>,
        property: String,
        span: Span,
        /// The receiver itself may short-circuit before member lookup. This is
        /// independent from `LocalInvocation::OptionalCall`, which guards the
        /// selected member value after GetValue.
        optional_receiver: bool,
    },
}

impl LocalCallee {
    fn lower(expression: &Expr, class_template: bool) -> Option<Self> {
        // Parentheses terminate an optional chain. In `(object?.method)()`
        // the outer call is still required even when the member read skipped.
        // This callee protocol cannot yet represent that detached reference.
        if matches!(expression, Expr::Paren(_))
            && matches!(expression.unwrap_parens(), Expr::OptChain(_))
        {
            return None;
        }
        match expression.unwrap_parens() {
            // A direct eval reference can enter dynamic scope.
            Expr::Ident(binding) if binding.sym != "eval" => Some(Self::Binding(binding.to_id())),
            Expr::Fn(_) | Expr::Arrow(_) => {
                let lowered = LocalExpression::lower(expression, class_template)?;
                let LocalExpression::Closure(creation) = lowered.without_source() else {
                    return None;
                };
                Some(Self::Creation(*creation))
            }
            Expr::Member(member) => {
                let MemberProp::Ident(property) = &member.prop else {
                    return None;
                };
                Some(Self::Member {
                    receiver: Arc::new(LocalExpression::lower(&member.obj, class_template)?),
                    property: property.sym.to_string(),
                    span: member.span,
                    optional_receiver: false,
                })
            }
            Expr::OptChain(chain) => Self::optional_member(chain, class_template),
            _ => None,
        }
    }

    fn optional_member(chain: &OptChainExpr, class_template: bool) -> Option<Self> {
        if !chain.optional {
            return None;
        }
        let OptChainBase::Member(member) = chain.base.as_ref() else {
            return None;
        };
        let MemberProp::Ident(property) = &member.prop else {
            return None;
        };
        Some(Self::Member {
            receiver: Arc::new(LocalExpression::lower(&member.obj, class_template)?),
            property: property.sym.to_string(),
            span: member.span,
            optional_receiver: true,
        })
    }
}

#[derive(Debug, Clone)]
pub(super) enum LocalInvocation {
    Call,
    OptionalCall,
    Construct,
}

impl LocalInvocation {
    pub(super) fn foreign_invocation(&self) -> ForeignInvocation {
        match self {
            Self::Call | Self::OptionalCall => ForeignInvocation::Call,
            Self::Construct => ForeignInvocation::Construct,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct LocalCall {
    pub(super) invocation: LocalInvocation,
    pub(super) callee: LocalCallee,
    pub(super) arguments: Arc<[LocalExpression]>,
    /// Positions expanded by the shared iterable transfer after each source
    /// expression has been evaluated once.
    pub(super) spread_arguments: BTreeMap<usize, Span>,
    /// Canonical sub-sites for spread protocol operations, indexed by source
    /// argument position because one call may contain multiple spreads.
    pub(super) spread_sites: BTreeMap<usize, SourceOperationSite>,
    pub(super) span: Span,
}

impl LocalCall {
    /// Bounded optional calls support direct bindings and static local member
    /// calls. The member receiver's optional chain, when present, is represented
    /// by `LocalCallee::Member::optional_receiver`; `chain.optional` remains
    /// the independent optional-callee check.
    pub(super) fn optional_callee(
        chain: &OptChainExpr,
        class_template: bool,
    ) -> Option<LocalCallee> {
        let OptChainBase::Call(call) = chain.base.as_ref() else {
            return None;
        };
        if call.args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let callee = match call.callee.unwrap_parens() {
            Expr::Ident(_) | Expr::Member(_) | Expr::OptChain(_) => {
                LocalCallee::lower(&call.callee, class_template)?
            }
            // Keep optional invocation of arbitrary expressions outside this
            // bounded source protocol until its callable-selection ABI exists.
            _ => return None,
        };
        // SWC represents `object?.method()` as an ordinary invocation segment
        // of a receiver-optional chain. Only `method?.()` tests the callee.
        (chain.optional
            || matches!(
                &callee,
                LocalCallee::Member {
                    optional_receiver: true,
                    ..
                }
            ))
        .then_some(callee)
    }
}

/// Home-object sites express source containment, never receiver ownership.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum DeferredCallableCreation {
    Function(u32),
    Arrow(u32),
    Method { callable: u32, home_object: u32 },
    Getter { callable: u32, home_object: u32 },
    Setter { callable: u32, home_object: u32 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum LocalLogicalOperator {
    And,
    Or,
    NullishCoalescing,
}

impl DeferredCallableCreation {
    /// Creating a source-checked callable defers its body and parameter
    /// defaults. Capture availability, retention and later invocation are
    /// separate consumer obligations.
    pub(super) fn effect_coverage(self) -> EffectCoverageFragment {
        match self {
            Self::Function(_)
            | Self::Arrow(_)
            | Self::Method { .. }
            | Self::Getter { .. }
            | Self::Setter { .. } => EffectCoverageFragment::no_effects(),
        }
    }

    pub(super) fn callable(self) -> u32 {
        match self {
            Self::Function(key) | Self::Arrow(key) => key,
            Self::Method { callable, .. }
            | Self::Getter { callable, .. }
            | Self::Setter { callable, .. } => callable,
        }
    }
    pub(super) fn home_object(self) -> Option<u32> {
        match self {
            Self::Function(_) | Self::Arrow(_) => None,
            Self::Method { home_object, .. }
            | Self::Getter { home_object, .. }
            | Self::Setter { home_object, .. } => Some(home_object),
        }
    }
}

#[derive(Debug, Clone)]
pub(super) enum LocalExpression {
    /// Source identity attached by canonical lowering; `site` is filled only
    /// when the owner/body identity is known.
    Sourced {
        span: Span,
        step: OperationStep,
        site: Option<SourceOperationSite>,
        substeps: BTreeMap<OperationStep, SourceOperationSite>,
        expression: Box<Self>,
    },
    Binary {
        operator: BinaryOp,
        left: Box<Self>,
        right: Box<Self>,
        span: Span,
    },
    Conditional {
        test: Box<Self>,
        consequent: Box<Self>,
        alternate: Box<Self>,
    },
    Logical {
        op: LocalLogicalOperator,
        left: Box<Self>,
        right: Box<Self>,
    },
    /// Each substitution is evaluated and converted before the next one.
    Template {
        substitutions: Arc<[Self]>,
        span: Span,
    },

    Value(ValueExpr),
    /// A canonical primitive literal has no coercion or user-code effect.
    PrimitiveLiteral(PrimKinds),
    /// The fresh receiver supplied by a checked constructor query.
    This,
    Sequence(Arc<[Self]>),
    Record {
        allocation: u32,
        properties: Arc<[PropertyDefinition<Self>]>,
    },
    Array {
        allocation: u32,
        elements: Arc<[LocalArrayElement]>,
    },
    /// One direct Set construction from the exact source array argument. The
    /// ordered consumer evaluates the array once, then applies the shared
    /// native iterator/constructor transfer to that value.
    SetConstruction {
        array: Box<Self>,
        source: ValueExpr,
        site: CallSite,
    },
    Assign {
        binding: Id,
        value: Arc<Self>,
        span: Span,
    },
    /// Prefix/postfix increment/decrement share read, ToNumeric and PutValue
    /// effects. This scalar domain tracks numeric kinds, not the old/new value.
    UpdateBinding {
        binding: Id,
        span: Span,
    },
    Closure(DeferredCallableCreation),
    ClassDefinition(ClassDefinition),
    Call(LocalCall),
    ReadField {
        receiver: Arc<LocalExpression>,
        property: String,
        span: Span,
        optional_receiver: bool,
    },
    AssignField {
        receiver: Id,
        property: String,
        value: Arc<Self>,
        span: Span,
    },
    /// Own public data definition or private-slot installation, distinct from
    /// assignment to an already installed private slot.
    DefineThisField {
        property: ClassFieldKey,
        value: Arc<Self>,
    },
    AssignPrivateThisField {
        property: String,
        value: Arc<Self>,
    },
    AssignPublicThisField {
        property: String,
        value: Arc<Self>,
    },
}

#[derive(Debug, Clone)]
pub(super) enum ClassFieldKey {
    Public(String),
    Private(String),
}

/// Canonical class creation, independent of deferred constructor/field bodies.
/// Heritage, decorators, computed names and unsupported static elements are
/// excluded before this operation reaches an interpreter.
#[derive(Debug, Clone)]
pub(super) struct ClassDefinition {
    pub(super) class: u32,
    pub(super) span: Span,
    /// The class declaration's outer binding identity. Class-definition
    /// evaluation uses a separate inner name environment; until that
    /// environment is represented, consumers must refuse an unshadowed
    /// self-reference instead of reading the outer TDZ cell early.
    pub(super) declaration_binding: Option<Id>,
    /// Static field definitions and blocks in source order. Computed keys,
    /// heritage, decorators and unsupported block control flow reject the
    /// complete plan before any class operation can execute.
    pub(super) static_steps: Arc<[ClassStaticStep]>,
    /// Public static methods/accessors are installed before static elements.
    /// Keep their actual descriptor attributes so DefineField can distinguish
    /// replacing a configurable member from a protected built-in property.
    pub(super) static_member_descriptors: BTreeMap<String, DescriptorAttributes>,
}

#[derive(Debug, Clone)]
pub(super) enum ClassStaticStep {
    Field {
        property: String,
        value: LocalExpression,
    },
    Block {
        expressions: Arc<[LocalExpression]>,
    },
}

impl ClassDefinition {
    pub(super) fn lower(class: &Class) -> Option<Self> {
        if class.super_class.is_some()
            || !class.decorators.is_empty()
            || class.is_abstract
            || class.type_params.is_some()
            || class.super_type_params.is_some()
            || !class.implements.is_empty()
        {
            return None;
        }
        let mut constructor = false;
        let mut static_steps = Vec::new();
        let mut static_member_descriptors = BTreeMap::new();
        for member in &class.body {
            let covered = match member {
                ClassMember::Constructor(body) if !constructor => {
                    constructor = true;
                    !matches!(body.key, PropName::Computed(_))
                        && body.params.iter().all(|parameter| {
                            matches!(parameter,
                        ParamOrTsParamProp::Param(parameter) if parameter.decorators.is_empty())
                        })
                }
                ClassMember::Method(method) => {
                    let covered = method.function.decorators.is_empty()
                        && method
                            .function
                            .params
                            .iter()
                            .all(|parameter| parameter.decorators.is_empty())
                        && !matches!(method.key, PropName::Computed(_));
                    if covered && method.is_static {
                        let name = static_property_name(&method.key)?;
                        if name == "prototype" {
                            return None;
                        }
                        let attributes = match method.kind {
                            MethodKind::Method => DescriptorAttributes {
                                writable: Some(true),
                                enumerable: Some(false),
                                configurable: Some(true),
                            },
                            MethodKind::Getter | MethodKind::Setter => DescriptorAttributes {
                                writable: None,
                                enumerable: Some(false),
                                configurable: Some(true),
                            },
                        };
                        static_member_descriptors.insert(name, attributes);
                    }
                    covered
                }
                ClassMember::PrivateMethod(method) => {
                    !method.is_static
                        && method.function.decorators.is_empty()
                        && method
                            .function
                            .params
                            .iter()
                            .all(|parameter| parameter.decorators.is_empty())
                }
                ClassMember::ClassProp(field) => {
                    if !field.decorators.is_empty() || matches!(field.key, PropName::Computed(_)) {
                        false
                    } else if field.is_static {
                        let property = static_property_name(&field.key)?;
                        // A static class element named prototype is an early
                        // syntax restriction. It cannot become a runtime
                        // DefineField attempt on the nonconfigurable slot.
                        if property == "prototype" {
                            return None;
                        }
                        let value = LocalExpression::sourced(
                            field.span,
                            OperationStep::DefineField,
                            match field.value.as_deref() {
                                Some(value) => LocalExpression::lower(value, true)?,
                                None => LocalExpression::PrimitiveLiteral(PrimKinds::UNDEFINED),
                            },
                        );
                        if value.uses_constructor_receiver_operation() {
                            return None;
                        }
                        static_steps.push(ClassStaticStep::Field { property, value });
                        true
                    } else {
                        true
                    }
                }
                ClassMember::PrivateProp(field) => !field.is_static && field.decorators.is_empty(),
                ClassMember::StaticBlock(block) => {
                    let mut expressions = Vec::new();
                    for statement in &block.body.stmts {
                        match statement {
                            Stmt::Empty(_) => {}
                            Stmt::Expr(statement) => {
                                let expression = LocalExpression::sourced(
                                    statement.span(),
                                    OperationStep::Statement,
                                    LocalExpression::lower(&statement.expr, true)?,
                                );
                                if expression.uses_constructor_receiver_operation() {
                                    return None;
                                }
                                expressions.push(expression);
                            }
                            // This bounded class plan supports straight-line
                            // expression transfers only. Lexical/control-flow
                            // forms need their existing region environment and
                            // are refused before any static element executes.
                            _ => return None,
                        }
                    }
                    static_steps.push(ClassStaticStep::Block {
                        expressions: expressions.into(),
                    });
                    true
                }
                ClassMember::Empty(_) => true,
                _ => false,
            };
            if !covered {
                return None;
            }
        }
        Some(Self {
            class: class.span.lo.0,
            span: class.span,
            declaration_binding: None,
            static_steps: static_steps.into(),
            static_member_descriptors,
        })
    }

    pub(super) fn with_declaration_binding(mut self, binding: Id) -> Self {
        self.declaration_binding = Some(binding);
        self
    }

    fn bind_source_sites(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        binder: &mut SourceOperationSiteBinding<'_>,
    ) -> Option<()> {
        for step in Arc::make_mut(&mut self.static_steps) {
            match step {
                ClassStaticStep::Field { value, .. } => {
                    value.bind_source_sites(owner, body, binder)?;
                }
                ClassStaticStep::Block { expressions, .. } => {
                    for expression in Arc::make_mut(expressions) {
                        expression.bind_source_sites(owner, body, binder)?;
                    }
                }
            }
        }
        Some(())
    }

    fn reads(&self, reads: &mut BTreeSet<Id>) {
        for step in self.static_steps.iter() {
            match step {
                ClassStaticStep::Field { value, .. } => value.reads(reads),
                ClassStaticStep::Block { expressions, .. } => {
                    for expression in expressions.iter() {
                        expression.reads(reads);
                    }
                }
            }
        }
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        for step in self.static_steps.iter() {
            match step {
                ClassStaticStep::Field { value, .. } => value.writes(writes),
                ClassStaticStep::Block { expressions, .. } => {
                    for expression in expressions.iter() {
                        expression.writes(writes);
                    }
                }
            }
        }
    }

    fn allocation_syntax(
        &self,
        bindings: &BTreeMap<Id, LocalBinding>,
        closure_sites: &mut BTreeSet<u32>,
    ) -> (bool, bool) {
        let mut closure_allowed = true;
        for step in self.static_steps.iter() {
            let (allocation, closure) = match step {
                ClassStaticStep::Field { value, .. } => {
                    value.allocation_syntax(bindings, closure_sites)
                }
                ClassStaticStep::Block { expressions, .. } => {
                    let mut allocation = true;
                    let mut closure = true;
                    for expression in expressions.iter() {
                        let (next_allocation, next_closure) =
                            expression.allocation_syntax(bindings, closure_sites);
                        allocation &= next_allocation;
                        closure &= next_closure;
                    }
                    (allocation, closure)
                }
            };
            closure_allowed &= closure;
            // Any eager static step is a distinct operation from class-object
            // allocation and cannot be represented by an allocation-only
            // candidate.
            if !allocation {
                closure_allowed = false;
            }
        }
        (self.static_steps.is_empty(), closure_allowed)
    }
}

#[derive(Debug, Clone)]
pub(super) struct ClassConstructorBody {
    pub(super) class: u32,
    pub(super) span: Span,
    /// Canonical activation sites for instance initializers and the body.
    pub(super) callables: Arc<[u32]>,
    pub(super) initializers: LocalBody,
    pub(super) constructor: LocalBody,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ConstructorPhase {
    Initializers,
    Body,
}

#[derive(Debug, Clone)]
pub(super) enum LocalArrayElement {
    Hole,
    Value(LocalExpression),
    Spread { value: LocalExpression, span: Span },
}

#[derive(Debug, Clone)]
pub(super) enum LocalOperation {
    /// Canonical source authority attached during lowering. Return and throw
    /// spans live here rather than being duplicated in their payloads.
    Sourced {
        span: Span,
        step: OperationStep,
        site: Option<SourceOperationSite>,
        operation: Box<Self>,
    },
    Initialize(Id, Option<LocalExpression>),
    InitializeObject {
        pattern: ObjectBindingPattern,
        value: LocalExpression,
    },
    Evaluate(LocalExpression),
    Return(Option<LocalExpression>),
    Throw(LocalExpression),
    If {
        test: LocalExpression,
        consequent: LocalRegion,
        alternate: Option<LocalRegion>,
    },
    TryCatch {
        body: LocalRegion,
        binding: Option<Id>,
        handler: LocalRegion,
    },
}

enum SourceOperationSiteBinding<'sites> {
    Sequential {
        ordinal: u32,
        sites: BTreeSet<SourceOperationSite>,
    },
    Registry {
        canonical: &'sites BTreeSet<SourceOperationSite>,
        sites: BTreeSet<SourceOperationSite>,
    },
}

impl SourceOperationSiteBinding<'_> {
    fn bind(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        span: Span,
        step: OperationStep,
    ) -> Option<SourceOperationSite> {
        self.bind_with_control_test(owner, body, span, step, None)
    }

    fn bind_branch_region(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        span: Span,
        step: OperationStep,
        control_test: &SourceOperationSite,
    ) -> Option<SourceOperationSite> {
        if !matches!(
            step,
            OperationStep::IfConsequent | OperationStep::IfAlternate
        ) || control_test.owner != owner
            || control_test.body != *body
            || control_test.step != OperationStep::IfTest
            || control_test.control_test_ordinal.is_some()
        {
            return None;
        }
        self.bind_with_control_test(owner, body, span, step, Some(control_test.ordinal))
    }

    fn bind_with_control_test(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        span: Span,
        step: OperationStep,
        control_test_ordinal: Option<u32>,
    ) -> Option<SourceOperationSite> {
        match self {
            Self::Sequential { ordinal, sites } => {
                let site = SourceOperationSite {
                    owner,
                    body: body.clone(),
                    span,
                    ordinal: *ordinal,
                    step,
                    control_test_ordinal,
                };
                *ordinal = ordinal.saturating_add(1);
                sites.insert(site.clone());
                Some(site)
            }
            Self::Registry { canonical, sites } => {
                let mut matches = canonical.iter().filter(|site| {
                    site.owner == owner
                        && site.body == *body
                        && site.span == span
                        && site.step == step
                        && site.control_test_ordinal == control_test_ordinal
                });
                let site = matches.next()?.clone();
                if matches.next().is_some() {
                    return None;
                }
                if !sites.insert(site.clone()) {
                    return None;
                }
                Some(site)
            }
        }
    }

    fn bind_substep(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        span: Span,
        ordinal: u32,
        step: OperationStep,
    ) -> Option<SourceOperationSite> {
        match self {
            Self::Sequential { sites, .. } => {
                let site = SourceOperationSite {
                    owner,
                    body: body.clone(),
                    span,
                    ordinal,
                    step,
                    control_test_ordinal: None,
                };
                sites.insert(site.clone());
                Some(site)
            }
            Self::Registry { canonical, sites } => {
                let mut matches = canonical.iter().filter(|site| {
                    site.owner == owner
                        && site.body == *body
                        && site.span == span
                        && site.step == step
                        && site.control_test_ordinal.is_none()
                });
                let site = matches.next()?.clone();
                if matches.next().is_some() {
                    return None;
                }
                if !sites.insert(site.clone()) {
                    return None;
                }
                Some(site)
            }
        }
    }

    fn ordinal(&self) -> u32 {
        match self {
            Self::Sequential { ordinal, .. } => *ordinal,
            Self::Registry { .. } => 0,
        }
    }

    fn sites(&self) -> &BTreeSet<SourceOperationSite> {
        match self {
            Self::Sequential { sites, .. } | Self::Registry { sites, .. } => sites,
        }
    }

    fn into_sites(self) -> BTreeSet<SourceOperationSite> {
        match self {
            Self::Sequential { sites, .. } | Self::Registry { sites, .. } => sites,
        }
    }
}

impl LocalOperation {
    fn sourced(span: Span, step: OperationStep, operation: Self) -> Self {
        Self::Sourced {
            span,
            step,
            site: None,
            operation: Box::new(operation),
        }
    }

    pub(super) fn source_site(&self) -> Option<&SourceOperationSite> {
        match self {
            Self::Sourced { site, .. } => site.as_ref(),
            _ => None,
        }
    }

    pub(super) fn without_source(&self) -> &Self {
        let mut operation = self;
        while let Self::Sourced {
            operation: inner, ..
        } = operation
        {
            operation = inner;
        }
        operation
    }

    fn without_source_mut(&mut self) -> &mut Self {
        let mut operation = self;
        while let Self::Sourced {
            operation: inner, ..
        } = operation
        {
            operation = inner;
        }
        operation
    }

    fn bind_source_sites(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        binder: &mut SourceOperationSiteBinding<'_>,
    ) -> Option<()> {
        if let Self::Sourced {
            span,
            step,
            site,
            operation,
        } = self
        {
            let source_site = binder.bind(owner, body, *span, *step)?;
            *site = Some(source_site);
            operation.bind_source_sites(owner, body, binder)?;
            return Some(());
        }

        match self.without_source_mut() {
            Self::Initialize(_, value) | Self::Return(value) => {
                if let Some(value) = value {
                    value.bind_source_sites(owner, body, binder)?;
                }
            }
            Self::InitializeObject { value, .. } | Self::Evaluate(value) | Self::Throw(value) => {
                value.bind_source_sites(owner, body, binder)?
            }
            Self::If {
                test,
                consequent,
                alternate,
            } => {
                test.bind_source_sites(owner, body, binder)?;
                let test_site = test.source_site_for_step(OperationStep::IfTest)?.clone();
                consequent.bind_source_sites(
                    owner,
                    body,
                    OperationStep::IfConsequent,
                    Some(&test_site),
                    binder,
                )?;
                if let Some(alternate) = alternate {
                    alternate.bind_source_sites(
                        owner,
                        body,
                        OperationStep::IfAlternate,
                        Some(&test_site),
                        binder,
                    )?;
                }
            }
            Self::TryCatch {
                body: region,
                handler,
                ..
            } => {
                region.bind_source_sites(owner, body, OperationStep::TryBody, None, binder)?;
                handler.bind_source_sites(
                    owner,
                    body,
                    OperationStep::CatchHandler,
                    None,
                    binder,
                )?;
            }
            Self::Sourced { .. } => unreachable!("source wrappers are peeled above"),
        }
        Some(())
    }
}

/// Ordered targets of one shallow object binding. Descriptor checks belong to
/// execution against the evaluated RHS, independently of this syntax plan.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct ObjectBindingPattern {
    pub(super) span: Span,
    pub(super) properties: Vec<ObjectBindingProperty>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct ObjectBindingProperty {
    pub(super) property: String,
    pub(super) binding: Id,
    pub(super) span: Span,
}

impl ObjectBindingPattern {
    pub(super) fn to_report(&self) -> Vec<crate::reports::schema::ObjectBindingTarget> {
        self.properties
            .iter()
            .map(|property| crate::reports::schema::ObjectBindingTarget {
                property: property.property.clone(),
                binding: property.binding.0.to_string(),
                span: property.span,
            })
            .collect()
    }

    pub(super) fn collect(pattern: &Pat) -> Option<Self> {
        let Pat::Object(pattern) = pattern else {
            return None;
        };
        if pattern.optional || pattern.type_ann.is_some() || pattern.props.is_empty() {
            return None;
        }
        let mut targets = BTreeSet::new();
        let properties = pattern
            .props
            .iter()
            .map(|property| {
                let (name, binding, span) = match property {
                    ObjectPatProp::KeyValue(property) => {
                        let name = match &property.key {
                            PropName::Ident(name) => name.sym.to_string(),
                            PropName::Str(name) => name.value.to_string_lossy().into_owned(),
                            _ => return None,
                        };
                        (
                            name,
                            simple_binding(&property.value)?,
                            property.value.span(),
                        )
                    }
                    ObjectPatProp::Assign(property)
                        if property.value.is_none()
                            && property.key.type_ann.is_none()
                            && !property.key.id.optional =>
                    {
                        (
                            property.key.id.sym.to_string(),
                            property.key.id.to_id(),
                            property.key.span(),
                        )
                    }
                    _ => return None,
                };
                if !targets.insert(binding.clone()) {
                    return None;
                }
                Some(ObjectBindingProperty {
                    property: name,
                    binding,
                    span,
                })
            })
            .collect::<Option<Vec<_>>>()?;
        Some(Self {
            span: pattern.span,
            properties,
        })
    }
}

#[derive(Debug, Clone)]
pub(super) struct LocalRegion {
    pub(super) span: Span,
    pub(super) source_site: Option<SourceOperationSite>,
    /// Lexical declarations whose environment exists only while this region
    /// executes. `var` declarations stay in the containing activation.
    pub(super) bindings: BTreeMap<Id, LocalBinding>,
    pub(super) operations: Arc<[LocalOperation]>,
}

impl LocalRegion {
    fn bind_source_sites(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        step: OperationStep,
        control_test: Option<&SourceOperationSite>,
        binder: &mut SourceOperationSiteBinding<'_>,
    ) -> Option<()> {
        self.source_site = Some(match control_test {
            Some(test) => binder.bind_branch_region(owner, body, self.span, step, test)?,
            None => binder.bind(owner, body, self.span, step)?,
        });
        for operation in Arc::make_mut(&mut self.operations) {
            operation.bind_source_sites(owner, body, binder)?;
        }
        Some(())
    }
}

#[derive(Debug, Clone)]
pub(super) struct LocalParameter {
    pub(super) binding: Id,
    pub(super) default: Option<LocalExpression>,
    /// A rest parameter is materialized as a fresh array by the shared
    /// allocation transfer. The source pattern start is its allocation key.
    pub(super) rest_allocation: Option<u32>,
    pub(super) rest_span: Option<Span>,
    pub(super) rest_site: Option<SourceOperationSite>,
}

#[derive(Debug, Clone)]
pub(super) struct OrderedRoot {
    pub(super) identity: RootEffectIdentity,
    pub(super) body: LocalBody,
}

#[derive(Debug, Clone)]
pub(super) struct LocalBody {
    /// Every declaration in this body, including declarations in nested
    /// lexical regions. Operations use this map to distinguish local writes
    /// from captures and to enforce binding mutability.
    pub(super) bindings: BTreeMap<Id, LocalBinding>,
    /// Declarations instantiated with the callable activation. Region-local
    /// `let`/`const` bindings are created at their region entry instead.
    pub(super) activation_bindings: BTreeMap<Id, LocalBinding>,
    /// Lexical ids already assigned to a represented nested region.
    region_binding_ids: BTreeSet<Id>,
    /// Canonical declaration facts can include syntax this ordered lowering
    /// intentionally omitted. Do not execute a partial body if its lexical
    /// environment cannot be assigned to an activation or represented region.
    pub(super) has_unscoped_lexical_bindings: bool,
    pub(super) parameters: Arc<[LocalParameter]>,
    pub(super) operations: Arc<[LocalOperation]>,
    pub(super) source_span: Option<Span>,
    pub(super) source_region_site: Option<SourceOperationSite>,
    /// Present for a binding initializer or expression-statement root lowered
    /// from its exact source expression. Callable bodies have no root-owner span.
    root_expression_span: Option<Span>,
    /// Sorted source reads outside this body's parameters and declarations.
    /// Shared across body clones and reused by every invocation/provider query.
    free_reads: Option<Arc<[Id]>>,
    captured_writes: Option<Arc<[Id]>>,
    allocation_candidate_syntax: bool,
    closure_candidate_syntax: bool,
    closure_sites: Arc<[u32]>,
    /// Live ordered lowering intentionally drops source after a terminal
    /// completion. The narrow provider proof excludes such candidates.
    has_post_terminal_operations: bool,
    /// Source contains a bare empty keyed-collection construction. Provider
    /// read projection treats that native callee separately from captures;
    /// every consumer still proves its actual constructor identity.
    requires_keyed_collection_new: bool,
    source_operation_sites: BTreeSet<SourceOperationSite>,
}

fn is_keyed_collection_new(value: &ValueExpr) -> bool {
    matches!(
        value,
        ValueExpr::New(callee, arguments, _)
            if arguments.is_empty()
                && matches!(callee.as_ref(), ValueExpr::Binding(binding)
                    if matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet"))
    )
}

fn value_contains_keyed_collection_new(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::New(callee, arguments, _) => {
            is_keyed_collection_new(value)
                || value_contains_keyed_collection_new(callee)
                || arguments.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Member(object, ..)
        | ValueExpr::ArrayIteratorPrototype(object)
        | ValueExpr::SetIteratorPrototype(object)
        | ValueExpr::LogicalNot(object)
        | ValueExpr::Void(object) => value_contains_keyed_collection_new(object),
        ValueExpr::Call(callee, arguments, _) => {
            value_contains_keyed_collection_new(callee)
                || arguments.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Primitive(operands)
        | ValueExpr::Sequence(operands)
        | ValueExpr::Alternatives(operands) => {
            operands.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .any(|(_, value)| value_contains_keyed_collection_new(value)),
        ValueExpr::ObjectSpreadLiteral(_, source) => value_contains_keyed_collection_new(source),
        ValueExpr::StrictEquality(left, right) | ValueExpr::Binary(_, left, right) => {
            value_contains_keyed_collection_new(left) || value_contains_keyed_collection_new(right)
        }
        ValueExpr::Conditional { test, alternatives } => {
            value_contains_keyed_collection_new(test)
                || alternatives.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Binding(_)
        | ValueExpr::Alloc(..)
        | ValueExpr::Undefined
        | ValueExpr::NonCallable(_)
        | ValueExpr::Unknown => false,
    }
}

fn simple_binding(pattern: &Pat) -> Option<Id> {
    match pattern {
        Pat::Ident(binding) if binding.type_ann.is_none() && !binding.id.optional => {
            Some(binding.id.to_id())
        }
        _ => None,
    }
}

fn lower_var_declaration(
    declaration: &VarDecl,
    class_template: bool,
    bindings: &mut BTreeMap<Id, LocalBinding>,
    declaration_scope: &mut BTreeMap<Id, LocalBinding>,
    operations: &mut Vec<LocalOperation>,
) -> Option<()> {
    let kind = match declaration.kind {
        VarDeclKind::Var => LocalBinding::Var,
        VarDeclKind::Let => LocalBinding::Let,
        VarDeclKind::Const => LocalBinding::Const,
    };
    for declarator in &declaration.decls {
        if declarator.definite {
            return None;
        }
        let pattern = ObjectBindingPattern::collect(&declarator.name);
        let targets = match &pattern {
            Some(pattern) => pattern
                .properties
                .iter()
                .map(|property| property.binding.clone())
                .collect(),
            None => vec![simple_binding(&declarator.name)?],
        };
        for binding in targets {
            match bindings.get(&binding) {
                Some(LocalBinding::Var | LocalBinding::Parameter) if kind == LocalBinding::Var => {}
                Some(_) => return None,
                None => {
                    bindings.insert(binding.clone(), kind);
                }
            }
            match declaration_scope.get(&binding) {
                Some(LocalBinding::Var | LocalBinding::Parameter) if kind == LocalBinding::Var => {}
                Some(existing) if *existing == kind => {}
                Some(_) => return None,
                None => {
                    declaration_scope.insert(binding, kind);
                }
            }
        }
        let value = LocalExpression::optional(declarator.init.as_deref(), class_template)?;
        if kind == LocalBinding::Const && value.is_none() {
            return None;
        }
        if let Some(pattern) = pattern {
            operations.push(LocalOperation::sourced(
                declarator.span,
                OperationStep::InitializeObjectBinding,
                LocalOperation::InitializeObject {
                    pattern,
                    value: value?,
                },
            ));
        } else {
            operations.push(LocalOperation::sourced(
                declarator.span,
                OperationStep::InitializeBinding,
                LocalOperation::Initialize(simple_binding(&declarator.name)?, value),
            ));
        }
    }
    Some(())
}

fn literal_definition_name(name: &PropName, kind: LiteralPropertyKind) -> Option<String> {
    if !matches!(
        name,
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::Computed(_)
    ) {
        return None;
    }
    match kind {
        // Only a key/value data definition has the special prototype form.
        LiteralPropertyKind::Data => object_data_property_name(name),
        LiteralPropertyKind::Method | LiteralPropertyKind::Getter | LiteralPropertyKind::Setter => {
            static_property_name(name)
        }
        LiteralPropertyKind::Opaque => None,
    }
}

/// The central allocation transfer has no conditional-path provenance. Keep
/// branches outside this ordered subset until their effects can be tagged.
fn unconditional_value(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::Binding(_) | ValueExpr::NonCallable(_) | ValueExpr::Undefined => true,
        ValueExpr::Alloc(Kind::RegExp, _) => true,
        ValueExpr::New(..) if is_keyed_collection_new(value) => true,
        ValueExpr::ObjectSpreadLiteral(_, source) => unconditional_value(source),
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .all(|(_, value)| unconditional_value(value)),
        ValueExpr::StrictEquality(left, right) | ValueExpr::Binary(_, left, right) => {
            unconditional_value(left) && unconditional_value(right)
        }
        ValueExpr::Void(value) | ValueExpr::LogicalNot(value) => unconditional_value(value),
        ValueExpr::Sequence(values) => values.iter().all(unconditional_value),
        _ => false,
    }
}

impl LocalExpression {
    /// Whether a call expression belongs to the shared ordered transfer
    /// grammar. Keep general lowering private so callers cannot construct
    /// detached plans from arbitrary AST expressions.
    pub(super) fn can_lower_call(call: &CallExpr) -> bool {
        Self::lower(&Expr::Call(call.clone()), false).is_some()
    }

    fn sourced(span: Span, step: OperationStep, expression: Self) -> Self {
        Self::Sourced {
            span,
            step,
            site: None,
            substeps: BTreeMap::new(),
            expression: Box::new(expression),
        }
    }

    fn source_span(&self) -> Option<Span> {
        match self {
            Self::Sourced { span, .. } => Some(*span),
            _ => None,
        }
    }

    pub(super) fn source_site_for_step(
        &self,
        requested: OperationStep,
    ) -> Option<&SourceOperationSite> {
        let mut expression = self;
        while let Self::Sourced {
            site,
            substeps,
            expression: inner,
            ..
        } = expression
        {
            if let Some(site) = site.as_ref().filter(|site| site.step == requested) {
                return Some(site);
            }
            if let Some(site) = substeps.get(&requested) {
                return Some(site);
            }
            expression = inner;
        }
        None
    }

    pub(super) fn without_source(&self) -> &Self {
        let mut expression = self;
        while let Self::Sourced {
            expression: inner, ..
        } = expression
        {
            expression = inner;
        }
        expression
    }

    fn without_source_mut(&mut self) -> &mut Self {
        let mut expression = self;
        while let Self::Sourced {
            expression: inner, ..
        } = expression
        {
            expression = inner;
        }
        expression
    }

    fn bind_source_sites(
        &mut self,
        owner: SourceUnitOrdinal,
        body: &LocalBodyIdentity,
        binder: &mut SourceOperationSiteBinding<'_>,
    ) -> Option<()> {
        if let Self::Sourced {
            span,
            step,
            site,
            substeps,
            expression,
        } = self
        {
            let source_site = binder.bind(owner, body, *span, *step)?;
            *site = Some(source_site.clone());
            substeps.clear();
            let inner = expression.without_source_mut();
            let steps = match inner {
                Self::Binary { span, .. } => vec![
                    (*span, OperationStep::BinaryCoercion),
                    (*span, OperationStep::BinaryOperator),
                ],
                Self::Conditional {
                    test,
                    consequent,
                    alternate,
                    ..
                } => vec![
                    (test.source_span()?, OperationStep::ConditionalTest),
                    (
                        consequent.source_span()?,
                        OperationStep::ConditionalConsequent,
                    ),
                    (
                        alternate.source_span()?,
                        OperationStep::ConditionalAlternate,
                    ),
                ],
                Self::Logical { left, right, .. } => vec![
                    (left.source_span()?, OperationStep::LogicalTest),
                    (right.source_span()?, OperationStep::LogicalRight),
                ],
                Self::Template { span, .. } => vec![(*span, OperationStep::TemplateCoercion)],
                Self::Call(call) => {
                    let mut steps = Vec::new();
                    if let LocalCallee::Member {
                        optional_receiver, ..
                    } = &call.callee
                    {
                        steps.push((call.span, OperationStep::CalleeReceiver));
                        if *optional_receiver {
                            steps.push((call.span, OperationStep::OptionalReceiverCheck));
                        }
                    }
                    steps.push((call.span, OperationStep::CallCalleeGetValue));
                    if matches!(call.invocation, LocalInvocation::OptionalCall) {
                        steps.push((call.span, OperationStep::OptionalCalleeCheck));
                    }
                    steps.push((call.span, OperationStep::CallInvocation));
                    call.spread_sites.clear();
                    for (index, spread_span) in &call.spread_arguments {
                        let site = binder.bind_substep(
                            owner,
                            body,
                            *spread_span,
                            source_site.ordinal,
                            OperationStep::IterateSpread,
                        )?;
                        call.spread_sites.insert(*index, site);
                    }
                    steps
                }
                Self::Value(ValueExpr::New(_, _, site)) => {
                    vec![(site.source_span(), OperationStep::CallInvocation)]
                }
                Self::Assign { span, .. } => vec![
                    (*span, OperationStep::BindingGetValue),
                    (*span, OperationStep::BindingPutValue),
                ],
                Self::UpdateBinding { span, .. } => vec![
                    (*span, OperationStep::BindingGetValue),
                    (*span, OperationStep::UpdateCoercion),
                    (*span, OperationStep::UpdatePutValue),
                ],
                Self::ReadField {
                    span,
                    optional_receiver,
                    ..
                } => {
                    let mut steps = Vec::new();
                    if *optional_receiver {
                        steps.push((*span, OperationStep::OptionalReceiverCheck));
                    }
                    steps.push((*span, OperationStep::FieldGetValue));
                    steps
                }
                Self::AssignField { span, .. } => vec![
                    (*span, OperationStep::FieldGetValue),
                    (*span, OperationStep::FieldPutValue),
                ],
                Self::SetConstruction { .. } => vec![
                    (*span, OperationStep::Allocate),
                    (*span, OperationStep::CallCalleeGetValue),
                    (*span, OperationStep::CallInvocation),
                    (*span, OperationStep::IterateSpread),
                ],
                Self::Record { .. } | Self::Array { .. } => {
                    vec![(*span, OperationStep::Allocate)]
                }
                Self::DefineThisField { .. } => vec![(*span, OperationStep::DefineField)],
                _ => Vec::new(),
            };
            for (step_span, step) in steps {
                let substep =
                    binder.bind_substep(owner, body, step_span, source_site.ordinal, step)?;
                substeps.insert(step, substep);
            }
            expression.bind_source_sites(owner, body, binder)?;
            return Some(());
        }

        match self.without_source_mut() {
            Self::Binary { left, right, .. } | Self::Logical { left, right, .. } => {
                left.bind_source_sites(owner, body, binder)?;
                right.bind_source_sites(owner, body, binder)?;
            }
            Self::Conditional {
                test,
                consequent,
                alternate,
                ..
            } => {
                test.bind_source_sites(owner, body, binder)?;
                consequent.bind_source_sites(owner, body, binder)?;
                alternate.bind_source_sites(owner, body, binder)?;
            }
            Self::Template { substitutions, .. } | Self::Sequence(substitutions) => {
                for substitution in Arc::make_mut(substitutions) {
                    substitution.bind_source_sites(owner, body, binder)?;
                }
            }
            Self::Record { properties, .. } => {
                for property in Arc::make_mut(properties) {
                    property
                        .value_mut()
                        .bind_source_sites(owner, body, binder)?;
                }
            }
            Self::Array { elements, .. } => {
                for element in Arc::make_mut(elements) {
                    match element {
                        LocalArrayElement::Hole => {}
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => {
                            value.bind_source_sites(owner, body, binder)?;
                        }
                    }
                }
            }
            Self::SetConstruction { array, .. } => {
                array.bind_source_sites(owner, body, binder)?;
            }
            Self::Assign { value, .. }
            | Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => {
                Arc::make_mut(value).bind_source_sites(owner, body, binder)?;
            }
            Self::Call(call) => {
                if let LocalCallee::Member { receiver, .. } = &mut call.callee {
                    Arc::make_mut(receiver).bind_source_sites(owner, body, binder)?;
                }
                for argument in Arc::make_mut(&mut call.arguments) {
                    argument.bind_source_sites(owner, body, binder)?;
                }
            }
            Self::ClassDefinition(definition) => {
                definition.bind_source_sites(owner, body, binder)?;
            }
            Self::ReadField { receiver, .. } => {
                Arc::make_mut(receiver).bind_source_sites(owner, body, binder)?;
            }
            Self::Sourced { .. } => unreachable!("source wrappers are peeled above"),
            Self::Value(_)
            | Self::PrimitiveLiteral(_)
            | Self::This
            | Self::UpdateBinding { .. }
            | Self::Closure(_) => {}
        }
        Some(())
    }

    fn optional(expression: Option<&Expr>, class_template: bool) -> Option<Option<Self>> {
        match expression {
            Some(expression) => Some(Some(Self::lower(expression, class_template)?)),
            None => Some(None),
        }
    }

    fn lower(expression: &Expr, class_template: bool) -> Option<Self> {
        let source_span = expression.span();
        let lowered = match expression.unwrap_parens() {
            Expr::Cond(conditional) => Some(Self::Conditional {
                test: Box::new(Self::lower(&conditional.test, class_template)?),
                consequent: Box::new(Self::lower(&conditional.cons, class_template)?),
                alternate: Box::new(Self::lower(&conditional.alt, class_template)?),
            }),
            Expr::Bin(binary)
                if matches!(
                    binary.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                let op = match binary.op {
                    BinaryOp::LogicalAnd => LocalLogicalOperator::And,
                    BinaryOp::LogicalOr => LocalLogicalOperator::Or,
                    BinaryOp::NullishCoalescing => LocalLogicalOperator::NullishCoalescing,
                    _ => unreachable!("guarded short-circuit operator"),
                };
                Some(Self::Logical {
                    op,
                    left: Box::new(Self::lower(&binary.left, class_template)?),
                    right: Box::new(Self::lower(&binary.right, class_template)?),
                })
            }
            Expr::Bin(binary) if super::value_model::modeled_binary_operator(binary.op) => {
                Some(Self::Binary {
                    operator: binary.op,
                    left: Box::new(Self::lower(&binary.left, class_template)?),
                    right: Box::new(Self::lower(&binary.right, class_template)?),
                    span: binary.span,
                })
            }

            Expr::Lit(Lit::Str(_)) => Some(Self::PrimitiveLiteral(PrimKinds::STRING)),
            Expr::Lit(Lit::Num(_)) => Some(Self::PrimitiveLiteral(PrimKinds::NUMBER)),
            Expr::Lit(Lit::Bool(_)) => Some(Self::PrimitiveLiteral(PrimKinds::BOOLEAN)),
            Expr::Lit(Lit::Null(_)) => Some(Self::PrimitiveLiteral(PrimKinds::NULL)),
            Expr::Lit(Lit::BigInt(_)) => Some(Self::PrimitiveLiteral(PrimKinds::BIGINT)),
            Expr::Object(object)
                if matches!(object.props.as_slice(), [PropOrSpread::Spread(_)]) =>
            {
                let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
                (covered && matches!(value, ValueExpr::ObjectSpreadLiteral(..)))
                    .then_some(Self::Value(value))
            }
            Expr::Object(object) => {
                let properties = object
                    .props
                    .iter()
                    .map(|property| {
                        let PropOrSpread::Prop(property) = property else {
                            return None;
                        };
                        match property.as_ref() {
                            Prop::Shorthand(binding) => Some(PropertyDefinition::Data {
                                name: Some(binding.sym.to_string()),
                                value: Self::sourced(
                                    binding.span,
                                    OperationStep::Expression,
                                    Self::Value(ValueExpr::Binding(binding.to_id())),
                                ),
                            }),
                            Prop::KeyValue(pair) => Some(PropertyDefinition::Data {
                                name: Some(literal_definition_name(
                                    &pair.key,
                                    LiteralPropertyKind::Data,
                                )?),
                                value: Self::lower(&pair.value, class_template)?,
                            }),
                            Prop::Method(method)
                                if method.function.decorators.is_empty()
                                    && method
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Method {
                                    name: Some(literal_definition_name(
                                        &method.key,
                                        LiteralPropertyKind::Method,
                                    )?),
                                    value: Self::sourced(
                                        method.function.span,
                                        OperationStep::Expression,
                                        Self::Closure(DeferredCallableCreation::Method {
                                            callable: method.function.span.lo.0,
                                            home_object: object.span.lo.0,
                                        }),
                                    ),
                                })
                            }
                            Prop::Getter(getter)
                                if getter.function.decorators.is_empty()
                                    && getter
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Getter {
                                    name: Some(literal_definition_name(
                                        &getter.key,
                                        LiteralPropertyKind::Getter,
                                    )?),
                                    value: Self::sourced(
                                        getter.function.span,
                                        OperationStep::Expression,
                                        Self::Closure(DeferredCallableCreation::Getter {
                                            callable: getter.function.span.lo.0,
                                            home_object: object.span.lo.0,
                                        }),
                                    ),
                                })
                            }
                            Prop::Setter(setter)
                                if setter.function.decorators.is_empty()
                                    && setter
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Setter {
                                    name: Some(literal_definition_name(
                                        &setter.key,
                                        LiteralPropertyKind::Setter,
                                    )?),
                                    value: Self::sourced(
                                        setter.function.span,
                                        OperationStep::Expression,
                                        Self::Closure(DeferredCallableCreation::Setter {
                                            callable: setter.function.span.lo.0,
                                            home_object: object.span.lo.0,
                                        }),
                                    ),
                                })
                            }
                            _ => None,
                        }
                    })
                    .collect::<Option<Arc<[_]>>>()?;
                Some(Self::Record {
                    allocation: object.span.lo.0,
                    properties,
                })
            }
            Expr::Array(array) => Some(Self::Array {
                allocation: array.span.lo.0,
                elements: array
                    .elems
                    .iter()
                    .map(|element| match element {
                        None => Some(LocalArrayElement::Hole),
                        Some(element) if element.spread.is_none() => Some(
                            LocalArrayElement::Value(Self::lower(&element.expr, class_template)?),
                        ),
                        Some(element) => {
                            let spread = element.spread.as_ref()?;
                            Some(LocalArrayElement::Spread {
                                value: Self::lower(&element.expr, class_template)?,
                                span: *spread,
                            })
                        }
                    })
                    .collect::<Option<Arc<[_]>>>()?,
            }),
            Expr::New(construction)
                if construction.type_args.is_none()
                    && matches!(construction.callee.unwrap_parens(), Expr::Ident(name) if name.sym == "Set")
                    && let [argument] = construction.args.as_deref().unwrap_or_default()
                    && argument.spread.is_none()
                    && matches!(argument.expr.unwrap_parens(), Expr::Array(array)
                        if array.elems.iter().flatten().any(|element| element.spread.is_some())) =>
            {
                let Expr::Array(array) = argument.expr.unwrap_parens() else {
                    unreachable!("guarded direct Set constructor has an array argument")
                };
                let (source, _) = ValueExpr::from_expr_with_coverage(&argument.expr);
                if !matches!(source, ValueExpr::ObjectLiteral(allocation, _) if allocation == array.span.lo.0)
                {
                    return None;
                }
                Some(Self::SetConstruction {
                    array: Box::new(Self::lower(&argument.expr, class_template)?),
                    source,
                    site: construction.span.into(),
                })
            }
            Expr::New(construction)
                if construction.type_args.is_none()
                    && !(construction.args.as_deref().unwrap_or_default().is_empty()
                        && matches!(construction.callee.unwrap_parens(), Expr::Ident(binding)
                            if matches!(binding.sym.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")))
                    && !construction
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .any(|argument| argument.spread.is_some()) =>
            {
                Some(Self::Call(LocalCall {
                    invocation: LocalInvocation::Construct,
                    callee: LocalCallee::lower(&construction.callee, class_template)?,
                    arguments: construction
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    spread_arguments: BTreeMap::new(),
                    spread_sites: BTreeMap::new(),
                    span: construction.span,
                }))
            }
            Expr::Tpl(template) => Some(Self::Template {
                substitutions: template
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, class_template))
                    .collect::<Option<Arc<[_]>>>()?,
                span: template.span,
            }),
            Expr::Fn(function)
                if (function.ident.is_none() || !class_template)
                    && function.function.decorators.is_empty()
                    && function
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty()) =>
            {
                Some(Self::Closure(DeferredCallableCreation::Function(
                    function.function.span.lo.0,
                )))
            }
            Expr::Arrow(arrow) => Some(Self::Closure(DeferredCallableCreation::Arrow(
                arrow.span.lo.0,
            ))),
            Expr::Class(class_expression) => {
                let definition = ClassDefinition::lower(&class_expression.class)?;
                // A named class expression has a distinct inner lexical
                // binding that is initialized before its static elements.
                // Until that environment is represented by the shared local
                // frame, do not transfer eager steps that might read it.
                if class_expression.ident.is_some() && !definition.static_steps.is_empty() {
                    return None;
                }
                Some(Self::ClassDefinition(definition))
            }
            Expr::Seq(sequence) => Some(Self::Sequence(
                sequence
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, class_template))
                    .collect::<Option<Arc<[_]>>>()?,
            )),
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                let callee = LocalCallee::lower(callee, class_template)?;
                let spread_arguments = call
                    .args
                    .iter()
                    .enumerate()
                    .filter_map(|(index, argument)| argument.spread.map(|span| (index, span)))
                    .collect();
                Some(Self::Call(LocalCall {
                    invocation: LocalInvocation::Call,
                    callee,
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    spread_arguments,
                    spread_sites: BTreeMap::new(),
                    span: call.span,
                }))
            }
            Expr::OptChain(chain) => match chain.base.as_ref() {
                OptChainBase::Call(call) => {
                    let callee = LocalCall::optional_callee(chain, class_template)?;
                    Some(Self::Call(LocalCall {
                        invocation: if chain.optional {
                            LocalInvocation::OptionalCall
                        } else {
                            LocalInvocation::Call
                        },
                        callee,
                        arguments: call
                            .args
                            .iter()
                            .map(|argument| Self::lower(&argument.expr, class_template))
                            .collect::<Option<Arc<[_]>>>()?,
                        spread_arguments: BTreeMap::new(),
                        spread_sites: BTreeMap::new(),
                        span: call.span,
                    }))
                }
                OptChainBase::Member(_) => Self::optional_member(chain, class_template),
            },
            Expr::Member(member) => Self::lower_member_read(member, false, class_template),
            Expr::Assign(assignment) if assignment.op == AssignOp::Assign => {
                Self::lower_assignment(assignment, class_template)
            }
            Expr::Update(update) => Self::lower_update(update),
            Expr::This(_) => Some(Self::This),
            _ => {
                let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
                let keyed_collection_new = is_keyed_collection_new(&value);
                (covered || keyed_collection_new)
                    .then(|| unconditional_value(&value).then_some(Self::Value(value)))
                    .flatten()
            }
        };
        lowered.map(|lowered| Self::sourced(source_span, OperationStep::Expression, lowered))
    }

    pub(super) fn optional_member(chain: &OptChainExpr, class_template: bool) -> Option<Self> {
        if !chain.optional {
            return None;
        }
        let OptChainBase::Member(member) = chain.base.as_ref() else {
            return None;
        };
        Self::lower_member_read(member, true, class_template)
    }

    fn lower_member_read(
        member: &MemberExpr,
        optional_receiver: bool,
        class_template: bool,
    ) -> Option<Self> {
        let property = super::value_model::static_prop(&member.prop)?;
        if !super::value_model::MemberKey::of(&member.prop).supports_own_data_read(&property) {
            return None;
        }
        Some(Self::ReadField {
            receiver: Arc::new(Self::lower(&member.obj, class_template)?),
            property,
            span: member.span,
            optional_receiver,
        })
    }

    fn lower_assignment(assignment: &AssignExpr, class_template: bool) -> Option<Self> {
        let value = Arc::new(Self::lower(&assignment.right, class_template)?);
        match &assignment.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => Some(Self::Assign {
                binding: binding.id.to_id(),
                value,
                span: assignment.span,
            }),
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if class_template && matches!(member.obj.unwrap_parens(), Expr::This(_)) {
                    return match &member.prop {
                        MemberProp::PrivateName(private) => Some(Self::AssignPrivateThisField {
                            property: private.name.to_string(),
                            value,
                        }),
                        MemberProp::Ident(property) => Some(Self::AssignPublicThisField {
                            property: property.sym.to_string(),
                            value,
                        }),
                        MemberProp::Computed(_) => None,
                    };
                }
                let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                    return None;
                };
                let MemberProp::Ident(property) = &member.prop else {
                    return None;
                };
                Some(Self::AssignField {
                    receiver: receiver.to_id(),
                    property: property.sym.to_string(),
                    value,
                    span: assignment.span,
                })
            }
            _ => None,
        }
    }

    fn lower_update(update: &UpdateExpr) -> Option<Self> {
        let Expr::Ident(binding) = update.arg.unwrap_parens() else {
            return None;
        };
        Some(Self::UpdateBinding {
            binding: binding.to_id(),
            span: update.span,
        })
    }

    fn reads(&self, reads: &mut BTreeSet<Id>) {
        match self.without_source() {
            Self::Binary { left, right, .. } => {
                left.reads(reads);
                right.reads(reads);
            }
            Self::Conditional {
                test,
                consequent,
                alternate,
                ..
            } => {
                test.reads(reads);
                consequent.reads(reads);
                alternate.reads(reads);
            }
            Self::Logical { left, right, .. } => {
                left.reads(reads);
                right.reads(reads);
            }

            Self::Value(value) => value.bindings(reads),
            Self::PrimitiveLiteral(_) => {}
            Self::Record { properties, .. } => {
                for property in properties.iter() {
                    let value = property.value();
                    value.reads(reads);
                }
            }
            Self::Array { elements, .. } => {
                for element in elements.iter() {
                    match element {
                        LocalArrayElement::Hole => {}
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value.reads(reads),
                    }
                }
            }
            Self::SetConstruction { array, source, .. } => {
                array.reads(reads);
                source.bindings(reads);
            }
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                for expression in expressions.iter() {
                    expression.reads(reads);
                }
            }
            Self::Assign { binding, value, .. } => {
                reads.insert(binding.clone());
                value.reads(reads);
            }
            Self::UpdateBinding { binding, .. } => {
                reads.insert(binding.clone());
            }
            Self::Closure(_) | Self::This => {}
            Self::ClassDefinition(definition) => definition.reads(reads),
            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                match callee {
                    LocalCallee::Binding(binding) => {
                        reads.insert(binding.clone());
                    }
                    LocalCallee::Member { receiver, .. } => receiver.reads(reads),
                    // Capturing a cell does not GetValue it. Invocation owns
                    // the deferred body's actual reads and effects.
                    LocalCallee::Creation(_) => {}
                }
                for argument in arguments.iter() {
                    argument.reads(reads);
                }
            }
            Self::ReadField { receiver, .. } => receiver.reads(reads),
            Self::AssignField {
                receiver, value, ..
            } => {
                reads.insert(receiver.clone());
                value.reads(reads);
            }
            Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.reads(reads),
            Self::Sourced { .. } => unreachable!("source wrapper is removed before reads"),
        }
    }

    fn allocation_syntax(
        &self,
        bindings: &BTreeMap<Id, LocalBinding>,
        closure_sites: &mut BTreeSet<u32>,
    ) -> (bool, bool) {
        match self.without_source() {
            Self::Binary { left, right, .. } => {
                let (left_allocation, left_closure) =
                    left.allocation_syntax(bindings, closure_sites);
                let (right_allocation, right_closure) =
                    right.allocation_syntax(bindings, closure_sites);
                (
                    left_allocation && right_allocation,
                    left_closure && right_closure,
                )
            }
            Self::Conditional { .. } | Self::Logical { .. } => (false, false),

            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                // Grammar admission is only a candidate. Each consumer must
                // select the actual callee and transfer its invocation.
                let (mut allocation, mut closure) = (true, true);
                if let LocalCallee::Member { receiver, .. } = callee {
                    let (receiver_allocation, receiver_closure) =
                        receiver.allocation_syntax(bindings, closure_sites);
                    allocation &= receiver_allocation;
                    closure &= receiver_closure;
                }
                for argument in arguments.iter() {
                    let (argument_allocation, argument_closure) =
                        argument.allocation_syntax(bindings, closure_sites);
                    allocation &= argument_allocation;
                    closure &= argument_closure;
                }
                (allocation, closure)
            }
            Self::Closure(callable) => {
                closure_sites.insert(callable.callable());
                (false, true)
            }
            Self::Record { properties, .. } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for property in properties.iter() {
                    let value = property.value();
                    let (allocation, closure) = value.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::Array { elements, .. } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for element in elements.iter() {
                    let value = match element {
                        LocalArrayElement::Hole => continue,
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value,
                    };
                    let (allocation, closure) = value.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::SetConstruction { array, .. } => {
                let (allocation, _) = array.allocation_syntax(bindings, closure_sites);
                (allocation, false)
            }
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for expression in expressions.iter() {
                    let (allocation, closure) =
                        expression.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::Assign { binding, value, .. } => {
                let (allocation_only, closure_allowed) =
                    value.allocation_syntax(bindings, closure_sites);
                let local = bindings.contains_key(binding);
                (local && allocation_only, local && closure_allowed)
            }
            Self::UpdateBinding { binding, .. } => {
                let local = bindings.contains_key(binding);
                (local, local)
            }
            Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => {
                let (allocation_only, _) = value.allocation_syntax(bindings, closure_sites);
                (allocation_only, false)
            }
            Self::Value(value) => (true, unconditional_value(value)),
            Self::PrimitiveLiteral(_) => (true, true),
            Self::ClassDefinition(definition) => {
                definition.allocation_syntax(bindings, closure_sites)
            }
            Self::This | Self::ReadField { .. } => (true, false),
            Self::Sourced { .. } => unreachable!("source wrapper is removed before syntax checks"),
        }
    }

    fn requires_keyed_collection_new(&self) -> bool {
        match self.without_source() {
            Self::Binary { left, right, .. } => {
                left.requires_keyed_collection_new() || right.requires_keyed_collection_new()
            }
            Self::Conditional {
                test,
                consequent,
                alternate,
                ..
            } => {
                test.requires_keyed_collection_new()
                    || consequent.requires_keyed_collection_new()
                    || alternate.requires_keyed_collection_new()
            }
            Self::Logical { left, right, .. } => {
                left.requires_keyed_collection_new() || right.requires_keyed_collection_new()
            }

            Self::Value(value) => value_contains_keyed_collection_new(value),
            Self::ClassDefinition(definition) => {
                definition.static_steps.iter().any(|step| match step {
                    ClassStaticStep::Field { value, .. } => value.requires_keyed_collection_new(),
                    ClassStaticStep::Block { expressions, .. } => {
                        expressions.iter().any(Self::requires_keyed_collection_new)
                    }
                })
            }
            Self::Record { properties, .. } => properties
                .iter()
                .any(|property| property.value().requires_keyed_collection_new()),
            Self::Array { elements, .. } => elements.iter().any(|element| match element {
                LocalArrayElement::Hole => false,
                LocalArrayElement::Value(value) | LocalArrayElement::Spread { value, .. } => {
                    value.requires_keyed_collection_new()
                }
            }),
            Self::SetConstruction { .. } => true,
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => expressions.iter().any(Self::requires_keyed_collection_new),
            Self::Assign { value, .. }
            | Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.requires_keyed_collection_new(),
            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                matches!(callee, LocalCallee::Member { receiver, .. }
                    if receiver.requires_keyed_collection_new())
                    || arguments.iter().any(Self::requires_keyed_collection_new)
            }
            Self::PrimitiveLiteral(_)
            | Self::This
            | Self::Closure(_)
            | Self::ReadField { .. }
            | Self::UpdateBinding { .. } => false,
            Self::Sourced { .. } => unreachable!("source wrapper is removed before syntax checks"),
        }
    }

    fn uses_constructor_receiver_operation(&self) -> bool {
        match self.without_source() {
            Self::DefineThisField { .. }
            | Self::AssignPrivateThisField { .. }
            | Self::AssignPublicThisField { .. } => true,
            Self::Binary { left, right, .. } | Self::Logical { left, right, .. } => {
                left.uses_constructor_receiver_operation()
                    || right.uses_constructor_receiver_operation()
            }
            Self::Conditional {
                test,
                consequent,
                alternate,
                ..
            } => {
                test.uses_constructor_receiver_operation()
                    || consequent.uses_constructor_receiver_operation()
                    || alternate.uses_constructor_receiver_operation()
            }
            Self::ReadField { receiver, .. } => receiver.uses_constructor_receiver_operation(),
            Self::Template { substitutions, .. } | Self::Sequence(substitutions) => substitutions
                .iter()
                .any(Self::uses_constructor_receiver_operation),
            Self::Record { properties, .. } => properties
                .iter()
                .any(|property| property.value().uses_constructor_receiver_operation()),
            Self::Array { elements, .. } => elements.iter().any(|element| match element {
                LocalArrayElement::Hole => false,
                LocalArrayElement::Value(value) | LocalArrayElement::Spread { value, .. } => {
                    value.uses_constructor_receiver_operation()
                }
            }),
            Self::SetConstruction { array, .. } => array.uses_constructor_receiver_operation(),
            Self::Assign { value, .. } | Self::AssignField { value, .. } => {
                value.uses_constructor_receiver_operation()
            }
            Self::UpdateBinding { .. }
            | Self::Value(_)
            | Self::PrimitiveLiteral(_)
            | Self::This
            | Self::Closure(_) => false,
            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                let callee_uses = match callee {
                    LocalCallee::Creation(_) | LocalCallee::Binding(_) => false,
                    LocalCallee::Member { receiver, .. } => {
                        receiver.uses_constructor_receiver_operation()
                    }
                };
                callee_uses
                    || arguments
                        .iter()
                        .any(Self::uses_constructor_receiver_operation)
            }
            Self::ClassDefinition(_) => false,
            Self::Sourced { .. } => unreachable!("source wrapper is removed before class checks"),
        }
    }

    pub(super) fn class_step_may_escape_receiver(&self) -> bool {
        fn value_may_carry_receiver(expression: &LocalExpression) -> bool {
            match expression.without_source() {
                LocalExpression::This | LocalExpression::Closure(_) => true,
                LocalExpression::Binary {
                    operator:
                        BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing,
                    left,
                    right,
                    ..
                } => value_may_carry_receiver(left) || value_may_carry_receiver(right),
                LocalExpression::Conditional {
                    consequent,
                    alternate,
                    ..
                } => value_may_carry_receiver(consequent) || value_may_carry_receiver(alternate),
                LocalExpression::Logical { left, right, .. } => {
                    value_may_carry_receiver(left) || value_may_carry_receiver(right)
                }
                LocalExpression::Sequence(expressions) => {
                    expressions.last().is_some_and(value_may_carry_receiver)
                }
                LocalExpression::Record { properties, .. } => properties
                    .iter()
                    .any(|property| value_may_carry_receiver(property.value())),
                LocalExpression::Array { elements, .. } => {
                    elements.iter().any(|element| match element {
                        LocalArrayElement::Hole => false,
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => {
                            value_may_carry_receiver(value)
                        }
                    })
                }
                LocalExpression::SetConstruction { array, .. } => value_may_carry_receiver(array),
                LocalExpression::Assign { value, .. }
                | LocalExpression::AssignField { value, .. }
                | LocalExpression::DefineThisField { value, .. }
                | LocalExpression::AssignPrivateThisField { value, .. }
                | LocalExpression::AssignPublicThisField { value, .. } => {
                    value_may_carry_receiver(value)
                }
                // A nested call may return the receiver or a closure retaining it.
                LocalExpression::Call(_) | LocalExpression::ReadField { .. } => true,
                LocalExpression::Binary { .. }
                | LocalExpression::Template { .. }
                | LocalExpression::UpdateBinding { .. } => false,
                LocalExpression::ClassDefinition(_)
                | LocalExpression::Value(_)
                | LocalExpression::PrimitiveLiteral(_) => false,
                LocalExpression::Sourced { .. } => unreachable!("source wrapper is removed above"),
            }
        }

        fn may_publish_or_reenter(expression: &LocalExpression) -> bool {
            match expression.without_source() {
                LocalExpression::Call(LocalCall {
                    callee, arguments, ..
                }) => {
                    let receiver_escapes = match callee {
                        LocalCallee::Creation(_) => true,
                        LocalCallee::Binding(_) => false,
                        LocalCallee::Member { receiver, .. } => {
                            value_may_carry_receiver(receiver) || may_publish_or_reenter(receiver)
                        }
                    };
                    receiver_escapes
                        || arguments.iter().any(value_may_carry_receiver)
                        || arguments.iter().any(may_publish_or_reenter)
                }
                LocalExpression::Assign { value, .. }
                | LocalExpression::AssignField { value, .. } => {
                    value_may_carry_receiver(value) || may_publish_or_reenter(value)
                }
                LocalExpression::Binary {
                    operator,
                    left,
                    right,
                    ..
                } => {
                    (!matches!(
                        operator,
                        BinaryOp::EqEqEq
                            | BinaryOp::NotEqEq
                            | BinaryOp::LogicalAnd
                            | BinaryOp::LogicalOr
                            | BinaryOp::NullishCoalescing
                    ) && (value_may_carry_receiver(left) || value_may_carry_receiver(right)))
                        || may_publish_or_reenter(left)
                        || may_publish_or_reenter(right)
                }
                LocalExpression::Conditional {
                    test,
                    consequent,
                    alternate,
                    ..
                } => {
                    may_publish_or_reenter(test)
                        || may_publish_or_reenter(consequent)
                        || may_publish_or_reenter(alternate)
                }
                LocalExpression::Logical { left, right, .. } => {
                    may_publish_or_reenter(left) || may_publish_or_reenter(right)
                }
                LocalExpression::ReadField { receiver, .. } => may_publish_or_reenter(receiver),
                LocalExpression::Template { substitutions, .. }
                | LocalExpression::Sequence(substitutions) => {
                    substitutions.iter().any(may_publish_or_reenter)
                }
                LocalExpression::Record { properties, .. } => properties
                    .iter()
                    .any(|property| may_publish_or_reenter(property.value())),
                LocalExpression::Array { elements, .. } => {
                    elements.iter().any(|element| match element {
                        LocalArrayElement::Hole => false,
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => may_publish_or_reenter(value),
                    })
                }
                LocalExpression::SetConstruction { array, .. } => may_publish_or_reenter(array),
                LocalExpression::AssignPrivateThisField { value, .. }
                | LocalExpression::AssignPublicThisField { value, .. }
                | LocalExpression::DefineThisField { value, .. } => may_publish_or_reenter(value),
                LocalExpression::ClassDefinition(_)
                | LocalExpression::UpdateBinding { .. }
                | LocalExpression::Closure(_)
                | LocalExpression::This
                | LocalExpression::Value(_)
                | LocalExpression::PrimitiveLiteral(_) => false,
                LocalExpression::Sourced { .. } => unreachable!("source wrapper is removed above"),
            }
        }

        may_publish_or_reenter(self)
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        match self.without_source() {
            Self::Binary { left, right, .. } => {
                left.writes(writes);
                right.writes(writes);
            }
            Self::Conditional {
                test,
                consequent,
                alternate,
                ..
            } => {
                test.writes(writes);
                consequent.writes(writes);
                alternate.writes(writes);
            }
            Self::Logical { left, right, .. } => {
                left.writes(writes);
                right.writes(writes);
            }

            Self::Record { properties, .. } => {
                for property in properties.iter() {
                    let value = property.value();
                    value.writes(writes);
                }
            }
            Self::Assign { binding, value, .. } => {
                writes.insert(binding.clone());
                value.writes(writes);
            }
            Self::UpdateBinding { binding, .. } => {
                writes.insert(binding.clone());
            }
            Self::Array { elements, .. } => {
                for element in elements.iter() {
                    match element {
                        LocalArrayElement::Hole => {}
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value.writes(writes),
                    }
                }
            }
            Self::SetConstruction { array, .. } => array.writes(writes),
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                for expression in expressions.iter() {
                    expression.writes(writes);
                }
            }
            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                if let LocalCallee::Member { receiver, .. } = callee {
                    receiver.writes(writes);
                }
                for argument in arguments.iter() {
                    argument.writes(writes);
                }
            }
            Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.writes(writes),
            Self::Value(_)
            | Self::PrimitiveLiteral(_)
            | Self::This
            | Self::ReadField { .. }
            | Self::Closure(_) => {}
            Self::ClassDefinition(definition) => definition.writes(writes),
            Self::Sourced { .. } => unreachable!("source wrapper is removed before writes"),
        }
    }
}

impl LocalBody {
    pub(super) fn has_source_operation_site(&self, site: &SourceOperationSite) -> bool {
        self.source_operation_sites.contains(site)
    }

    pub(super) fn from_object_binding(
        declaration: &VarDecl,
    ) -> Option<(Self, ObjectBindingPattern)> {
        let [declarator] = declaration.decls.as_slice() else {
            return None;
        };
        if declaration.declare || declarator.definite {
            return None;
        }
        let pattern = ObjectBindingPattern::collect(&declarator.name)?;
        let expression = declarator.init.as_deref()?;
        let mut bindings = BTreeMap::new();
        let mut operations = Vec::new();
        lower_var_declaration(
            declaration,
            false,
            &mut bindings,
            &mut BTreeMap::new(),
            &mut operations,
        )?;
        let mut body = Self::from_operations(bindings, Vec::new(), operations);
        body.source_span = Some(declarator.span);
        body.root_expression_span = Some(expression.span());
        Some((body, pattern))
    }
    pub(super) fn from_initializer(expression: &Expr) -> Option<Self> {
        let mut body = Self::lower_expression(std::iter::empty(), expression)?;
        body.root_expression_span = Some(expression.span());
        Some(body)
    }

    pub(super) fn from_class_declaration(definition: &ClassDefinition) -> Self {
        let mut body = Self::from_operations(
            BTreeMap::new(),
            Vec::new(),
            vec![LocalOperation::sourced(
                definition.span,
                // Class declaration lowering models the class value as this
                // root body's result. The source span remains the declaration
                // span; the step names the semantic transfer being lowered.
                OperationStep::Return,
                LocalOperation::Return(Some(LocalExpression::sourced(
                    definition.span,
                    OperationStep::Expression,
                    LocalExpression::ClassDefinition(definition.clone()),
                ))),
            )],
        );
        body.root_expression_span = Some(definition.span);
        body
    }

    pub(super) fn from_expression_statement(expression: &Expr) -> Option<Self> {
        match expression {
            Expr::Assign(assignment) if assignment.op == AssignOp::Assign => {
                return Self::from_assignment_expression_statement(assignment);
            }
            Expr::Update(update) => return Self::from_update_expression_statement(update),
            _ => {}
        }
        let mut body = Self::from_initializer(expression)?;
        let [operation] = body.operations.as_ref() else {
            unreachable!("expression lowering produces one value operation");
        };
        let LocalOperation::Return(Some(value)) = operation.without_source() else {
            unreachable!("expression lowering produces one return operation");
        };
        let value = value.clone();
        body.operations = Arc::from([LocalOperation::sourced(
            expression.span(),
            OperationStep::Statement,
            LocalOperation::Evaluate(value),
        )]);
        Some(body)
    }

    /// Retain a lexical assignment as one canonical ordered operation for
    /// consumers that collect the write through the target walker.
    pub(super) fn from_assignment_expression_statement(assignment: &AssignExpr) -> Option<Self> {
        if assignment.op != AssignOp::Assign {
            return None;
        }
        let value = LocalExpression::sourced(
            assignment.span,
            OperationStep::Expression,
            LocalExpression::lower_assignment(assignment, false)?,
        );
        Some(Self::from_single_evaluation(value, assignment.span))
    }

    /// A detached writer transfer reuses its enclosing body's canonical
    /// statement site. Its isolated plan is not itself a source body region.
    pub(super) fn from_assignment_store_transfer(assignment: &AssignExpr) -> Option<Self> {
        if assignment.op != AssignOp::Assign {
            return None;
        }
        let value = LocalExpression::sourced(
            assignment.span,
            OperationStep::Expression,
            LocalExpression::lower_assignment(assignment, false)?,
        );
        Some(Self::from_transfer_evaluation(value, assignment.span))
    }

    /// Retain a lexical update as one canonical ordered operation for
    /// consumers that collect the write through the target walker.
    pub(super) fn from_update_expression_statement(update: &UpdateExpr) -> Option<Self> {
        let value = LocalExpression::sourced(
            update.span,
            OperationStep::Expression,
            LocalExpression::lower_update(update)?,
        );
        Some(Self::from_single_evaluation(value, update.span))
    }

    /// A detached writer transfer reuses its enclosing body's canonical
    /// statement site. Its isolated plan is not itself a source body region.
    pub(super) fn from_update_store_transfer(update: &UpdateExpr) -> Option<Self> {
        let value = LocalExpression::sourced(
            update.span,
            OperationStep::Expression,
            LocalExpression::lower_update(update)?,
        );
        Some(Self::from_transfer_evaluation(value, update.span))
    }

    fn from_single_evaluation(value: LocalExpression, span: Span) -> Self {
        let mut body = Self::from_operations(
            BTreeMap::new(),
            Vec::new(),
            vec![LocalOperation::sourced(
                span,
                OperationStep::Statement,
                LocalOperation::Evaluate(value),
            )],
        );
        body.source_span = Some(span);
        body.root_expression_span = Some(span);
        body
    }

    fn from_transfer_evaluation(value: LocalExpression, span: Span) -> Self {
        let mut body = Self::from_operations(
            BTreeMap::new(),
            Vec::new(),
            vec![LocalOperation::Evaluate(value)],
        );
        body.root_expression_span = Some(span);
        body
    }

    pub(super) fn untagged_template_root(&self) -> bool {
        let [operation] = self.operations.as_ref() else {
            return false;
        };
        self.root_expression_span.is_some()
            && matches!(
                operation.without_source(),
                LocalOperation::Return(Some(value))
                    if matches!(value.without_source(), LocalExpression::Template { .. })
            )
    }

    /// Exact terminal source call. Its operands retain independent coverage.
    /// This selects ordered execution, never positive effect coverage.
    pub(super) fn terminal_call_root(&self) -> Option<Span> {
        self.root_expression_span?;
        let [operation] = self.operations.as_ref() else {
            return None;
        };
        let value = match operation.without_source() {
            LocalOperation::Return(Some(value)) | LocalOperation::Evaluate(value) => value,
            _ => return None,
        };
        match value.without_source() {
            LocalExpression::Call(LocalCall { span, .. }) => Some(*span),
            _ => None,
        }
    }

    /// Exact root syntax admitted by a source-validated initializer call
    /// effect input. Selection alone grants no operation or completion proof.
    pub(super) fn single_identifier_call_with_function_argument_initializer(
        &self,
    ) -> Option<(&Id, Span)> {
        self.root_expression_span?;
        let [operation] = self.operations.as_ref() else {
            return None;
        };
        let LocalOperation::Return(Some(value)) = operation.without_source() else {
            return None;
        };
        let LocalExpression::Call(LocalCall {
            invocation: LocalInvocation::Call,
            callee: LocalCallee::Binding(callee),
            arguments,
            spread_arguments,
            span,
            ..
        }) = value.without_source()
        else {
            return None;
        };
        if spread_arguments.is_empty()
            && matches!(
                arguments.as_ref(),
                [argument] if matches!(
                    argument.without_source(),
                    LocalExpression::Closure(DeferredCallableCreation::Function(_))
                )
            )
        {
            Some((callee, *span))
        } else {
            None
        }
    }

    /// Called by the canonical function collector. Unsupported syntax discards
    /// the entire candidate; the ordinary conservative lanes remain available.
    pub(super) fn lower<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        body: &FunctionBody,
    ) -> Option<Self> {
        let (mut bindings, parameters) = Self::lower_parameters(parameters)?;
        let has_defaults = parameters
            .iter()
            .any(|parameter| parameter.default.is_some());
        let mut activation_bindings = bindings.clone();
        let (operations, has_post_terminal_operations, _) = Self::lower_region(
            &body.stmts,
            &mut bindings,
            &mut activation_bindings,
            has_defaults,
            false,
        )?;
        let mut lowered = Self::from_operations(bindings, parameters, operations);
        lowered.activation_bindings = activation_bindings;
        lowered.source_span = Some(body.span);
        lowered.has_post_terminal_operations = has_post_terminal_operations;
        Some(lowered)
    }

    fn lower_region(
        statements: &[Stmt],
        bindings: &mut BTreeMap<Id, LocalBinding>,
        activation_bindings: &mut BTreeMap<Id, LocalBinding>,
        has_defaults: bool,
        nested: bool,
    ) -> Option<(Vec<LocalOperation>, bool, BTreeMap<Id, LocalBinding>)> {
        let mut operations = Vec::new();
        let mut region_bindings = BTreeMap::new();
        let mut terminal = false;
        let mut has_post_terminal_operations = false;
        for statement in statements {
            // Non-simple parameters have a separate body var environment.
            // The shared plan does not yet represent that second scope.
            if has_defaults
                && matches!(statement, Stmt::Decl(Decl::Var(declaration))
                    if declaration.kind == VarDeclKind::Var)
            {
                return None;
            }
            if terminal {
                // The canonical collector supplies all declaration identities
                // and kinds after visiting the complete body, including dead
                // pattern and loop declarations. Their initializers do not run.
                has_post_terminal_operations |= !matches!(statement, Stmt::Empty(_));
                continue;
            }
            match statement {
                Stmt::Empty(_) => {}
                Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                    let declaration_scope = if nested && declaration.kind != VarDeclKind::Var {
                        &mut region_bindings
                    } else {
                        &mut *activation_bindings
                    };
                    lower_var_declaration(
                        declaration,
                        false,
                        bindings,
                        declaration_scope,
                        &mut operations,
                    )?;
                }
                Stmt::Expr(expression) => operations.push(LocalOperation::sourced(
                    statement.span(),
                    OperationStep::Statement,
                    LocalOperation::Evaluate(LocalExpression::lower(&expression.expr, false)?),
                )),
                Stmt::Return(statement) => {
                    operations.push(LocalOperation::sourced(
                        statement.span,
                        OperationStep::Return,
                        LocalOperation::Return(LocalExpression::optional(
                            statement.arg.as_deref(),
                            false,
                        )?),
                    ));
                    terminal = true;
                }
                Stmt::Throw(statement) => {
                    operations.push(LocalOperation::sourced(
                        statement.span,
                        OperationStep::Throw,
                        LocalOperation::Throw(LocalExpression::lower(&statement.arg, false)?),
                    ));
                    terminal = true;
                }
                Stmt::If(statement) => {
                    let lowered_test = LocalExpression::lower(&statement.test, false)?
                        .without_source()
                        .clone();
                    let test = LocalExpression::sourced(
                        statement.test.span(),
                        OperationStep::IfTest,
                        lowered_test,
                    );
                    let (consequent, consequent_post_terminal) = Self::lower_branch_region(
                        &statement.cons,
                        bindings,
                        activation_bindings,
                        has_defaults,
                    )?;
                    let alternate = match statement.alt.as_deref() {
                        Some(statement) => Some(Self::lower_branch_region(
                            statement,
                            bindings,
                            activation_bindings,
                            has_defaults,
                        )?),
                        None => None,
                    };
                    has_post_terminal_operations |= consequent_post_terminal
                        || alternate
                            .as_ref()
                            .is_some_and(|(_, post_terminal)| *post_terminal);
                    operations.push(LocalOperation::sourced(
                        statement.span,
                        OperationStep::Statement,
                        LocalOperation::If {
                            test,
                            consequent,
                            alternate: alternate.map(|(region, _)| region),
                        },
                    ));
                }
                Stmt::Try(statement) if statement.finalizer.is_none() => {
                    let handler = statement.handler.as_ref()?;
                    let binding = match &handler.param {
                        Some(parameter) => Some(simple_binding(parameter)?),
                        None => None,
                    };
                    if let Some(binding) = &binding
                        && bindings
                            .insert(binding.clone(), LocalBinding::CatchParameter)
                            .is_some()
                    {
                        return None;
                    }
                    let (body_operations, body_post_terminal, body_bindings) = Self::lower_region(
                        &statement.block.stmts,
                        bindings,
                        activation_bindings,
                        has_defaults,
                        true,
                    )?;
                    let (handler_operations, handler_post_terminal, handler_bindings) =
                        Self::lower_region(
                            &handler.body.stmts,
                            bindings,
                            activation_bindings,
                            has_defaults,
                            true,
                        )?;
                    has_post_terminal_operations |= body_post_terminal || handler_post_terminal;
                    operations.push(LocalOperation::sourced(
                        statement.span,
                        OperationStep::Statement,
                        LocalOperation::TryCatch {
                            body: LocalRegion {
                                span: statement.block.span,
                                source_site: None,
                                bindings: body_bindings,
                                operations: body_operations.into(),
                            },
                            binding,
                            handler: LocalRegion {
                                span: handler.body.span,
                                source_site: None,
                                bindings: handler_bindings,
                                operations: handler_operations.into(),
                            },
                        },
                    ));
                }
                _ => return None,
            }
        }
        Some((operations, has_post_terminal_operations, region_bindings))
    }

    fn lower_branch_region(
        statement: &Stmt,
        bindings: &mut BTreeMap<Id, LocalBinding>,
        activation_bindings: &mut BTreeMap<Id, LocalBinding>,
        has_defaults: bool,
    ) -> Option<(LocalRegion, bool)> {
        let (statements, span) = match statement {
            Stmt::Block(block) => (block.stmts.as_slice(), block.span),
            _ => (std::slice::from_ref(statement), statement.span()),
        };
        let (operations, has_post_terminal_operations, region_bindings) = Self::lower_region(
            statements,
            bindings,
            activation_bindings,
            has_defaults,
            true,
        )?;
        Some((
            LocalRegion {
                span,
                source_site: None,
                bindings: region_bindings,
                operations: operations.into(),
            },
            has_post_terminal_operations,
        ))
    }

    /// A concise arrow has the same ordered return semantics as a block body.
    /// Retain that operation directly without manufacturing or revisiting AST.
    /// Its operation site is a semantic Return step at the original expression
    /// span, while the value itself keeps its Expression site.
    pub(super) fn lower_expression<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        expression: &Expr,
    ) -> Option<Self> {
        let (bindings, parameters) = Self::lower_parameters(parameters)?;
        let mut lowered = Self::from_operations(
            bindings,
            parameters,
            vec![LocalOperation::sourced(
                expression.span(),
                OperationStep::Return,
                LocalOperation::Return(Some(LocalExpression::lower(expression, false)?)),
            )],
        );
        lowered.source_span = Some(expression.span());
        Some(lowered)
    }

    /// Canonically lower the allocation effects of a base class constructor.
    /// The caller still proves binding stability and native identity; this
    /// collector retains every constructor-time field operation in order.
    pub(super) fn lower_class_constructor(class: &Class) -> Option<ClassConstructorBody> {
        if class.super_class.is_some() || !class.decorators.is_empty() {
            return None;
        }

        let mut constructor = None;
        let mut private_fields = BTreeSet::new();
        let mut field_operations = Vec::new();
        let mut callables = Vec::new();
        for member in &class.body {
            match member {
                ClassMember::Constructor(definition) if constructor.is_none() => {
                    callables.push(definition.span.lo.0);
                    constructor = Some(definition);
                }
                ClassMember::Constructor(_) => return None,
                ClassMember::PrivateProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if field.value.is_some() {
                            callables.push(field.span.lo.0);
                        }
                        let property = field.key.name.to_string();
                        private_fields.insert(property.clone());
                        field_operations.push(LocalOperation::sourced(
                            field.span,
                            OperationStep::DefineField,
                            LocalOperation::Evaluate(LocalExpression::sourced(
                                field.span,
                                OperationStep::DefineField,
                                LocalExpression::DefineThisField {
                                    property: ClassFieldKey::Private(property),
                                    value: Arc::new(match field.value.as_deref() {
                                        Some(value) => LocalExpression::lower(value, true)?,
                                        None => {
                                            LocalExpression::PrimitiveLiteral(PrimKinds::UNDEFINED)
                                        }
                                    }),
                                },
                            )),
                        ));
                    }
                }
                ClassMember::ClassProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if field.value.is_some() {
                            callables.push(field.span.lo.0);
                        }
                        if !matches!(
                            field.key,
                            PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                        ) {
                            return None;
                        }
                        field_operations.push(LocalOperation::sourced(
                            field.span,
                            OperationStep::DefineField,
                            LocalOperation::Evaluate(LocalExpression::sourced(
                                field.span,
                                OperationStep::DefineField,
                                LocalExpression::DefineThisField {
                                    property: ClassFieldKey::Public(static_property_name(
                                        &field.key,
                                    )?),
                                    value: Arc::new(match field.value.as_deref() {
                                        Some(value) => LocalExpression::lower(value, true)?,
                                        None => {
                                            LocalExpression::PrimitiveLiteral(PrimKinds::UNDEFINED)
                                        }
                                    }),
                                },
                            )),
                        ));
                    }
                }
                ClassMember::AutoAccessor(accessor) if !accessor.is_static => return None,
                // Installation and captures of instance private methods are
                // absent from this allocation/retention plan.
                ClassMember::PrivateMethod(method) if !method.is_static => return None,
                ClassMember::Method(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                ClassMember::PrivateMethod(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                _ => {}
            }
        }

        let mut bindings = BTreeMap::new();
        let mut declaration_scope = BTreeMap::new();
        let mut local_parameters = Vec::new();
        let mut constructor_operations = Vec::new();
        if let Some(constructor) = constructor {
            for parameter in &constructor.params {
                let ParamOrTsParamProp::Param(parameter) = parameter else {
                    return None;
                };
                if !parameter.decorators.is_empty() {
                    return None;
                }
                let Pat::Ident(binding) = &parameter.pat else {
                    return None;
                };
                if bindings
                    .insert(binding.id.to_id(), LocalBinding::Parameter)
                    .is_some()
                {
                    return None;
                }
                local_parameters.push(LocalParameter {
                    binding: binding.id.to_id(),
                    default: None,
                    rest_allocation: None,
                    rest_span: None,
                    rest_site: None,
                });
            }

            for statement in &constructor.body.as_ref()?.stmts {
                match statement {
                    Stmt::Empty(_) => {}
                    Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                        lower_var_declaration(
                            declaration,
                            true,
                            &mut bindings,
                            &mut declaration_scope,
                            &mut constructor_operations,
                        )?;
                    }
                    Stmt::Expr(expression) => {
                        let operation = LocalExpression::lower(&expression.expr, true)?;
                        fn checked_stores(
                            expression: &LocalExpression,
                            private_fields: &BTreeSet<String>,
                        ) -> bool {
                            match expression.without_source() {
                                LocalExpression::AssignPublicThisField { .. } => true,
                                LocalExpression::AssignPrivateThisField { property, .. } => {
                                    private_fields.contains(property)
                                }
                                LocalExpression::Sequence(expressions) => expressions
                                    .iter()
                                    .all(|expression| checked_stores(expression, private_fields)),
                                _ => false,
                            }
                        }
                        if !checked_stores(&operation, &private_fields) {
                            return None;
                        }
                        constructor_operations.push(LocalOperation::sourced(
                            statement.span(),
                            OperationStep::Statement,
                            LocalOperation::Evaluate(operation),
                        ));
                    }
                    Stmt::Return(statement) => {
                        constructor_operations.push(LocalOperation::sourced(
                            statement.span,
                            OperationStep::Return,
                            LocalOperation::Return(LocalExpression::optional(
                                statement.arg.as_deref(),
                                true,
                            )?),
                        ));
                        break;
                    }
                    _ => return None,
                }
            }
        }

        Some(ClassConstructorBody {
            class: class.span.lo.0,
            span: class.span,
            callables: callables.into(),
            initializers: Self::from_operations(BTreeMap::new(), Vec::new(), field_operations),
            constructor: Self::from_operations(bindings, local_parameters, constructor_operations),
        })
    }

    fn lower_parameters<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
    ) -> Option<(BTreeMap<Id, LocalBinding>, Vec<LocalParameter>)> {
        let parameters: Vec<_> = parameters.collect();
        let mut bindings = BTreeMap::new();
        let mut lowered = Vec::new();
        for (index, parameter) in parameters.iter().enumerate() {
            let (binding, default, rest_allocation, rest_span) = match parameter {
                Pat::Ident(_) => (simple_binding(parameter)?, None, None, None),
                Pat::Assign(assignment) => (
                    simple_binding(&assignment.left)?,
                    Some(LocalExpression::lower(&assignment.right, false)?),
                    None,
                    None,
                ),
                Pat::Rest(rest) if index + 1 == parameters.len() => (
                    simple_binding(&rest.arg)?,
                    None,
                    Some(rest.span.lo.0),
                    Some(rest.span),
                ),
                _ => return None,
            };
            let parameter = LocalParameter {
                binding,
                default,
                rest_allocation,
                rest_span,
                rest_site: None,
            };
            if bindings
                .insert(parameter.binding.clone(), LocalBinding::Parameter)
                .is_some()
            {
                return None;
            }
            lowered.push(parameter);
        }
        Some((bindings, lowered))
    }

    fn from_operations(
        bindings: BTreeMap<Id, LocalBinding>,
        parameters: Vec<LocalParameter>,
        operations: Vec<LocalOperation>,
    ) -> Self {
        fn collect_region_binding_ids(operations: &[LocalOperation], bindings: &mut BTreeSet<Id>) {
            for operation in operations {
                match operation.without_source() {
                    LocalOperation::If {
                        consequent,
                        alternate,
                        ..
                    } => {
                        bindings.extend(consequent.bindings.keys().cloned());
                        collect_region_binding_ids(&consequent.operations, bindings);
                        if let Some(alternate) = alternate {
                            bindings.extend(alternate.bindings.keys().cloned());
                            collect_region_binding_ids(&alternate.operations, bindings);
                        }
                    }
                    LocalOperation::TryCatch { body, handler, .. } => {
                        bindings.extend(body.bindings.keys().cloned());
                        bindings.extend(handler.bindings.keys().cloned());
                        collect_region_binding_ids(&body.operations, bindings);
                        collect_region_binding_ids(&handler.operations, bindings);
                    }
                    _ => {}
                }
            }
        }
        fn contains_if(operations: &[LocalOperation]) -> bool {
            operations
                .iter()
                .any(|operation| match operation.without_source() {
                    LocalOperation::If { .. } => true,
                    LocalOperation::TryCatch { body, handler, .. } => {
                        contains_if(&body.operations) || contains_if(&handler.operations)
                    }
                    _ => false,
                })
        }
        let expressions = || {
            parameters
                .iter()
                .filter_map(|parameter| parameter.default.as_ref())
                .chain(Self::expressions(&operations))
        };
        let mut reads = BTreeSet::new();
        for expression in expressions() {
            expression.reads(&mut reads);
        }
        reads.retain(|binding| !bindings.contains_key(binding));
        let mut writes = BTreeSet::new();
        for expression in expressions() {
            expression.writes(&mut writes);
        }
        writes.retain(|binding| !bindings.contains_key(binding));
        // Provider execution does not consume defaults yet. Retaining their
        // source plan must not admit a previously refused provider summary.
        let no_defaults = parameters
            .iter()
            .all(|parameter| parameter.default.is_none());
        let (mut allocation_candidate_syntax, mut closure_candidate_syntax) =
            (no_defaults, no_defaults);
        let mut closure_sites = BTreeSet::new();
        for expression in expressions() {
            let (allocation, closure) = expression.allocation_syntax(&bindings, &mut closure_sites);
            allocation_candidate_syntax &= allocation;
            closure_candidate_syntax &= closure;
        }
        let has_if = contains_if(&operations);
        allocation_candidate_syntax &= !has_if;
        closure_candidate_syntax &= !operations.iter().any(|operation| {
            matches!(
                operation.without_source(),
                LocalOperation::Throw(..)
                    | LocalOperation::TryCatch { .. }
                    | LocalOperation::If { .. }
            )
        });
        closure_candidate_syntax &= !closure_sites.is_empty();
        let requires_keyed_collection_new =
            expressions().any(LocalExpression::requires_keyed_collection_new);
        let closure_sites: Arc<[_]> = closure_sites.into_iter().collect();
        let activation_bindings = bindings.clone();
        let mut region_binding_ids = BTreeSet::new();
        collect_region_binding_ids(&operations, &mut region_binding_ids);
        Self {
            bindings,
            activation_bindings,
            region_binding_ids,
            has_unscoped_lexical_bindings: false,
            parameters: parameters.into(),
            operations: operations.into(),
            source_span: None,
            source_region_site: None,
            root_expression_span: None,
            free_reads: (!reads.is_empty()).then(|| reads.into_iter().collect()),
            captured_writes: (!writes.is_empty()).then(|| writes.into_iter().collect()),
            allocation_candidate_syntax,
            closure_candidate_syntax,
            closure_sites,
            has_post_terminal_operations: false,
            requires_keyed_collection_new,
            source_operation_sites: BTreeSet::new(),
        }
    }

    pub(super) fn bind_source_operation_sites(
        &mut self,
        identity: &LocalBodyIdentity,
        additional_call_spans: impl IntoIterator<Item = Span>,
    ) -> &BTreeSet<SourceOperationSite> {
        let mut sites = BTreeSet::new();
        let mut ordinal = 0;
        self.bind_source_operation_sites_into(
            identity,
            &mut ordinal,
            &mut sites,
            additional_call_spans,
        );
        self.source_operation_sites = sites;
        &self.source_operation_sites
    }

    pub(super) fn bind_source_operation_sites_into(
        &mut self,
        identity: &LocalBodyIdentity,
        ordinal: &mut u32,
        sites: &mut BTreeSet<SourceOperationSite>,
        additional_call_spans: impl IntoIterator<Item = Span>,
    ) {
        let mut binder = SourceOperationSiteBinding::Sequential {
            ordinal: *ordinal,
            sites: BTreeSet::new(),
        };
        self.bind_source_operation_sites_with_binder(
            identity,
            &mut binder,
            sites,
            additional_call_spans,
        )
        .expect("sequential source-site binding always creates a site");
        *ordinal = binder.ordinal();
        sites.extend(binder.into_sites());
    }

    /// Rebind a stored transfer plan to source sites already produced by the
    /// canonical root/callable lowering. A detached plan may select existing
    /// sites, but it cannot create new source identities or ordinals.
    pub(super) fn bind_source_operation_sites_from_registry(
        &mut self,
        identity: &LocalBodyIdentity,
        canonical_sites: &BTreeSet<SourceOperationSite>,
    ) -> bool {
        let mut binder = SourceOperationSiteBinding::Registry {
            canonical: canonical_sites,
            sites: BTreeSet::new(),
        };
        self.source_region_site = None;
        self.source_operation_sites.clear();
        let previous_sites = BTreeSet::new();
        let bound = self
            .bind_source_operation_sites_with_binder(
                identity,
                &mut binder,
                &previous_sites,
                std::iter::empty(),
            )
            .is_some();
        if bound {
            self.source_operation_sites = binder.into_sites();
        }
        bound
    }

    fn bind_source_operation_sites_with_binder(
        &mut self,
        identity: &LocalBodyIdentity,
        binder: &mut SourceOperationSiteBinding<'_>,
        previously_bound_sites: &BTreeSet<SourceOperationSite>,
        additional_call_spans: impl IntoIterator<Item = Span>,
    ) -> Option<()> {
        let owner = match identity {
            LocalBodyIdentity::RootInitializer { owner, .. }
            | LocalBodyIdentity::RootClassDeclaration { owner, .. }
            | LocalBodyIdentity::RootExpression { owner, .. }
            | LocalBodyIdentity::Callable { owner, .. }
            | LocalBodyIdentity::Construction { owner, .. } => *owner,
            LocalBodyIdentity::ImportedCallable(_)
            | LocalBodyIdentity::ProviderCallable { .. }
            | LocalBodyIdentity::Native { .. }
            | LocalBodyIdentity::NativeConstruction { .. } => {
                unreachable!("local operation bodies have source owners")
            }
        };
        if let Some(span) = self.source_span {
            self.source_region_site =
                Some(binder.bind(owner, identity, span, OperationStep::BodyRegion)?);
        }
        for parameter in Arc::make_mut(&mut self.parameters) {
            parameter.rest_site = if let Some(span) = parameter.rest_span {
                Some(binder.bind(owner, identity, span, OperationStep::Allocate)?)
            } else {
                None
            };
            if let Some(default) = &mut parameter.default {
                default.bind_source_sites(owner, identity, binder)?;
            }
        }
        for operation in Arc::make_mut(&mut self.operations) {
            operation.bind_source_sites(owner, identity, binder)?;
        }
        let mut calls: Vec<_> = additional_call_spans.into_iter().collect();
        calls.sort_by_key(|span| (span.lo.0, span.hi.0));
        calls.dedup();
        for span in calls {
            let already_present = binder
                .sites()
                .iter()
                .chain(previously_bound_sites.iter())
                .any(|site: &SourceOperationSite| {
                    site.owner == owner
                        && site.body == *identity
                        && site.span == span
                        && site.step == OperationStep::CallInvocation
                });
            if already_present {
                continue;
            }
            binder.bind(owner, identity, span, OperationStep::CallInvocation)?;
        }
        Some(())
    }

    pub(super) fn with_declarations(
        mut self,
        variables: impl IntoIterator<Item = Id>,
        lexical: impl IntoIterator<Item = (Id, LocalBinding)>,
    ) -> Self {
        for binding in variables {
            self.bindings
                .entry(binding.clone())
                .or_insert(LocalBinding::Var);
            self.activation_bindings
                .entry(binding)
                .or_insert(LocalBinding::Var);
        }
        for (binding, kind) in lexical {
            self.bindings.insert(binding.clone(), kind);
            match kind {
                LocalBinding::CatchParameter => {}
                LocalBinding::Let | LocalBinding::Const
                    if !self.region_binding_ids.contains(&binding)
                        && !self.activation_bindings.contains_key(&binding) =>
                {
                    // The source census saw a lexical declaration that this
                    // ordered body did not place into a represented scope.
                    // Refuse the partial body instead of guessing its lifetime.
                    self.has_unscoped_lexical_bindings = true;
                }
                _ if !self.region_binding_ids.contains(&binding) => {
                    self.activation_bindings.insert(binding, kind);
                }
                _ => {}
            }
        }
        self.free_reads = self.free_reads.as_ref().and_then(|reads| {
            let reads: Arc<[_]> = reads
                .iter()
                .filter(|binding| !self.bindings.contains_key(*binding))
                .cloned()
                .collect();
            (!reads.is_empty()).then_some(reads)
        });
        self.captured_writes = self.captured_writes.as_ref().and_then(|writes| {
            let writes: Arc<[_]> = writes
                .iter()
                .filter(|binding| !self.bindings.contains_key(*binding))
                .cloned()
                .collect();
            (!writes.is_empty()).then_some(writes)
        });
        self
    }

    pub(super) fn free_reads(&self) -> &[Id] {
        self.free_reads.as_deref().unwrap_or_default()
    }

    pub(super) fn closure_sites(&self) -> &[u32] {
        &self.closure_sites
    }

    pub(super) fn root_expression_span(&self) -> Option<Span> {
        self.root_expression_span
    }

    pub(super) fn direct_set_construction_root(&self) -> bool {
        let [operation] = self.operations.as_ref() else {
            return false;
        };
        self.root_expression_span.is_some()
            && matches!(operation.without_source(), LocalOperation::Return(Some(value))
                if matches!(value.without_source(), LocalExpression::SetConstruction { .. }))
    }

    pub(super) fn captured_writes(&self) -> &[Id] {
        self.captured_writes.as_deref().unwrap_or_default()
    }

    /// Static admission only for provider-local operation support. The shared
    /// local transfer remains the sole value/effect evaluator; this predicate
    /// admits only a straight-line body whose only possible publication is its
    /// one final return.
    pub(super) fn provider_local_birth_candidate(&self) -> bool {
        if !self.allocation_candidate_syntax
            || self
                .parameters
                .iter()
                .any(|parameter| parameter.default.is_some())
            || !self.captured_writes().is_empty()
            || !self.closure_sites.is_empty()
            || self.has_post_terminal_operations
            || self.bindings.values().any(|kind| {
                !matches!(
                    kind,
                    LocalBinding::Parameter | LocalBinding::Let | LocalBinding::Const
                )
            })
        {
            return false;
        }

        let Some((terminal, prefix)) = self.operations.split_last() else {
            return false;
        };
        if !matches!(terminal.without_source(), LocalOperation::Return(Some(value)) if self.provider_birth_expression(value))
        {
            return false;
        }
        prefix
            .iter()
            .all(|operation| match operation.without_source() {
                LocalOperation::Initialize(binding, Some(value)) => {
                    matches!(
                        self.bindings.get(binding),
                        Some(LocalBinding::Let | LocalBinding::Const)
                    ) && self.provider_birth_expression(value)
                }
                LocalOperation::Evaluate(value) => self.provider_birth_expression(value),
                LocalOperation::Initialize(..)
                | LocalOperation::InitializeObject { .. }
                | LocalOperation::Return(..)
                | LocalOperation::Throw(..)
                | LocalOperation::If { .. }
                | LocalOperation::TryCatch { .. } => false,
                LocalOperation::Sourced { .. } => {
                    unreachable!("source wrapper is removed before candidate checks")
                }
            })
    }

    fn provider_birth_expression(&self, expression: &LocalExpression) -> bool {
        match expression.without_source() {
            LocalExpression::Value(ValueExpr::Binding(_) | ValueExpr::Undefined)
            | LocalExpression::PrimitiveLiteral(_) => true,
            LocalExpression::Record { properties, .. } => properties.iter().all(|property| {
                matches!(property, PropertyDefinition::Data { name: Some(name), value }
                    if name != "__proto__" && self.provider_birth_expression(value))
            }),
            LocalExpression::Sequence(expressions) => expressions
                .iter()
                .all(|expression| self.provider_birth_expression(expression)),
            LocalExpression::Assign { binding, value, .. } => {
                self.bindings.contains_key(binding) && self.provider_birth_expression(value)
            }
            LocalExpression::ReadField {
                receiver,
                optional_receiver,
                ..
            } => {
                !optional_receiver
                    && matches!(
                        receiver.without_source(),
                        LocalExpression::Value(ValueExpr::Binding(binding))
                            if self.bindings.contains_key(binding)
                    )
            }
            LocalExpression::AssignField {
                receiver, value, ..
            } => self.bindings.contains_key(receiver) && self.provider_birth_expression(value),
            LocalExpression::Binary { .. }
            | LocalExpression::Conditional { .. }
            | LocalExpression::Logical { .. }
            | LocalExpression::Template { .. }
            | LocalExpression::This
            | LocalExpression::Array { .. }
            | LocalExpression::SetConstruction { .. }
            | LocalExpression::UpdateBinding { .. }
            | LocalExpression::Closure(_)
            | LocalExpression::ClassDefinition(_)
            | LocalExpression::Call(_)
            | LocalExpression::DefineThisField { .. }
            | LocalExpression::AssignPrivateThisField { .. }
            | LocalExpression::AssignPublicThisField { .. }
            | LocalExpression::Value(_) => false,
            LocalExpression::Sourced { .. } => {
                unreachable!("source wrapper is removed before candidate checks")
            }
        }
    }

    /// Grammar candidate for the shared allocation consumer. Calls still need
    /// actual callee selection and body transfer; captured writes remain
    /// unsupported. A refusal never retries another evaluator.
    pub(super) fn allocation_candidate_syntax(&self) -> bool {
        self.allocation_candidate_syntax
    }

    pub(super) fn requires_keyed_collection_new(&self) -> bool {
        self.requires_keyed_collection_new
    }

    /// Also includes callable creation. Capture availability and returned
    /// reference retention remain independent consumer obligations.
    pub(super) fn closure_candidate_syntax(&self) -> bool {
        self.closure_candidate_syntax
    }

    fn expressions(operations: &[LocalOperation]) -> std::vec::IntoIter<&LocalExpression> {
        let mut expressions = Vec::new();
        for operation in operations {
            match operation.without_source() {
                LocalOperation::Initialize(_, value) | LocalOperation::Return(value) => {
                    expressions.extend(value)
                }
                LocalOperation::Evaluate(value) | LocalOperation::Throw(value) => {
                    expressions.push(value)
                }
                LocalOperation::InitializeObject { value, .. } => expressions.push(value),
                LocalOperation::If {
                    test,
                    consequent,
                    alternate,
                } => {
                    expressions.push(test);
                    expressions.extend(Self::expressions(&consequent.operations));
                    if let Some(alternate) = alternate {
                        expressions.extend(Self::expressions(&alternate.operations));
                    }
                }
                LocalOperation::TryCatch { body, handler, .. } => {
                    expressions.extend(Self::expressions(&body.operations));
                    expressions.extend(Self::expressions(&handler.operations));
                }
                LocalOperation::Sourced { .. } => {
                    unreachable!("source wrapper is removed before expression collection")
                }
            }
        }
        expressions.into_iter()
    }
}

#[cfg(test)]
mod implicit_result_source_site_tests {
    use super::{
        ClassDefinition, ClassStaticStep, LocalBody, LocalBodyIdentity, LocalExpression,
        OperationStep,
    };
    use source_identity::SourceUnitOrdinal;
    use swc_common::Spanned;
    use swc_ecma_ast::{ArrowFunctionBody, Decl, Expr, ModuleItem, Stmt};

    fn assert_single_result_site(body: &mut LocalBody, identity: &LocalBodyIdentity) {
        let sites = body
            .bind_source_operation_sites(identity, std::iter::empty())
            .clone();
        let [operation] = body.operations.as_ref() else {
            panic!("result body should lower to one semantic operation")
        };
        assert_eq!(
            operation.source_site().map(|site| site.step),
            Some(OperationStep::Return),
            "the lowered operation site must identify its semantic return"
        );
        assert!(sites.iter().any(|site| site.step == OperationStep::Return));

        let super::LocalOperation::Return(Some(value)) = operation.without_source() else {
            panic!("lowered result operation")
        };
        assert_eq!(
            value
                .source_site_for_step(OperationStep::Expression)
                .map(|site| site.step),
            Some(OperationStep::Expression),
            "the returned expression keeps its separate source step"
        );
    }

    #[test]
    fn explicit_concise_and_root_results_share_return_site_semantics() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "implicit-result.js",
                "function explicit() { return 1; } const concise = () => 1; const root = 1; class C {}",
            )
            .expect("test source parses");

            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(explicit))) = &parsed.module.body[0] else {
                panic!("explicit function declaration")
            };
            let explicit_body = explicit.function.body.as_ref().expect("function body");
            let mut explicit_plan = LocalBody::lower(
                explicit
                    .function
                    .params
                    .iter()
                    .map(|parameter| &parameter.pat),
                explicit_body,
            )
            .expect("explicit return lowers");
            assert_single_result_site(
                &mut explicit_plan,
                &LocalBodyIdentity::Callable {
                    key: explicit.function.span.lo.0,
                    owner: SourceUnitOrdinal(0),
                },
            );

            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(concise_decl))) = &parsed.module.body[1]
            else {
                panic!("concise arrow declaration")
            };
            let Expr::Arrow(concise) = concise_decl.decls[0]
                .init
                .as_deref()
                .expect("concise arrow initializer")
            else {
                panic!("concise arrow")
            };
            let ArrowFunctionBody::Expr(expression) = concise.body.as_ref() else {
                panic!("concise arrow body")
            };
            let mut concise_plan = LocalBody::lower_expression(concise.params.iter(), expression)
                .expect("concise result lowers");
            assert_single_result_site(
                &mut concise_plan,
                &LocalBodyIdentity::Callable {
                    key: concise.span.lo.0,
                    owner: SourceUnitOrdinal(0),
                },
            );

            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(root_decl))) = &parsed.module.body[2] else {
                panic!("root initializer declaration")
            };
            let expression = root_decl.decls[0]
                .init
                .as_deref()
                .expect("root initializer expression");
            let mut root_plan = LocalBody::from_initializer(expression).expect("root lowers");
            assert_single_result_site(
                &mut root_plan,
                &LocalBodyIdentity::RootInitializer {
                    owner: SourceUnitOrdinal(0),
                    initializer_span: expression.span(),
                },
            );

            let ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) = &parsed.module.body[3] else {
                panic!("class declaration")
            };
            let definition = ClassDefinition::lower(&class.class).expect("plain class lowers");
            let mut class_plan = LocalBody::from_class_declaration(&definition);
            assert_single_result_site(
                &mut class_plan,
                &LocalBodyIdentity::RootClassDeclaration {
                    owner: SourceUnitOrdinal(0),
                    declaration_span: class.class.span,
                },
            );
        });
    }

    #[test]
    fn class_static_plan_preserves_definition_order_and_rejects_partial_shapes() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "class-static-plan.js",
                "class C { static first = 1; static { helper(); } static second = 2; static method() {} }",
            )
            .expect("test source parses");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) = &parsed.module.body[0] else {
                panic!("class declaration")
            };
            let definition = ClassDefinition::lower(&class.class).expect("supported class plan");
            assert_eq!(
                definition.static_steps.len(),
                3,
                "static fields and blocks retain source order"
            );
            assert!(matches!(
                &definition.static_steps[0],
                ClassStaticStep::Field { property, .. } if property == "first"
            ));
            assert!(matches!(
                &definition.static_steps[1],
                ClassStaticStep::Block { expressions, .. } if expressions.len() == 1
            ));
            assert!(matches!(
                &definition.static_steps[2],
                ClassStaticStep::Field { property, .. } if property == "second"
            ));
            assert!(definition.static_member_descriptors.contains_key("method"));

            for source in [
                "class C extends Base { static field = 1; }",
                "class C { static [key] = 1; }",
                "class C { static field = 1; static { const local = 2; } }",
            ] {
                let parsed = js_ast::parse_js_module("unsupported-class.js", source)
                    .expect("negative test source parses");
                let ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) = &parsed.module.body[0]
                else {
                    panic!("class declaration")
                };
                assert!(
                    ClassDefinition::lower(&class.class).is_none(),
                    "unsupported class shape must reject before transfer: {source}"
                );
            }
        });
    }

    #[test]
    fn static_receiver_escape_checks_recurse_conditional_logical_and_optional_operands() {
        js_ast::with_swc_globals(|| {
            for (source, escapes) in [
                (
                    "class C { static field = external(flag ? this : null); }",
                    true,
                ),
                (
                    "class C { static field = external(flag && (() => this)); }",
                    true,
                ),
                ("class C { static field = this?.method(); }", true),
                (
                    "class C { static field = other?.method(flag ? this : null); }",
                    true,
                ),
                (
                    "class C { static field = flag ? external(this) : null; }",
                    true,
                ),
                (
                    "class C { static field = flag ? ordinary() : null; }",
                    false,
                ),
                ("class C { static field = () => this; }", false),
            ] {
                let parsed = js_ast::parse_js_module("class-receiver-escape.js", source)
                    .expect("test source parses");
                let ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) = &parsed.module.body[0]
                else {
                    panic!("class declaration")
                };
                let definition =
                    ClassDefinition::lower(&class.class).expect("canonical static plan");
                let ClassStaticStep::Field { value, .. } = &definition.static_steps[0] else {
                    panic!("static field")
                };
                assert_eq!(value.class_step_may_escape_receiver(), escapes, "{source}");
            }
        });
    }

    #[test]
    fn anonymous_class_expression_uses_static_transfer_and_named_one_fails_closed() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "class-expression-plan.js",
                "const Anonymous = class { static value = 1; }; const Named = class Named { static value = Named; };",
            )
            .expect("test source parses");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = &parsed.module.body[0]
            else {
                panic!("anonymous class initializer")
            };
            let Expr::Class(anonymous) = declaration.decls[0]
                .init
                .as_deref()
                .expect("initializer")
                .unwrap_parens()
            else {
                panic!("anonymous class expression")
            };
            assert!(matches!(
                LocalExpression::lower(&Expr::Class(anonymous.clone()), true),
                Some(LocalExpression::Sourced { expression, .. })
                    if matches!(*expression, LocalExpression::ClassDefinition(_))
            ));

            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = &parsed.module.body[1]
            else {
                panic!("named class initializer")
            };
            let Expr::Class(named) = declaration.decls[0]
                .init
                .as_deref()
                .expect("initializer")
                .unwrap_parens()
            else {
                panic!("named class expression")
            };
            assert!(
                LocalExpression::lower(&Expr::Class(named.clone()), true).is_none(),
                "named class expression static steps require the unmodeled inner name environment"
            );
        });
    }

    #[test]
    fn conditional_test_and_arms_keep_distinct_source_sites() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "conditional-sites.js",
                "function choose(flag) { if (flag) { const selected = 1; return selected; } else { throw flag; } flag; }",
            )
            .expect("test source parses");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) = &parsed.module.body[0] else {
                panic!("function declaration")
            };
            let source_body = function.function.body.as_ref().expect("function body");
            let mut plan = LocalBody::lower(
                function
                    .function
                    .params
                    .iter()
                    .map(|parameter| &parameter.pat),
                source_body,
            )
            .expect("conditional body lowers");
            let identity = LocalBodyIdentity::Callable {
                key: function.function.span.lo.0,
                owner: SourceUnitOrdinal(0),
            };
            let sites = plan
                .bind_source_operation_sites(&identity, std::iter::empty())
                .clone();

            let [conditional, suffix] = plan.operations.as_ref() else {
                panic!("conditional and following operation are retained")
            };
            assert!(matches!(
                suffix.without_source(),
                super::LocalOperation::Evaluate(_)
            ));
            let super::LocalOperation::If {
                test,
                consequent,
                alternate: Some(alternate),
            } = conditional.without_source()
            else {
                panic!("if lowers with both branch regions")
            };

            assert_eq!(
                test.source_site_for_step(OperationStep::IfTest)
                    .map(|site| site.step),
                Some(OperationStep::IfTest)
            );
            assert_eq!(
                consequent.source_site.as_ref().map(|site| site.step),
                Some(OperationStep::IfConsequent)
            );
            assert_eq!(
                alternate.source_site.as_ref().map(|site| site.step),
                Some(OperationStep::IfAlternate)
            );
            let test_site = test
                .source_site_for_step(OperationStep::IfTest)
                .expect("if test source site");
            assert_eq!(
                consequent
                    .source_site
                    .as_ref()
                    .and_then(|site| site.control_test_ordinal),
                Some(test_site.ordinal)
            );
            assert_eq!(
                alternate
                    .source_site
                    .as_ref()
                    .and_then(|site| site.control_test_ordinal),
                Some(test_site.ordinal)
            );
            assert!(sites.iter().any(|site| site.step == OperationStep::IfTest));
            assert!(
                sites
                    .iter()
                    .any(|site| site.step == OperationStep::IfConsequent)
            );
            assert!(
                sites
                    .iter()
                    .any(|site| site.step == OperationStep::IfAlternate)
            );
        });
    }

    #[test]
    fn conditional_and_logical_expressions_keep_typed_source_sites() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "conditional-expression-sites.js",
                "function choose(flag) { return flag ? left() && right() : fallback(); }",
            )
            .expect("test source parses");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) = &parsed.module.body[0] else {
                panic!("function declaration")
            };
            let source_body = function.function.body.as_ref().expect("function body");
            let mut plan = LocalBody::lower(
                function
                    .function
                    .params
                    .iter()
                    .map(|parameter| &parameter.pat),
                source_body,
            )
            .expect("conditional expression body lowers");
            let identity = LocalBodyIdentity::Callable {
                key: function.function.span.lo.0,
                owner: SourceUnitOrdinal(0),
            };
            let sites = plan
                .bind_source_operation_sites(&identity, std::iter::empty())
                .clone();

            let [operation] = plan.operations.as_ref() else {
                panic!("one return operation")
            };
            let super::LocalOperation::Return(Some(return_value)) = operation.without_source()
            else {
                panic!("return operation")
            };
            let super::LocalExpression::Conditional {
                consequent,
                alternate,
                ..
            } = return_value.without_source()
            else {
                panic!("conditional expression")
            };
            assert!(
                return_value
                    .source_site_for_step(OperationStep::ConditionalTest)
                    .is_some()
            );
            assert!(
                return_value
                    .source_site_for_step(OperationStep::ConditionalConsequent)
                    .is_some()
            );
            assert!(
                return_value
                    .source_site_for_step(OperationStep::ConditionalAlternate)
                    .is_some()
            );

            let super::LocalExpression::Logical { left, right, .. } = consequent.without_source()
            else {
                panic!("logical-and consequent")
            };
            assert_eq!(
                left.source_span(),
                Some(
                    left.source_site_for_step(OperationStep::Expression)
                        .unwrap()
                        .span
                )
            );
            assert!(
                consequent
                    .source_site_for_step(OperationStep::LogicalTest)
                    .is_some()
            );
            assert!(
                consequent
                    .source_site_for_step(OperationStep::LogicalRight)
                    .is_some()
            );
            assert!(
                left.source_site_for_step(OperationStep::CallInvocation)
                    .is_some()
            );
            assert!(
                right
                    .source_site_for_step(OperationStep::CallInvocation)
                    .is_some()
            );
            assert!(matches!(
                alternate.without_source(),
                super::LocalExpression::Call(_)
            ));
            for step in [
                OperationStep::ConditionalTest,
                OperationStep::ConditionalConsequent,
                OperationStep::ConditionalAlternate,
                OperationStep::LogicalTest,
                OperationStep::LogicalRight,
            ] {
                assert!(sites.iter().any(|site| site.step == step));
            }
        });
    }
}

#[cfg(test)]
mod optional_member_lowering_tests {
    use super::{
        LocalBody, LocalBodyIdentity, LocalCallee, LocalExpression, LocalInvocation,
        LocalOperation, OperationStep,
    };
    use source_identity::SourceUnitOrdinal;
    use swc_ecma_ast::{Decl, ModuleItem, Stmt};

    #[test]
    fn parentheses_do_not_extend_optional_call_short_circuit() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "optional-call-parentheses.js",
                "function call() { return (receiver?.method)(); }",
            )
            .expect("test source parses");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) = &parsed.module.body[0] else {
                panic!("function declaration")
            };
            assert!(
                LocalBody::lower(
                    function
                        .function
                        .params
                        .iter()
                        .map(|parameter| &parameter.pat),
                    function.function.body.as_ref().unwrap(),
                )
                .is_none(),
                "a detached optional reference requires a nonoptional invocation"
            );
        });
    }

    #[test]
    fn member_optional_receiver_and_callee_have_distinct_ordered_sites() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module(
                "optional-member-call-sites.js",
                "function receiverOptional() { return receiverFactory()?.method(argument()); }\n\
                 function calleeOptional() { return receiverFactory().method?.(argument()); }\n\
                 function bothOptional() { return receiverFactory()?.method?.(argument()); }\n",
            )
            .expect("test source parses");
            let expected = [
                ("receiverOptional", true, false),
                ("calleeOptional", false, true),
                ("bothOptional", true, true),
            ];

            for (index, (name, optional_receiver, optional_callee)) in
                expected.into_iter().enumerate()
            {
                let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) = &parsed.module.body[index]
                else {
                    panic!("{name} function declaration")
                };
                let source_body = function.function.body.as_ref().expect("function body");
                let identity = LocalBodyIdentity::Callable {
                    key: function.function.span.lo.0,
                    owner: SourceUnitOrdinal(0),
                };
                let mut body = LocalBody::lower(
                    function
                        .function
                        .params
                        .iter()
                        .map(|parameter| &parameter.pat),
                    source_body,
                )
                .expect("optional member call lowers");
                let sites = body
                    .bind_source_operation_sites(&identity, std::iter::empty())
                    .clone();
                let [operation] = body.operations.as_ref() else {
                    panic!("one return operation")
                };
                let LocalOperation::Return(Some(expression)) = operation.without_source() else {
                    panic!("return expression")
                };
                let LocalExpression::Call(call) = expression.without_source() else {
                    panic!("member invocation lowers to a call")
                };
                let LocalCallee::Member {
                    receiver,
                    optional_receiver: actual_optional_receiver,
                    ..
                } = &call.callee
                else {
                    panic!("member callee is preserved")
                };
                assert_eq!(*actual_optional_receiver, optional_receiver, "{name}");
                assert!(matches!(
                    receiver.without_source(),
                    LocalExpression::Call(_)
                ));
                assert_eq!(
                    matches!(call.invocation, LocalInvocation::OptionalCall),
                    optional_callee,
                    "{name}"
                );

                let call_has_step = |step| {
                    sites
                        .iter()
                        .any(|site| site.span == call.span && site.step == step)
                };
                assert!(call_has_step(OperationStep::CalleeReceiver), "{name}");
                assert_eq!(
                    call_has_step(OperationStep::OptionalReceiverCheck),
                    optional_receiver,
                    "{name}"
                );
                assert!(call_has_step(OperationStep::CallCalleeGetValue), "{name}");
                assert_eq!(
                    call_has_step(OperationStep::OptionalCalleeCheck),
                    optional_callee,
                    "{name}"
                );
                assert!(call_has_step(OperationStep::CallInvocation), "{name}");

                let invocation_sites = sites
                    .iter()
                    .filter(|site| site.step == OperationStep::CallInvocation)
                    .count();
                assert_eq!(
                    invocation_sites, 3,
                    "receiver, argument, and member calls: {name}"
                );
                assert!(
                    body.free_reads()
                        .iter()
                        .any(|binding| binding.0.as_ref() == "receiverFactory"),
                    "nested receiver call remains in the binding inventory: {name}"
                );
                assert!(
                    body.free_reads()
                        .iter()
                        .any(|binding| binding.0.as_ref() == "argument"),
                    "argument call remains in the binding inventory: {name}"
                );
            }
        });
    }
}
