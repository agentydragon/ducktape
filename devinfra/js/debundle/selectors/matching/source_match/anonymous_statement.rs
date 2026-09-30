use super::*;

pub(crate) fn anonymous_selector_statement_indices(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
) -> Result<Vec<usize>> {
    match parsed_items {
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to zero \
             statements; selector source must contain exactly one top-level statement:\n{match_source}",
            match_source = selector.match_source,
        ),
        [single] if module_item_list_hole_name(single).is_some() => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to a STMT_LIST \
             hole; selector source must contain a pinned top-level statement to claim:\n{match_source}",
            match_source = selector.match_source,
        ),
        [_] => Ok(vec![0]),
        _ => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to {} statements; \
             selector source must contain exactly one top-level statement:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    }
}
