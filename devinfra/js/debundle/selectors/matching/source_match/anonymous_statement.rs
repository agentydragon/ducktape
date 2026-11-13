use super::*;

pub(crate) fn anonymous_selector_target_index(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
) -> MatchResult<usize> {
    let reject = |problem| SourceMatchError::anonymous(request_id, selector, problem);
    match parsed_items {
        [] => Err(reject(AnonymousProblem::Empty)),
        [single] if module_item_list_hole_name(single).is_some() => {
            Err(reject(AnonymousProblem::Unpinned))
        }
        [_] => Ok(0),
        _ if selector.identifiers == SourceMatchIdentifierMode::Exact => {
            Err(reject(AnonymousProblem::ExactWindow {
                count: parsed_items.len(),
            }))
        }
        _ => {
            if parsed_items
                .iter()
                .any(|item| module_item_list_hole_name(item).is_some())
            {
                return Err(reject(AnonymousProblem::ContextHole));
            }
            let target = parsed_items.len() - 1;
            if !declared_bindings(&parsed_items[target]).is_empty() {
                return Err(reject(AnonymousProblem::DeclaresBinding));
            }
            Ok(target)
        }
    }
}
