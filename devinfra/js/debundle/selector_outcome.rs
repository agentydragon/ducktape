//! The per-entity selector outcome record, shared by every command that
//! resolves selectors: `debundle run --dry-run` (per-chunk
//! `selector_diagnostics.json` and its stderr report), `debundle spec validate`
//! (both modes) and `debundle spec match-selector`. Each command emits a
//! [`SelectorOutcomeReport`], and every human line comes from
//! [`SelectorOutcome::render_line`], so the same entity reads the same in all
//! of them.
//!
//! `run` and `validate` list only entities that did not resolve, plus
//! resolved-by-elimination warnings; `match-selector` reports its one
//! entity's outcome, resolved or not.
//!
//! Not yet recorded as outcomes: name-pin debt annotated with `note:`, and
//! `alpha_all` readable names that are free references rather than local
//! binders.

use std::collections::BTreeMap;
use std::fmt::Write;

use serde::ser::SerializeStruct;
use serde::{Deserialize, Serialize, Serializer};

/// A selector the shape matcher places at more than this many places is
/// [`Outcome::TooBroad`], rejected without a solve: a selector that loose names
/// no declaration.
pub const MAX_CANDIDATES_PER_SELECTOR: usize = 100;

/// [`Outcome::Ambiguous`] lists at most this many candidates. The solver stops
/// enumerating alternatives at the same bound.
pub const MAX_LISTED_CANDIDATES: usize = 5;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Deserialize)]
pub struct SelectorOutcome {
    /// Chunk id (`run`, keep-going `validate`) or the chunk path the command
    /// was given (source-only `validate`, `match-selector`).
    pub chunk: String,
    /// Where the selector sits in the spec. `None` for `match-selector`,
    /// whose probe is not part of a spec.
    #[serde(default)]
    pub placement: Option<Placement>,
    /// The selector-local name the entity claims, for `source_match`
    /// selectors that name one.
    #[serde(default)]
    pub target_binding: Option<String>,
    /// A `source_match`'s `match` text, whitespace-collapsed and truncated; a
    /// name pin's or relational selector's parameters. `None` where the
    /// entity has no parsed selector.
    #[serde(default)]
    pub selector_preview: Option<String>,
    pub outcome: Outcome,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Placement {
    /// Module path, e.g. `ui/widgets` (without the chunk).
    pub logical_module: String,
    /// `None` for a module-level entry such as a stale `annotations:` key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entity: Option<Entity>,
    pub selector_kind: SelectorKind,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Entity {
    /// A member or `source_matches[].bindings[]` entry, by its readable name.
    Export(String),
    /// An `anonymous_statements[]` entry, by its index in the module file.
    AnonymousStatement(usize),
}

/// Another spec entity an outcome names: the selectors a conflict involves,
/// the claimers that eliminated alternatives, the earlier owner of a
/// duplicate claim.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct EntityRef {
    pub logical_module: String,
    /// `None` for a solver target that has neither a readable name nor an
    /// anonymous-statement index.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entity: Option<Entity>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SelectorKind {
    #[serde(rename = "source_matches")]
    SourceMatches,
    #[serde(rename = "members.source_match")]
    MemberSourceMatch,
    #[serde(rename = "anonymous_statements.source_match")]
    AnonymousStatement,
    #[serde(rename = "members.binding")]
    Binding,
    #[serde(rename = "members.cross_ref")]
    CrossRef,
    #[serde(rename = "members.reads_member")]
    ReadsMember,
    #[serde(rename = "members.member_of_module")]
    MemberOfModule,
    #[serde(rename = "members.passed_to_call")]
    PassedToCall,
    #[serde(rename = "members.makes_decorate_call")]
    MakesDecorateCall,
    #[serde(rename = "members.intrinsic_alias")]
    IntrinsicAlias,
    /// A member whose selector did not parse, so its kind is unknown.
    #[serde(rename = "members.selector")]
    UnparsedMember,
    #[serde(rename = "annotations")]
    Annotations,
}

/// One top-level place a selector matched: the statement's index in the chunk
/// body and, for a declaration, the minified binding it declares there.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Candidate {
    pub owner: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<String>,
}

/// The top-level statement that declares a binding.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Declaration {
    /// Index of the statement in the chunk body.
    pub owner: usize,
    pub kind: DeclarationKind,
}

/// The keyword a top-level binding is declared with.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeclarationKind {
    Function,
    Class,
    Var,
    Let,
    Const,
    Using,
    AwaitUsing,
    Import,
}

