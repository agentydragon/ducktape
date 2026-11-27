use super::*;
use std::fs;
use tempfile::TempDir;

fn compute_selector_debt(modules_root: &Path) -> Result<SelectorDebtReport> {
    compute_selector_debt_with_source(modules_root, None)
}

fn write(root: &Path, rel: &str, body: &str) {
    let p = root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, body).unwrap();
}

fn report_with_chunk(root: &Path, chunk_source: &str) -> SelectorDebtReport {
    let source_file = root.join("chunk.js");
    fs::write(&source_file, chunk_source).unwrap();
    let config = SourceAwareSelectorDebtConfig {
        source_file: &source_file,
        near_match_min_score: 55,
        near_match_limit: 3,
    };
    compute_selector_debt_with_source(root, Some(&config)).unwrap()
}

#[test]
fn minified_score_ranks_short_and_vowelless_above_words() {
    // Single/double-char tokens are maximal; readable words are zero.
    assert_eq!(minified_score("a"), 100);
    assert_eq!(minified_score("$"), 100);
    assert_eq!(minified_score("aQ"), 90);
    assert!(minified_score("XOe") >= FRAGILE_THRESHOLD);
    // A vowel-less 3-char beats a voweled 3-char.
    assert!(minified_score("Rng") > minified_score("XOe"));
    // Real words score zero regardless of length / single vowel.
    assert_eq!(minified_score("registry"), 0);
    assert_eq!(minified_score("parseConfig"), 0);
    assert_eq!(minified_score("Test"), 0);
    assert_eq!(minified_score("List"), 0);
    // Longer consonant soup is still flagged.
    assert!(minified_score("wXyz") >= FRAGILE_THRESHOLD);
}

#[test]
fn name_only_selectors_ranked_most_fragile_first() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "ui/widgets.yaml",
        "members:\n  - name: WidgetRegistry\n    selector: { binding: { name: registry } }\n  - selector: { binding: { name: a } }\n",
    );
    let report = compute_selector_debt(root).unwrap();
    assert_eq!(report.summary.name_only_total, 2);
    assert_eq!(report.summary.name_only_fragile, 1);
    // `a` (score 100) ranks before `registry` (score 0).
    assert_eq!(report.name_only[0].binding_name, "a");
    assert_eq!(report.name_only[0].minified_score, 100);
    assert_eq!(report.name_only[1].binding_name, "registry");
    assert_eq!(
        report.name_only[1].export_name.as_deref(),
        Some("WidgetRegistry")
    );
}

#[test]
fn name_only_module_groups_use_filtered_rows_and_prefix_depth() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "app/panel/core.yaml",
        "members:\n  - name: PanelEntry\n    selector: { binding: { name: a } }\n  - name: ReadablePanelEntry\n    selector: { binding: { name: readableName } }\n",
    );
    write(
        root,
        "app/panel/menu.yaml",
        "members:\n  - name: PanelMenu\n    selector: { binding: { name: bQ } }\n",
    );
    write(
        root,
        "app/search/index.yaml",
        "members:\n  - name: SearchEntry\n    selector: { binding: { name: sRx } }\n",
    );
    write(
        root,
        "runtime/init.yaml",
        "members:\n  - name: Startup\n    selector: { binding: { name: c } }\n",
    );

    let mut report = compute_selector_debt(root).unwrap();
    report
        .name_only
        .retain(|entry| entry.minified_score >= FRAGILE_THRESHOLD);
    populate_name_only_module_groups(&mut report, 2);
    // Detail limits should not change the already-derived group counts.
    report.name_only.truncate(1);

    assert_eq!(report.name_only_module_groups.len(), 3);
    assert_eq!(report.name_only_module_groups[0].module_prefix, "app/panel");
    assert_eq!(report.name_only_module_groups[0].module_prefix_depth, 2);
    assert_eq!(report.name_only_module_groups[0].name_only_count, 2);
    assert_eq!(report.name_only_module_groups[0].fragile_count, 2);
    assert_eq!(report.name_only_module_groups[0].module_count, 2);
    assert_eq!(report.name_only_module_groups[0].max_minified_score, 100);
    assert_eq!(
        report.name_only_module_groups[1].module_prefix,
        "runtime/init"
    );
    assert_eq!(
        report.name_only_module_groups[2].module_prefix,
        "app/search"
    );
}

#[test]
fn default_json_omits_name_only_module_groups() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "app/panel/core.yaml",
        "members:\n  - name: PanelEntry\n    selector: { binding: { name: a } }\n",
    );

    let json = serde_json::to_string(&compute_selector_debt(root).unwrap()).unwrap();

    assert!(!json.contains("name_only_module_groups"));
}

