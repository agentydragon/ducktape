//! Native invalid-selector causes and their explicit report projection.

use serde::{Deserialize, Serialize};
use source_match::{
    AnonymousProblem, AnythingPosition, BindingProblem, ClaimProblem, SourceMatchError,
};

#[derive(Debug, Clone, thiserror::Error)]
pub enum InvalidSelectorError {
    #[error(transparent)]
    SourceMatch(#[from] SourceMatchError),
    #[error(transparent)]
    Projection(#[from] ProjectionError),
    #[error(
        "template identifier `{identifier}` is exported by modules {modules:?}; rename it in the template or rename one export"
    )]
    AmbiguousReference {
        identifier: String,
        modules: Vec<String>,
    },
    #[error(transparent)]
    Member(#[from] spec::MemberSelectorError),
    #[error(transparent)]
    Anonymous(#[from] spec::AnonymousStatementSelectorError),
    #[error(transparent)]
    Claims(#[from] ClaimValidationError),
}

#[derive(
    Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, thiserror::Error,
)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ProjectionError {
    #[error("anonymous source_match candidate has no final statement to claim")]
    EmptyAnonymousCandidate,
    #[error(
        "anonymous source_match final statement at body index {body_index} declares a binding and cannot be claimed as anonymous"
    )]
    AnonymousDeclaresBinding { body_index: usize },
    #[error(
        "anonymous source_match final statement at body index {body_index} does not map to an assignable dependency graph node"
    )]
    AnonymousSourceUnitMissing { body_index: usize },
    #[error(
        "source_match candidate at body index {body_index} binding `{binding}` does not map to a dependency graph node"
    )]
    BindingSourceUnitMissing { body_index: usize, binding: String },
}

