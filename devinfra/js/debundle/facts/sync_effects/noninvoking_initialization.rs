//! Whole-source-unit coverage for a small, non-invoking initializer subset.

use super::allocation_transfer::{AllocationJudgment, AllocationValue};
use super::completion::{Completion, TransferLedger};
use super::effect_coverage::RootEffectIdentity;
use super::value_model::{AbstractValue, CallSite, ValueExpr};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::abstract_domain::value::{HeapAddress, Kind, SourceHeapResolver, Value};
use crate::facts::item_views::{SplitDeclaratorProvenance, TopLevelItemView, var_decl_of_item};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum NativeConstructorKind {
    Set,
    TextEncoder,
}

/// Source form whose exact dedicated New dispatch may complete this owner.
/// The array identity is retained so the lowered operand must be the same
/// direct source literal collected before syntax lowering.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum NativeConstructorForm {
    Empty,
    SetPrimitiveArray { array: u32 },
}

impl NativeConstructorKind {
    pub(super) fn global_name(self) -> &'static str {
        match self {
            Self::Set => "Set",
            Self::TextEncoder => "TextEncoder",
        }
    }
}

#[derive(Clone)]
enum Leaf {
    Composed(ValueExpr),
    Function(u32),
    RegExp(u32),
    /// Exact direct source form; only a checked operation receipt may cover it.
    KeyedCollectionOperation {
        callee: Id,
        call_site: CallSite,
    },
    NativeConstructor {
        callee: Id,
        call_site: CallSite,
        kind: NativeConstructorKind,
        form: NativeConstructorForm,
    },
}

/// Collected from the original owner before ValueExpr can erase syntax.
/// This says nothing about executing a function's lazy body.
#[derive(Clone)]
pub(super) struct OriginalInitializer {
    identity: InitializationSourceIdentity,
    leaf: Leaf,
}

pub(super) struct EvaluatedInitializer {
    owner: SourceUnitOrdinal,
    initializer_span: Span,
    dependencies: ProofDependencies,
}

/// A candidate exists only while one dedicated root initializer is evaluated.
/// Its matching transfer is recorded by that root's native New dispatch.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct NativeConstructorCandidate {
    owner: SourceUnitOrdinal,
    call_site: CallSite,
    kind: NativeConstructorKind,
    form: NativeConstructorForm,
}

/// Evidence that the exact root New dispatch selected and applied the native
/// constructor row for its collected source form. This is not a value identity
/// or a heap-shape fact.
#[derive(Clone, PartialEq, Eq)]
pub(super) struct NativeConstructorTransfer {
    owner: SourceUnitOrdinal,
    call_site: CallSite,
    kind: NativeConstructorKind,
    form: NativeConstructorForm,
    dependencies: ProofDependencies,
}

impl NativeConstructorTransfer {
    pub(super) fn applied(
        candidate: NativeConstructorCandidate,
        dependencies: ProofDependencies,
    ) -> Self {
        Self {
            owner: candidate.owner,
            call_site: candidate.call_site,
            kind: candidate.kind,
            form: candidate.form,
            dependencies,
        }
    }

    fn matches(&self, candidate: NativeConstructorCandidate) -> bool {
        self.owner == candidate.owner
            && self.call_site == candidate.call_site
            && self.kind == candidate.kind
            && self.form == candidate.form
    }
}

impl NativeConstructorCandidate {
    pub(super) fn kind(self) -> NativeConstructorKind {
        self.kind
    }

    fn matches_set_primitive_array_allocation(
        self,
        resolver: SourceHeapResolver,
        allocation: HeapAddress,
    ) -> bool {
        matches!(
            (self.kind, self.form),
            (
                NativeConstructorKind::Set,
                NativeConstructorForm::SetPrimitiveArray { .. }
            )
        ) && allocation.kind() == Some(Kind::Set)
            && resolver.source_origin(allocation) == Some(self.call_site.allocation_id())
    }

    pub(super) fn matches_dispatch(
        self,
        call_site: CallSite,
        kind: NativeConstructorKind,
        form: NativeConstructorForm,
    ) -> bool {
        self.call_site == call_site && self.kind == kind && self.form == form
    }
}

/// Terminal evidence for this source owner only. Never composed through a
/// child ledger, generic Evaluate result, callee outcome or invocation memo.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct CompletedInitialization {
    owner: SourceUnitOrdinal,
    dependencies: ProofDependencies,
}

