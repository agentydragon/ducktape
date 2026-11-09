//! Lexical-cell semantics: the kernel creates, initializes and stores the
//! `World` cells closures capture by identity. The adapter's `cell_policy`
//! says only which bindings get one.

use super::super::allocation_transfer::AllocationValue;
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::local_operations::{LocalBinding, LocalBody};
use super::super::world::cells::{CellInitialization, LexicalCell};
use super::context::{Disposition, KernelContext, Unproved};
use super::frame::{LocalFrame, RemainderDisposition};
use swc_common::Span;
use swc_ecma_ast::Id;

/// A self or hoisted-function binding whose value this activation could not
/// establish from source identity alone.
pub(in crate::facts::sync_effects) fn missing_activation_binding(
    body: &LocalBody,
    binding: &Id,
    frame: &LocalFrame,
) -> bool {
    matches!(
        body.bindings.get(binding),
        Some(LocalBinding::NamedSelf(_) | LocalBinding::HoistedFunction(_))
    ) && !frame.environment.contains_key(binding)
}

/// Parameter cells are created in TDZ before an earlier default expression
/// can capture a later parameter.
pub(super) fn instantiate_parameter_cells(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &LocalFrame,
) {
    for parameter in body.parameters.iter() {
        let binding = &parameter.binding;
        if !context.cell_policy().owns(binding) {
            continue;
        }
        let Some(values) = frame.environment.get(binding) else {
            continue;
        };
        let world = context.world_mut();
        let cell = world.cells.insert(LexicalCell {
            values: values.clone(),
            kind: LocalBinding::Parameter,
            initialization: CellInitialization::Uninitialized,
        });
        world.active_cells.insert(binding.clone(), cell);
        world.revision.bump_prefix();
    }
}

/// The body's declaration instantiation: a fresh cell per captured binding.
pub(super) fn instantiate_body_cells(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &LocalFrame,
) {
    let mut allocated = false;
    for (binding, kind) in &body.bindings {
        if *kind == LocalBinding::Parameter && frame.parameter_inputs.is_some() {
            // The parameter environment already exists, including cells
            // captured while a default expression was evaluated.
            continue;
        }
        if missing_activation_binding(body, binding, frame) {
            // A captured context can carry a prior activation's cell. Its
            // source binding is not this missing function value.
            if context.world().active_cells.contains_key(binding) {
                context.world_mut().active_cells.remove(binding);
            }
            continue;
        }
        if !context.cell_policy().owns(binding) {
            continue;
        }
        let Some(values) = frame.environment.get(binding) else {
            // The shared activation model cannot instantiate a missing
            // self/hoisted-function value from source identity alone.
            continue;
        };
        let world = context.world_mut();
        let cell = world.cells.insert(LexicalCell {
            values: values.clone(),
            kind: *kind,
            initialization: match kind {
                LocalBinding::Parameter
                | LocalBinding::Var
                | LocalBinding::NamedSelf(_)
                | LocalBinding::HoistedFunction(_) => CellInitialization::Initialized,
                LocalBinding::Let | LocalBinding::Const => CellInitialization::Uninitialized,
            },
        });
        world.active_cells.insert(binding.clone(), cell);
        allocated = true;
    }
    if allocated {
        // A cached factory result cannot stand in for another activation:
        // its captured cells have distinct identities, even at one site.
        context.world_mut().revision.bump_prefix();
    }
}

/// The frame now holds the declared binding's stored value; mirror it into
/// the binding's cell.
pub(super) fn declared_binding_stored(
    context: &mut impl KernelContext,
    body: &LocalBody,
    binding: &Id,
    frame: &LocalFrame,
) {
    let Some(values) = frame.environment.get(binding) else {
        return;
    };
    let active = context.world().active_cells.get(binding).copied();
    let id = match active {
        Some(id) => id,
        None => {
            let Some(kind @ LocalBinding::HoistedFunction(_)) = body.bindings.get(binding) else {
                return;
            };
            if !context.cell_policy().owns(binding) {
                return;
            }
            // The prologue could not manufacture this function value. An
            // actually executed initializer now establishes this activation's
            // mutable cell using its evaluated value, as for other locals.
            let world = context.world_mut();
            let id = world.cells.insert(LexicalCell {
                values: values.clone(),
                kind: *kind,
                initialization: CellInitialization::Initialized,
            });
            world.active_cells.insert(binding.clone(), id);
            world.revision.bump_prefix();
            id
        }
    };
    let world = context.world_mut();
    if world.cells.store_declared(id, values) {
        world.revision.bump_prefix();
    }
}

/// Effects of declarative-cell creation, initialization, or a checked store
/// to an initialized local. A binding with a live or policy-owned cell can be
/// observed by a closure, so it has no receipt here.
pub(super) fn declarative_binding_operation(
    context: &impl KernelContext,
    body: &LocalBody,
    binding: &Id,
) -> EffectCoverageFragment {
    if !matches!(
        body.bindings.get(binding),
        Some(LocalBinding::Parameter | LocalBinding::Let | LocalBinding::Const)
    ) || context.world().active_cells.contains_key(binding)
        || context.cell_policy().owns(binding)
    {
        // Live captured cells and other activation kinds need their own
        // receipt families, independently of a stored value.
        EffectCoverageFragment::default()
    } else {
        context.cell_effect()
    }
}

/// A store to a binding this body does not declare: through its active cell,
/// or else an operation the kernel cannot represent.
pub(super) fn store_captured(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    binding: &Id,
    values: &super::super::value_model::Values,
    span: Span,
) -> Result<AllocationValue, RemainderDisposition> {
    if let Some(id) = context.world().active_cells.get(binding).copied() {
        let world = context.world_mut();
        if let Some((result, changed)) = world.cells.store_captured(id, values) {
            if changed {
                world.revision.bump_prefix();
            }
            return Ok(AllocationValue::opaque(result));
        }
    }
    Err(
        match context.unproved(frame, Unproved::CaptureStore { values, span }) {
            Disposition::Summarized => RemainderDisposition::AlreadySummarized,
            Disposition::Resolved(_) | Disposition::Refused => RemainderDisposition::NeedsSummary,
        },
    )
}
