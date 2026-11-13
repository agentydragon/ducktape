//! Typed selector-synthesis rejection evidence and explicit human adapters.

use selector_outcome::SelectorOutcome;
use serde::Serialize;
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SynthesisSkipReason {
    BindingDeclarationCount {
        binding: String,
        count: usize,
    },
    UnrepresentableDeclaration,
    ExactSelectorDiscarded,
    ExactDeclarationRejected {
        failure: ProofFailure,
    },
    NormalizationRejected {
        failure: ProofFailure,
    },
    AnonymousStatementNotUnique {
        body_index: usize,
        outcome: Box<SelectorOutcome>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ProofFailure {
    EmptyTargets,
    TargetNotProven {
        binding: String,
        export_name: String,
        outcome: Box<SelectorOutcome>,
    },
    WrongBodyIndex {
        actual: usize,
        expected: usize,
    },
}

impl SynthesisSkipReason {
    pub fn explanation(&self) -> SynthesisExplanation<'_> {
        SynthesisExplanation(self)
    }
}

impl ProofFailure {
    pub fn explanation(&self) -> ProofExplanation<'_> {
        ProofExplanation(self)
    }

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

pub struct SynthesisExplanation<'a>(&'a SynthesisSkipReason);

impl fmt::Display for SynthesisExplanation<'_> {
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

pub struct ProofExplanation<'a>(&'a ProofFailure);

impl fmt::Display for ProofExplanation<'_> {
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
