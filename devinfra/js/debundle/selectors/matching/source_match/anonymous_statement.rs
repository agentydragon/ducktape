use super::*;

pub(crate) fn anonymous_selector_target_index(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
) -> Result<usize> {
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
        [_] => Ok(0),
        _ if selector.identifiers == SourceMatchIdentifierMode::Exact => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to {} statements; \
             exact `match` must contain exactly one top-level statement:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
        _ => {
            if parsed_items
                .iter()
                .any(|item| module_item_list_hole_name(item).is_some())
            {
                bail!(
                    "logical_module {request_id}: anonymous_statements[].source_match \
                     context cannot contain a STMT_LIST hole; context must be a contiguous \
                     window ending in the statement to claim:\n{match_source}",
                    match_source = selector.match_source,
                );
            }
            let target = parsed_items.len() - 1;
            if !declared_bindings(&parsed_items[target]).is_empty() {
                bail!(
                    "logical_module {request_id}: anonymous_statements[].source_match final \
                     statement declares a binding; only an anonymous final statement can be \
                     claimed:\n{match_source}",
                    match_source = selector.match_source,
                );
            }
            Ok(target)
        }
    }
}
