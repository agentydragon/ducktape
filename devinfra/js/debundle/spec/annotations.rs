use crate::InitializerBindingEffects;
use crate::InitializerExternalEffects;
use crate::InitializerPurity;
use crate::MemberEffect;
use crate::MemberPurity;
use crate::SourceMatchBindingDetail;
use crate::SourceMatchIdentifierMode;
use crate::is_default_initializer_purity;
use crate::is_default_member_effect;
use crate::is_default_member_purity;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::collections::BTreeSet;

/// Co-mover spec for a top-level anonymous side-effect statement.
/// See [`LogicalModule::anonymous_statements`].
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AnonymousStatement {
    /// JS source of the target top-level statement, verbatim.
    /// Parsed as a single `Stmt` and compared structurally
    /// (modulo spans) against the chunk's top-level statements.
    /// Must match exactly one — zero matches and ambiguous matches
    /// are spec errors.
    #[serde(rename = "match", default, skip_serializing_if = "Option::is_none")]
    pub match_source: Option<String>,
    /// Readable structural selector for the target top-level statement.
    /// `source_match` treats binding/value identifiers in `match` as
    /// alpha-renamable placeholders while keeping literals, operators, member
    /// property names, and overall AST structure significant.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_match: Option<SourceMatch>,
    /// Author assertion that the selected top-level call (bare or the rightmost
    /// operand of `&&`) cannot synchronously invoke callbacks or read caller
    /// lexical state. Arguments remain exposed after the call; not purity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub no_reentry_call: Option<NoReentryCall>,
    /// Optional YAML-only note. Use this for provenance,
    /// uncertainty, and other scratch reverse-engineering notes that
    /// should survive spec edits without appearing in generated JS.
    /// Ignored by the materializer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    /// Optional human-readable comment. Preserved on round-trip and
    /// emitted immediately above the matched anonymous statement in
    /// generated JS. Accepted for symmetry with the top-level
    /// [`LogicalModule::comment`] field and binding `annotations:` comments.
    /// Prefer `note:` for scratch text that should stay YAML-only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
}

/// Callback positions on one source-selected top-level call. The matched
/// source supplies the argument count; every other argument must satisfy the
/// existing inert-primitive syntax admission.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct NoReentryCall {
    pub callback_arguments: Vec<usize>,
}

impl<'de> Deserialize<'de> for NoReentryCall {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct NoReentryCallFields {
            callback_arguments: Vec<usize>,
        }

