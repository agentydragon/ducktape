//! Resolve `anonymous_statements[]` selectors on a spec
//! `LogicalRequest` to pre-split body ordinals in the runtime
//! module. Match equality ignores both `Span` and `SyntaxContext`
//! because needle and runtime are parsed in different `resolver`
//! passes; SWC's `SyntaxContext::within_ignored_ctxt` makes
//! `eq_ignore_span` compare the source-level identifier shape.

use super::*;

#[derive(Debug, Clone)]
pub(super) struct ResolvedAnonymousStatement {
    pub(super) ordinal: usize,
    pub(super) comment: Option<String>,
}

/// Resolve every anonymous statement entry on `request` to a
/// pre-split body index in `runtime_module`'s top-level body. The
/// resolver requires exactly one match per entry — a 0-match or
/// ambiguous-match selector is a spec error.
pub(super) fn resolve_anonymous_statement_ordinals(
    request: &LogicalRequest,
    runtime_module: &Module,
) -> Result<Vec<ResolvedAnonymousStatement>> {
    let mut resolved = Vec::with_capacity(request.anonymous_statements.len());
    for statement in &request.anonymous_statements {
        let ordinal = source_match::resolve_anonymous_statement_body_index(
            runtime_module,
            &request.id,
            &statement.selector,
        )?;
        resolved.push(ResolvedAnonymousStatement {
            ordinal,
            comment: statement.comment.clone(),
        });
    }
    Ok(resolved)
}
