//! Closure creation: one kernel rule for every adapter. A closure is its
//! callable's address in the creating world plus the lexical captures it
//! retains; the adapter supplies only the capture and admission policy.

use super::super::allocation_transfer::{AllocationJudgment, AllocationValue};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::execution_index::ExecutionIndex;
use super::super::local_operations::DeferredCallableCreation;
use super::super::value_model::{
    AbstractValue, Context, HeapAddress, LexicalCaptures, LexicalCellId, Values,
};
use super::context::KernelContext;
use super::frame::{CallableSelection, LocalFrame, LocalValueResult, RemainderDisposition};
use crate::facts::abstract_domain::value::Value;
use imbl::OrdMap;
use rustc_hash::FxHashSet;
use std::collections::BTreeSet;
use swc_ecma_ast::Id;

/// How a closure created now retains a free binding that has no active cell.
#[derive(Clone, Copy)]
pub(in crate::facts::sync_effects) enum CapturePolicy<'a> {
    /// The application prefix. Module bindings are read live at call time,
    /// so creation captures nothing for them. A binding past the capture
    /// budget is unknown. A binding some closure writes, and a value only a
    /// nested closure reads, keep their capabilities but not their exact
    /// value: unknown alone reaches no unescaped closure.
    Application {
        module_bindings: &'a FxHashSet<Id>,
        mutable: &'a BTreeSet<Id>,
    },
    /// An isolated provider query: each frame value is retained exactly. The
    /// query's retention walk decides what a returned closure exposes.
    Exact,
}

/// Captures beyond this many make the remaining snapshots unknown.
const CAPTURE_BUDGET: usize = 64;

/// What the adapter admits about one closure creation.
pub(in crate::facts::sync_effects) struct ClosureAdmission {
    pub(in crate::facts::sync_effects) effects: EffectCoverageFragment,
    pub(in crate::facts::sync_effects) judgment: AllocationJudgment,
}

/// The lexical captures of `callable` created in `environment`: a captured
/// binding with an active cell is shared by identity, any other is a snapshot.
pub(in crate::facts::sync_effects) fn lexical_captures(
    index: &ExecutionIndex,
    active_cells: &OrdMap<Id, LexicalCellId>,
    environment: &Context,
    callable: u32,
    policy: CapturePolicy<'_>,
) -> LexicalCaptures {
    let Some(body) = index.callables.get(&callable) else {
        return LexicalCaptures::default();
    };
    // This canonical census also contains nested closures' references. An
    // intermediate closure must retain cells needed by its children.
    let reads = index
        .prototype_callable_reads
        .get(&callable)
        .unwrap_or(&body.reads);
    let mut captured = LexicalCaptures {
        home_object: index.callable_home_object(callable),
        ..Default::default()
    };
    for id in reads.iter().chain(body.rebinds.iter()) {
        if body.declared_locals.contains(id)
            || body.local_functions.contains_key(id)
            || body
                .params
                .iter()
                .any(|parameter| parameter.binding.as_ref() == Some(id))
        {
            continue;
        }
        if let CapturePolicy::Application {
            module_bindings, ..
        } = policy
            && module_bindings.contains(id)
        {
            continue;
        }
        if let Some(cell) = active_cells.get(id) {
            captured.cells.insert(id.clone(), *cell);
            continue;
        }
        let Some(values) = environment.get(id) else {
            continue;
        };
        let snapshot = match policy {
            CapturePolicy::Exact => values.clone(),
            CapturePolicy::Application { .. }
                if captured.snapshots.len() + captured.cells.len() >= CAPTURE_BUDGET =>
            {
                Values::from([AbstractValue::Value(Value::Top)])
            }
            CapturePolicy::Application { mutable, .. }
                if mutable.contains(id)
                    || (!body.reads.contains(id) && !body.rebinds.contains(id)) =>
            {
                // A later write can replace a written binding's value, and a
                // source-summary activation supplied a descendant-only value
                // snapshot, not a live binding proof. Retain the capabilities
                // while forfeiting the exact value.
                let mut possible = values.clone();
                possible.insert(AbstractValue::Value(Value::Top));
                possible
            }
            CapturePolicy::Application { .. } => values.clone(),
        };
        captured.snapshots.insert(id.clone(), snapshot);
    }
    captured
}

/// Create the closure for one canonical callable creation in this frame. Its
/// body does not run; the address's scope keeps it apart from closures other
/// worlds create at the same source site.
pub(super) fn create(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    creation: DeferredCallableCreation,
) -> Result<LocalValueResult, RemainderDisposition> {
    let callable = creation.callable();
    let index = context.index();
    if index.callable_creation(callable) != Some(creation)
        || !index.callables.contains_key(&callable)
    {
        // Lowering names only canonical creations; anything else has no
        // callable census to capture from.
        return Err(RemainderDisposition::NeedsSummary);
    }
    let captures = lexical_captures(
        index,
        &context.world().active_cells,
        &frame.environment,
        callable,
        context.capture_policy(),
    );
    let ClosureAdmission { effects, judgment } =
        context.admit_closure(frame, creation, &captures)?;
    let address = HeapAddress {
        scope: context.world().scope,
        callable,
    };
    let values = Values::from([AbstractValue::Closure(
        address,
        context.intern_captures(captures),
    )]);
    frame.inputs.extend(values.iter().cloned());
    Ok(LocalValueResult {
        selection: effects
            .lexical_selection_dependencies()
            .and_then(|dependencies| CallableSelection::observed(&values, dependencies)),
        allocation: AllocationValue { values, judgment },
        effects,
    })
}
