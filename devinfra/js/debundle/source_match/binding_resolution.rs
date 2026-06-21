use super::*;

pub fn source_match_declared_binding_names(
    request_id: &str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "binding_groups[].source_match",
        format!("<binding group source_match in {request_id}>"),
        &source_match.match_source,
        "binding_groups[].source_match",
    )?;
    Ok(parsed
        .body
        .iter()
        .flat_map(declared_bindings)
        .map(|binding| binding.binding_name)
        .collect())
}

pub fn binding_group_member_selectors(
    request_id: &str,
    group: &BindingGroup,
) -> Result<Vec<BindingGroupMemberSelector>> {
    if group.source_match.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match must not include \
             `target_binding`; use the `exports` keys to choose selector-local bindings"
        );
    }
    let exports = effective_binding_group_exports(group, request_id)?;
    let unknown_comments = group
        .comments
        .keys()
        .filter(|name| !exports.contains_key(*name))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown_comments.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].comments names bindings that \
             are not exported by the group: {}",
            unknown_comments.join(", ")
        );
    }
    Ok(exports
        .into_iter()
        .map(|(target_binding, export_name)| {
            let mut selector = group.source_match.selector();
            selector.target_binding = Some(target_binding.clone());
            selector.target_statement = None;
            selector.target_statements = None;
            let comment = group.comments.get(&target_binding).cloned();
            BindingGroupMemberSelector {
                export_name,
                selector,
                comment,
            }
        })
        .collect())
}

pub fn binding_group_anonymous_statement_selector(
    group: &BindingGroup,
) -> Option<AnonymousStatementSelector> {
    if group.source_match.target_statement.is_none()
        && group.source_match.target_statements.is_none()
    {
        return None;
    }
    let mut selector = group.source_match.selector();
    selector.target_binding = None;
    Some(selector)
}

pub(crate) fn effective_binding_group_exports(
    group: &BindingGroup,
    request_id: &str,
) -> Result<BTreeMap<String, String>> {
    let mut exports = match &group.adopt_names {
        BindingGroupAdoptNames::None | BindingGroupAdoptNames::All(false) => BTreeMap::new(),
        BindingGroupAdoptNames::All(true) => {
            let names = declared_selector_binding_names(group, request_id)?;
            names
                .into_iter()
                .map(|name| (name.clone(), name))
                .collect::<BTreeMap<_, _>>()
        }
        BindingGroupAdoptNames::Names(names) => {
            let declared = declared_selector_binding_names(group, request_id)?;
            let declared_set = declared.into_iter().collect::<BTreeSet<_>>();
            let mut adopted = BTreeMap::new();
            for name in names {
                if !declared_set.contains(name) {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names entry \
                         `{name}` is not declared by source_match.match"
                    );
                }
                if adopted.insert(name.clone(), name.clone()).is_some() {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names repeats \
                         `{name}`"
                    );
                }
            }
            adopted
        }
    };
    exports.extend(group.exports.clone());
    if exports.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[] must include non-empty `exports` \
             or `adopt_names`"
        );
    }
    Ok(exports)
}

pub(crate) fn declared_selector_binding_names(
    group: &BindingGroup,
    request_id: &str,
) -> Result<Vec<String>> {
    let names = source_match_declared_binding_names(request_id, &group.source_match)?;
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for name in &names {
        if !seen.insert(name.clone()) {
            duplicates.insert(name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match declares duplicate \
             selector-local binding names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    if names.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].adopt_names found no declared \
             bindings in source_match.match"
        );
    }
    Ok(names)
}
