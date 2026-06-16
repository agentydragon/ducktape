use super::*;

pub(crate) fn anonymous_selector_target_statement_indices(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
) -> Result<Vec<usize>> {
    if selector.target_statement.is_some() && selector.target_statements.is_some() {
        bail!(
            "logical_module {request_id}: anonymous_statements[].source_match cannot include \
             both `target_statement` and `target_statements`:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    if let Some(target_statement) = selector.target_statement {
        if parsed_items.is_empty() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match with \
                 target_statement parsed to zero statements:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        return validate_anonymous_target_statement_indices(
            request_id,
            selector,
            parsed_items,
            vec![target_statement],
            "target_statement",
        );
    }
    if let Some(target_statements) = &selector.target_statements {
        if parsed_items.is_empty() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match with \
                 target_statements parsed to zero statements:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        let indices = match target_statements {
            TargetStatements::Indices(indices) => indices.clone(),
            TargetStatements::All(TargetStatementsAll::All) => parsed_items
                .iter()
                .enumerate()
                .filter_map(|(idx, item)| module_item_list_hole_name(item).is_none().then_some(idx))
                .collect(),
        };
        return validate_anonymous_target_statement_indices(
            request_id,
            selector,
            parsed_items,
            indices,
            "target_statements",
        );
    }

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
             selector source must contain exactly one top-level statement unless \
             `target_statement` or `target_statements` is set:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    }
}

pub(crate) fn validate_anonymous_target_statement_indices(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
    indices: Vec<usize>,
    field_name: &str,
) -> Result<Vec<usize>> {
    if indices.is_empty() {
        bail!(
            "logical_module {request_id}: anonymous_statements[].source_match \
             `{field_name}` selected no top-level statements:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    let mut seen = BTreeSet::new();
    for idx in &indices {
        if !seen.insert(*idx) {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` contains duplicate index {idx}:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        if *idx >= parsed_items.len() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` index {idx} is out of range for {} parsed top-level \
                 statements:\n{match_source}",
                parsed_items.len(),
                match_source = selector.match_source,
            );
        }
        if module_item_list_hole_name(&parsed_items[*idx]).is_some() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` index {idx} points at a STMT_LIST hole, not a pinned \
                 selector statement:\n{match_source}",
                match_source = selector.match_source,
            );
        }
    }
    Ok(indices)
}
