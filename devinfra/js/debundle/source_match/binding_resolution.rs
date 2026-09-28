use super::*;

pub fn source_match_declared_binding_names(
    request_id: &str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    declared_binding_names_for_source_match(request_id, "source_match", source_match)
}

fn declared_binding_names_for_source_match(
    request_id: &str,
    selector_label: &'static str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    Ok(parsed_source_match(request_id, selector_label, source_match)?.declared_binding_names())
}

fn parsed_source_match(
    request_id: &str,
    selector_label: &'static str,
    source_match: &SourceMatch,
) -> Result<ParsedSourceMatchSelector> {
    ParsedSourceMatchSelector::parse(
        request_id,
        selector_label,
        format!("<source_match in {request_id}>"),
        &source_match.selector(),
        selector_label,
    )
}

pub fn source_match_claim_member_selectors(
    request_id: &str,
    claim: &SourceMatchClaim,
) -> Result<Vec<BindingGroupMemberSelector>> {
    if claim.bindings.is_empty() {
        bail!("logical_module {request_id}: source_matches[] must include non-empty `bindings`");
    }

    let source_match = claim.source_match();
    let parsed = parsed_source_match(request_id, "source_matches[]", &source_match)?;
    let declared = parsed.declared_binding_names();
    let mut declared_set = BTreeSet::new();
    let mut duplicate_declared = BTreeSet::new();
    for name in declared {
        if !declared_set.insert(name.clone()) {
            duplicate_declared.insert(name);
        }
    }
    if !duplicate_declared.is_empty() {
        bail!(
            "logical_module {request_id}: source_matches[] declares duplicate \
             selector-local binding names: {}",
            duplicate_declared
                .into_iter()
                .collect::<Vec<_>>()
                .join(", ")
        );
    }

    let free = super::chunk_resolver::template_free_identifiers(&parsed);
    let mut seen_locals = BTreeSet::new();
    let mut seen_names = BTreeSet::new();
    let mut out = Vec::new();
    for binding in &claim.bindings {
        let local = binding.local();
        if !seen_locals.insert(local.to_string()) {
            bail!("logical_module {request_id}: source_matches[].bindings repeats `{local}`");
        }
        let name = binding.name();
        if !seen_names.insert(name.to_string()) {
            bail!(
                "logical_module {request_id}: source_matches[].bindings repeats readable name \
                 `{name}`"
            );
        }
        // A free identifier pins the declaration it binds to: pinning by use
        // site.
        if !declared_set.contains(local) && !free.contains(local) {
            bail!(
                "logical_module {request_id}: source_matches[].bindings entry `{local}` is \
                 neither declared nor used by source_matches[].match"
            );
        }
        let parsed_selector = parsed.with_target_binding(Some(local.to_string()));
        let selector = parsed_selector.selector().clone();
        out.push(BindingGroupMemberSelector {
            export_name: name.to_string(),
            selector,
            parsed_selector,
            comment: None,
            note: None,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_match_claim_rejects_duplicate_readable_names() {
        let claim = SourceMatchClaim {
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            match_source: "const left = 1, right = 2;".to_string(),
            bindings: vec![
                spec::SourceMatchBinding::Detailed(spec::SourceMatchBindingDetail {
                    local: "left".to_string(),
                    name: Some("Duplicate".to_string()),
                }),
                spec::SourceMatchBinding::Detailed(spec::SourceMatchBindingDetail {
                    local: "right".to_string(),
                    name: Some("Duplicate".to_string()),
                }),
            ],
            note: None,
        };
        let result =
            js_ast::with_swc_globals(|| source_match_claim_member_selectors("test/module", &claim));
        let Err(error) = result else {
            panic!("duplicate readable names should be rejected");
        };
        let message = format!("{error:#}");
        assert!(
            message.contains("repeats readable name `Duplicate`"),
            "unexpected error: {message}"
        );
    }
}
