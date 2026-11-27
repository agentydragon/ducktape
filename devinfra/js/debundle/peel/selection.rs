//! Resolve a source-unit, module, binding or atomic-group selection against a
//! dependency graph, and report the selected units' source locations.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anonymous_resolution::{SourceClaimSet, resolve_source_claims_of, resolve_source_file};
use anyhow::{Context, Result, bail};
use serde::Serialize;

use analysis_vocabulary::execution_evidence::SourceLocation;
use graph::{DependencyGraphInput, DependencyGraphReport, SourceUnitReport};
use graph_provenance::VerifiedGraphSourceContext;
use spec_modules::{collect_module_files, module_path_from_file, read_module_claims};

use crate::plan::CommonArgs;

#[derive(Debug, Clone)]
pub struct SourceSelectionArgs {
    pub common: CommonArgs,
    pub selection: SelectionKind,
    /// Root used to resolve relative `source_location.source_path` values.
    pub source_root: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct QueryReport {
    pub kind: QueryKind,
    pub value: String,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    SourceUnit,
    Module,
    Binding,
    AtomicGroup,
}

/// What `describe` / `inspect-source --id` can resolve; each variant names its query kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectionKind {
    /// One source-unit ID from `dependency_graph.json.gz`.
    SourceUnit(String),
    /// Every source unit claimed by this module path.
    ModulePath(String),
    /// Every source unit assigned to this module ID.
    Module(String),
    /// The source unit that declares this input binding id.
    Binding(String),
    /// One atomic group id from `dependency_graph.json.gz`.
    AtomicGroup(String),
}

impl SelectionKind {
    fn value(&self) -> &str {
        match self {
            Self::SourceUnit(v)
            | Self::ModulePath(v)
            | Self::Module(v)
            | Self::Binding(v)
            | Self::AtomicGroup(v) => v,
        }
    }

