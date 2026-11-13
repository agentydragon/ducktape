//! Typed selector-synthesis rejection evidence and explicit human adapters.

use selector_outcome::{InvalidSelectorError, InvalidSelectorReport, SelectorOutcome};
use serde::Serialize;
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "snake_case",
    bound(serialize = "E: Serialize + fmt::Display")
)]
pub enum SynthesisSkipReason<E = InvalidSelectorError> {
    BindingDeclarationCount {
        binding: String,
        count: usize,
    },
    UnrepresentableDeclaration,
    ExactSelectorDiscarded,
    ExactDeclarationRejected {
        failure: ProofFailure<E>,
    },
    NormalizationRejected {
        failure: ProofFailure<E>,
    },
    AnonymousStatementNotUnique {
        body_index: usize,
        outcome: Box<SelectorOutcome<E>>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "snake_case",
    bound(serialize = "E: Serialize + fmt::Display")
)]
pub enum ProofFailure<E = InvalidSelectorError> {
    EmptyTargets,
    TargetNotProven {
        binding: String,
        export_name: String,
        outcome: Box<SelectorOutcome<E>>,
    },
    WrongBodyIndex {
        actual: usize,
        expected: usize,
    },
}

impl<E: fmt::Display> SynthesisSkipReason<E> {
    pub fn explanation(&self) -> SynthesisExplanation<'_, E> {
        SynthesisExplanation(self)
    }
}

impl<E: fmt::Display> ProofFailure<E> {
    pub fn explanation(&self) -> ProofExplanation<'_, E> {
        ProofExplanation(self)
    }
}

impl ProofFailure {
    pub(crate) fn from_error(error: anyhow::Error) -> anyhow::Result<Self> {
        error.downcast::<ProofError>().map(|error| error.0)
    }
}

/// Internal error transport keeps a proof rejection downcastable through anyhow.
#[derive(Debug)]
pub(crate) struct ProofError(pub ProofFailure);

impl fmt::Display for ProofError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.explanation().fmt(f)
    }
}

impl std::error::Error for ProofError {}

pub struct SynthesisExplanation<'a, E>(&'a SynthesisSkipReason<E>);

impl<E: fmt::Display> fmt::Display for SynthesisExplanation<'_, E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.0 {
            SynthesisSkipReason::BindingDeclarationCount { binding, count: 0 } => {
                write!(f, "binding `{binding}` is not declared in the source chunk")
            }
            SynthesisSkipReason::BindingDeclarationCount { binding, count } => write!(
                f,
                "binding `{binding}` is declared in {count} source declarations"
            ),
            SynthesisSkipReason::UnrepresentableDeclaration => {
                f.write_str("the declaration cannot be represented as a source_match selector")
            }
            SynthesisSkipReason::ExactSelectorDiscarded => {
                f.write_str("the exact declaration proves, but minimization discarded it")
            }
            SynthesisSkipReason::ExactDeclarationRejected { failure } => write!(
                f,
                "the exact declaration does not prove: {}",
                failure.explanation()
            ),
            SynthesisSkipReason::NormalizationRejected { failure } => write!(
                f,
                "the synthesized selector stopped proving after normalization: {}",
                failure.explanation()
            ),
            SynthesisSkipReason::AnonymousStatementNotUnique {
                body_index,
                outcome,
            } => write!(
                f,
                "the exact statement does not select body index {body_index} alone: {}",
                outcome.render_line()
            ),
        }
    }
}

pub struct ProofExplanation<'a, E>(&'a ProofFailure<E>);

impl<E: fmt::Display> fmt::Display for ProofExplanation<'_, E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.0 {
            ProofFailure::EmptyTargets => f.write_str("selector synthesis group has no targets"),
            ProofFailure::TargetNotProven {
                binding,
                export_name,
                outcome,
            } => write!(
                f,
                "synthesized selector does not prove `{binding}` as `{export_name}`: {}",
                outcome.render_line()
            ),
            ProofFailure::WrongBodyIndex { actual, expected } => write!(
                f,
                "synthesized selector matched body index {actual} instead of intended {expected}"
            ),
        }
    }
}

impl SynthesisSkipReason {
    pub fn to_report(&self) -> SynthesisSkipReason<InvalidSelectorReport> {
        match self {
            Self::BindingDeclarationCount { binding, count } => {
                SynthesisSkipReason::BindingDeclarationCount {
                    binding: binding.clone(),
                    count: *count,
                }
            }
            Self::UnrepresentableDeclaration => SynthesisSkipReason::UnrepresentableDeclaration,
            Self::ExactSelectorDiscarded => SynthesisSkipReason::ExactSelectorDiscarded,
            Self::ExactDeclarationRejected { failure } => {
                SynthesisSkipReason::ExactDeclarationRejected {
                    failure: failure.to_report(),
                }
            }
            Self::NormalizationRejected { failure } => SynthesisSkipReason::NormalizationRejected {
                failure: failure.to_report(),
            },
            Self::AnonymousStatementNotUnique {
                body_index,
                outcome,
            } => SynthesisSkipReason::AnonymousStatementNotUnique {
                body_index: *body_index,
                outcome: Box::new(outcome.to_report()),
            },
        }
    }
}

impl ProofFailure {
    pub fn to_report(&self) -> ProofFailure<InvalidSelectorReport> {
        match self {
            Self::EmptyTargets => ProofFailure::EmptyTargets,
            Self::TargetNotProven {
                binding,
                export_name,
                outcome,
            } => ProofFailure::TargetNotProven {
                binding: binding.clone(),
                export_name: export_name.clone(),
                outcome: Box::new(outcome.to_report()),
            },
            Self::WrongBodyIndex { actual, expected } => ProofFailure::WrongBodyIndex {
                actual: *actual,
                expected: *expected,
            },
        }
    }
}
