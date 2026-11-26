use super::*;

/// Compact two-section-plus-summary text rendering.
pub fn render_selector_debt_text(report: &SelectorDebtReport, out: &mut String) {
    let s = &report.summary;
    out.push_str(&format!(
        "name-only selectors: {} ({} fragile)  source_match: {} ({} repeated group(s))  source-aware near-ambiguous: {}/{} unique checked  source-aware repeated exact: {}  source-aware source_matches suggestions: {} selector(s) in {} group(s)\n",
        s.name_only_total,
        s.name_only_fragile,
        s.source_match_total,
        s.repeated_source_match_groups,
        s.source_aware_near_ambiguous,
        s.source_aware_unique,
        s.source_aware_repeated_exact,
        s.source_aware_binding_group_suggested_selectors,
        s.source_aware_binding_group_suggestions,
    ));

    if !report.name_only_module_groups.is_empty() {
        out.push_str("name-only groups by module prefix:\n");
        for group in &report.name_only_module_groups {
            out.push_str(&format!(
                "  {:>4} selector(s)  {:>4} fragile  {:>4} module(s)  max={}  depth={}  {}\n",
                group.name_only_count,
                group.fragile_count,
                group.module_count,
                group.max_minified_score,
                group.module_prefix_depth,
                group.module_prefix,
            ));
        }
    }

    if !report.name_only.is_empty() {
        out.push_str("name-only (most fragile first):\n");
        for entry in &report.name_only {
            let readable = entry.export_name.as_deref().unwrap_or("-");
            out.push_str(&format!(
                "  {:>3}  {}  {}  [{}]\n",
                entry.minified_score, entry.binding_name, entry.module, readable,
            ));
        }
    }

    if !report.repeated_source_match.is_empty() {
        out.push_str("repeated source_match bodies:\n");
        for group in &report.repeated_source_match {
            out.push_str(&format!(
                "  {}x  {}\n",
                group.occurrences.len(),
                group.normalized,
            ));
            for occurrence in &group.occurrences {
                out.push_str(&format!(
                    "      {:?} {}\n",
                    occurrence.site, occurrence.module,
                ));
            }
        }
    }

    if !report.source_aware_near_ambiguous.is_empty() {
        out.push_str("source-aware near-ambiguous structural selectors:\n");
        for selector in &report.source_aware_near_ambiguous {
            let readable = selector.export_name.as_deref().unwrap_or("-");
            out.push_str(&format!(
                "  {:?} {} [{}] exact={:?}\n",
                selector.site, selector.module, readable, selector.exact_body_indices,
            ));
            for near in &selector.near_misses {
                let declared = if near.declared_bindings.is_empty() {
                    "-".to_owned()
                } else {
                    near.declared_bindings.join(",")
                };
                out.push_str(&format!(
                    "      score={} body={} declared=[{}] {}\n",
                    near.score,
                    near.body_idx,
                    declared,
                    near.reason.display(),
                ));
            }
        }
    }

    if !report.source_aware_repeated_exact.is_empty() {
        out.push_str("source-aware repeated exact structural selectors:\n");
        for group in &report.source_aware_repeated_exact {
            out.push_str(&format!(
                "  {}x exact={:?}  {}\n",
                group.occurrences.len(),
                group.exact_body_indices,
                group.normalized,
            ));
            for occurrence in &group.occurrences {
                let readable = occurrence.export_name.as_deref().unwrap_or("-");
                out.push_str(&format!(
                    "      {:?} {} [{}]\n",
                    occurrence.site, occurrence.module, readable,
                ));
            }
        }
    }

    if !report.source_aware_binding_group_suggestions.is_empty() {
        out.push_str("source-aware source_matches suggestions:\n");
        for group in &report.source_aware_binding_group_suggestions {
            out.push_str(&format!(
                "  {} body={} kind={} selectors={}\n",
                group.module,
                group.body_idx,
                group.declaration_kind,
                group.selectors.len(),
            ));
            for selector in &group.selectors {
                let readable = selector.export_name.as_deref().unwrap_or("-");
                out.push_str(&format!(
                    "      local_binding={} [{}] {} decl#{}\n",
                    selector.target_binding, readable, selector.module, selector.declarator_idx,
                ));
            }
            out.push_str("      candidate:\n");
            for line in group.candidate_yaml.lines() {
                out.push_str(&format!("        {line}\n"));
            }
        }
    }
}
