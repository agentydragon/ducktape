//! AST collection for synchronous execution: callables, roots, writes, and source order.

pub mod bundle_globals;
pub mod callable_capabilities;
mod callback_arguments;
mod class_value_facts;
mod deferred_global_publication;
mod foreign_call;
mod fresh_values;
mod global_receiver;
mod input_validation;
pub mod intrinsic_roots;
pub mod local_constructors;
mod namespace_family;
mod namespace_wrapper;
pub mod native_operation;
mod native_point_effects;
mod native_write_census;
pub mod noninvoking_initialization;
pub mod patterns;
mod property_keys;
mod prototype_properties;
mod provider;
mod receiver_flow;
mod recording;
mod strict_provider;
mod visit;

use native_write_census::{
    ArraySpreadNativeIteratorPremise, NativeSlotOverlap, array_spread_protocol_slot_overlap,
    source_slot_overlap,
};
pub use native_write_census::{
    ArraySpreadProtocolIntegrity, NativeWriteCensus, apply_intrinsics_never_written,
    resolve_array_spread_protocol_integrity, resolve_native_integrity,
};
pub use native_write_census::{IntrinsicWriteTarget, NativePointEffects};
use property_keys::{
    PropertyKeyDomain, PropertyKeyEvidence, PropertyKeyExpr, key_domain_may_name,
    property_key_evidence,
};

