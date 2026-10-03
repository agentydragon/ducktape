//! Read a single authored selector without compiling or resolving its spec.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::Serialize;
use source_match::ExplainRangeKind;
use spec::{
    AnonymousStatement, AnonymousStatementSelector, SourceMatchClaim, SourceMatchIdentifierMode,
};

#[derive(Debug, Serialize)]
pub(super) struct SelectedSelector {
    pub origin: String,
    #[serde(rename = "match")]
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
    pub kind: ExplainRangeKind,
}

impl SelectedSelector {
    pub fn selector(&self) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: self.match_source.clone(),
            identifiers: self.identifiers,
            target_binding: self.target_binding.clone(),
        }
    }
}

pub(super) fn inline_selector(
    match_source: String,
    target_binding: Option<String>,
    anonymous: bool,
) -> SelectedSelector {
    SelectedSelector {
        origin: "--match".to_string(),
        match_source,
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding,
        kind: if anonymous {
            ExplainRangeKind::Anonymous
        } else {
            ExplainRangeKind::Member
        },
    }
}

/// `file.yaml#/source_matches/0`, or `--spec file.yaml --selector '#/…'`.
/// JSON Pointer escaping allows chunk/module paths in a flat spec's address.
pub(super) fn load_selectors(
    address: &str,
    spec_file: Option<&Path>,
    target_binding: Option<&str>,
) -> Result<Vec<SelectedSelector>> {
    let (file, pointer) = if let Some(spec_file) = spec_file {
        ensure!(
            address.starts_with('#'),
            "with --spec, --selector must start with #/ and address an entry in that file"
        );
        (spec_file.to_path_buf(), &address[1..])
    } else {
        let (file, pointer) = address.split_once('#').context("--selector requires FILE.yaml#/source_matches/INDEX or FILE.yaml#/anonymous_statements/INDEX")?;
        ensure!(
            !file.is_empty(),
            "--selector requires a file before #, or --spec"
        );
        (PathBuf::from(file), pointer)
    };
    let tokens = pointer.split('/').collect::<Vec<_>>();
    ensure!(
        tokens.len() >= 3 && tokens[0].is_empty(),
        "selector address must be a JSON Pointer ending in /source_matches/INDEX or /anonymous_statements/INDEX"
    );
    let collection = tokens[tokens.len() - 2];
    let index = tokens[tokens.len() - 1];
    ensure!(
        index.parse::<usize>().is_ok(),
        "selector entry index must be a non-negative integer"
    );
    let text =
        std::fs::read_to_string(&file).with_context(|| format!("reading {}", file.display()))?;
    // The document may be a module file or a flat transform spec. Only the
    // selected entry is interpreted as a typed spec object.
    let document: serde_json::Value =
        serde_yaml::from_str(&text).with_context(|| format!("parsing {}", file.display()))?;
    let value = document.pointer(pointer).with_context(|| {
        format!(
            "selector address {}#{pointer} does not exist",
            file.display()
        )
    })?;
    let origin = format!("{}#{pointer}", file.display());
    match collection {
        "source_matches" => {
            let claim: SourceMatchClaim = serde_json::from_value(value.clone())
                .with_context(|| format!("reading source-match entry {origin}"))?;
            let members = source_match::source_match_claim_member_selectors(&origin, &claim)?;
            if let Some(target) = target_binding {
                ensure!(
                    claim
                        .bindings
                        .iter()
                        .any(|binding| binding.local() == target),
                    "{origin} does not claim selector-local binding `{target}`"
                );
            }
            Ok(members
                .iter()
                .map(|member| member.parsed_selector.selector())
                .filter(|selector| {
                    target_binding
                        .is_none_or(|target| selector.target_binding.as_deref() == Some(target))
                })
                .map(|selector| SelectedSelector {
                    origin: origin.clone(),
                    match_source: selector.match_source.clone(),
                    identifiers: selector.identifiers,
                    target_binding: selector.target_binding.clone(),
                    kind: ExplainRangeKind::BindingGroup,
                })
                .collect())
        }
        "anonymous_statements" => {
            ensure!(
                target_binding.is_none(),
                "--target-binding cannot override an anonymous spec entry"
            );
            let statement: AnonymousStatement = serde_json::from_value(value.clone())
                .with_context(|| format!("reading anonymous-statement entry {origin}"))?;
            let selector = statement.selector()?;
            Ok(vec![SelectedSelector {
                origin,
                match_source: selector.match_source,
                identifiers: selector.identifiers,
                target_binding: selector.target_binding,
                kind: ExplainRangeKind::Anonymous,
            }])
        }
        _ => bail!("selector address must name a source_matches or anonymous_statements entry"),
    }
}
