//! Deterministic synchronous models that consume a settled runtime prefix.

use super::super::array_transfer::{ArrayIterationExecution, ArrayIterationSelection};
use super::super::value_model::{AbstractValue, Values};
use super::interpreter::{Executor, Outcome, ValidatedMetadataDecoration};
use crate::facts::local_effect_targets::MetadataDecorateCall;
use source_identity::SourceUnitOrdinal;

pub(super) enum ModelOperation {
    ExecuteArrayIteration {
        selection: ArrayIterationSelection,
        arguments: Vec<Values>,
        outcome: Box<Outcome>,
    },
    ConstructLocalClass {
        key: u32,
        arguments: Vec<Values>,
    },
    SummarizeMetadataDecoration {
        application: MetadataDecorateCall,
        validated: ValidatedMetadataDecoration,
        owner: SourceUnitOrdinal,
        outcome: Box<Outcome>,
    },
}

#[derive(Clone)]
pub(super) enum ModelResult {
    ArrayIteration {
        execution: Option<Box<ArrayIterationExecution>>,
        outcome: Box<Outcome>,
    },
    LocalClassConstruction(
        Box<Result<(Outcome, AbstractValue), crate::execution_evidence::ConstructionRejection>>,
    ),
    MetadataDecoration {
        outcome: Box<Outcome>,
    },
}

pub(super) fn dispatch(executor: &mut Executor<'_, '_>, operation: ModelOperation) -> ModelResult {
    match operation {
        ModelOperation::ExecuteArrayIteration {
            selection,
            arguments,
            mut outcome,
        } => {
            let execution =
                executor.execute_array_iteration_transfer(&selection, &arguments, &mut outcome);
            ModelResult::ArrayIteration {
                execution: execution.map(Box::new),
                outcome,
            }
        }
        ModelOperation::ConstructLocalClass { key, arguments } => {
            ModelResult::LocalClassConstruction(Box::new(
                executor.construct_local_class_transfer(key, &arguments),
            ))
        }
        ModelOperation::SummarizeMetadataDecoration {
            application,
            validated,
            owner,
            mut outcome,
        } => {
            executor.summarize_metadata_decoration_transfer(
                &application,
                validated,
                owner,
                &mut outcome,
            );
            ModelResult::MetadataDecoration { outcome }
        }
    }
}