impl DeclarationKind {
    pub fn keyword(self) -> &'static str {
        match self {
            Self::Function => "function",
            Self::Class => "class",
            Self::Var => "var",
            Self::Let => "let",
            Self::Const => "const",
            Self::Using => "using",
            Self::AwaitUsing => "await using",
            Self::Import => "import",
        }
    }
}

/// A top-level statement a selector does not match, with where it first
/// diverges; higher `score` is closer.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct NearMiss {
    /// Index of the statement in the chunk body.
    pub owner: usize,
    /// The bindings it declares.
    pub bindings: Vec<String>,
    pub score: usize,
    pub reason: String,
}

impl NearMiss {
    fn render(&self) -> String {
        let declares = match self.bindings.as_slice() {
            [] => String::new(),
            bindings => format!(
                " declaring {}",
                bindings
                    .iter()
                    .map(|binding| format!("`{binding}`"))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        };
        format!(
            "body[{}]{declares} (score {}): {}",
            self.owner, self.score, self.reason
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Outcome {
    Resolved {
        owner: usize,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        binding: Option<String>,
        resolved_by: ResolvedBy,
    },
    NoMatch {
        /// The unclaimed top-level statements closest to the selector, closest
        /// first.
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        nearest_unclaimed: Vec<NearMiss>,
    },
    Ambiguous {
        candidates: Vec<Candidate>,
        /// More candidates exist than are listed.
        truncated: bool,
    },
    /// In an unsatisfiable core of the joint solve with `with`; the set need
    /// not be minimal.
    Conflict {
        with: Vec<EntityRef>,
    },
    TooBroad {
        count: usize,
        limit: usize,
    },
    /// Resolved to `binding`, which `claimed_by` already claims.
    DuplicateClaim {
        binding: String,
        declaration: Declaration,
        claimed_by: EntityRef,
    },
    /// The selector could not be evaluated: it does not parse, uses an
    /// unsupported construct, or its matches do not map to owners.
    Invalid {
        error: String,
    },
    /// The solver stopped (a time limit) before proving the entity unique or
    /// listing its alternatives.
    Undecided {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "by", rename_all = "snake_case")]
pub enum ResolvedBy {
    OwnSelector,
    /// Several places match its selector, and exactly one of them agrees
    /// with where the entities its template names (`references`) resolved.
    OwnReferences {
        references: Vec<EntityRef>,
    },
    /// Unique only because `claimers` took its other candidates; it moves
    /// silently when one of them is edited.
    Elimination {
        claimers: Vec<EntityRef>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum OutcomeKind {
    Resolved,
    NoMatch,
    Ambiguous,
    Conflict,
    TooBroad,
    DuplicateClaim,
    Invalid,
    Undecided,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    Ok,
    /// Resolved, but fragile; the run continues.
    Warning,
    /// Did not resolve; the run fails.
    Error,
}

impl OutcomeKind {
    /// The `kind` tag [`Outcome`] serializes with.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Resolved => "resolved",
            Self::NoMatch => "no_match",
            Self::Ambiguous => "ambiguous",
            Self::Conflict => "conflict",
            Self::TooBroad => "too_broad",
            Self::DuplicateClaim => "duplicate_claim",
            Self::Invalid => "invalid",
            Self::Undecided => "undecided",
        }
    }
}

impl Serialize for OutcomeKind {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl Outcome {
    /// The outcome of a selector matched on its own, from every place it
    /// matched.
    pub fn no_match() -> Self {
        Self::NoMatch {
            nearest_unclaimed: Vec::new(),
        }
    }

    pub fn from_matches(mut candidates: Vec<Candidate>) -> Self {
        candidates.sort();
        candidates.dedup();
        match candidates.len() {
            0 => Self::no_match(),
            1 => {
                let Candidate { owner, binding } = candidates.remove(0);
                Self::Resolved {
                    owner,
                    binding,
                    resolved_by: ResolvedBy::OwnSelector,
                }
            }
            count if count > MAX_CANDIDATES_PER_SELECTOR => Self::too_broad(count),
            _ => Self::ambiguous(candidates, false),
        }
    }

    pub fn too_broad(count: usize) -> Self {
        Self::TooBroad {
            count,
            limit: MAX_CANDIDATES_PER_SELECTOR,
        }
    }

    /// Sorted, capped at [`MAX_LISTED_CANDIDATES`]; `truncated` is set when the
    /// caller already knows more exist or the cap drops some.
    pub fn ambiguous(mut candidates: Vec<Candidate>, truncated: bool) -> Self {
        candidates.sort();
        candidates.dedup();
        let truncated = truncated || candidates.len() > MAX_LISTED_CANDIDATES;
        candidates.truncate(MAX_LISTED_CANDIDATES);
        Self::Ambiguous {
            candidates,
            truncated,
        }
    }

    pub fn kind(&self) -> OutcomeKind {
        match self {
            Self::Resolved { .. } => OutcomeKind::Resolved,
            Self::NoMatch { .. } => OutcomeKind::NoMatch,
            Self::Ambiguous { .. } => OutcomeKind::Ambiguous,
            Self::Conflict { .. } => OutcomeKind::Conflict,
            Self::TooBroad { .. } => OutcomeKind::TooBroad,
            Self::DuplicateClaim { .. } => OutcomeKind::DuplicateClaim,
            Self::Invalid { .. } => OutcomeKind::Invalid,
            Self::Undecided { .. } => OutcomeKind::Undecided,
        }
    }

    pub fn severity(&self) -> Severity {
        match self {
            Self::Resolved {
                resolved_by: ResolvedBy::OwnSelector | ResolvedBy::OwnReferences { .. },
                ..
            } => Severity::Ok,
            Self::Resolved {
                resolved_by: ResolvedBy::Elimination { .. },
                ..
            } => Severity::Warning,
            _ => Severity::Error,
        }
    }

    /// `statements` says the entity claims statements rather than
    /// declarations (an anonymous statement).
    fn describe(&self, statements: bool) -> String {
        let places = if statements {
            "top-level statement"
        } else {
            "top-level declaration"
        };
        match self {
            Self::Resolved {
                owner,
                binding,
                resolved_by,
            } => {
                let target = render_candidate(&Candidate {
                    owner: *owner,
                    binding: binding.clone(),
                });
                match resolved_by {
                    ResolvedBy::OwnSelector => format!("resolved to {target}"),
                    ResolvedBy::OwnReferences { references } => format!(
                        "resolved through its references to {target}: only that match agrees \
                         with {}",
                        render_refs(references)
                    ),
                    ResolvedBy::Elimination { claimers } => format!(
                        "resolved by elimination to {target}: its other matches are claimed by {}",
                        render_refs(claimers)
                    ),
                }
            }
            Self::NoMatch { nearest_unclaimed } if nearest_unclaimed.is_empty() => {
                format!("did not match any {places}")
            }
            Self::NoMatch { nearest_unclaimed } => format!(
                "did not match any {places}; nearest unclaimed: {}",
                nearest_unclaimed
                    .iter()
                    .map(NearMiss::render)
                    .collect::<Vec<_>>()
                    .join("; ")
            ),
            Self::Ambiguous {
                candidates,
                truncated,
            } => format!(
                "is ambiguous -- matched {}{} {places}s: {}",
                if *truncated { "at least " } else { "" },
                candidates.len(),
                candidates
                    .iter()
                    .map(render_candidate)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Self::Conflict { with } => format!(
                "conflicts with {}: these selectors admit no joint assignment (the listed set \
                 need not be minimal)",
                render_refs(with)
            ),
            Self::TooBroad { count, limit } => {
                format!("matches {count} places (limit {limit}); anchor it more specifically")
            }
            Self::DuplicateClaim {
                binding,
                declaration,
                claimed_by,
            } => format!(
                "binding {binding:?} (`{}` at body[{}]) is already claimed by {} as `{}`; each \
                 binding may belong to exactly one logical module",
                declaration.kind.keyword(),
                declaration.owner,
                claimed_by.logical_module,
                match &claimed_by.entity {
                    Some(Entity::Export(name)) => name.as_str(),
                    _ => "<unnamed>",
                },
            ),
            Self::Invalid { error } => error.clone(),
            Self::Undecided { reason } => {
                format!("was not decided: the solver stopped before deciding it ({reason})")
            }
        }
    }
}

fn render_candidate(candidate: &Candidate) -> String {
    match &candidate.binding {
        Some(binding) => format!("`{binding}` (body[{}])", candidate.owner),
        None => format!("body[{}]", candidate.owner),
    }
}

fn render_refs(refs: &[EntityRef]) -> String {
    refs.iter()
        .map(|entity_ref| match &entity_ref.entity {
            Some(Entity::Export(name)) => format!("`{name}` in {}", entity_ref.logical_module),
            Some(Entity::AnonymousStatement(index)) => {
                format!(
                    "anonymous_statements[{index}] in {}",
                    entity_ref.logical_module
                )
            }
            None => format!("an unnamed selector in {}", entity_ref.logical_module),
        })
        .collect::<Vec<_>>()
        .join(", ")
}

impl SelectorKind {
    /// The spec field the selector is authored in.
    fn origin(self, target_binding: Option<&str>) -> String {
        match (self, target_binding) {
            (Self::SourceMatches, Some(target)) => format!("source_matches[].bindings[`{target}`]"),
            (Self::SourceMatches, None) => "source_matches[]".to_string(),
            (Self::MemberSourceMatch, _) => "members[].selector.source_match".to_string(),
            (Self::AnonymousStatement, _) => "anonymous_statements[]".to_string(),
            (Self::Binding, _) => "members[].selector.binding".to_string(),
            (Self::CrossRef, _) => "members[].selector.cross_ref".to_string(),
            (Self::ReadsMember, _) => "members[].selector.reads_member".to_string(),
            (Self::MemberOfModule, _) => "members[].selector.member_of_module".to_string(),
            (Self::PassedToCall, _) => "members[].selector.passed_to_call".to_string(),
            (Self::MakesDecorateCall, _) => "members[].selector.makes_decorate_call".to_string(),
            (Self::IntrinsicAlias, _) => "members[].selector.intrinsic_alias".to_string(),
            (Self::UnparsedMember, _) => "members[].selector".to_string(),
            (Self::Annotations, _) => "annotations".to_string(),
        }
    }
}

impl SelectorOutcome {
    pub fn severity(&self) -> Severity {
        self.outcome.severity()
    }

    /// One line naming the entity, what happened to it and its selector.
    pub fn render_line(&self) -> String {
        let label = match self.severity() {
            Severity::Warning => "warning: ",
            Severity::Ok | Severity::Error => "",
        };
        let kind = self.outcome.kind().as_str();
        let mut line = format!("[{label}{kind}] {}", self.chunk);
        let mut statements = false;
        match &self.placement {
            Some(placement) => {
                let origin = placement
                    .selector_kind
                    .origin(self.target_binding.as_deref());
                let _ = write!(line, "::{}", placement.logical_module);
                match &placement.entity {
                    Some(Entity::Export(name)) => {
                        let _ = write!(line, " as `{name}` ({origin})");
                    }
                    Some(Entity::AnonymousStatement(index)) => {
                        statements = true;
                        let _ = write!(line, " anonymous_statements[{index}]");
                    }
                    None => {
                        let _ = write!(line, " ({origin})");
                    }
                }
            }
            None => {
                if let Some(target) = &self.target_binding {
                    let _ = write!(line, " target `{target}`");
                }
            }
        }
        let _ = write!(line, ": {}", self.outcome.describe(statements));
        if let Some(preview) = &self.selector_preview {
            let _ = write!(line, " -- selector: {preview}");
        }
        line
    }
}

/// Serialized with its derived `severity` alongside the stored fields.
impl Serialize for SelectorOutcome {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut record = serializer.serialize_struct("SelectorOutcome", 6)?;
        record.serialize_field("chunk", &self.chunk)?;
        match &self.placement {
            Some(placement) => record.serialize_field("placement", placement)?,
            None => record.skip_field("placement")?,
        }
        match &self.target_binding {
            Some(target_binding) => record.serialize_field("target_binding", target_binding)?,
            None => record.skip_field("target_binding")?,
        }
        match &self.selector_preview {
            Some(preview) => record.serialize_field("selector_preview", preview)?,
            None => record.skip_field("selector_preview")?,
        }
        record.serialize_field("outcome", &self.outcome)?;
        record.serialize_field("severity", &self.severity())?;
        record.end()
    }
}

/// What one free identifier of a template means (<../SPEC.md> § Matching).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum IdentifierMeaning {
    /// Matches only where it is `entity`'s own binding.
    Reference { entity: EntityRef },
    /// Several other modules export the name: the template is `invalid`.
    Ambiguous { modules: Vec<String> },
    /// An unshadowed runtime global: matches only itself.
    Global,
    /// Alpha-renamed: matches any identifier.
    Wildcard,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct FreeIdentifier {
    pub name: String,
    #[serde(flatten)]
    pub meaning: IdentifierMeaning,
}

/// The free identifiers of one `source_match` template that matched in
/// `chunk`, with what each means there.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct TemplateIdentifiers {
    pub chunk: String,
    pub logical_module: String,
    /// The entities the template places: its member or anonymous statement,
    /// or every binding of its `source_matches[]` entry.
    pub entities: Vec<Entity>,
    pub identifiers: Vec<FreeIdentifier>,
}

impl TemplateIdentifiers {
    /// One line per reference and ambiguous name.
    fn render_lines(&self, out: &mut String) {
        let entities = self
            .entities
            .iter()
            .map(|entity| match entity {
                Entity::Export(name) => format!("`{name}`"),
                Entity::AnonymousStatement(index) => format!("anonymous_statements[{index}]"),
            })
            .collect::<Vec<_>>()
            .join(", ");
        for identifier in &self.identifiers {
            let meaning = match &identifier.meaning {
                IdentifierMeaning::Wildcard | IdentifierMeaning::Global => continue,
                IdentifierMeaning::Reference { entity } => {
                    format!("references {}", render_refs(std::slice::from_ref(entity)))
                }
                IdentifierMeaning::Ambiguous { modules } => {
                    format!("is ambiguous: exported by {}", modules.join(", "))
                }
            };
            let _ = writeln!(
                out,
                "  - {}::{} {entities}: `{}` {meaning}",
                self.chunk, self.logical_module, identifier.name
            );
        }
    }
}

/// A list of outcomes, serialized with a count per [`OutcomeKind`], and
/// optionally what each matched template's free identifiers mean.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct SelectorOutcomeReport {
    pub outcomes: Vec<SelectorOutcome>,
    /// Filled only by `spec validate`.
    #[serde(default)]
    pub templates: Vec<TemplateIdentifiers>,
}

impl SelectorOutcomeReport {
    pub fn counts(&self) -> BTreeMap<OutcomeKind, usize> {
        let mut counts = BTreeMap::new();
        for outcome in &self.outcomes {
            *counts.entry(outcome.outcome.kind()).or_insert(0) += 1;
        }
        counts
    }

    /// A count line, then one [`SelectorOutcome::render_line`] per outcome,
    /// at most `limit` of them.
    pub fn render_text(&self, out: &mut String, limit: Option<usize>) {
        if !self.templates.is_empty() {
            let mut kinds = BTreeMap::<&str, usize>::new();
            for identifier in self
                .templates
                .iter()
                .flat_map(|template| &template.identifiers)
            {
                *kinds
                    .entry(match identifier.meaning {
                        IdentifierMeaning::Reference { .. } => "reference",
                        IdentifierMeaning::Ambiguous { .. } => "ambiguous",
                        IdentifierMeaning::Global => "global",
                        IdentifierMeaning::Wildcard => "wildcard",
                    })
                    .or_insert(0) += 1;
            }
            let kinds = kinds
                .into_iter()
                .map(|(kind, count)| format!("{kind}={count}"))
                .collect::<Vec<_>>()
                .join(", ");
            let _ = writeln!(
                out,
                "{} matched template(s) with free identifiers: {kinds}",
                self.templates.len()
            );
            for template in &self.templates {
                template.render_lines(out);
            }
        }
        if self.outcomes.is_empty() {
            out.push_str("No selector problems found.\n");
            return;
        }
        let counts = self
            .counts()
            .into_iter()
            .map(|(kind, count)| format!("{}={count}", kind.as_str()))
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(out, "{} selector outcome(s): {counts}", self.outcomes.len());
        let shown = limit.unwrap_or(self.outcomes.len());
        for outcome in self.outcomes.iter().take(shown) {
            let _ = writeln!(out, "  - {}", outcome.render_line());
        }
        if self.outcomes.len() > shown {
            let _ = writeln!(
                out,
                "  ... showing first {shown} of {}; selector_diagnostics.json retains the full \
                 report.",
                self.outcomes.len()
            );
        }
    }
}

impl Serialize for SelectorOutcomeReport {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut report = serializer.serialize_struct("SelectorOutcomeReport", 3)?;
        report.serialize_field("counts", &self.counts())?;
        report.serialize_field("outcomes", &self.outcomes)?;
        if self.templates.is_empty() {
            report.skip_field("templates")?;
        } else {
            report.serialize_field("templates", &self.templates)?;
        }
        report.end()
    }
}
