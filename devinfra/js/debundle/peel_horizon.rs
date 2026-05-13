use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{BindingReport, OwnerGraphReport};
use spec::BindingSourceKind;
use spec_modules::{
    collect_module_files, is_deferred_yaml, module_path_from_file, read_module_file,
};

#[derive(Debug, Clone)]
pub struct PeelHorizonOptions {
    pub owner_graph_path: PathBuf,
    pub modules_root: PathBuf,
    pub near_missing: usize,
    pub max_companions: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PeelHorizonReport {
    pub full: Vec<ModuleCoverage>,
    pub with_companions: Vec<ModuleCoverage>,
    pub near: Vec<ModuleCoverage>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ModuleCoverage {
    pub path: String,
    pub file: String,
    pub total: usize,
    pub covered: usize,
    pub missing: Vec<String>,
    pub covered_with_companions: usize,
    pub missing_with_companions: Vec<String>,
    pub companions: Vec<String>,
    pub companion_labels: Vec<String>,
    pub companion_details: Vec<CompanionDetail>,
    pub companion_candidates: Vec<CompanionCandidate>,
    pub owners: usize,
    pub closure_candidates: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CompanionCandidate {
    pub members: Vec<BindingReport>,
    pub add_members: Vec<BindingReport>,
    pub owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CompanionDetail {
    pub binding: String,
    pub member: BindingReport,
    pub homes: Vec<SymbolHome>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SymbolHome {
    pub binding: String,
    pub name: String,
    pub path: String,
    pub file: String,
    pub deferred: bool,
}

type DeferredModules = Vec<DeferredModule>;
type SymbolHomesByBinding = BTreeMap<String, Vec<SymbolHome>>;

#[derive(Debug, Clone)]
struct PeelCandidate {
    members: Vec<BindingReport>,
    owner_ids: Vec<String>,
    bindings: BTreeSet<String>,
    binding_order: Vec<String>,
}

#[derive(Debug, Clone)]
struct GraphIndex {
    candidates: Vec<PeelCandidate>,
    candidate_indices_by_binding: BTreeMap<String, Vec<usize>>,
    owner_by_binding: BTreeMap<String, String>,
    member_by_binding: BTreeMap<String, BindingReport>,
}

#[derive(Debug, Clone)]
struct DeferredModule {
    path: String,
    file: PathBuf,
    bindings: BTreeSet<String>,
    owners: BTreeSet<String>,
}

pub fn analyze_peel_horizon(options: &PeelHorizonOptions) -> Result<PeelHorizonReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    analyze_peel_horizon_from_graph(&graph, options)
}

pub fn analyze_peel_horizon_from_graph(
    graph: &OwnerGraphReport,
    options: &PeelHorizonOptions,
) -> Result<PeelHorizonReport> {
    let graph_index = graph_index(graph);
    let (modules, symbols) =
        load_modules_and_symbols(&options.modules_root, &graph_index.owner_by_binding)?;
    let mut rows: Vec<ModuleCoverage> = modules
        .iter()
        .filter(|module| !module.bindings.is_empty())
        .map(|module| coverage(module, &graph_index, options.max_companions, &symbols))
        .collect();

    let mut full: Vec<ModuleCoverage> = rows
        .iter()
        .filter(|row| row.missing.is_empty())
        .cloned()
        .collect();
    let mut with_companions: Vec<ModuleCoverage> = rows
        .iter()
        .filter(|row| {
            !row.missing.is_empty()
                && row.covered_with_companions == row.total
                && row.missing_with_companions.is_empty()
                && !row.companions.is_empty()
        })
        .cloned()
        .collect();
    let companion_paths: BTreeSet<String> =
        with_companions.iter().map(|row| row.path.clone()).collect();
    let mut near: Vec<ModuleCoverage> = rows
        .drain(..)
        .filter(|row| {
            !companion_paths.contains(&row.path)
                && !row.missing.is_empty()
                && row.missing.len() <= options.near_missing
        })
        .collect();

    sort_ranked_rows(&mut full);
    with_companions.sort_by_key(|row| (row.companions.len(), Reverse(row.total), row.path.clone()));
    sort_ranked_rows(&mut near);

    Ok(PeelHorizonReport {
        full,
        with_companions,
        near,
    })
}

pub fn render_peel_horizon_report(
    report: &PeelHorizonReport,
    limit: usize,
    max_companions: usize,
    near_missing: usize,
) -> String {
    let mut out = String::new();
    push_table(
        &mut out,
        "fully peelable deferred modules",
        &report.full,
        limit,
    );
    out.push('\n');
    push_companion_table(
        &mut out,
        &format!("peelable with <= {max_companions} companion bindings"),
        &report.with_companions,
        limit,
    );
    out.push('\n');
    push_table(
        &mut out,
        &format!("near misses (<= {near_missing} uncovered bindings)"),
        &report.near,
        limit,
    );
    out
}

fn graph_index(graph: &OwnerGraphReport) -> GraphIndex {
    let mut candidates = Vec::new();
    let mut candidate_indices_by_binding: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for candidate in graph
        .peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| !candidate.members.is_empty())
    {
        let bindings: BTreeSet<String> = candidate
            .members
            .iter()
            .map(|member| member.binding.clone())
            .collect();
        let binding_order: Vec<String> = bindings.iter().cloned().collect();
        let candidate_index = candidates.len();
        for binding in &binding_order {
            candidate_indices_by_binding
                .entry(binding.clone())
                .or_default()
                .push(candidate_index);
        }
        candidates.push(PeelCandidate {
            members: sorted_members(candidate.members.clone()),
            owner_ids: candidate.owner_ids.clone(),
            bindings,
            binding_order,
        });
    }

    let mut owner_by_binding = BTreeMap::new();
    let mut member_by_binding = BTreeMap::new();
    for node in &graph.nodes {
        for member in &node.declared_bindings {
            owner_by_binding.insert(member.binding.clone(), node.id.clone());
            member_by_binding.insert(member.binding.clone(), member.clone());
        }
    }

    GraphIndex {
        candidates,
        candidate_indices_by_binding,
        owner_by_binding,
        member_by_binding,
    }
}

fn load_modules_and_symbols(
    root: &Path,
    owner_by_binding: &BTreeMap<String, String>,
) -> Result<(DeferredModules, SymbolHomesByBinding)> {
    let mut deferred_modules = Vec::new();
    let mut symbols: BTreeMap<String, Vec<SymbolHome>> = BTreeMap::new();

    for path in collect_module_files(root)? {
        let (module, module_symbols) = parse_module_file(&path, root, owner_by_binding)?;
        if is_deferred_yaml(&path) {
            deferred_modules.push(module);
        }
        for (binding, home) in module_symbols {
            symbols.entry(binding).or_default().push(home);
        }
    }

    Ok((deferred_modules, symbols))
}

fn parse_module_file(
    path: &Path,
    root: &Path,
    owner_by_binding: &BTreeMap<String, String>,
) -> Result<(DeferredModule, BTreeMap<String, SymbolHome>)> {
    let is_deferred = is_deferred_yaml(path);
    let data = read_module_file(path)?;
    let module_path = module_path_from_file(path, root, is_deferred);

    let mut bindings = BTreeSet::new();
    let mut owners = BTreeSet::new();
    let mut symbol_homes = BTreeMap::new();

    for member in data.members {
        let binding = member.selector.binding;
        if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
            continue;
        }
        let export_name = member.name.unwrap_or_else(|| binding.name.clone());
        bindings.insert(binding.name.clone());
        if let Some(owner_id) = owner_by_binding.get(&binding.name) {
            owners.insert(owner_id.clone());
        }
        symbol_homes.insert(
            binding.name.clone(),
            SymbolHome {
                binding: binding.name,
                name: export_name,
                path: module_path.clone(),
                file: path.display().to_string(),
                deferred: is_deferred,
            },
        );
    }

    Ok((
        DeferredModule {
            path: module_path,
            file: path.to_path_buf(),
            bindings,
            owners,
        },
        symbol_homes,
    ))
}

fn coverage(
    module: &DeferredModule,
    graph_index: &GraphIndex,
    max_companions: usize,
    symbols: &BTreeMap<String, Vec<SymbolHome>>,
) -> ModuleCoverage {
    let mut covered = BTreeSet::new();
    let mut closure_candidates = 0;
    for candidate_index in candidate_indices_for_bindings(graph_index, &module.bindings) {
        let candidate = &graph_index.candidates[candidate_index];
        if !candidate.bindings.is_subset(&module.bindings) {
            continue;
        }
        if candidate.owner_ids.len() > 1 {
            closure_candidates += 1;
        }
        covered.extend(candidate.binding_order.iter().cloned());
    }
    let direct_missing: BTreeSet<String> = module.bindings.difference(&covered).cloned().collect();

    let mut companion_missing = direct_missing.clone();
    let mut companion_covered = covered.clone();
    let mut companions = BTreeSet::new();
    let mut companion_candidates = Vec::new();
    let mut ordered_candidates = candidate_indices_for_bindings(graph_index, &direct_missing);
    ordered_candidates.sort_by(|left, right| {
        let left = &graph_index.candidates[*left];
        let right = &graph_index.candidates[*right];
        companion_candidate_rank(left, &module.bindings)
            .cmp(&companion_candidate_rank(right, &module.bindings))
    });

    for candidate_index in ordered_candidates {
        let candidate = &graph_index.candidates[candidate_index];
        if candidate.bindings.is_subset(&module.bindings) {
            continue;
        }
        if candidate.bindings.is_disjoint(&companion_missing) {
            continue;
        }
        let extra: BTreeSet<String> = candidate
            .bindings
            .difference(&module.bindings)
            .cloned()
            .collect();
        if extra.len() > max_companions {
            continue;
        }
        companion_candidates.push(CompanionCandidate {
            members: candidate.members.clone(),
            add_members: extra
                .iter()
                .map(|binding| report_member(binding, &graph_index.member_by_binding))
                .collect(),
            owner_ids: candidate.owner_ids.clone(),
        });
        companion_covered.extend(candidate.bindings.intersection(&module.bindings).cloned());
        companions.extend(extra);
        companion_missing = module
            .bindings
            .difference(&companion_covered)
            .cloned()
            .collect();
        if companion_missing.is_empty() {
            break;
        }
    }

    ModuleCoverage {
        path: module.path.clone(),
        file: module.file.display().to_string(),
        total: module.bindings.len(),
        covered: covered.intersection(&module.bindings).count(),
        missing: direct_missing.into_iter().collect(),
        covered_with_companions: companion_covered.intersection(&module.bindings).count(),
        missing_with_companions: companion_missing.into_iter().collect(),
        companions: companions.iter().cloned().collect(),
        companion_labels: companions
            .iter()
            .map(|binding| format_companion(binding, symbols, &graph_index.member_by_binding))
            .collect(),
        companion_details: companions
            .iter()
            .map(|binding| companion_detail(binding, symbols, &graph_index.member_by_binding))
            .collect(),
        companion_candidates,
        owners: module.owners.len(),
        closure_candidates,
    }
}

fn candidate_indices_for_bindings(
    graph_index: &GraphIndex,
    bindings: &BTreeSet<String>,
) -> Vec<usize> {
    let mut candidate_indices = BTreeSet::new();
    for binding in bindings {
        if let Some(indices) = graph_index.candidate_indices_by_binding.get(binding) {
            candidate_indices.extend(indices.iter().copied());
        }
    }
    candidate_indices.into_iter().collect()
}

fn companion_candidate_rank<'a>(
    candidate: &'a PeelCandidate,
    module_bindings: &BTreeSet<String>,
) -> (usize, usize, &'a [String]) {
    (
        candidate.bindings.difference(module_bindings).count(),
        candidate.bindings.len(),
        &candidate.binding_order,
    )
}

fn report_member(
    binding: &str,
    member_by_binding: &BTreeMap<String, BindingReport>,
) -> BindingReport {
    member_by_binding
        .get(binding)
        .cloned()
        .unwrap_or_else(|| BindingReport {
            binding: binding.to_string(),
            export_name: binding.to_string(),
        })
}

fn companion_detail(
    binding: &str,
    symbols: &BTreeMap<String, Vec<SymbolHome>>,
    member_by_binding: &BTreeMap<String, BindingReport>,
) -> CompanionDetail {
    CompanionDetail {
        binding: binding.to_string(),
        member: report_member(binding, member_by_binding),
        homes: symbols.get(binding).cloned().unwrap_or_default(),
    }
}

fn format_companion(
    binding: &str,
    symbols: &BTreeMap<String, Vec<SymbolHome>>,
    member_by_binding: &BTreeMap<String, BindingReport>,
) -> String {
    let Some(homes) = symbols.get(binding) else {
        let member = report_member(binding, member_by_binding);
        let name = if member.export_name == binding {
            binding.to_string()
        } else {
            format!("{binding}->{}", member.export_name)
        };
        return format!("{name}@<owner_graph>");
    };
    homes
        .iter()
        .map(|home| {
            let name = if home.name == home.binding {
                home.binding.clone()
            } else {
                format!("{}->{}", home.binding, home.name)
            };
            let suffix = if home.deferred { ".deferred" } else { "" };
            format!("{name}@{}{suffix}", home.path)
        })
        .collect::<Vec<_>>()
        .join("|")
}

fn sorted_members(mut members: Vec<BindingReport>) -> Vec<BindingReport> {
    members.sort_by(|left, right| left.binding.cmp(&right.binding));
    members
}

fn sort_ranked_rows(rows: &mut [ModuleCoverage]) {
    rows.sort_by_key(|row| (row.missing.len(), Reverse(row.covered), row.path.clone()));
}

fn push_table(out: &mut String, title: &str, rows: &[ModuleCoverage], limit: usize) {
    out.push_str(title);
    out.push('\n');
    if rows.is_empty() {
        out.push_str("(none)\n");
        return;
    }
    for row in rows.iter().take(limit) {
        let missing = if row.missing.is_empty() {
            "-".to_string()
        } else {
            row.missing.join(",")
        };
        let closure = if row.closure_candidates == 0 {
            String::new()
        } else {
            format!(" closures={}", row.closure_candidates)
        };
        out.push_str(&format!(
            "{:>4}/{:<4} {} owners={}{} missing={}\n",
            row.covered, row.total, row.path, row.owners, closure, missing
        ));
    }
}

fn push_companion_table(out: &mut String, title: &str, rows: &[ModuleCoverage], limit: usize) {
    out.push_str(title);
    out.push('\n');
    if rows.is_empty() {
        out.push_str("(none)\n");
        return;
    }
    for row in rows.iter().take(limit) {
        let companions = if row.companion_labels.is_empty() {
            "-".to_string()
        } else {
            row.companion_labels.join("; ")
        };
        out.push_str(&format!(
            "{:>4}/{:<4} +{:<3} {} companions={}\n",
            row.covered_with_companions,
            row.total,
            row.companions.len(),
            row.path,
            companions
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        ModuleReportRef, OwnerGraphNodeReport, OwnerGraphPeelSetReport,
        OwnerGraphPeelabilityReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity,
        StatementKind, StatementOrdinal,
    };

    fn options(root: &Path, graph: &Path) -> PeelHorizonOptions {
        PeelHorizonOptions {
            owner_graph_path: graph.to_path_buf(),
            modules_root: root.to_path_buf(),
            near_missing: 1,
            max_companions: 4,
        }
    }

    fn write_file(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn member_yaml(binding: &str, kind: &str) -> String {
        format!(
            r#"  - name: {binding}
    selector:
      binding:
        name: {binding}
        kind: {kind}
"#
        )
    }

    fn module_yaml(members: &[(&str, &str)]) -> String {
        let mut out = "members:\n".to_string();
        for (binding, kind) in members {
            out.push_str(&member_yaml(binding, kind));
        }
        out
    }

    fn owner_graph_report(
        bindings: &[&str],
        minimal_peel_sets: Vec<OwnerGraphPeelSetReport>,
    ) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes: bindings
                .iter()
                .enumerate()
                .map(|(ordinal, binding)| OwnerGraphNodeReport {
                    id: format!("owner:{ordinal}"),
                    statement_ordinal: StatementOrdinal(ordinal),
                    source_location: None,
                    declared_bindings: vec![BindingReport {
                        binding: binding.to_string(),
                        export_name: binding.to_string(),
                    }],
                    statement_kind: StatementKind::VarDecl,
                    purity: Purity::Pure,
                    destination: ModuleReportRef {
                        id: "residual_entry".to_string(),
                        label: "residual entry".to_string(),
                        residual: true,
                        index: None,
                        target_file: None,
                    },
                })
                .collect(),
            edges: Vec::new(),
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            peelability: OwnerGraphPeelabilityReport {
                residual_destinations: Vec::new(),
                minimal_peel_sets,
                residual_owner_horizon: Vec::new(),
                evaluated_owner_sets: Vec::new(),
            },
            pre_existing_entry_exports: Vec::new(),
            factorize: analysis::FactorizeReport::default(),
        }
    }

    fn peel_set(
        candidate_id: &str,
        owner_ids: &[usize],
        members: &[&str],
    ) -> OwnerGraphPeelSetReport {
        OwnerGraphPeelSetReport {
            candidate_id: candidate_id.to_string(),
            owner_ids: owner_ids
                .iter()
                .map(|ordinal| format!("owner:{ordinal}"))
                .collect(),
            members: members
                .iter()
                .map(|binding| BindingReport {
                    binding: binding.to_string(),
                    export_name: binding.to_string(),
                })
                .collect(),
            emit_blocked_residual_bindings: Vec::new(),
        }
    }

    fn graph_fixture(graph_path: &Path) {
        let report = owner_graph_report(
            &["a", "b", "c", "d"],
            vec![
                peel_set("candidate:a", &[0], &["a"]),
                peel_set("candidate:bc", &[1, 2], &["b", "c"]),
            ],
        );
        fs::write(graph_path, serde_json::to_string(&report).unwrap()).unwrap();
    }

    #[test]
    fn ranks_direct_companion_and_near_peels() {
        let temp = tempfile::tempdir().unwrap();
        let modules = temp.path().join("modules");
        let graph = temp.path().join("owner_graph.json");
        graph_fixture(&graph);
        write_file(
            &modules.join("direct.yaml.deferred"),
            &module_yaml(&[("a", "variable_declarator")]),
        );
        write_file(
            &modules.join("needs_companion.yaml.deferred"),
            &module_yaml(&[("b", "variable_declarator")]),
        );
        write_file(
            &modules.join("support.yaml"),
            &module_yaml(&[("c", "variable_declarator")]),
        );
        write_file(
            &modules.join("near.yaml.deferred"),
            &module_yaml(&[("d", "variable_declarator")]),
        );
        write_file(
            &modules.join("import_only.yaml.deferred"),
            &module_yaml(&[("imported", "import_specifier")]),
        );

        let report = analyze_peel_horizon(&options(&modules, &graph)).unwrap();

        assert_eq!(
            report
                .full
                .iter()
                .map(|row| row.path.as_str())
                .collect::<Vec<_>>(),
            vec!["direct"]
        );
        assert_eq!(
            report
                .with_companions
                .iter()
                .map(|row| (row.path.as_str(), row.companions.clone()))
                .collect::<Vec<_>>(),
            vec![("needs_companion", vec!["c".to_string()])]
        );
        assert_eq!(
            report
                .near
                .iter()
                .map(|row| row.path.as_str())
                .collect::<Vec<_>>(),
            vec!["near"]
        );
    }

    #[test]
    fn ignores_anonymous_statements_top_level_field() {
        // Spec authoring sometimes co-locates top-level side-effect
        // statements with declared members (e.g. mobx decorator
        // applications, the size-100 app/bootstrap/types_and_models
        // closure peel). peel_horizon does not consume them, but it
        // shares ModuleFile with spec_tree so any field the canonical
        // parser accepts must also parse here without breaking the
        // helper.
        let temp = tempfile::tempdir().unwrap();
        let modules = temp.path().join("modules");
        let graph = temp.path().join("owner_graph.json");
        graph_fixture(&graph);
        write_file(
            &modules.join("with_side_effects.yaml.deferred"),
            &format!(
                "{}\nanonymous_statements:\n  - match: 'window.foo;'\n    note: smoke test\n",
                module_yaml(&[("a", "variable_declarator")]),
            ),
        );

        let report = analyze_peel_horizon(&options(&modules, &graph)).unwrap();

        assert_eq!(
            report
                .full
                .iter()
                .map(|row| row.path.as_str())
                .collect::<Vec<_>>(),
            vec!["with_side_effects"]
        );
    }

    #[test]
    fn rejects_legacy_binding_kind_spelling() {
        let temp = tempfile::tempdir().unwrap();
        let modules = temp.path().join("modules");
        let graph = temp.path().join("owner_graph.json");
        graph_fixture(&graph);
        write_file(
            &modules.join("legacy.yaml.deferred"),
            &module_yaml(&[("a", "VariableDeclarator")]),
        );

        let error = analyze_peel_horizon(&options(&modules, &graph)).unwrap_err();
        assert!(
            error.to_string().contains("legacy.yaml.deferred"),
            "{error:#}"
        );
    }

    #[test]
    fn keeps_companion_candidate_order_deterministic() {
        let temp = tempfile::tempdir().unwrap();
        let modules = temp.path().join("modules");
        let graph_path = temp.path().join("owner_graph.json");
        let graph = owner_graph_report(
            &["a", "b", "c", "d", "e"],
            vec![
                peel_set("candidate:a", &[0], &["a"]),
                peel_set("candidate:e", &[4], &["e"]),
                peel_set("candidate:bd", &[1, 3], &["b", "d"]),
                peel_set("candidate:bc", &[1, 2], &["b", "c"]),
            ],
        );
        fs::write(&graph_path, serde_json::to_string(&graph).unwrap()).unwrap();
        write_file(
            &modules.join("needs_companion.yaml.deferred"),
            &module_yaml(&[("a", "variable_declarator"), ("b", "variable_declarator")]),
        );
        write_file(
            &modules.join("support.yaml"),
            &module_yaml(&[
                ("c", "variable_declarator"),
                ("d", "variable_declarator"),
                ("e", "variable_declarator"),
            ]),
        );

        let report = analyze_peel_horizon(&options(&modules, &graph_path)).unwrap();

        assert_eq!(
            report
                .with_companions
                .iter()
                .map(|row| (row.path.as_str(), row.companions.clone()))
                .collect::<Vec<_>>(),
            vec![("needs_companion", vec!["c".to_string()])]
        );
        assert_eq!(
            report.with_companions[0]
                .companion_candidates
                .iter()
                .map(|candidate| candidate.add_members[0].binding.as_str())
                .collect::<Vec<_>>(),
            vec!["c"]
        );
    }
}