    fn query_kind(&self) -> QueryKind {
        match self {
            Self::SourceUnit(_) => QueryKind::SourceUnit,
            Self::ModulePath(_) => QueryKind::Module,
            Self::Module(_) => QueryKind::Module,
            Self::Binding(_) => QueryKind::Binding,
            Self::AtomicGroup(_) => QueryKind::AtomicGroup,
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SourceSelectionReport {
    pub query: QueryReport,
    pub source_unit_ids: Vec<String>,
    /// Bindings claimed by a module-path selection that do not
    /// appear in the dependency graph. Always empty for other selections.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unknown_binding_ids: Vec<String>,
    pub source_units: Vec<SelectedSourceUnitSource>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SelectedSourceUnitSource {
    pub source_unit_id: String,
    pub source_path: String,
    pub resolved_path: String,
    pub statement_ordinal: usize,
    pub source_location: SourceLocation,
}

pub fn run_source_selection_report(
    args: &SourceSelectionArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<SourceSelectionReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let selection = &args.selection;
    let module_files = if matches!(selection, SelectionKind::ModulePath(_)) {
        collect_module_files(&args.common.modules_root)?
    } else {
        Vec::new()
    };
    let query = query_report(selection);
    let ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    } = resolve_source_unit_ids(
        selection,
        &input,
        &ResolveSourceUnitContext {
            common: &args.common,
            source_root: args.source_root.as_deref(),
            source_context,
            module_files: &module_files,
        },
        diagnostics,
    )?;
    let source_units = source_units_for_ids(graph, &source_unit_ids);
    let mut selected = Vec::new();
    let mut resolved_paths: BTreeMap<String, String> = BTreeMap::new();
    for source_unit in source_units {
        let location = source_unit
            .source_location
            .as_ref()
            .with_context(|| format!("source unit {} has no source location", source_unit.id))?;
        let source_path = &location.source_path;
        let resolved_path = match resolved_paths.get(source_path) {
            Some(path) => path.clone(),
            None => {
                let path = resolve_source_file(
                    source_path,
                    args.source_root.as_deref(),
                    &args.common.dependency_graph_path,
                    &args.common.modules_root,
                    source_context,
                )?
                .display()
                .to_string();
                resolved_paths.insert(source_path.clone(), path.clone());
                path
            }
        };
        selected.push(SelectedSourceUnitSource {
            source_unit_id: source_unit.id.clone(),
            source_path: source_path.clone(),
            resolved_path,
            statement_ordinal: source_unit.statement_ordinal.0,
            source_location: location.clone(),
        });
    }
    selected.sort_by(|a, b| {
        (&a.source_path, a.statement_ordinal, &a.source_unit_id).cmp(&(
            &b.source_path,
            b.statement_ordinal,
            &b.source_unit_id,
        ))
    });
    Ok(SourceSelectionReport {
        query,
        source_unit_ids: source_unit_ids.into_iter().collect(),
        unknown_binding_ids,
        source_units: selected,
    })
}

pub(crate) fn source_units_for_ids(
    graph: &DependencyGraphReport,
    source_unit_ids: &BTreeSet<String>,
) -> Vec<SourceUnitReport> {
    graph
        .nodes
        .iter()
        .filter(|source_unit| source_unit_ids.contains(&source_unit.id))
        .cloned()
        .collect()
}

/// Map each declared (minified) binding name to all source-unit IDs that declare it.
///
/// A repeated top-level `var` is one binding with several declaration sites,
/// and the dependency graph retains each site as a source-unit node. Binding-keyed claims
/// therefore cover every matching source unit so a co-declaration atomic group is
/// reported complete; the selector resolver uses the first site as its single
/// placement representative.
pub(crate) fn binding_to_source_units(
    graph: &DependencyGraphReport,
) -> BTreeMap<String, Vec<String>> {
    let mut out = BTreeMap::new();
    for node in &graph.nodes {
        for binding in &node.declared_bindings {
            out.entry(binding.binding.to_string())
                .or_insert_with(Vec::new)
                .push(node.id.clone());
        }
    }
    out
}

pub(crate) fn query_report(selection: &SelectionKind) -> QueryReport {
    QueryReport {
        kind: selection.query_kind(),
        value: selection.value().to_string(),
    }
}

/// Find every source-unit node whose `declared_bindings` matches `name`.
///
/// `name` is compared against both the minified `BindingReport.binding`
/// and the readable `BindingReport.export_name`. Matches by minified
/// name come first, so when both forms happen to spell `name` on
/// different bindings (extremely rare — readable names are picked to
/// avoid collisions with live minified ids), the minified match wins
/// the back-compat order; both still appear in the returned slice so
/// callers can detect ambiguity if they care.
///
/// This is the one shared resolver every CLI verb that accepts a
/// binding name from the dependency graph (`cluster`, `scc --binding`,
/// `describe`, `inspect-source --id`) routes through. Spec-edit verbs
/// (`bindings rename` / `assign` / `unassign` / `comment`) use
/// `cli::binding::resolve_unambiguous` instead — they need YAML
/// member positions, not dependency-graph node refs.
pub fn resolve_binding_source_units<'a>(
    graph: &'a DependencyGraphReport,
    name: &str,
) -> Vec<&'a SourceUnitReport> {
    let mut by_minified: Vec<&SourceUnitReport> = Vec::new();
    let mut by_readable: Vec<&SourceUnitReport> = Vec::new();
    for node in &graph.nodes {
        let mut matched_minified = false;
        let mut matched_readable = false;
        for binding in &node.declared_bindings {
            if binding.binding == *name {
                matched_minified = true;
            }
            if binding.export_name == *name {
                matched_readable = true;
            }
        }
        if matched_minified {
            by_minified.push(node);
        } else if matched_readable {
            by_readable.push(node);
        }
    }
    by_minified.extend(by_readable);
    by_minified
}

/// Result of resolving a selection to source-unit IDs.
/// `unknown_binding_ids` is non-empty only for module-path selections
/// whose YAML claims bindings the dependency graph does not declare.
#[derive(Debug, Clone)]
pub(crate) struct ResolvedSelection {
    pub(crate) source_unit_ids: BTreeSet<String>,
    pub(crate) unknown_binding_ids: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
#[error("source-unit ID {source_unit_id:?} not found in dependency graph")]
pub struct SourceUnitNotFound {
    source_unit_id: String,
}

pub(crate) struct ResolveSourceUnitContext<'a> {
    pub(crate) common: &'a CommonArgs,
    pub(crate) source_root: Option<&'a Path>,
    pub(crate) source_context: Option<&'a VerifiedGraphSourceContext>,
    pub(crate) module_files: &'a [PathBuf],
}

pub(crate) fn resolve_source_unit_ids(
    selection: &SelectionKind,
    input: &DependencyGraphInput,
    context: &ResolveSourceUnitContext<'_>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ResolvedSelection> {
    let graph = input.report();
    let mut unknown_binding_ids: Vec<String> = Vec::new();
    let source_unit_ids: BTreeSet<String> = match selection {
        SelectionKind::SourceUnit(source_unit_id) => {
            if graph.nodes.iter().any(|node| node.id == *source_unit_id) {
                BTreeSet::from([source_unit_id.clone()])
            } else {
                return Err(SourceUnitNotFound {
                    source_unit_id: source_unit_id.clone(),
                }
                .into());
            }
        }
        SelectionKind::ModulePath(module_path) => {
            let resolved =
                resolve_module_path_source_unit_ids(graph, context, module_path, diagnostics)?;
            unknown_binding_ids = resolved.unknown_binding_ids;
            resolved.source_unit_ids
        }
        SelectionKind::Module(module_id) => {
            let source_unit_ids: BTreeSet<String> = graph
                .nodes
                .iter()
                .filter(|node| node.destination.as_str() == *module_id)
                .map(|node| node.id.clone())
                .collect();
            if source_unit_ids.is_empty()
                && !graph
                    .quotient
                    .nodes
                    .iter()
                    .any(|module| module.key.as_str() == *module_id)
            {
                bail!("module id {module_id:?} not found in dependency graph");
            }
            source_unit_ids
        }
        SelectionKind::Binding(binding_id) => resolve_binding_source_units(graph, binding_id)
            .into_iter()
            .map(|node| node.id.clone())
            .collect(),
        SelectionKind::AtomicGroup(atomic_group_id) => graph
            .atomic_graph
            .nodes
            .iter()
            .find(|atomic_group| atomic_group.id == *atomic_group_id)
            .into_iter()
            .flat_map(|group| group.source_unit_ids.iter().cloned())
            .collect(),
    };
    if source_unit_ids.is_empty() {
        bail!("selection did not resolve to any source-unit IDs");
    }
    Ok(ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    })
}