impl CompletedInitialization {
    /// Seal only the exact normally completed source root. Child
    /// operations and invocation memos carry fragments, never this evidence.
    pub(super) fn from_root_transfer<N, A>(
        source: &RootEffectIdentity,
        owner: SourceUnitOrdinal,
        initialized: &BTreeSet<Id>,
        transfer: &TransferLedger<N, A>,
    ) -> Option<Self> {
        if source.owner != owner || !source.targets_initialized(initialized) {
            return None;
        }
        Some(Self {
            owner,
            dependencies: transfer.normal_dependencies()?,
        })
    }

    pub(crate) fn covers(&self, owner: SourceUnitOrdinal) -> bool {
        self.owner == owner
    }

    pub(crate) fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }
}

/// Exact-owner evidence that synchronous module evaluation completes
/// normally. Unlike effect-domain closure, this receipt says nothing about
/// which global, heap or host effects the owner may have produced.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct RootNormalCompletion {
    owner: SourceUnitOrdinal,
    dependencies: ProofDependencies,
}

impl RootNormalCompletion {
    /// An exact completed-initializer receipt is itself whole-root normality
    /// evidence. It need not also have an operation-root identity, which is
    /// absent for some scalar and non-invoking function-creation roots.
    pub(crate) fn from_completed_initialization(
        owner: SourceUnitOrdinal,
        proof: &CompletedInitialization,
    ) -> Option<Self> {
        proof.covers(owner).then(|| Self {
            owner,
            dependencies: proof.dependencies().clone(),
        })
    }

    /// Seal normal completion only for the exact whole-owner root selected by
    /// the shared executor. A completed-initializer theorem may supply the
    /// normality proof even when unrelated may-effect domains remain unknown.
    pub(super) fn from_root<N, A>(
        source: &RootEffectIdentity,
        owner: SourceUnitOrdinal,
        initialized: &BTreeSet<Id>,
        transfer: &TransferLedger<N, A>,
    ) -> Option<Self> {
        if source.owner != owner || !source.targets_initialized(initialized) {
            return None;
        }
        Some(Self {
            owner,
            dependencies: transfer.normal_dependencies()?,
        })
    }

    pub(crate) fn covers(&self, owner: SourceUnitOrdinal) -> bool {
        self.owner == owner
    }

    pub(crate) fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }
}

/// Exact-root evidence that the shared transfer reached an outward known
/// throw. Missing normal-completion evidence alone never creates this receipt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct RootOutwardThrow {
    owner: SourceUnitOrdinal,
    dependencies: ProofDependencies,
}

impl RootOutwardThrow {
    /// Seal only a known throw returned from the exact owner root.
    pub(super) fn from_root<N, A>(
        source: &RootEffectIdentity,
        owner: SourceUnitOrdinal,
        transfer: &TransferLedger<N, A>,
    ) -> Option<Self> {
        if source.owner != owner {
            return None;
        }
        Some(Self {
            owner,
            dependencies: transfer.known_abrupt_dependencies()?,
        })
    }

    pub(crate) fn covers(&self, owner: SourceUnitOrdinal) -> bool {
        self.owner == owner
    }