use crate::namespace_wrapper::NamespaceWrapperCandidate;
use crate::noninvoking_initialization::{InitializationSourceIdentity, OriginalInitializer};
use analysis::facts::OwnerInitializationCoverageRefusalReason;
use analysis::facts::global_object::{unshadowed_global_names, unshadowed_global_object_aliases};
use analysis::facts::local_effect_targets::{MetadataDecorateCall, metadata_decorate_call};
use analysis::facts::statement_facts::{
    CallbackFactoryCallArgumentWitness, CallbackFactoryWriteWitness,
};
use analysis_domain::builtins::{self, IntrinsicOp, Invocation, Row};
use analysis_domain::integrity::{IntegrityFlags, IntrinsicIntact, IntrinsicSlot, PrototypeWrites};
use analysis_domain::item_views::{TopLevelItemView, collect_declared_names, var_decl_of_item};
use analysis_vocabulary::inputs::{
    AnalysisInputs, BundleGlobalNames, HostBehaviorKey, InferredSetConstruction,
    InferredSetOperation, NamespaceWrapperSummary, NativeInputKind, NativeIntegrity,
    NativeIntrinsic, NativeOperationInputEvidence, NoAccessorInstallRef, ProgramPremiseKey,
    ProofDependencies, ResolvedBindingValueInput, ResolvedNativeOperationInput,
    SelectedBindingValueInput, SelectedNativeOperationInput, ValueInputEvidence, ValueInputRef,
};
use analysis_vocabulary::report_schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativePrototypeMutation,
    NativeReceiverPath, NativeWriteSlot,
};
use analysis_vocabulary::value::{Kind, Value};
use deferred_global_publication::{
    DeferredGlobalPublication, deferred_global_publication, discarded_synchronous_iife,
};
use fresh_values::{FreshValueStep, RecordFactoryArgument, fresh_value_step};
use global_receiver::{DestructuringMemberWrite, GlobalMembershipProbe, NamespacePrivacy};
use inference::{FactContext, IterativeInference, IterativeRules};
use intrinsic_roots::IntrinsicRoots;
use kernel::closure::CaptureCensus;
use kernel::context::IndexReads;
use local_constructors::LocalConstructor;
use patterns::{
    collect_pattern_assignments, iterated_defaults, pattern_iterates, pattern_iterates_part,
    pattern_loses_values, pattern_source,
};
use purity::callable_inference::CallableInferenceInputs;
use purity::semantic_inference::{
    DescriptorCopyCallCandidate as SemanticDescriptorCopyCallCandidate, NativeBindingContext,
    NativeCallee, NativePremises, SemanticFacts, SemanticRules, native_premise,
};
use purity::{
    LocalClassConstructionProof, LocalClassConstructionShapes, SetIterableShapes,
    StableArraySpreadShapes,
};
use source_identity::SourceUnitOrdinal;
use spec::ReentryFreeOperation;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;
use std::sync::Arc;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::VisitWith;
use value_flow::ChunkValueFlow;
use value_model::callable_body_facts::CallableBodyFacts;
use value_model::local_operations::{DeferredCallableCreation, LocalBinding, LocalBody};
use value_model::{
    ArrayElement, ArrayIteration, CallSite, ClassValueFacts, FromEntriesMappedProof,
    LiteralPropertyKind, MemberKey, NamespaceValueFacts, OwnPropertyDescriptorSyntax,
    OwnPropertyShape, ValueExpr, object_data_property_name, static_prop, static_property_name,
};
use world::allocation_transfer::LiteralLayout;

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

    type MergeScratch = ();
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
pub struct Parameter {
    pub binding: Option<Id>,
    pub bindings: BTreeSet<Id>,
    pub default: Option<ValueExpr>,
    /// A pattern parameter's bindings as projections of its incoming value,
    /// bound to `pattern_source()`, and whether some binding receives a part
    /// of it only as unknown.
    pub pattern_assignments: Vec<(Id, ValueExpr)>,
    pub pattern_loses_values: bool,
    /// The pattern destructures an array, so binding it runs the iterator
    /// protocol on the incoming value (or on a part of it).
    pub pattern_iterates: bool,
    /// Some array pattern below the root iterates a part of the incoming
    /// value, or a default, which no source tracks.
    pub pattern_iterates_part: bool,
    /// Defaults below the root that supply a value an array pattern
    /// iterates.
    pub iterated_defaults: Vec<ValueExpr>,
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
pub struct Call {
    pub kind: CallKind,
    pub callee: ValueExpr,
    pub args: Vec<ValueExpr>,
    pub span: Span,
    pub caller: Option<u32>,
    pub owner: SourceUnitOrdinal,
    pub plain_object_sources: bool,
    pub no_sync_arguments: bool,
    pub no_reentry_candidate: bool,
    pub no_reentry_record_candidate: bool,
    pub has_spread: bool,
}

impl Call {
    pub fn member_read(
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
pub struct RetainedCallbackCall {
    pub namespace_member: Option<(Id, String)>,
    pub signatures: Vec<spec::RetainedCallbackSignature>,
    pub argument_count: usize,
    pub callback_argument_exprs: BTreeMap<usize, ValueExpr>,
    pub require_single_callback_identity: bool,
}

#[derive(Debug, Clone)]
pub struct IndexedRetainedCallbackCall {
    pub call: RetainedCallbackCall,
    pub owner: SourceUnitOrdinal,
    pub caller: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct DescriptorCopyCallCandidate {
    pub source_object: u32,
    pub signature: spec::DescriptorCopyFactorySignature,
    pub provider_binding: Id,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallKind {
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
pub fn native_call_identity_key(
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
        _ => "supported native operation",
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
pub struct CallbackResultWrite {
    pub receiver: ValueExpr,
    pub property_key: ValueExpr,
    pub simple_assignment: bool,
    pub data_read_operands: bool,
    pub property: Option<String>,
    pub value: ValueExpr,
    pub site: CallSite,
}

#[derive(Debug, Clone)]
pub struct MemberValueWrite {
    pub receiver: Id,
    pub property: Option<String>,
    pub value: ValueExpr,
    pub site: CallSite,
    pub owner: SourceUnitOrdinal,
}

#[derive(Debug, Clone)]
pub struct Callable {
    pub owner: SourceUnitOrdinal,
    created_after_parent_await: bool,
    pub params: Vec<Parameter>,
    pub reads: BTreeSet<Id>,
    bare_reads: BTreeSet<Id>,
    pub rebinds: BTreeSet<Id>,
    pub calls: Vec<Arc<Call>>,
    pub local_assignments: Vec<(Id, ValueExpr)>,
    pub(crate) declared_locals: BTreeSet<Id>,
    /// Locals a read can observe before any assignment, as `undefined`: a
    /// declarator without an initializer outside a `for`-`in`/`of` head, and
    /// every `var` (hoisted). `local_assignments` never contributes that value.
    pub undefined_before_assignment: BTreeSet<Id>,
    /// Bindings a `for`-`in`/`of` head of this callable writes. No
    /// `local_assignments` entry records the elements a head binding takes.
    pub head_bindings: BTreeSet<Id>,
    /// Values whose parts reach a binding only as unknown: `for`-`of`
    /// iterables, initializers of destructuring that loses values, thrown
    /// values (a `catch` parameter) and yielded values (a generator's
    /// consumer).
    pub published_sources: Vec<ValueExpr>,
    /// Operands this callable runs the iterator protocol on: `for`-`of`
    /// iterables, array-destructured values, spread operands and `yield*`
    /// operands. Over anything but a proven native iterable, the protocol
    /// calls user code (`[Symbol.iterator]`, `next`).
    pub iterated_sources: Vec<ValueExpr>,
    /// Bindings an update expression (`x++`) of this callable writes; neither
    /// `rebinds` nor `local_assignments` records them.
    pub(crate) update_targets: BTreeSet<Id>,
    hoisted_vars: BTreeSet<Id>,
    lexical_declarations: BTreeMap<Id, LocalBinding>,
    pub(crate) self_binding: Option<Id>,
    /// Catch parameters and destructured declarations are locals for closure
    /// projection. Keep them separate from `declared_locals`, whose consumers
    /// also include direct callback-factory and class-closure admission.
    pub(crate) pattern_locals: BTreeSet<Id>,
    pub body_facts: CallableBodyFacts,
    pub local_functions: BTreeMap<Id, u32>,
    pub literal_captures: BTreeSet<Id>,
    literal_exposures: BTreeSet<Id>,
    pub escaped_bindings: BTreeSet<Id>,
    pub namespace_escape_values: Vec<ValueExpr>,
    pub member_writes: Vec<(Id, String)>,
    pub member_value_writes: Vec<MemberValueWrite>,
    pub callback_result_writes: Vec<CallbackResultWrite>,
    pub dynamic_property_effect: bool,
    pub dynamic_property_receivers: Vec<ValueExpr>,
    /// Receivers of member targets in this body's destructuring assignments.
    pub destructuring_member_receivers: Vec<ValueExpr>,
    pub implicit_capture: bool,
    pub opaque: bool,
    pub generator: bool,
    pub may_fall_through: bool,
}

impl Callable {
    /// The bindings this callable's own scope declares, parameters included.
    pub(crate) fn own_bindings(&self) -> BTreeSet<&Id> {
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
    pub fn writes(&self) -> impl Iterator<Item = &Id> {
        self.rebinds
            .iter()
            .chain(&self.head_bindings)
            .chain(&self.update_targets)
    }

    /// Bindings an enclosing scope declares that this callable writes.
    pub fn captured_writes(&self) -> BTreeSet<&Id> {
        let own = self.own_bindings();
        self.writes()
            .filter(|binding| !own.contains(binding))
            .collect()
    }

    /// Bindings an enclosing scope declares that this callable reads.
    pub fn captured_reads(&self) -> BTreeSet<&Id> {
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
    pub fn reads_before_assignment(&self) -> (BTreeSet<&Id>, BTreeSet<Id>) {
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
pub struct CallablePrefixEffects {
    pub reads: BTreeSet<Id>,
    pub rebinds: BTreeSet<Id>,
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

/// Global-object names the native census resolves.
#[derive(Debug, Clone, Default)]
pub(crate) struct GlobalNames {
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
pub(crate) struct BundleGlobals {
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

#[derive(Debug, Clone)]
pub struct BindingDeclarationWrite {
    owner: SourceUnitOrdinal,
    /// A simple `var` in a callable's scope. Such a binding is initialized to
    /// undefined on entry; this proves no kind or privacy of its stored value.
    private_var_scope: Option<u32>,
    binding_span: Span,
    initializer_span: Option<Span>,
    pub initializer: Option<ValueExpr>,
    pub direct_top_level: bool,
    pub simple_identifier: bool,
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

fn matches_resolved_native_initializer(
    input: &ResolvedNativeOperationInput,
    declaration: &BindingDeclarationWrite,
    call_site: CallSite,
) -> bool {
    declaration.direct_top_level
        && declaration.simple_identifier
        && input
            .selected_statement_span
            .contains(declaration.binding_span)
        && declaration.binding_span == input.declaration_span
        && declaration.initializer_span == Some(input.initializer_span)
        && declaration.owner == input.initializer_ordinal
        && input.binding.0.as_ref() == input.evidence.selected_binding
        && call_site == CallSite::from(input.call_span)
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
        if js_ast::is_primitive_literal(element.expr.unwrap_parens()) {
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
            element.as_ref().is_some_and(|element| {
                element.spread.is_none()
                    && js_ast::is_primitive_literal(element.expr.unwrap_parens())
            })
        }))
    .then(|| {
        (
            source.to_id(),
            source.span,
            first.spread.expect("checked spread"),
        )
    })
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

#[derive(Default)]
pub struct ExecutionIndex {
    pub member_owners: BTreeMap<CallSite, SourceUnitOrdinal>,
    pub retained_record_factories: BTreeSet<String>,
    owner: SourceUnitOrdinal,
    current: Option<u32>,
    /// Lexical var scope, distinct from the effect owner inherited by
    /// unindexed accessors and static blocks.
    variable_scope: Option<u32>,
    allowed_local_member_receiver: Option<u32>,
    past_await: bool,
    may_skip_await_depth: u32,
    conditional_depth: u32,
    /// Additional expression contexts that may skip a nested member read.
    /// Kept separate from `conditional_depth`, which controls existing
    /// analyses with different admission rules.
    may_skip_member_read_depth: u32,
    literal_depth: u32,
    factory_member_value_depth: u32,
    factory_callback_depth: u32,
    pub callables: BTreeMap<u32, Callable>,
    /// Functions whose call cannot inspect or retain any argument.
    inert_argument_functions: BTreeSet<u32>,
    pub callable_spans: BTreeMap<u32, Span>,
    immediate_anonymous_callers: BTreeMap<u32, Option<u32>>,
    inline_callback_entries: BTreeMap<u32, callback_arguments::InlineCallbackEntry>,
    native_guarded_regions: Vec<callback_arguments::GuardedRegion>,
    pub local_constructors: BTreeMap<u32, LocalConstructor>,
    /// Source-covered constructor operations for exported provider summaries.
    pub class_provider_bodies: BTreeMap<u32, (LocalBody, Vec<Id>)>,
    pub local_class_construction_shapes: LocalClassConstructionShapes,
    pub local_class_construction_proof: LocalClassConstructionProof,
    pub semantic_facts: Option<Arc<SemanticFacts>>,
    /// Slot flags of the completed census and validated inputs; set with
    /// `semantic_facts`.
    pub integrity: IntegrityFlags,
    /// Native own-data lookups from the canonical operation index, with the
    /// slots required at each read's source position.
    pub native_own_data_reads: BTreeMap<u32, [IntrinsicSlot; 2]>,
    pub class_values: BTreeMap<u32, ClassValueFacts>,
    pub class_owners: BTreeMap<u32, SourceUnitOrdinal>,
    pub class_bindings: BTreeMap<u32, Id>,
    pub class_spans: BTreeMap<u32, Span>,
    /// The `extends` expression of each class that has one.
    pub class_heritage_spans: BTreeMap<u32, Span>,
    /// Object literals with a getter or setter that reads `this`: a member
    /// read or write that may run it hands it the receiver.
    pub this_reading_accessor_objects: BTreeSet<u32>,
    /// The non-null `__proto__` expression of each object literal with one.
    pub literal_prototypes: BTreeMap<u32, ValueExpr>,
    /// Constructors, methods and accessors of every class: they run as class
    /// members, not as values the class body creates.
    pub class_member_callables: BTreeSet<u32>,
    /// The constructors among `class_member_callables`.
    pub class_constructors: BTreeSet<u32>,
    /// Per class, the spans that run at construction or class creation and
    /// can store values on the instance or the class: the constructor,
    /// field initializers and static blocks.
    pub class_store_regions: BTreeMap<u32, Vec<Span>>,
    /// The getters and setters (static or not) each class declares.
    class_accessors: BTreeMap<u32, Vec<u32>>,
    /// Classes declaring a getter or setter that reads `this`.
    pub this_reading_accessor_classes: BTreeSet<u32>,
    /// Receivers of member targets in destructuring assignments, per
    /// top-level owner; a callable's own are on `Callable`.
    pub top_destructuring_member_receivers: Vec<Vec<ValueExpr>>,
    class_ids: BTreeSet<Id>,
    /// Top-level `const` declarations, exported or not: a store throws.
    pub top_const_bindings: BTreeSet<Id>,
    /// Every binding read or written anywhere inside a callable body,
    /// including after an `await` (unlike `CallableFacts::{reads,rebinds}`).
    pub captured_bindings: BTreeSet<Id>,
    pub other_class_uses: BTreeSet<Id>,
    safe_class_target: Option<Id>,
    pub plain_data_objects: BTreeSet<u32>,
    /// The top-level owner whose source contains each object literal.
    pub object_literal_owners: BTreeMap<u32, SourceUnitOrdinal>,
    pub own_descriptor_shapes: BTreeMap<u32, OwnPropertyShape>,
    descriptor_copy_factories:
        BTreeMap<Id, analysis_vocabulary::inputs::DescriptorCopyFactorySummary>,
    pub descriptor_copy_calls: BTreeMap<CallSite, DescriptorCopyCallCandidate>,
    pub(crate) null_proto_data_objects: BTreeSet<u32>,
    pub exact_object_spread_copies: BTreeSet<u32>,
    pub map_entry_arrays: BTreeSet<u32>,
    pub dense_array_literals: BTreeMap<u32, usize>,
    pub array_elements: BTreeMap<u32, Vec<Option<ArrayElement>>>,
    literal_property_kinds: BTreeMap<u32, Vec<LiteralPropertyKind>>,
    callable_creations: BTreeMap<u32, DeferredCallableCreation>,
    pub(crate) method_record_members: BTreeMap<u32, Vec<crate::foreign_call::RecordMember>>,
    pub array_iterations: BTreeMap<CallSite, ArrayIteration>,
    pub async_callables: BTreeSet<u32>,
    pub callable_member_reads: BTreeMap<u32, Vec<(ValueExpr, Option<String>)>>,
    direct_call_members: BTreeSet<CallSite>,
    /// Direct calls of annotated record factories, with the syntax check of
    /// their argument. Semantic admission is checked by the interpreter.
    pub record_factory_calls: BTreeMap<CallSite, RecordFactoryArgument>,
    member_write_sites: BTreeSet<CallSite>,
    pub primitive_data_properties: BTreeSet<(u32, String)>,
    pub from_entries_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    pub mapped_array_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    pub plain_array_bindings: BTreeSet<String>,
    mutated_intrinsic_roots: IntrinsicRoots,
    native_integrity_witnesses: Arc<Vec<NativeMutationWitness>>,
    native_prototype_mutations: Arc<Vec<NativePrototypeMutation>>,
    source_chunk_id: Option<String>,
    pub native_point_effects: BTreeMap<(SourceUnitOrdinal, Option<u32>), NativePointEffects>,
    pub initial_native_point_effects: NativePointEffects,
    pub all_native_point_effects: NativePointEffects,
    pub reassigned_map: bool,
    namespace_privacy: NamespacePrivacy,
    fresh_error_claims: Vec<FreshErrorWriteClaim>,
    pub intrinsic_writes: Vec<IntrinsicWriteTarget>,
    prototype_calls: Vec<Arc<Call>>,
    prototype_field_writes: Vec<(SourceUnitOrdinal, Option<u32>, Span, PropertyKeyExpr)>,
    pub prototype_callable_reads: BTreeMap<u32, BTreeSet<Id>>,
    /// Per callable, the bindings it or a callable nested in it writes
    /// (assignment and destructuring targets, loop heads, updates). A closure
    /// must capture a cell for each so a nested writer reaches the owner.
    pub(crate) nested_callable_writes: BTreeMap<u32, BTreeSet<Id>>,
    prototype_capture_stack: Vec<u32>,
    property_key_assignments: BTreeMap<Id, Vec<PropertyKeyExpr>>,
    pub(crate) property_key_declarations: BTreeSet<Id>,
    native_write_census: NativeWriteCensus,
    pub binding_declaration_writes: BTreeMap<Id, Vec<BindingDeclarationWrite>>,
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
    pub direct_primitive_const_bindings: BTreeSet<Id>,
    pub reassigned_bindings: BTreeSet<Id>,
    /// Complete target-walker writes for primitive Set operand certification.
    binding_target_writes: BTreeSet<Id>,
    ordinary_input_refs: BTreeMap<Id, (ValueInputRef, SourceUnitOrdinal)>,
    value_input_evidence: BTreeMap<ValueInputRef, ValueInputEvidence>,
    resolved_value_inputs: Vec<ResolvedBindingValueInput>,
    value_input_errors: Vec<String>,
    pub resolved_native_operation_inputs: Vec<ResolvedNativeOperationInput>,
    pub native_operation_input_evidence: BTreeMap<CallSite, NativeOperationInputEvidence>,
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
    pub setter_owners: BTreeMap<Option<String>, BTreeSet<SourceUnitOrdinal>>,
    hoisted_nested_functions: BTreeSet<u32>,
    pub owner_callable_prefix_effects: Vec<Option<CallablePrefixEffects>>,
    pub uncovered_callable_effect_owners: BTreeSet<SourceUnitOrdinal>,
    pub top_calls: Vec<Vec<Arc<Call>>>,
    /// Top-level calls that syntax may skip even when an execution path
    /// happens to evaluate them during abstract interpretation.
    conditional_top_member_reads: BTreeSet<CallSite>,
    /// Per top-level owner, what `Callable::published_sources` lists for a
    /// callable: values whose parts reach a binding only as unknown.
    pub top_published_sources: BTreeMap<SourceUnitOrdinal, Vec<ValueExpr>>,
    /// Per top-level owner, what `Callable::iterated_sources` lists for a
    /// callable.
    pub top_iterated_sources: BTreeMap<SourceUnitOrdinal, Vec<ValueExpr>>,
    original_statement_spans: Vec<Span>,
    /// Ordinary CallExpr syntax at lexical top level; optional calls use a
    /// conservative Call record too, but do not satisfy this witness shape.
    ordinary_top_call_sites: BTreeSet<CallSite>,
    bare_constructor_sites: BTreeSet<CallSite>,
    pub literal_true_second_arguments: BTreeSet<CallSite>,
    pub top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    pub fresh_instance_field_writes: BTreeSet<SourceUnitOrdinal>,
    top_dynamic_property_receivers: Vec<Vec<ValueExpr>>,
    pub top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    pub ordered_initializers: BTreeMap<SourceUnitOrdinal, LocalBody>,
    /// Top-level `receiver.property = value` and `binding = value` statements
    /// lowered from their exact source expression. Lowering selects nothing: `roots/operands`
    /// admits one.
    pub ordered_statements: BTreeMap<SourceUnitOrdinal, LocalBody>,
    /// Canonical owner/binding/initializer identity, independent of the
    /// legacy initializer leaf and allocation admission.
    pub initialization_source_identities: BTreeMap<SourceUnitOrdinal, InitializationSourceIdentity>,
    pub noninvoking_initializers: BTreeMap<SourceUnitOrdinal, OriginalInitializer>,
    pub initialization_coverage_refusals: Vec<Option<OwnerInitializationCoverageRefusalReason>>,
    pub top_definite_initializers: Vec<BTreeSet<Id>>,
    pub(crate) const_call_owners: BTreeSet<SourceUnitOrdinal>,
    pub top_member_value_writes: Vec<Vec<MemberValueWrite>>,
    pub top_callback_result_writes: Vec<Vec<CallbackResultWrite>>,
    pub top_exported_values: Vec<Vec<ValueExpr>>,
    pub top_metadata_decorations: Vec<Option<MetadataDecorateCall>>,
    pub deferred_global_candidates: Vec<Option<DeferredGlobalPublication>>,
    discarded_sync_iifes: Vec<Option<u32>>,
    pub deferred_global_certificates: BTreeSet<SourceUnitOrdinal>,
    deferred_global_requested: bool,
    global_guards_requested: bool,
    global_membership_candidates: BTreeMap<CallSite, GlobalMembershipProbe>,
    pub non_reentrant_global_membership: BTreeSet<CallSite>,
    global_slot_mentions: BTreeMap<String, usize>,
    dynamic_global_member: bool,
    global_alias_written: bool,
    pub imported_bindings: BTreeSet<Id>,
    pub hoisted_bindings: BTreeMap<Id, u32>,
    pub direct_top_level_functions: BTreeSet<Id>,
    /// Exact sole-return calls in top-level function declarations, collected
    /// by the canonical structural visitor alongside callable/call facts.
    direct_return_calls: BTreeMap<Id, Span>,
    pub exported_hoisted_functions: BTreeSet<u32>,
    pub global_object_names: BTreeSet<&'static str>,
    /// Global names for the native census; the bundle-owned properties are
    /// filled in once the census resolves them.
    census_globals: GlobalNames,
    pub hoisted_function_owners: BTreeSet<SourceUnitOrdinal>,
    pub exported_declaration_owners: BTreeSet<SourceUnitOrdinal>,
    pub exported_bindings: BTreeSet<Id>,
    /// Local binding of each local export name; `default` is not a binding.
    pub exported_names: BTreeMap<String, Id>,
    /// Source units holding `export default`.
    pub default_export_owners: BTreeSet<SourceUnitOrdinal>,
    pub dynamic_scope_owners: BTreeSet<SourceUnitOrdinal>,
    pub opaque_top: BTreeSet<SourceUnitOrdinal>,
    no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    pub callback_factory_imports: BTreeSet<Id>,
    pub callback_factory_summaries: BTreeMap<Id, analysis_vocabulary::inputs::CallableSummary>,
    /// The existing canonical call-shape check accepted one syntactic inline
    /// callback. Lowered values cannot establish this source prerequisite.
    pub direct_inline_callback_calls: BTreeMap<CallSite, Id>,
    pub foreign_call_imports: BTreeMap<Id, analysis_vocabulary::inputs::ForeignCallSummary>,
    pub importer_cell_reads: BTreeMap<Id, BTreeSet<Id>>,
    /// The derived summary of an import whose callback-factory evidence is
    /// all source-inferred. The evidence keeps the call's timing and result
    /// consumers; the summary is the receipt for the provider body's effects.
    pub callback_factory_call_summaries:
        BTreeMap<Id, analysis_vocabulary::inputs::ForeignCallSummary>,
    pub inline_callback_factory_imports: BTreeSet<Id>,
    pub callback_factory_members: BTreeMap<Id, BTreeSet<String>>,
    /// Union used only for namespace privacy/mutation guards. Admission of the
    /// old and stronger contracts remains separate.
    pub audited_factory_members: BTreeMap<Id, BTreeSet<String>>,
    pub rejected_namespace_member_writes: BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    pub rejected_namespace_dynamic_property_writes:
        BTreeMap<(Id, String), CallbackFactoryWriteWitness>,
    pub rejected_namespace_call_arguments: BTreeMap<Id, CallbackFactoryCallArgumentWitness>,
    retained_callback_factory_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    retained_callback_constructor_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    no_reentry_callback_imports: BTreeMap<Id, Vec<spec::RetainedCallbackSignature>>,
    no_reentry_statement_calls: BTreeMap<(u32, u32), spec::RetainedCallbackSignature>,
    retained_callback_factory_members:
        BTreeMap<Id, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub retained_callback_calls: BTreeMap<CallSite, IndexedRetainedCallbackCall>,
    pub retained_constructor_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    pub no_reentry_callback_calls: BTreeMap<CallSite, RetainedCallbackCall>,
    /// Synthetic getter reads for a source-selected member callee. The map
    /// lets runtime timing suppress only foreign-callback promotion when the
    /// exact outer call's callback identities satisfy its contract.
    pub no_reentry_accessor_calls: BTreeMap<CallSite, CallSite>,
    /// The ordinary call whose callee is evaluated by a synthetic lookup.
    pub accessor_callee_calls: BTreeMap<CallSite, Call>,
    pub no_reentry_exports: BTreeSet<Id>,
    pub host_async_local_storage_imports: BTreeSet<Id>,
    pub host_promise_resolve: bool,
    pub no_reentry_members: BTreeMap<Id, BTreeSet<String>>,
    pub no_reentry_record_members: BTreeMap<Id, BTreeSet<String>>,
    pub returned_record_summaries: BTreeMap<Id, analysis_vocabulary::inputs::ReturnedRecordSummary>,
    pub namespace_wrapper_summaries: BTreeMap<Id, NamespaceWrapperSummary>,
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

impl ExecutionIndex {
    /// A unique top-level function identity with no source writes or dynamic
    /// scope; earlier statements cannot silently replace this class base.
    pub fn stable_heritage_function(&self, binding: &Id, flow: &ChunkValueFlow) -> Option<u32> {
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
    pub fn inert_heritage_callee(&self, binding: &Id, flow: &ChunkValueFlow) -> bool {
        self.stable_heritage_function(binding, flow)
            .is_some_and(|key| self.inert_argument_functions.contains(&key))
    }

    /// A closure can resolve module bindings and unresolved globals when it is
    /// later invoked. Creating it does not read those values into the factory
    /// activation, so they are not retained provider captures. Enclosing
    /// lexical cells still require the local transfer or checked private-var
    /// capture projection to represent them.
    pub(crate) fn deferred_module_reference(&self, binding: &Id) -> bool {
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

    pub(crate) fn private_var_binding(&self, binding: &Id) -> bool {
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

    pub fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        if self.array_elements.contains_key(&allocation) {
            return None;
        }
        self.own_descriptor_shapes.get(&allocation)
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
}

impl IndexReads for ExecutionIndex {
    fn callable_creation(&self, callable: u32) -> Option<DeferredCallableCreation> {
        self.callable_creations.get(&callable).copied()
    }

    fn capture_census(&self, callable: u32) -> Option<CaptureCensus<'_>> {
        let body = self.callables.get(&callable)?;
        // This canonical census also contains nested closures' references. An
        // intermediate closure must retain cells needed by its children.
        let reads = self
            .prototype_callable_reads
            .get(&callable)
            .unwrap_or(&body.reads);
        // A nested closure's write needs the cell as much as a read does.
        let writes = self.nested_callable_writes.get(&callable);
        let free = reads
            .iter()
            .chain(&body.rebinds)
            .chain(writes.into_iter().flatten())
            .filter(|id| {
                !body.declared_locals.contains(*id)
                    && !body.local_functions.contains_key(*id)
                    && !body
                        .params
                        .iter()
                        .any(|parameter| parameter.binding.as_ref() == Some(*id))
            })
            .map(|id| (id, body.reads.contains(id) || body.rebinds.contains(id)))
            .collect();
        Some(CaptureCensus {
            home_object: self
                .callable_creations
                .get(&callable)
                .and_then(|creation| creation.home_object()),
            free,
        })
    }

    fn allocation_layout(&self, allocation: u32) -> LiteralLayout {
        if self.exact_object_spread_copies.contains(&allocation) {
            return LiteralLayout::Unsupported;
        }
        if let Some(elements) = self.array_elements.get(&allocation) {
            return LiteralLayout::Array(elements.clone());
        }
        LiteralLayout::Record
    }

    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind {
        self.literal_property_kinds
            .get(&allocation)
            .and_then(|kinds| kinds.get(position))
            .cloned()
            .unwrap_or(LiteralPropertyKind::Opaque)
    }
}

pub struct SemanticShapeInputs<'a> {
    pub classes: LocalClassConstructionShapes,
    pub sets: SetIterableShapes,
    pub array_spreads: &'a StableArraySpreadShapes,
    pub callables: CallableInferenceInputs,
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

    pub fn collect(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        flow: Option<&ChunkValueFlow>,
        plain_array_bindings: &BTreeSet<String>,
        construction_proof: &LocalClassConstructionShapes,
    ) -> Self {
        let mut index =
            Self::collection_seed(body, hints, plain_array_bindings, construction_proof);
        let max_source_position = index.seed_top_level_declarations(body, hints);
        let fresh_steps = index.visit_top_level_items(body, hints);
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
        let mutated_intrinsics =
            index.census_native_writes(body, hints, flow, &namespace_assignments);
        let claim_counts = index.certified_fresh_error_sites(flow, &mutated_intrinsics);
        index.close_namespace_family(
            &namespace_assignments,
            &namespace_value_facts,
            &fresh_steps,
            &claim_counts,
        );
        index.admit_deferred_global_publications(hints);
        index.admit_non_reentrant_global_membership(body, hints);
        index.finish_collection(hints);
        index
    }

    /// The index before any item is visited: per-owner tables sized to `body`
    /// and the author inputs the visitor consults.
    fn collection_seed(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        plain_array_bindings: &BTreeSet<String>,
        construction_proof: &LocalClassConstructionShapes,
    ) -> Self {
        let host_operations = &hints.assumptions.reentry_free_operations;
        let global_guards_requested = !hints.assumptions.deferred_global_publications.is_empty()
            || !hints.assumptions.non_reentrant_global_membership.is_empty();
        // A standard host defines `global` only as Node's global object.
        let node_global = hints.assumptions.predefined_globals != spec::PredefinedGlobals::Unknown
            && unshadowed_global_names(body, &["global"]).contains("global");
        Self {
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
        }
    }

    /// Seeds namespace-privacy roots, class and `const` bindings from every
    /// item before any item is visited. Returns the last source position.
    fn seed_top_level_declarations(
        &mut self,
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
    ) -> u32 {
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
                        self.namespace_privacy.members.insert(
                            local.to_id(),
                            hints
                                .assumptions
                                .namespace_independent_members
                                .get(name)
                                .cloned()
                                .unwrap_or_default(),
                        );
                        self.namespace_privacy.import_sites.insert(local.span.lo.0);
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
                self.class_bindings
                    .insert(declaration.class.span.lo.0, id.clone());
                self.class_ids.insert(id);
            }
            if let Some(declaration) = var_decl_of_item(item.as_module_item())
                && declaration.kind == VarDeclKind::Const
            {
                for declarator in &declaration.decls {
                    self.top_const_bindings
                        .extend(binding_targets::binding_names(&declarator.name));
                }
            }
        }
        max_source_position
    }

    /// Visits every item in source order. Returns each item's fresh-value step.
    fn visit_top_level_items(
        &mut self,
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
    ) -> Vec<Option<FreshValueStep>> {
        let mut fresh_steps = Vec::with_capacity(body.len());
        for (ordinal, item) in body.iter().enumerate() {
            let owner = SourceUnitOrdinal(ordinal);
            self.admit_ordered_bodies(item, owner, hints);
            fresh_steps.push(fresh_value_step(item.as_module_item()));
            if self.global_guards_requested {
                self.deferred_global_candidates[ordinal] =
                    deferred_global_publication(item.as_module_item());
                self.discarded_sync_iifes[ordinal] =
                    discarded_synchronous_iife(item.as_module_item());
            }
            self.owner = SourceUnitOrdinal(ordinal);
            self.direct_top_level_var_declaration = matches!(
                item.as_module_item(),
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(_)))
                    | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                        decl: Decl::Var(_),
                        ..
                    }))
            );
            self.direct_top_level_const_declaration = var_decl_of_item(item.as_module_item())
                .is_some_and(|declaration| declaration.kind == VarDeclKind::Const);
            self.past_await = false;
            self.may_skip_await_depth = 0;
            self.conditional_depth = 0;
            self.may_skip_member_read_depth = 0;
            self.literal_depth = 0;
            self.factory_member_value_depth = 0;
            self.factory_callback_depth = 0;
            self.collect_uninitialized_lexical_bindings(item.as_module_item());
            self.collect_export_roots(item.as_module_item());
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item() {
                self.record_import_specifiers(import, hints);
            }
            match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
                    self.direct_top_level_functions
                        .insert(function.ident.to_id());
                }
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Fn(function) = &export.decl {
                        self.direct_top_level_functions
                            .insert(function.ident.to_id());
                    }
                }
                _ => {}
            }
            item.as_module_item().visit_with(&mut *self);
            self.direct_top_level_var_declaration = false;
            self.direct_top_level_const_declaration = false;
        }
        fresh_steps
    }

    /// Admits `item`'s ordered initializer or statement body and records its
    /// initialization source identity and non-invoking initializer.
    fn admit_ordered_bodies(
        &mut self,
        item: &TopLevelItemView<'_>,
        owner: SourceUnitOrdinal,
        hints: &AnalysisInputs,
    ) {
        let ordinal = owner.0;
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
                .all(|callee| self.class_ids.contains(callee))
        {
            debug_assert_eq!(body.root_span(), Some(expression.span()));
            self.ordered_initializers.insert(owner, body);
        }
        if let TopLevelItemView::Borrowed(ModuleItem::Stmt(Stmt::Expr(statement))) = item
            && let Some(body) = LocalBody::from_expression_statement(&statement.expr)
            && body.root_store_statement().is_some()
        {
            self.ordered_statements.insert(owner, body);
        }
        let source_identity = InitializationSourceIdentity::collect(item, owner);
        if let Ok((identity, _)) = &source_identity {
            self.initialization_source_identities
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
                (if input.returns_opaque_function_from_const_primitive {
                    body.single_identifier_call_with_binding_argument_initializer()
                } else {
                    body.single_identifier_call_with_function_argument_initializer()
                })
            && input.callee == *callee
            && input.call_span == call_span
        {
            self.ordered_initializers.insert(owner, body);
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
                    && self
                        .initialization_source_identities
                        .get(&owner)
                        .is_some_and(|identity| {
                            identity.matches(owner, &binding.id.to_id(), expression.span())
                        })
                {
                    self.ordered_initializers.insert(owner, body);
                }
                self.noninvoking_initializers.insert(owner, initializer);
            }
            Err(reason) => self.initialization_coverage_refusals[ordinal] = Some(reason),
        }
    }

    /// Records each imported binding of `import` with the summaries and author
    /// contracts that name it.
    fn record_import_specifiers(&mut self, import: &ImportDecl, hints: &AnalysisInputs) {
        let host_operations = &hints.assumptions.reentry_free_operations;
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
            self.imported_bindings.insert(local.to_id());
            if host_async_local_storage
                && let ImportSpecifier::Named(named) = specifier
                && !named.is_type_only
                && binding_targets::imported_export_name(named) == "AsyncLocalStorage"
            {
                self.host_async_local_storage_imports.insert(local.to_id());
            }
            if let Some(record) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.returned_record.as_ref())
            {
                self.returned_record_summaries
                    .insert(local.to_id(), record.clone());
            }
            if let Some(wrapper) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.namespace_wrapper.as_ref())
            {
                self.namespace_wrapper_summaries
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
                            analysis_vocabulary::inputs::CallbackFactoryEvidence::InferredImmutableWrapper { .. }
                        )
                    }) {
                        self
                            .callback_factory_call_summaries
                            .insert(local.to_id(), call.clone());
                    }
                } else {
                    self.foreign_call_imports
                        .insert(local.to_id(), call.clone());
                }
            }
            if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                && summary.supports_callback_factory()
            {
                self.callback_factory_imports.insert(local.to_id());
                self.callback_factory_summaries
                    .insert(local.to_id(), summary.clone());
            }
            if let Some(summary) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.descriptor_copy_factory.as_ref())
            {
                self.descriptor_copy_factories
                    .insert(local.to_id(), summary.clone());
            }
            if hints
                .assumptions
                .inline_callback_factories
                .contains(local.sym.as_ref())
            {
                self.inline_callback_factory_imports.insert(local.to_id());
            }
            if hints
                .assumptions
                .no_reentry_exports
                .contains(local.sym.as_ref())
            {
                self.no_reentry_exports.insert(local.to_id());
            }
            if let Some(signatures) = hints
                .assumptions
                .no_reentry_callback_exports
                .get(local.sym.as_ref())
            {
                self.no_reentry_callback_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(signatures) = hints
                .assumptions
                .retained_callback_factories
                .get(local.sym.as_ref())
            {
                self.retained_callback_factory_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(signatures) = hints
                .assumptions
                .retained_callback_constructors
                .get(local.sym.as_ref())
            {
                self.retained_callback_constructor_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(members) = hints
                .assumptions
                .retained_callback_factory_members
                .get(local.sym.as_ref())
            {
                self.retained_callback_factory_members
                    .insert(local.to_id(), members.clone());
                self.audited_factory_members
                    .entry(local.to_id())
                    .or_default()
                    .extend(members.keys().cloned());
            }
            if let Some(members) = hints
                .assumptions
                .callback_factory_members
                .get(local.sym.as_ref())
            {
                self.callback_factory_members
                    .insert(local.to_id(), members.clone());
                self.audited_factory_members
                    .entry(local.to_id())
                    .or_default()
                    .extend(members.iter().cloned());
            }
            if let Some(members) = hints.assumptions.no_reentry_members.get(local.sym.as_ref()) {
                self.no_reentry_members
                    .insert(local.to_id(), members.clone());
            }
            if let Some(members) = hints
                .assumptions
                .no_reentry_record_members
                .get(local.sym.as_ref())
            {
                self.no_reentry_record_members
                    .insert(local.to_id(), members.clone());
            }
        }
    }

    fn finish_collection(&mut self, hints: &AnalysisInputs) {
        let primitive_const_candidates = std::mem::take(&mut self.direct_primitive_const_bindings);
        self.direct_primitive_const_bindings = primitive_const_candidates
            .into_iter()
            .filter(|binding| {
                if !self.dynamic_scope_owners.is_empty()
                    || self.reassigned_bindings.contains(binding)
                {
                    return false;
                }
                matches!(
                    self.binding_declaration_writes.get(binding).map(Vec::as_slice),
                    Some([declaration])
                        if declaration.direct_top_level && declaration.simple_identifier
                )
            })
            .collect();
        // Native-operation inputs annotate exact source sites without
        // refining the native census. Array-protocol contradictions are
        // checked later against the completed all-source census.
        self.validate_native_operation_inputs(
            &hints.selected_native_operation_inputs,
            &hints.assumptions.native_operation_inputs,
        );
        self.ident_references.sort_unstable();
        self.ident_references.dedup();
        self.this_reading_accessor_objects = self
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
                } => self.reads_this(callable).then_some(home_object),
                _ => None,
            })
            .collect();
        self.this_reading_accessor_classes = self
            .class_accessors
            .iter()
            .filter(|(_, accessors)| accessors.iter().any(|accessor| self.reads_this(*accessor)))
            .map(|(class, _)| *class)
            .collect();
    }

    /// Whether `callable`, or an arrow or other callable nested in it, reads
    /// `this`. An unindexed or opaque callable may.
    pub fn reads_this(&self, callable: u32) -> bool {
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
    pub fn references_within(&self, lo: u32, hi: u32) -> impl Iterator<Item = &Id> {
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
    pub fn sole_binding_reference_at(&self, binding: &Id, span: Span) -> bool {
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
    pub fn single_eager_initializer_read(&self, call: &Call) -> bool {
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
                            .is_some_and(|span| span.contains(call.span))
                })
    }

    pub fn unconditionally_evaluated_member_read(&self, call: &Call) -> bool {
        !self
            .conditional_top_member_reads
            .contains(&CallSite::from(call.span))
    }

    pub fn statement_span(&self, owner: SourceUnitOrdinal) -> Span {
        self.original_statement_spans[owner.0]
    }

    pub fn locate_native_integrity_witnesses(
        &mut self,
        mut locate: impl FnMut(Span) -> Option<analysis_vocabulary::execution_evidence::SourceLocation>,
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

    fn collect_uninitialized_lexical_bindings(&mut self, item: &ModuleItem) {
        let Some(declaration) = var_decl_of_item(item) else {
            return;
        };
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
                        let exported = binding_targets::public_export_name(named);
                        self.exported_names.insert(exported, local.to_id());
                        self.exported_bindings.insert(local.to_id());
                    }
                }
            }
            _ => {}
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