/// Preserve partial misses for the report. A module with only unknown binding IDs
/// gets a diagnostic that names them.
fn resolve_module_path_source_unit_ids(
    graph: &DependencyGraphReport,
    context: &ResolveSourceUnitContext<'_>,
    module_path: &str,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<ResolvedSelection> {
    let common = context.common;
    let module_files = context.module_files;
    // The module's claims resolve jointly with every other module's, as in
    // `run`.
    let all_claims = module_files
        .iter()
        .map(|file| read_module_claims(file))
        .collect::<Result<Vec<_>>>()?;
    let index = module_files
        .iter()
        .position(|file| module_path_from_file(file, &common.modules_root) == module_path)
        .with_context(|| format!("module {module_path:?} is not in the modules tree"))?;
    let claims = &all_claims[index];
    if claims.is_empty() {
        bail!("module {module_path:?} has no members or anonymous_statements; nothing to describe");
    }

    let resolved = resolve_source_claims_of(
        anonymous_resolution::SourceClaimsContext {
            graph,
            dependency_graph_path: &common.dependency_graph_path,
            modules_root: &common.modules_root,
            source_root: context.source_root,
            source_context: context.source_context,
            diagnostics,
        },
        &module_files
            .iter()
            .zip(&all_claims)
            .map(|(file, claims)| SourceClaimSet {
                module_path: file,
                module: claims,
            })
            .collect::<Vec<_>>(),
        index,
    )?;
    let pins = claims.binding_pins().collect::<BTreeSet<_>>();
    let selection = resolve_claimed_units(
        graph,
        &binding_to_source_units(graph),
        pins.into_iter()
            .chain(resolved.bindings.iter().map(String::as_str)),
        &resolved.anonymous_source_units,
    );
    if selection.source_unit_ids.is_empty() && !selection.unknown_binding_ids.is_empty() {
        bail!(
            "module {module_path:?} declares bindings that do not appear in the dependency graph: {}",
            selection.unknown_binding_ids.join(", ")
        );
    }
    Ok(selection)
}

pub(crate) fn resolve_claimed_units<'a>(
    graph: &DependencyGraphReport,
    binding_to_source_units: &BTreeMap<String, Vec<String>>,
    bindings: impl IntoIterator<Item = &'a str>,
    anonymous_source_units: &BTreeSet<source_identity::SourceUnitId>,
) -> ResolvedSelection {
    let mut source_unit_ids = BTreeSet::new();
    let mut unknown_binding_ids = Vec::new();
    for binding in bindings {
        if let Some(ids) = binding_to_source_units.get(binding) {
            source_unit_ids.extend(ids.iter().cloned());
        } else {
            unknown_binding_ids.push(binding.to_owned());
        }
    }
    for unit in anonymous_source_units {
        if let Some(node) = graph.nodes.get(unit.0) {
            source_unit_ids.insert(node.id.clone());
        }
    }
    ResolvedSelection {
        source_unit_ids,
        unknown_binding_ids,
    }
}