    pub(crate) fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
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
            let Some(declaration) = var_decl_of_item(item) else {
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

    let Some(original) = var_decl_of_item(provenance.parent_item) else {
        return Err(SplitDeclaration);
    };
    if original.decls.len() <= 1 {
        return Err(SplitDeclaration);
    }
    let Some(declarator) = original.decls.get(provenance.declarator_index) else {
        return Err(SplitDeclaration);
    };
    let Some(single) = var_decl_of_item(view) else {
        return Err(SplitDeclaration);
    };
    let [single_declarator] = single.decls.as_slice() else {
        return Err(SplitDeclaration);
    };
    let same_initializer_span = single_declarator.init.as_ref().map(|init| init.span())
        == declarator.init.as_ref().map(|init| init.span());
    if matches!(view, ModuleItem::ModuleDecl(_))
        != matches!(provenance.parent_item, ModuleItem::ModuleDecl(_))
        || single.kind != original.kind
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

/// Canonical source identity for one top-level variable initializer. This
/// records which owner, binding, and original initializer span the syntax
/// belongs to; it says nothing about the initializer's value or effects.
#[derive(Clone)]
pub(super) struct InitializationSourceIdentity {
    owner: SourceUnitOrdinal,
    binding: Id,
    initializer_span: Span,
}

impl InitializationSourceIdentity {
    fn new(owner: SourceUnitOrdinal, binding: Id, initializer_span: Span) -> Self {
        Self {
            owner,
            binding,
            initializer_span,
        }
    }

    /// Recover the exact original initializer for a canonical source unit,
    /// including a split view's parent/index provenance. This admits source
    /// identity independently of the leaf forms understood below.
    pub(super) fn collect<'source>(
        item: &TopLevelItemView<'source>,
        owner: SourceUnitOrdinal,
    ) -> Result<(Self, &'source Expr), OwnerInitializationCoverageRefusalReason> {
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
        Ok((
            Self::new(owner, binding.id.to_id(), initializer.span()),
            initializer,
        ))
    }

    pub(super) fn owner(&self) -> SourceUnitOrdinal {
        self.owner
    }

    pub(super) fn binding(&self) -> &Id {
        &self.binding
    }

    pub(super) fn initializer_span(&self) -> Span {
        self.initializer_span
    }

    pub(super) fn matches(
        &self,
        owner: SourceUnitOrdinal,
        binding: &Id,
        initializer_span: Span,
    ) -> bool {
        self.owner == owner && self.binding == *binding && self.initializer_span == initializer_span
    }
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

fn empty_direct_constructor(expression: &Expr) -> Option<(Id, CallSite)> {
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
    Some((callee.to_id(), CallSite::from(construction.span)))
}

fn empty_native_constructor(expression: &Expr) -> Option<(Id, CallSite, NativeConstructorKind)> {
    let (callee, call_site) = empty_direct_constructor(expression)?;
    let kind = match callee.0.as_ref() {
        "Set" => NativeConstructorKind::Set,
        "TextEncoder" => NativeConstructorKind::TextEncoder,
        _ => return None,
    };
    Some((callee, call_site, kind))
}

fn primitive_literal(expression: &Expr) -> bool {
    matches!(
        expression,
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
    )
}

fn set_primitive_array_constructor(
    expression: &Expr,
) -> Option<(Id, CallSite, NativeConstructorForm)> {
    let Expr::New(construction) = expression else {
        return None;
    };
    if construction.type_args.is_some() {
        return None;
    }
    let Expr::Ident(callee) = construction.callee.as_ref() else {
        return None;
    };
    if callee.sym != "Set" {
        return None;
    }
    let Some([argument]) = construction.args.as_deref() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Array(array) = argument.expr.as_ref() else {
        return None;
    };
    if !array.elems.iter().all(|element| {
        element
            .as_ref()
            .is_some_and(|element| element.spread.is_none() && primitive_literal(&element.expr))
    }) {
        return None;
    }
    Some((
        callee.to_id(),
        CallSite::from(construction.span),
        NativeConstructorForm::SetPrimitiveArray {
            array: array.span.lo.0,
        },
    ))
}

impl OriginalInitializer {
    pub(super) fn collect(
        source: Result<
            (InitializationSourceIdentity, &Expr),
            OwnerInitializationCoverageRefusalReason,
        >,
    ) -> Result<Self, OwnerInitializationCoverageRefusalReason> {
        let (identity, initializer) = source?;
        let leaf = match initializer.unwrap_parens() {
            Expr::New(_) => {
                if let Some((callee, call_site)) = empty_direct_constructor(initializer)
                    && matches!(callee.0.as_ref(), "Map" | "WeakMap")
                {
                    Leaf::KeyedCollectionOperation { callee, call_site }
                } else if let Some((callee, call_site, kind)) =
                    empty_native_constructor(initializer)
                {
                    Leaf::NativeConstructor {
                        callee,
                        call_site,
                        kind,
                        form: NativeConstructorForm::Empty,
                    }
                } else if let Some((callee, call_site, form)) =
                    set_primitive_array_constructor(initializer)
                {
                    Leaf::NativeConstructor {
                        callee,
                        call_site,
                        kind: NativeConstructorKind::Set,
                        form,
                    }
                } else {
                    return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
                }
            }
            Expr::Lit(Lit::Regex(regex)) => {
                // ECMA-262 evaluates a validated literal through RegExpCreate
                // with its source text; this does not resolve the mutable
                // global RegExp binding or invoke source-defined code. Later
                // reads and calls on the RegExp remain outside this proof.
                Leaf::RegExp(regex.span.lo.0)
            }
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
                let (expression, covered) = ValueExpr::from_expr_with_coverage(initializer);
                if !covered {
                    return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
                }
                Leaf::Composed(expression)
            }
        };
        Ok(Self { identity, leaf })
    }

    pub(super) fn matches_binding(&self, owner: SourceUnitOrdinal, binding: &Id) -> bool {
        self.identity.owner() == owner && self.identity.binding() == binding
    }

    /// The expression and coverage come from the same canonical lowering.
    /// Only this retained source expression may enable covered child transfers.
    pub(super) fn composed_expression(&self) -> Option<&ValueExpr> {
        match &self.leaf {
            Leaf::Composed(expression) => Some(expression),
            _ => None,
        }
    }

    pub(super) fn operation_source(
        &self,
        expression: &ValueExpr,
    ) -> Option<&InitializationSourceIdentity> {
        let Leaf::KeyedCollectionOperation {
            callee: expected,
            call_site,
        } = &self.leaf
        else {
            return None;
        };
        let ValueExpr::New(callee, arguments, evaluated_site) = expression else {
            return None;
        };
        (arguments.is_empty()
            && call_site == evaluated_site
            && matches!(callee.as_ref(), ValueExpr::Binding(binding) if binding == expected))
        .then_some(&self.identity)
    }

    pub(super) fn native_constructor_candidate(
        &self,
        expression: &ValueExpr,
    ) -> Option<NativeConstructorCandidate> {
        let Leaf::NativeConstructor {
            callee: expected_callee,
            call_site,
            kind,
            form,
        } = &self.leaf
        else {
            return None;
        };
        let ValueExpr::New(callee, arguments, evaluated_site) = expression else {
            return None;
        };
        let form_matches = match form {
            NativeConstructorForm::Empty => arguments.is_empty(),
            NativeConstructorForm::SetPrimitiveArray { array } => matches!(
                arguments.as_ref(),
                [ValueExpr::ObjectLiteral(lowered_array, _)] if lowered_array == array
            ),
        };
        if *call_site != *evaluated_site
            || !form_matches
            || !matches!(callee.as_ref(), ValueExpr::Binding(binding) if binding == expected_callee)
        {
            return None;
        }
        Some(NativeConstructorCandidate {
            owner: self.identity.owner(),
            call_site: *call_site,
            kind: *kind,
            form: *form,
        })
    }

    /// The dedicated root Evaluate transfer must actually finish with its
    /// expected value. Every source cell read must be initialized before that
    /// transfer; imported and global reads cannot supply this proof.
    /// Generic lowering or an empty effect ledger is no proof.
    pub(super) fn evaluated(
        &self,
        resolver: SourceHeapResolver,
        expression: &ValueExpr,
        transfer: &AllocationValue,
        native_constructor_transfer: Option<NativeConstructorTransfer>,
    ) -> Option<EvaluatedInitializer> {
        let Completion::Normal(values) = &transfer.ledger.completion else {
            return None;
        };
        if values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable)) {
            return None;
        }
        let transfer_dependencies = transfer.ledger.dependencies.as_ref().clone();
        let matches = match (&self.leaf, expression, values.first()?) {
            (Leaf::Composed(_), _, _) => matches!(
                &transfer.ledger.allocation,
                AllocationJudgment::CoveredAllocationOnly(_)
            ),
            (Leaf::NativeConstructor { .. }, _, AbstractValue::Value(Value::Top)) => self
                .native_constructor_candidate(expression)
                .zip(native_constructor_transfer.as_ref())
                .is_some_and(|(candidate, transfer)| {
                    values.len() == 1 && transfer.matches(candidate)
                }),
            (
                Leaf::NativeConstructor { .. },
                _,
                AbstractValue::Value(Value::Alloc(Kind::Set, allocation)),
            ) => self
                .native_constructor_candidate(expression)
                .zip(native_constructor_transfer.as_ref())
                .is_some_and(|(candidate, transfer)| {
                    values.len() == 1
                        && transfer.matches(candidate)
                        && candidate.matches_set_primitive_array_allocation(resolver, *allocation)
                }),
            (
                Leaf::RegExp(allocation),
                ValueExpr::Alloc(Kind::RegExp, lowered),
                AbstractValue::Value(Value::Alloc(Kind::RegExp, evaluated)),
            ) => {
                values.len() == 1
                    && allocation == lowered
                    && evaluated.kind() == Some(Kind::RegExp)
                    && resolver.source_origin(*evaluated) == Some(*allocation)
            }
            (
                Leaf::Function(expected),
                ValueExpr::Alloc(Kind::Function, lowered),
                AbstractValue::Closure(evaluated, _),
            ) => {
                values.len() == 1
                    && expected == lowered
                    && evaluated.kind() == Some(Kind::Function)
                    && resolver.source_origin(*evaluated) == Some(*expected)
            }
            _ => false,
        };
        if !matches {
            return None;
        }
        let mut dependencies = transfer_dependencies;
        if let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) =
            &transfer.ledger.allocation
        {
            dependencies.extend(allocation_dependencies.as_ref());
        }
        if let Some(transfer) = native_constructor_transfer {
            dependencies.extend(&transfer.dependencies);
        }
        Some(EvaluatedInitializer {
            owner: self.identity.owner(),
            initializer_span: self.identity.initializer_span(),
            dependencies,
        })
    }

    pub(super) fn complete(
        &self,
        evaluated: EvaluatedInitializer,
        owner: SourceUnitOrdinal,
        initialized: &BTreeSet<Id>,
    ) -> Option<CompletedInitialization> {
        (self
            .identity
            .matches(owner, self.identity.binding(), evaluated.initializer_span)
            && evaluated.owner == owner
            && initialized.contains(self.identity.binding()))
        .then_some(CompletedInitialization {
            owner,
            dependencies: evaluated.dependencies,
        })
    }
}

