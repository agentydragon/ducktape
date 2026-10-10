//! Read a single authored selector without compiling or resolving its spec.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::Serialize;
use source_match::ExplainRangeKind;
use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};
use spec_modules::{read_flat_transform_spec, read_module_file};

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
        tokens.first() == Some(&""),
        "selector address must be a JSON Pointer ending in /source_matches/INDEX or /anonymous_statements/INDEX"
    );
    let module = if spec_file.is_some() {
        ensure!(
            tokens.len() == 6 && tokens[1] == "logical_modules",
            "flat spec selector address must be /logical_modules/CHUNK/MODULE/source_matches/INDEX or /anonymous_statements/INDEX"
        );
        let chunk = decode_pointer_token(tokens[2])?;
        let module_path = decode_pointer_token(tokens[3])?;
        read_flat_transform_spec(&file)?
            .logical_modules
            .get(&chunk)
            .and_then(|modules| modules.get(&module_path))
            .cloned()
            .with_context(|| {
                format!(
                    "selector address {}#{pointer} does not exist",
                    file.display()
                )
            })?
    } else {
        ensure!(
            tokens.len() == 3,
            "module selector address must be /source_matches/INDEX or /anonymous_statements/INDEX"
        );
        read_module_file(&file)?
    };
    let collection = tokens[tokens.len() - 2];
    let index = tokens[tokens.len() - 1]
        .parse::<usize>()
        .context("selector entry index must be a non-negative integer")?;
    let origin = format!("{}#{pointer}", file.display());
    match collection {
        "source_matches" => {
            let claim = module
                .source_matches
                .get(index)
                .with_context(|| format!("selector address {origin} does not exist"))?;
            let members = source_match::source_match_claim_member_selectors(&origin, claim)?;
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
            let statement = module
                .anonymous_statements
                .get(index)
                .with_context(|| format!("selector address {origin} does not exist"))?;
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

fn decode_pointer_token(token: &str) -> Result<String> {
    let mut decoded = String::new();
    let mut chars = token.chars();
    while let Some(ch) = chars.next() {
        if ch == '~' {
            match chars.next() {
                Some('0') => decoded.push('~'),
                Some('1') => decoded.push('/'),
                _ => bail!("invalid JSON Pointer escape in `{token}`"),
            }
        } else {
            decoded.push(ch);
        }
    }
    Ok(decoded)
}