#[test]
fn repeated_source_match_bodies_grouped_across_sites() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    // Same decorator-helper body copied into a member source_match
    // and an anonymous statement, modulo whitespace.
    write(
        root,
        "a.yaml",
        "source_matches:\n  - match: 'var x = decorate(y);'\n    bindings: [x]\n",
    );
    write(
        root,
        "b.yaml",
        "anonymous_statements:\n  - match: |\n      var   x =   decorate(y);\n",
    );
    // A body that appears only once must not be reported.
    write(
        root,
        "c.yaml",
        "anonymous_statements:\n  - match: 'register(z);'\n",
    );
    let report = compute_selector_debt(root).unwrap();
    assert_eq!(report.summary.source_match_total, 3);
    assert_eq!(report.repeated_source_match.len(), 1);
    let group = &report.repeated_source_match[0];
    assert_eq!(group.normalized, "var x = decorate(y);");
    assert_eq!(group.occurrences.len(), 2);
    assert_eq!(group.occurrences[0].site, SelectorSite::SourceMatch);
    assert_eq!(group.occurrences[1].site, SelectorSite::AnonymousStatement);
}

#[test]
fn source_aware_mode_reports_unique_selector_with_near_matching_sibling() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "classes.yaml",
        "source_matches:\n  - match: |\n      class PickedClass {\n        important() {\n          return 1;\n        }\n      }\n    bindings: [PickedClass]\n",
    );
    let report = report_with_chunk(
        root,
        "class a { important() { return 1; } }\nclass b { important() { return 2; } }\n",
    );

    assert_eq!(report.summary.source_aware_checked, 1);
    assert_eq!(report.summary.source_aware_unique, 1);
    assert_eq!(report.summary.source_aware_near_ambiguous, 1);
    let row = &report.source_aware_near_ambiguous[0];
    assert_eq!(row.module, "classes");
    assert_eq!(row.site, SelectorSite::SourceMatch);
    assert_eq!(row.export_name.as_deref(), None);
    assert_eq!(row.exact_body_indices, vec![0]);
    assert_eq!(row.near_misses.len(), 1);
    assert_eq!(row.near_misses[0].body_idx, 1);
    assert_eq!(row.near_misses[0].declared_bindings, vec!["b".to_owned()]);
}

#[test]
fn source_aware_mode_does_not_report_ambiguous_exact_matches_as_unique_near_misses() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "classes.yaml",
        "source_matches:\n  - match: |\n      class PickedClass {\n        important() {\n          return 1;\n        }\n      }\n    bindings: [PickedClass]\n",
    );
    let report = report_with_chunk(
        root,
        "class a { important() { return 1; } }\nclass b { important() { return 1; } }\n",
    );

    assert_eq!(report.summary.source_aware_checked, 1);
    assert_eq!(report.summary.source_aware_unique, 0);
    assert_eq!(report.summary.source_aware_near_ambiguous, 0);
    assert!(report.source_aware_near_ambiguous.is_empty());
}

#[test]
fn source_aware_mode_reports_repeated_exact_body_index_matches() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let selector = "source_matches:\n  - match: |\n      function selectedFormatter(value) {\n        return value.trim().toUpperCase();\n      }\n    bindings:\n      - local: selectedFormatter\n        name: SelectedFormatter\n";
    write(root, "format/primary.yaml", selector);
    write(root, "format/duplicate.yaml", selector);
    let report = report_with_chunk(
        root,
        "function runtimeFormatter(value) { return value.trim().toUpperCase(); }\n\
         function otherFormatter(value) { return value.trim().toLowerCase(); }\n",
    );

    assert_eq!(report.summary.source_match_total, 2);
    assert_eq!(report.summary.repeated_source_match_groups, 1);
    assert_eq!(report.summary.source_aware_checked, 2);
    assert_eq!(report.summary.source_aware_unique, 2);
    assert_eq!(report.summary.source_aware_repeated_exact, 1);
    let group = &report.source_aware_repeated_exact[0];
    assert_eq!(group.exact_body_indices, vec![0]);
    assert_eq!(group.occurrences.len(), 2);
    assert_eq!(group.occurrences[0].module, "format/duplicate");
    assert_eq!(group.occurrences[1].module, "format/primary");
}