#[cfg(test)]
mod source_identity_tests {
    use super::{InitializationSourceIdentity, OriginalInitializer};
    use crate::facts::OwnerInitializationCoverageRefusalReason;
    use crate::facts::item_views::top_level_item_views;
    use source_identity::SourceUnitOrdinal;
    use swc_common::{Spanned, SyntaxContext};
    use swc_ecma_ast::Id;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    #[test]
    fn exact_split_sibling_identity_survives_leaf_refusal() {
        let module =
            raw_js_test_support::parse("const first = 1, unsupported = new Date(), last = 3;");
        let views = top_level_item_views(&module.body);
        assert_eq!(views.len(), 3);

        let first = InitializationSourceIdentity::collect(&views[0], SourceUnitOrdinal(0))
            .expect("first split child has exact source identity");
        let unsupported = InitializationSourceIdentity::collect(&views[1], SourceUnitOrdinal(1))
            .expect("unsupported initializer still has exact source identity");
        let last = InitializationSourceIdentity::collect(&views[2], SourceUnitOrdinal(2))
            .expect("last split child has exact source identity");

        let (unsupported_identity, unsupported_expression) = unsupported;
        assert_eq!(unsupported_identity.owner(), SourceUnitOrdinal(1));
        assert_eq!(unsupported_identity.binding(), &id("unsupported"));
        assert_eq!(
            unsupported_identity.initializer_span(),
            unsupported_expression.span()
        );
        assert!(unsupported_identity.matches(
            SourceUnitOrdinal(1),
            &id("unsupported"),
            unsupported_expression.span()
        ));
        assert!(!first.0.matches(
            SourceUnitOrdinal(1),
            &id("unsupported"),
            unsupported_expression.span()
        ));
        assert!(!last.0.matches(
            SourceUnitOrdinal(1),
            &id("unsupported"),
            unsupported_expression.span()
        ));

        assert!(matches!(
            OriginalInitializer::collect(Ok((unsupported_identity, unsupported_expression))),
            Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer)
        ));
    }

    #[test]
    fn exported_initializer_identity_does_not_admit_unsupported_leaf() {
        let module = raw_js_test_support::parse("export const result = new Date();");
        let views = top_level_item_views(&module.body);
        let (identity, expression) =
            InitializationSourceIdentity::collect(&views[0], SourceUnitOrdinal(0))
                .expect("export wrapper still identifies the original binding and initializer");
        assert!(identity.matches(SourceUnitOrdinal(0), &id("result"), expression.span()));
        assert!(matches!(
            OriginalInitializer::collect(Ok((identity, expression))),
            Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer)
        ));
    }
}
