//! Resolve `anonymous_statements[]` selectors on a spec
//! `LogicalRequest` to pre-split body ordinals in the runtime
//! module. Match equality ignores both `Span` and `SyntaxContext`
//! because needle and runtime are parsed in different `resolver`
//! passes; SWC's `SyntaxContext::within_ignored_ctxt` makes
//! `eq_ignore_span` compare the source-level identifier shape.

use super::*;
use crate::plans::AnonymousStatementRequest;

#[derive(Debug, Clone)]
pub(super) struct ResolvedAnonymousStatement {
    pub(super) ordinal: usize,
    pub(super) comment: Option<String>,
}

#[derive(Debug, Clone)]
pub(super) struct AnonymousStatementDiagnostic {
    pub(super) module_id: String,
    pub(super) selector: spec::AnonymousStatementSelector,
    pub(super) message: String,
}

impl AnonymousStatementDiagnostic {
    pub(super) fn render(&self) -> String {
        format!("module {}: {}", self.module_id, self.message)
    }
}

/// Categorical reduction of the seam's matched groups to one group's body
/// indices: an `anonymous_statements[]` entry must match exactly one top-level
/// group (mirrors the former `source_match::resolve_anonymous_statement_body_indices`,
/// now reached through the resolver seam so the matcher flip carries this path too).
fn one_anonymous_group(
    request_id: &str,
    selector: &spec::AnonymousStatementSelector,
    groups: Vec<Vec<usize>>,
) -> Result<Vec<usize>> {
    match groups.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match did not match any \
             top-level statement group in the chunk. Selector:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].match is ambiguous — \
             matched {} top-level statement groups at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple,
            match_source = selector.match_source,
        ),
    }
}

pub(super) fn resolve_anonymous_statement_request(
    request_id: &str,
    statement: &AnonymousStatementRequest,
    resolver: &dyn source_match::SelectorResolver,
    keep_going: bool,
    diagnostics: &mut Vec<AnonymousStatementDiagnostic>,
) -> Result<Vec<ResolvedAnonymousStatement>> {
    let ordinals = match resolver
        .resolve_anonymous_groups(request_id, &statement.selector)
        .and_then(|groups| one_anonymous_group(request_id, &statement.selector, groups))
    {
        Ok(ordinals) => ordinals,
        Err(error) if keep_going => {
            diagnostics.push(AnonymousStatementDiagnostic {
                module_id: request_id.to_string(),
                selector: statement.selector.clone(),
                message: format!("{error:#}"),
            });
            return Ok(Vec::new());
        }
        Err(error) => return Err(error),
    };
    Ok(ordinals
        .into_iter()
        .map(|ordinal| ResolvedAnonymousStatement {
            ordinal,
            comment: statement.comment.clone(),
        })
        .collect())
}
