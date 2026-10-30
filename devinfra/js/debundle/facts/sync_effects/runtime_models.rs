//! Deterministic synchronous models that consume a settled runtime prefix.

use super::super::value_model::{AbstractValue, CallSite, Context, Values};
use super::interpreter::{Executor, Outcome, ValidatedMetadataDecoration};
use crate::facts::local_effect_targets::MetadataDecorateCall;
use source_identity::SourceUnitOrdinal;

pub(super) enum ModelOperation {
    ExecuteArrayIteration {
        span: CallSite,
        environment: Context,
        captures: Context,
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
        handled: bool,
        outcome: Box<Outcome>,
    },
    LocalClassConstruction(
        Box<Result<(Outcome, AbstractValue), crate::reports::schema::ConstructionRejection>>,
    ),
    MetadataDecoration {
        outcome: Box<Outcome>,
    },
}

pub(super) fn dispatch(executor: &mut Executor<'_, '_>, operation: ModelOperation) -> ModelResult {
    match operation {
        ModelOperation::ExecuteArrayIteration {
            span,
            environment,
            captures,
            mut outcome,
        } => {
            let handled = executor.execute_array_iteration_transfer(
                span,
                &environment,
                &captures,
                &mut outcome,
            );
            ModelResult::ArrayIteration { handled, outcome }
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
