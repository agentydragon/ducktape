//! Native selector failures. Formatting belongs to Display or report projection.

#[derive(Debug, Clone, thiserror::Error)]
pub enum SourceMatchError {
    #[error(
        "logical_module {logical_module}: {field} did not parse as JS:\n{match_source}\n{source}"
    )]
    Parse {
        logical_module: String,
        field: SelectorField,
        match_source: String,
        #[source]
        source: std::sync::Arc<js_ast::JsParseError>,
    },
    #[error("source_match `ANYTHING` is unsupported in {}. `ANYTHING` is anonymous sugar only for expression (`EXPR`), statement (`STMT`), pattern, variable-declarator-list (`DECLARATORS`), object-property-list, and class-member-list holes. Use typed holes when the position needs stronger diagnostics. Object property keys still need exact keys; use `key: ANYTHING` to wildcard a property value or `{{ ANYTHING }}` to skip arbitrary properties.", AnythingPositionsDisplay(.positions))]
    Anything { positions: Vec<AnythingPosition> },
    #[error("source_match needle did not project to facts: {source}")]
    Facts { source: chunk_facts::Unsupported },
    #[error("source_match comparison unsupported: {source}")]
    Unsupported { source: selector_match::Unsupported },
    #[error(
        "logical_module {logical_module}: source_matches[].bindings local `{target}` {problem}:\n{match_source}"
    )]
    Binding {
        logical_module: String,
        target: String,
        match_source: String,
        problem: BindingProblem,
    },
    #[error(
        "logical_module {logical_module}: source_matches[] matched top-level statement at body index {body_index}, but that statement declares {count} bindings. Use a single-declarator selector or refine the match."
    )]
    MultipleBindings {
        logical_module: String,
        body_index: usize,
        count: usize,
    },
    #[error(
        "logical_module {logical_module}: anonymous_statements[].match {problem}:\n{match_source}"
    )]
    Anonymous {
        logical_module: String,
        match_source: String,
        problem: AnonymousProblem,
    },
    #[error("logical_module {logical_module}: source_matches[] {problem}")]
    Claim {
        logical_module: String,
        problem: ClaimProblem,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AnythingPosition {
    ClassMemberInitializer,
    ObjectPropertyKey,
    ObjectAssignmentKey,
    ObjectGetterKey,
    ObjectSetterKey,
    ObjectMethodKey,
    BindingIdentifier,
    Identifier,
}

#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    thiserror::Error,
)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BindingProblem {
    #[error("is not declared by the selector source")]
    NotDeclared,
    #[error(
        "is ambiguous within the selector source at {index_kind} indices {indices:?}; refine the selector source"
    )]
    Ambiguous {
        index_kind: ProjectionIndexKind,
        indices: Vec<(usize, usize)>,
    },
    #[error("was matched by a {hole} hole, not a pinned declaration")]
    MatchedHole { hole: ProjectionHole },
}

#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    strum::Display,
)]
#[serde(rename_all = "snake_case")]
pub enum ProjectionIndexKind {
    #[strum(serialize = "statement/binding")]
    StatementBinding,
    #[strum(serialize = "declarator/binding")]
    DeclaratorBinding,
}

#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    thiserror::Error,
)]
#[serde(rename_all = "snake_case")]
pub enum ProjectionHole {
    #[error("DECLARATORS")]
    Declarators,
    #[error("STMT_LIST")]
    StatementList,
}

#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    thiserror::Error,
)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AnonymousProblem {
    #[error(
        "parsed to zero statements; selector source must contain exactly one top-level statement"
    )]
    Empty,
    #[error(
        "parsed to a STMT_LIST hole; selector source must contain a pinned top-level statement to claim"
    )]
    Unpinned,
    #[error(
        "parsed to {count} statements; exact `match` must contain exactly one top-level statement"
    )]
    ExactWindow { count: usize },
    #[error(
        "context cannot contain a STMT_LIST hole; context must be a contiguous window ending in the statement to claim"
    )]
    ContextHole,
    #[error("final statement declares a binding; only an anonymous final statement can be claimed")]
    DeclaresBinding,
}

#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    thiserror::Error,
)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClaimProblem {
    #[error("must include non-empty `bindings`")]
    EmptyBindings,
    #[error("parsed to zero statements")]
    EmptySource,
    #[error("declares duplicate selector-local binding names: {names:?}")]
    DuplicateDeclarations { names: Vec<String> },
    #[error("bindings repeats `{local}`")]
    DuplicateLocal { local: String },
    #[error("bindings repeats readable name `{name}`")]
    DuplicateName { name: String },
    #[error("bindings entry `{local}` is neither declared nor used by source_matches[].match")]
    UnknownLocal { local: String },
}

#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    serde::Serialize,
    serde::Deserialize,
    strum::Display,
)]
#[serde(rename_all = "snake_case")]
pub enum SelectorField {
    #[strum(serialize = "source_match")]
    SourceMatch,
    #[strum(serialize = "source_matches[]")]
    SourceMatches,
}

/// These failures indicate a broken matcher/projection contract, not an invalid selector.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum MatcherInvariant {
    MissingTarget,
    MissingGroupTarget { target: String },
    ExpectedVariableDeclaration,
    MissingDeclarator,
    MissingBinding,
    DeclaratorAlignment,
    UnexpectedGroupTarget,
}

impl SourceMatchError {
    pub fn binding(
        request: &str,
        selector: &spec::AnonymousStatementSelector,
        target: &str,
        problem: BindingProblem,
    ) -> Self {
        Self::Binding {
            logical_module: request.to_string(),
            target: target.to_string(),
            match_source: selector.match_source.clone(),
            problem,
        }
    }

    pub fn anonymous(
        request: &str,
        selector: &spec::AnonymousStatementSelector,
        problem: AnonymousProblem,
    ) -> Self {
        Self::Anonymous {
            logical_module: request.to_string(),
            match_source: selector.match_source.clone(),
            problem,
        }
    }

    pub fn claim(request: &str, problem: ClaimProblem) -> Self {
        Self::Claim {
            logical_module: request.to_string(),
            problem,
        }
    }
}

#[derive(Debug, Clone, thiserror::Error)]
pub enum MatcherError {
    #[error(transparent)]
    Invalid(#[from] SourceMatchError),
    #[error("source_match resolver invariant failed: {problem:?}")]
    Invariant { problem: MatcherInvariant },
}

impl MatcherError {
    pub fn invariant(problem: MatcherInvariant) -> Self {
        Self::Invariant { problem }
    }
}

struct AnythingPositionsDisplay<'a>(&'a [AnythingPosition]);

impl std::fmt::Display for AnythingPositionsDisplay<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        for (index, position) in self.0.iter().enumerate() {
            if index != 0 {
                f.write_str(", ")?;
            }
            f.write_str(match position {
                AnythingPosition::ClassMemberInitializer => "a class member with an initializer",
                AnythingPosition::ObjectPropertyKey => "an object property key",
                AnythingPosition::ObjectAssignmentKey => "an object assignment property key",
                AnythingPosition::ObjectGetterKey => "an object getter key",
                AnythingPosition::ObjectSetterKey => "an object setter key",
                AnythingPosition::ObjectMethodKey => "an object method key",
                AnythingPosition::BindingIdentifier => "a binding identifier",
                AnythingPosition::Identifier => "an identifier",
            })?;
        }
        Ok(())
    }
}