#[derive(
    Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, thiserror::Error,
)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimValidationError {
    #[error(
        "logical_module {logical_module}: {selector_kind:?} requires `name:` because the public export name cannot default to a binding name until the selector is resolved"
    )]
    MissingExportName {
        logical_module: String,
        selector_kind: super::SelectorKind,
    },
    #[error(
        "logical_module {logical_module}: annotations key `{export_name}` does not match any member name or source_matches[].bindings name"
    )]
    UnknownAnnotation {
        logical_module: String,
        export_name: String,
    },
    #[error(
        "logical_module {logical_module}: annotations key `{export_name}` is ambiguous because that readable binding name is claimed {count} times"
    )]
    AmbiguousAnnotation {
        logical_module: String,
        export_name: String,
        count: usize,
    },
    #[error(
        "logical_module {logical_module}: annotations.{export_name}.{field} conflicts with metadata already declared on the member"
    )]
    AnnotationConflict {
        logical_module: String,
        export_name: String,
        field: AnnotationField,
    },
    #[error("{operation} {id} has duplicate source binding claims:{}", DuplicateClaimsDisplay(.claims))]
    DuplicateBindings {
        operation: ClaimOperation,
        id: String,
        claims: Vec<DuplicateBinding>,
    },
    #[error("{operation} {id} has duplicate exported logical names: {}", .names.join(", "))]
    DuplicateExports {
        operation: ClaimOperation,
        id: String,
        names: Vec<String>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct DuplicateBinding {
    pub binding: String,
    pub claimants: Vec<BindingClaimant>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct BindingClaimant {
    pub export_name: String,
    pub claim_origin: String,
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum ClaimOperation {
    LogicalModule,
    ChunkRenames,
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum AnnotationField {
    Purity,
    InitializerPurity,
    InitializerBindingEffects,
    Effect,
    PureMembers,
    NoSyncCallbackMembers,
    Comment,
    Note,
}

/// Report-only representation: foreign diagnostic text is constructed here,
/// while the domain retains the parser's and matcher's native errors.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct InvalidSelectorReport {
    pub message: String,
    #[serde(flatten)]
    pub cause: InvalidSelectorCause,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "cause", rename_all = "snake_case")]
pub enum InvalidSelectorCause {
    Parse {
        logical_module: String,
        field: source_match::SelectorField,
        match_source: String,
        source_name: String,
        diagnostics: Vec<ParserDiagnostic>,
    },
    Anything {
        positions: Vec<AnythingPosition>,
    },
    Facts {
        context: String,
    },
    Unsupported {
        reason: String,
    },
    Binding {
        logical_module: String,
        target: String,
        match_source: String,
        problem: BindingProblem,
    },
    MultipleBindings {
        logical_module: String,
        body_index: usize,
        count: usize,
    },
    Anonymous {
        logical_module: String,
        match_source: String,
        problem: AnonymousProblem,
    },
    Claim {
        logical_module: String,
        problem: ClaimProblem,
    },
    Projection {
        problem: ProjectionError,
    },
    AmbiguousReference {
        identifier: String,
        modules: Vec<String>,
    },
    Member {
        problem: spec::MemberSelectorError,
    },
    AnonymousSpec {
        problem: spec::AnonymousStatementSelectorError,
    },
    Claims {
        problem: ClaimValidationError,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct ParserDiagnostic {
    pub range: Option<ParserRange>,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct ParserRange {
    pub start: u32,
    pub end: u32,
}

impl std::fmt::Display for InvalidSelectorReport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl InvalidSelectorError {
    pub fn from_request_error(error: anyhow::Error) -> anyhow::Result<Self> {
        let error = match error.downcast::<SourceMatchError>() {
            Ok(error) => return Ok(error.into()),
            Err(error) => error,
        };
        let error = match error.downcast::<spec::MemberSelectorError>() {
            Ok(error) => return Ok(error.into()),
            Err(error) => error,
        };
        let error = match error.downcast::<spec::AnonymousStatementSelectorError>() {
            Ok(error) => return Ok(error.into()),
            Err(error) => error,
        };
        error.downcast::<ClaimValidationError>().map(Self::Claims)
    }

    pub fn to_report(&self) -> InvalidSelectorReport {
        let cause = match self {
            Self::SourceMatch(error) => match error {
                SourceMatchError::Parse {
                    logical_module,
                    field,
                    match_source,
                    source,
                } => InvalidSelectorCause::Parse {
                    logical_module: logical_module.clone(),
                    field: *field,
                    match_source: match_source.clone(),
                    source_name: source.source_name.clone(),
                    diagnostics: source
                        .errors()
                        .map(|error| ParserDiagnostic {
                            range: error
                                .span()
                                .lo
                                .0
                                .checked_sub(source.source_start.0)
                                .zip(error.span().hi.0.checked_sub(source.source_start.0))
                                .filter(|(start, end)| {
                                    start <= end && *end as usize <= match_source.len()
                                })
                                .map(|(start, end)| ParserRange { start, end }),
                            message: error.kind().msg().into_owned(),
                        })
                        .collect(),
                },
                SourceMatchError::Anything { positions } => InvalidSelectorCause::Anything {
                    positions: positions.clone(),
                },
                SourceMatchError::Facts { source } => InvalidSelectorCause::Facts {
                    context: source.context.to_string(),
                },
                SourceMatchError::Unsupported { source } => InvalidSelectorCause::Unsupported {
                    reason: source.reason.to_string(),
                },
                SourceMatchError::Binding {
                    logical_module,
                    target,
                    match_source,
                    problem,
                } => InvalidSelectorCause::Binding {
                    logical_module: logical_module.clone(),
                    target: target.clone(),
                    match_source: match_source.clone(),
                    problem: problem.clone(),
                },
                SourceMatchError::MultipleBindings {
                    logical_module,
                    body_index,
                    count,
                } => InvalidSelectorCause::MultipleBindings {
                    logical_module: logical_module.clone(),
                    body_index: *body_index,
                    count: *count,
                },
                SourceMatchError::Anonymous {
                    logical_module,
                    match_source,
                    problem,
                } => InvalidSelectorCause::Anonymous {
                    logical_module: logical_module.clone(),
                    match_source: match_source.clone(),
                    problem: problem.clone(),
                },
                SourceMatchError::Claim {
                    logical_module,
                    problem,
                } => InvalidSelectorCause::Claim {
                    logical_module: logical_module.clone(),
                    problem: problem.clone(),
                },
            },
            Self::Projection(problem) => InvalidSelectorCause::Projection {
                problem: problem.clone(),
            },
            Self::AmbiguousReference {
                identifier,
                modules,
            } => InvalidSelectorCause::AmbiguousReference {
                identifier: identifier.clone(),
                modules: modules.clone(),
            },
            Self::Member(problem) => InvalidSelectorCause::Member { problem: *problem },
            Self::Anonymous(problem) => InvalidSelectorCause::AnonymousSpec { problem: *problem },
            Self::Claims(problem) => InvalidSelectorCause::Claims {
                problem: problem.clone(),
            },
        };
        InvalidSelectorReport {
            message: self.to_string(),
            cause,
        }
    }
}

struct DuplicateClaimsDisplay<'a>(&'a [DuplicateBinding]);

impl std::fmt::Display for DuplicateClaimsDisplay<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        for claim in self.0 {
            write!(
                f,
                "\n- source binding `{}` claimed {} times:",
                claim.binding,
                claim.claimants.len()
            )?;
            for claimant in &claim.claimants {
                write!(
                    f,
                    "\n  - export `{}` ({})",
                    claimant.export_name, claimant.claim_origin
                )?;
            }
        }
        Ok(())
    }
}