        let fields = NoReentryCallFields::deserialize(deserializer)?;
        if fields.callback_arguments.is_empty() {
            return Err(serde::de::Error::custom(
                "no_reentry_call must list at least one callback argument position",
            ));
        }
        let mut seen = BTreeSet::new();
        for position in &fields.callback_arguments {
            if !seen.insert(*position) {
                return Err(serde::de::Error::custom(format!(
                    "duplicate no_reentry_call callback argument position {position}"
                )));
            }
        }
        Ok(Self {
            callback_arguments: fields.callback_arguments,
        })
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Default)]
#[serde(deny_unknown_fields)]
pub struct BindingAnnotation {
    /// Audited prototype premises for a source-checked unary local constructor
    /// factory: missing input fields are actually absent and instance assignments
    /// successfully create ordinary own data properties. Source execution, eager reads and
    /// effects remain checked; this does not imply purity or native integrity.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub retained_record_factory: bool,
    /// Spec-level purity annotation for the annotated binding.
    #[serde(default, skip_serializing_if = "is_default_member_purity")]
    pub purity: MemberPurity,
    /// Trust assertion for evaluation of one selected simple top-level
    /// variable initializer; it says nothing about later calls or construction.
    #[serde(default, skip_serializing_if = "is_default_initializer_purity")]
    pub initializer_purity: InitializerPurity,
    /// Source-only lexical effect assertion for one selected top-level
    /// variable initializer. Omission makes no assertion.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initializer_binding_effects: Option<InitializerBindingEffects>,
    /// Audited external-state contract for this initializer's evaluation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initializer_external_effects: Option<InitializerExternalEffects>,
    /// Spec-level local-effect annotation for the annotated binding.
    #[serde(default, skip_serializing_if = "is_default_member_effect")]
    pub effect: MemberEffect,
    /// Property names whose member calls are author-asserted pure.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pure_members: Vec<String>,
    /// Property names whose calls do not synchronously invoke callback
    /// arguments.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub no_sync_callback_members: Vec<String>,
    /// Optional human-readable comment emitted above the matched binding's
    /// source_unit statement in generated JS.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    /// Optional YAML-only note. Preserved by spec edits but never emitted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

impl BindingAnnotation {
    /// Whether the analyzer needs a resolved binding to apply this annotation.
    pub fn has_semantic_annotations(&self) -> bool {
        self.purity != MemberPurity::Default
            || self.initializer_purity != InitializerPurity::Default
            || self.initializer_binding_effects.is_some()
            || self.initializer_external_effects.is_some()
            || self.retained_record_factory
            || self.effect != MemberEffect::Default
            || !self.pure_members.is_empty()
            || !self.no_sync_callback_members.is_empty()
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SourceMatchClaim {
    /// Identifier matching mode for this source-backed claim. Omitted means
    /// alpha-equivalent identifier matching, matching [`SourceMatch`].
    #[serde(skip)]
    pub identifiers: SourceMatchIdentifierMode,
    /// JS source pattern to match against one top-level source statement.
    #[serde(rename = "match")]
    pub match_source: String,
    /// Bindings to claim from the matched source pattern: names the pattern
    /// declares, or free identifiers it uses, which claim the top-level
    /// declaration they bind to. String shorthand means
    /// `{ local: <name>, name: <name> }`; object entries can override the final
    /// readable binding name with `name:`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bindings: Vec<SourceMatchBinding>,
    /// Optional YAML-only note about the selector shape as a whole. Per-binding
    /// semantic notes live in [`LogicalModule::annotations`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

impl SourceMatchClaim {
    /// Return this selector without its YAML-only author rationale. The
    /// explicit field projection keeps future selector fields owned here.
    pub fn without_author_note(&self) -> Self {
        let Self {
            identifiers,
            match_source,
            bindings,
            note: _,
        } = self;
        Self {
            identifiers: *identifiers,
            match_source: match_source.clone(),
            bindings: bindings.clone(),
            note: None,
        }
    }

    pub fn selector(&self) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            identifiers: self.identifiers,
            target_binding: None,
            match_source: self.match_source.clone(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq)]
#[serde(untagged)]
pub enum SourceMatchBinding {
    Local(String),
    Detailed(SourceMatchBindingDetail),
}

impl SourceMatchBinding {
    pub fn local(&self) -> &str {
        match self {
            Self::Local(local) => local,
            Self::Detailed(detail) => &detail.local,
        }
    }

    pub fn name(&self) -> &str {
        match self {
            Self::Local(local) => local,
            Self::Detailed(detail) => detail.name.as_deref().unwrap_or(&detail.local),
        }
    }
}

impl AnonymousStatement {
    pub fn selector(
        &self,
    ) -> std::result::Result<AnonymousStatementSelector, AnonymousStatementSelectorError> {
        match (&self.match_source, &self.source_match) {
            (Some(match_source), None) => Ok(AnonymousStatementSelector::exact(match_source)),
            (None, Some(source_match)) if source_match.target_binding.is_some() => {
                Err(AnonymousStatementSelectorError::TargetBinding)
            }
            (None, Some(source_match)) => Ok(source_match.selector()),
            (Some(_), Some(_)) => Err(AnonymousStatementSelectorError::MultipleSelectors),
            (None, None) => Err(AnonymousStatementSelectorError::MissingSelector),
        }
    }
}

#[derive(
    Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize, thiserror::Error,
)]
#[serde(rename_all = "snake_case")]
pub enum AnonymousStatementSelectorError {
    #[error("anonymous_statements source_match cannot include `target_binding`")]
    TargetBinding,
    #[error("anonymous_statements entry must use either `match` or `source_match`, not both")]
    MultipleSelectors,
    #[error("anonymous_statements entry must include either `match` or `source_match.match`")]
    MissingSelector,
}

#[derive(Debug, Clone, Serialize, Eq, PartialEq, Ord, PartialOrd)]
pub struct SourceMatch {
    #[serde(default, skip_serializing)]
    pub identifiers: SourceMatchIdentifierMode,
    /// Internal selector-local binding name to project after parsing a
    /// canonical `source_matches[].bindings` claim. Public YAML selector bodies
    /// do not accept this field; claim projection lives outside the selector.
    #[serde(default, skip_serializing)]
    pub target_binding: Option<String>,
    #[serde(rename = "match")]
    pub match_source: String,
}

#[derive(Deserialize)]
struct SourceMatchWire {
    #[serde(rename = "match")]
    match_source: String,
    #[serde(flatten)]
    unsupported_fields: BTreeMap<String, serde::de::IgnoredAny>,
}

impl<'de> Deserialize<'de> for SourceMatch {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let wire = SourceMatchWire::deserialize(deserializer)?;
        if !wire.unsupported_fields.is_empty() {
            let fields = wire
                .unsupported_fields
                .keys()
                .map(|field| format!("`{field}`"))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(serde::de::Error::custom(format!(
                "unsupported selector capability: source_match field(s) {fields} are not \
                 supported by this debundler; upgrade the debundler or remove the field(s)"
            )));
        }
        Ok(Self {
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
            match_source: wire.match_source,
        })
    }
}

impl SourceMatch {
    pub fn selector(&self) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: self.match_source.clone(),
            identifiers: self.identifiers,
            target_binding: self.target_binding.clone(),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
pub struct AnonymousStatementSelector {
    #[serde(rename = "match")]
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
}

impl AnonymousStatementSelector {
    pub fn exact(match_source: impl Into<String>) -> Self {
        Self {
            match_source: match_source.into(),
            identifiers: SourceMatchIdentifierMode::Exact,
            target_binding: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_match_unknown_field_is_rejected_naming_the_field() {
        let error: serde_json::Error = serde_json::from_str::<SourceMatch>(
            r#"{
              "match": "const readable = 1;",
              "object_props": true
            }"#,
        )
        .unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("object_props"),
            "unexpected error: {message}"
        );
    }

    #[test]
    fn source_match_serializes_only_public_match_field() {
        let source_match = SourceMatch {
            identifiers: SourceMatchIdentifierMode::Exact,
            target_binding: Some("readable".to_string()),
            match_source: "const readable = runtime;".to_string(),
        };
        let serialized = serde_json::to_value(source_match).unwrap();
        assert_eq!(
            serialized,
            serde_json::json!({ "match": "const readable = runtime;" })
        );
    }
}