#[test]
fn source_aware_mode_suggests_grouped_source_match_for_repeated_multideclarator_claims() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "styles.yaml",
        r#"source_matches:
  - match: |
      const primaryStyle = "WidgetShell-42",
        ignoredValue = EXPR_IGNORED,
        secondaryReader = computeReader(EXPR_INPUT);
    bindings:
      - local: primaryStyle
        name: PrimaryStyle
  - match: |
      const primaryStyle = "WidgetShell-42",
        ignoredValue = EXPR_IGNORED,
        secondaryReader = computeReader(EXPR_INPUT);
    bindings:
      - local: secondaryReader
        name: SecondaryReader
"#,
    );
    let report = report_with_chunk(
        root,
        r#"const a = "WidgetShell-42", b = makeIgnored(), c = computeReader(1);
const stableName = 1;
"#,
    );

    assert_eq!(report.summary.source_match_total, 2);
    assert_eq!(report.summary.source_aware_checked, 2);
    assert_eq!(report.summary.source_aware_unique, 2);
    assert_eq!(report.summary.source_aware_binding_group_suggestions, 1);
    assert_eq!(
        report
            .summary
            .source_aware_binding_group_suggested_selectors,
        2,
    );
    let group = &report.source_aware_binding_group_suggestions[0];
    assert_eq!(group.module, "styles");
    assert_eq!(group.body_idx, 0);
    assert_eq!(group.declaration_kind, "const");
    assert_eq!(group.selectors.len(), 2);
    assert_eq!(group.selectors[0].target_binding, "primaryStyle");
    assert_eq!(
        group.selectors[0].export_name.as_deref(),
        Some("PrimaryStyle")
    );
    assert_eq!(group.selectors[0].declarator_idx, 0);
    assert_eq!(group.selectors[1].target_binding, "secondaryReader");
    assert_eq!(
        group.selectors[1].export_name.as_deref(),
        Some("SecondaryReader")
    );
    assert_eq!(group.selectors[1].declarator_idx, 2);
    assert!(group.candidate_yaml.contains("source_matches:"));
    assert!(!group.candidate_yaml.contains("identifiers:"));
    assert!(
        group
            .candidate_yaml
            .contains("const DECLARATORS_BEFORE = null,")
    );
    assert!(
        group
            .candidate_yaml
            .contains("DECLARATORS_BETWEEN_1 = null,")
    );
    assert!(group.candidate_yaml.contains("DECLARATORS_AFTER = null;"));
    assert!(group.candidate_yaml.contains("- local: primaryStyle"));
    assert!(group.candidate_yaml.contains("name: PrimaryStyle"));
    assert!(group.candidate_yaml.contains("- local: secondaryReader"));
    assert!(
        group
            .candidate_yaml
            .contains("STR_LITERAL_MATCHING_RE(\"^WidgetShell-42$\")")
    );
    assert!(
        group
            .candidate_yaml
            .contains("secondaryReader = EXPR_secondaryReader")
    );

    let mut text = String::new();
    render_selector_debt_text(&report, &mut text);
    for identifier in [
        "styles",
        "primaryStyle",
        "PrimaryStyle",
        "DECLARATORS_BEFORE",
    ] {
        assert!(text.contains(identifier), "{identifier}\n{text}");
    }
}

#[test]
fn source_aware_mode_keeps_grouped_source_match_suggestions_same_module() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let first_selector = r#"source_matches:
  - match: |
      const primaryStyle = "WidgetShell-42",
        secondaryReader = computeReader(EXPR_INPUT);
    bindings:
      - local: primaryStyle
        name: PrimaryStyle
"#;
    let second_selector = r#"source_matches:
  - match: |
      const primaryStyle = "WidgetShell-42",
        secondaryReader = computeReader(EXPR_INPUT);
    bindings:
      - local: secondaryReader
        name: SecondaryReader
"#;
    write(root, "styles/primary.yaml", first_selector);
    write(root, "readers/secondary.yaml", second_selector);
    let report = report_with_chunk(
        root,
        r#"const a = "WidgetShell-42", c = computeReader(1);
"#,
    );

    assert_eq!(report.summary.source_aware_binding_group_suggestions, 0);
    assert!(report.source_aware_binding_group_suggestions.is_empty());
}

#[test]
fn source_aware_mode_skips_single_multideclarator_source_match_suggestions() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    write(
        root,
        "values.yaml",
        r#"source_matches:
  - match: |
      let skippedValue = EXPR_SKIPPED,
        selectedValue = computeValue();
    bindings:
      - local: selectedValue
        name: SelectedValue
"#,
    );
    let report = report_with_chunk(root, "let p = ignored(), q = computeValue();\n");

    assert_eq!(report.summary.source_aware_binding_group_suggestions, 0,);
    assert!(report.source_aware_binding_group_suggestions.is_empty());
}
