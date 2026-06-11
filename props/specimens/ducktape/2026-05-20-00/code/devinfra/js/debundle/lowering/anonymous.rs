//! Resolve `anonymous_statements[]` selectors on a spec
//! `LogicalRequest` to pre-split body ordinals in the runtime
//! module. Match equality ignores both `Span` and `SyntaxContext`
//! because needle and runtime are parsed in different `resolver`
//! passes; SWC's `SyntaxContext::within_ignored_ctxt` makes
//! `eq_ignore_span` compare the source-level identifier shape.

use super::*;

/// Resolve every `anonymous_match_sources` entry on `request` to a
/// pre-split body index in `runtime_module`'s top-level body. The
/// resolver requires exactly one match per entry — a 0-match or
/// ambiguous-match selector is a spec error.
pub(super) fn resolve_anonymous_statement_ordinals(
    request: &LogicalRequest,
    runtime_module: &Module,
) -> Result<Vec<usize>> {
    let mut resolved = Vec::with_capacity(request.anonymous_match_sources.len());
    for match_source in &request.anonymous_match_sources {
        let parsed = js_ast::parse_js_module_ast(
            &format!("<anonymous_statement match in {}>", request.id),
            match_source,
        )
        .with_context(|| {
            format!(
                "logical_module {}: anonymous_statements[].match did not parse as JS:\n{match_source}",
                request.id
            )
        })?;
        let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
        let needle = match parsed_items.as_slice() {
            [single] => *single,
            [] => bail!(
                "logical_module {}: anonymous_statements[].match parsed to zero \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
            ),
            _ => bail!(
                "logical_module {}: anonymous_statements[].match parsed to {} \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
                parsed_items.len(),
            ),
        };
        // `eq_ignore_span` on `Ident` normally compares `(sym, ctxt)`.
        // `needle` and `runtime_module` were parsed in different resolver
        // passes, so compare inside SWC's ignored-context scope instead of
        // cloning and syntax-context-stripping every candidate item.
        //
        // TODO(perf): if this remains material after the context-clone fix,
        // index top-level runtime statements by a context-insensitive
        // structural fingerprint and run exact equality only within matching
        // buckets. The current scan is still O(matches * top-level items).
        let matches: Vec<usize> = SyntaxContext::within_ignored_ctxt(|| {
            runtime_module
                .body
                .iter()
                .enumerate()
                .filter_map(|(ordinal, item)| {
                    if needle.eq_ignore_span(item) {
                        Some(ordinal)
                    } else {
                        None
                    }
                })
                .collect()
        });
        match matches.as_slice() {
            [single] => resolved.push(*single),
            [] => bail!(
                "logical_module {}: anonymous_statements[].match did not match any \
                 top-level statement in the chunk. Selector:\n{match_source}",
                request.id,
            ),
            multiple => bail!(
                "logical_module {}: anonymous_statements[].match is ambiguous — \
                 matched {} top-level statements at ordinals {:?}. Refine the \
                 selector. Source:\n{match_source}",
                request.id,
                multiple.len(),
                multiple,
            ),
        }
    }
    Ok(resolved)
}
