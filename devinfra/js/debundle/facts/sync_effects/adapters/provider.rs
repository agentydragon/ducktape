//! The provider adapter: the five kernel context traits over the strict
//! provider's isolated allocation-only query.

use super::super::allocation_transfer::{self, AllocationJudgment, AllocationValue};
use super::super::alternatives::{PathState, join_worlds};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::execution_index::ExecutionIndex;
use super::super::kernel::closure::{CapturePolicy, ClosureAdmission};
use super::super::kernel::context::{
    BindingContext, CallContext, CellPolicy, ControlContext, Disposition, ForkPolicy, HeapContext,
    NativeContext, Unproved,
};
use super::super::kernel::frame::{
    LocalCallRequest, LocalFrame, LocalValueResult, RemainderDisposition,
};
use super::super::local_operations::DeferredCallableCreation;
use super::super::own_data_transfer::OwnDataContext;
use super::super::value_model::{
    CallSite, CaptureInterner, CapturedContext, Context, LexicalCaptures, OwnPropertyShape,
    ValueExpr, Values,
};
use super::super::world::World;
use super::super::world::join::Joined;
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::abstract_domain::builtins::{self, Invocation};
use crate::facts::abstract_domain::integrity::Integrity;
use rustc_hash::FxHashSet;
use std::collections::BTreeSet;
use swc_ecma_ast::Id;

pub(in crate::facts::sync_effects) struct ProviderContext<'a> {
    pub(in crate::facts::sync_effects) index: &'a ExecutionIndex,
    pub(in crate::facts::sync_effects) bindings: Context,
    /// Every closure retains all arguments: the walk does not prove which
    /// argument a closure body can reach.
    pub(in crate::facts::sync_effects) argument_values: Values,
    /// Outer locals some closure reads; they get cells.
    pub(in crate::facts::sync_effects) captured_locals: FxHashSet<Id>,
    pub(in crate::facts::sync_effects) capture_contexts: CaptureInterner<LexicalCaptures>,
    /// Closures whose capture this query cannot represent; retaining one
    /// refuses the summary.
    pub(in crate::facts::sync_effects) unsupported_closures: BTreeSet<u32>,
    pub(in crate::facts::sync_effects) class_mode: bool,
    pub(in crate::facts::sync_effects) local_bindings: BTreeSet<Id>,
    pub(in crate::facts::sync_effects) shadowed: BTreeSet<&'static str>,
    pub(in crate::facts::sync_effects) native_constructions: BTreeSet<NativeIntrinsic>,
    /// The query's fresh heap: an isolated scope, so its cells never reach a
    /// caller's effects and no literal writability is proven.
    pub(in crate::facts::sync_effects) world: World,
    /// The retained-closure proof projects one closure and refuses a fork;
    /// an allocation-only body explores it.
    pub(in crate::facts::sync_effects) fork_policy: ForkPolicy,
}

impl ProviderContext<'_> {
    fn construct_empty_collection(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        if !arguments.is_empty() {
            return None;
        }
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let (name, family) = match binding.0.as_ref() {
            "Map" => ("Map", NativeIntrinsic::Map),
            "Set" => ("Set", NativeIntrinsic::Set),
            "WeakMap" => ("WeakMap", NativeIntrinsic::WeakMap),
            "WeakSet" => ("WeakSet", NativeIntrinsic::WeakSet),
            _ => return None,
        };
        if self.shadowed.contains(name)
            || self.index.property_key_declarations.contains(binding)
            || self
                .local_bindings
                .iter()
                .any(|binding| binding.0.as_ref() == name)
        {
            return None;
        }
        let row = builtins::lookup(name, None, Invocation::Construct)?;
        let Integrity::Intact(dependencies) = self.index.integrity.require(row.requires, None)
        else {
            return None;
        };
        let result = allocation_transfer::keyed_collection_new(row, site, dependencies.clone())?;
        self.native_constructions.insert(family);
        Some(result.allocation)
    }
}

impl OwnDataContext for ProviderContext<'_> {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.index.ordinary_record_shape(allocation)
    }
    fn object_properties_are_settled(&self, _allocation: u32) -> bool {
        // This isolated query refuses external mutation and publication.
        true
    }
    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values> {
        self.world.surface.data_values(allocation, property)
    }
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.world
            .surface
            .has_accessor_property(allocation, property)
    }
    fn property_is_writable(&self, _allocation: u32, _property: &str) -> bool {
        false
    }
}

