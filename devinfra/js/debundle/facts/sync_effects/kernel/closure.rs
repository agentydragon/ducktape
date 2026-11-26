//! Closure creation: one kernel rule for every adapter. A closure is its
//! callable's address in the creating world plus the lexical captures it
//! retains; the adapter supplies only the capture and admission policy.

use super::context::KernelContext;
use super::frame::{LocalFrame, LocalValueResult, RemainderDisposition};
use analysis_vocabulary::value::Value;
use imbl::OrdMap;
use rustc_hash::FxHashSet;
use std::collections::BTreeSet;
use swc_ecma_ast::Id;
use value_model::CallableSelection;
use value_model::local_operations::DeferredCallableCreation;
use value_model::{AbstractValue, Context, HeapAddress, LexicalCaptures, LexicalCellId, Values};
use world::allocation_transfer::{AllocationJudgment, AllocationValue};
use world::effect_coverage::EffectCoverageFragment;

/// How a closure created now retains a free binding that has no active cell.
#[derive(Clone, Copy)]
pub enum CapturePolicy<'a> {
    /// The application prefix. Module bindings are read live at call time,
    /// so creation captures nothing for them. A binding some closure writes
    /// is unknown (its stores reach a cell or are published); a value past
    /// the capture budget, or only a nested closure reads, keeps its
    /// capabilities but not its exact value.
    Application {
        module_bindings: &'a FxHashSet<Id>,
        mutable: &'a BTreeSet<Id>,
    },
    /// An isolated provider query: each frame value is retained exactly. The
    /// query's retention walk decides what a returned closure exposes.
    Exact,
}

/// Captures beyond this many keep only their values' capabilities.
const CAPTURE_BUDGET: usize = 64;

/// What the adapter admits about one closure creation.
pub struct ClosureAdmission {
    pub effects: EffectCoverageFragment,
    pub judgment: AllocationJudgment,
}

/// What `lexical_captures` reads of one callable's census.
pub struct CaptureCensus<'a> {
    pub home_object: Option<u32>,
    /// Bindings the callable or a closure nested in it reads, rebinds or
    /// writes without binding them itself, each with whether the callable's
    /// own body reads or rebinds it. May repeat.
    pub free: Vec<(&'a Id, bool)>,
}

/// The lexical captures of a callable created in `environment`: a captured
/// binding with an active cell is shared by identity, any other is a snapshot.
pub fn lexical_captures(
    census: CaptureCensus<'_>,
    active_cells: &OrdMap<Id, LexicalCellId>,
    environment: &Context,
    policy: CapturePolicy<'_>,
) -> LexicalCaptures {
    let mut captured = LexicalCaptures {
        home_object: census.home_object,
        ..Default::default()
    };
    for (id, own) in census.free {
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
            CapturePolicy::Application { mutable, .. } if mutable.contains(id) => {
                Values::from([AbstractValue::Value(Value::Top)])
            }
            CapturePolicy::Application { .. }
                if captured.snapshots.len() + captured.cells.len() >= CAPTURE_BUDGET || !own =>
            {
                // Past the budget, or for a descendant-only snapshot a
                // source-summary activation supplied (not a live binding
                // proof), retain the capabilities while forfeiting the
                // exact value: unknown alone reaches no unescaped callable.
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
pub(crate) fn create(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    creation: DeferredCallableCreation,
) -> Result<LocalValueResult, RemainderDisposition> {
    let callable = creation.callable();
    let index = context.index();
    let census = match index.capture_census(callable) {
        Some(census) if index.callable_creation(callable) == Some(creation) => census,
        // Lowering names only canonical creations; anything else has no
        // callable census to capture from.
        _ => return Err(RemainderDisposition::NeedsSummary),
    };
    let captures = lexical_captures(
        census,
        &context.world().active_cells,
        &frame.environment,
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