impl HeapContext for ProviderContext<'_> {
    fn index(&self) -> &ExecutionIndex {
        self.index
    }
    fn world(&self) -> &World {
        &self.world
    }
    fn world_mut(&mut self) -> &mut World {
        &mut self.world
    }
    fn current_prerequisites(&self) -> Box<ProofDependencies> {
        Box::new(self.world.prerequisites.clone())
    }
    fn admit_closure(
        &mut self,
        frame: &LocalFrame,
        creation: DeferredCallableCreation,
        _captures: &LexicalCaptures,
    ) -> Result<ClosureAdmission, RemainderDisposition> {
        let callable = creation.callable();
        let Some(body) = self.index.callables.get(&callable) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let parameters: BTreeSet<_> = body
            .params
            .iter()
            .filter_map(|parameter| parameter.binding.as_ref())
            .collect();
        let parameter_bindings: BTreeSet<_> = body
            .params
            .iter()
            .flat_map(|parameter| parameter.bindings.iter())
            .collect();
        let class_unavailable_capture = body.reads.iter().any(|binding| {
            !body.declared_locals.contains(binding)
                && !parameters.contains(binding)
                && !self.local_bindings.contains(binding)
        });
        let unavailable_capture = body.reads.iter().any(|binding| {
            body.self_binding.as_ref() != Some(binding)
                && !body.declared_locals.contains(binding)
                && !body.pattern_locals.contains(binding)
                && !parameter_bindings.contains(binding)
                && !self.local_bindings.contains(binding)
                && !frame.environment.contains_key(binding)
                && !self.index.deferred_module_reference(binding)
        });
        if self.class_mode
            && (class_unavailable_capture
                || body.generator
                || self.index.async_callables.contains(&callable))
        {
            return Err(RemainderDisposition::NeedsSummary);
        }
        if unavailable_capture
            || ((self.class_mode || creation.home_object().is_none())
                && (body.generator || self.index.async_callables.contains(&callable)))
        {
            self.unsupported_closures.insert(callable);
        }
        if body.implicit_capture && self.class_mode {
            // Method/accessor receivers are dynamic, not the constructor's
            // lexical receiver. Keep class-mode admission conservative. The
            // lexical receiver is the constructed instance, the retention
            // walk's root.
            if creation.home_object().is_some() || frame.this_values.is_none() {
                return Err(RemainderDisposition::NeedsSummary);
            }
        } else if body.implicit_capture && matches!(creation, DeferredCallableCreation::Arrow(_)) {
            self.unsupported_closures.insert(callable);
        }
        Ok(ClosureAdmission {
            // Provider queries have no application call/execution receipt.
            effects: EffectCoverageFragment::default(),
            judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
        })
    }
    fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext {
        self.capture_contexts.intern(captures)
    }
}

impl BindingContext for ProviderContext<'_> {
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        // The frame starts with scoped arguments and checked private var
        // captures. Other entries come from the covered local operations.
        let Some(values) = frame.environment.get(binding) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        Ok(LocalValueResult {
            allocation: AllocationValue {
                values: values.clone(),
                judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            },
            effects: Default::default(),
            selection: None,
        })
    }
    fn cell_policy(&self) -> CellPolicy<'_> {
        // A returned closure retains the final contents of the locals it
        // reads; a captured store would publish outside the query's heap.
        CellPolicy {
            captured: &self.captured_locals,
        }
    }
    fn capture_policy(&self) -> CapturePolicy<'_> {
        CapturePolicy::Exact
    }
    fn cell_effect(&self) -> EffectCoverageFragment {
        // Provider queries have no application execution receipt.
        EffectCoverageFragment::default()
    }
}

impl CallContext for ProviderContext<'_> {
    fn call(
        &mut self,
        _frame: &mut LocalFrame,
        _request: &LocalCallRequest,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        // An allocation-only query has no invocation certificate. Forwarding
        // a symbolic callable does not prove its execution or result shape.
        Err(RemainderDisposition::NeedsSummary)
    }
}

impl NativeContext for ProviderContext<'_> {
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        self.construct_empty_collection(callee, arguments, site)
    }
}

/// The path state of a provider query: its private heap and what the path
/// found about its own allocations.
#[derive(Clone)]
pub(in crate::facts::sync_effects) struct ProviderPath {
    world: World,
    unsupported_closures: BTreeSet<u32>,
    native_constructions: BTreeSet<NativeIntrinsic>,
}

impl PathState for ProviderPath {
    fn world(&self) -> &World {
        &self.world
    }
}

impl ProviderContext<'_> {
    /// Install the join of a family's leaves. Refusals and consumed native
    /// identities are may-facts; a world the join refuses refuses the query.
    #[must_use]
    pub(in crate::facts::sync_effects) fn join(&mut self, leaves: Vec<ProviderPath>) -> bool {
        let Joined {
            world,
            widened: _,
            refused,
        } = join_worlds(leaves.iter().map(|leaf| &leaf.world));
        self.world = world;
        for leaf in leaves {
            self.unsupported_closures.extend(leaf.unsupported_closures);
            self.native_constructions.extend(leaf.native_constructions);
        }
        refused.is_none()
    }
}

impl ControlContext for ProviderContext<'_> {
    type Branch = ProviderPath;
    fn fork_policy(&self) -> ForkPolicy {
        self.fork_policy
    }
    fn branch(&self) -> ProviderPath {
        ProviderPath {
            world: self.world.clone(),
            unsupported_closures: self.unsupported_closures.clone(),
            native_constructions: self.native_constructions.clone(),
        }
    }
    fn enter(&mut self, path: ProviderPath) {
        let ProviderPath {
            world,
            unsupported_closures,
            native_constructions,
        } = path;
        self.world = world;
        self.unsupported_closures = unsupported_closures;
        self.native_constructions = native_constructions;
    }
    fn unproved(&mut self, _frame: &mut LocalFrame, _what: Unproved<'_>) -> Disposition {
        // Symbolic inputs and captures prove no descriptor, getter or coercion
        // behavior. Only the shared transfers can certify an operation, and
        // there is no application state to summarize into.
        Disposition::Refused
    }
}
