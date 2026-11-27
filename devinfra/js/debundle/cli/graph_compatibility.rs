//! Joins a saved dependency graph to the current authoring files before a
//! graph-backed edit or query interprets them.

use anyhow::{Context, Result};
use graph_provenance::{CurrentRoots, GraphMismatch, GraphMismatchReason};

/// Retain the checked source/configuration for every graph a command consumes.
/// Corpus proposals must not reuse the first chunk's context for other chunks.
pub type VerifiedGraphContexts =
    std::collections::BTreeMap<String, graph_provenance::VerifiedGraphSourceContext>;

/// Which current authoring scope a graph-backed command actually interprets.
#[derive(Clone, Copy)]
pub enum GraphCompatibilityScope<'a> {
    AuthoredModuleTree {
        tree_config: Option<&'a std::path::Path>,
    },
    ExactSourceUnit,
}

/// Shared pre-edit/pre-query check for any operation that joins a saved graph
/// to current authoring files. Historical graph-only views load a `DependencyGraphInput` directly.
pub fn verify_graph_compatibility(
    graph_path: &std::path::Path,
    modules_root: &std::path::Path,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    tree_config: Option<&std::path::Path>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    verify_graph_compatibility_with_scope(
        graph_path,
        Some(modules_root),
        selector_source_root,
        roots,
        GraphCompatibilityScope::AuthoredModuleTree { tree_config },
    )
}

/// `modules_root` is required by the authored-module-tree scope; the
/// exact-source-unit scope uses it only to reject a fallback source shadow
/// when no selector root is supplied.
pub fn verify_graph_compatibility_with_scope(
    graph_path: &std::path::Path,
    modules_root: Option<&std::path::Path>,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    scope: GraphCompatibilityScope<'_>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    let graph = graph_provenance::read_graph_header(graph_path)?;
    verify_graph_header_compatibility(
        graph_path,
        &graph,
        modules_root,
        selector_source_root,
        roots,
        scope,
    )
}

fn verify_graph_header_compatibility(
    graph_path: &std::path::Path,
    graph: &graph_provenance::GraphHeader,
    modules_root: Option<&std::path::Path>,
    selector_source_root: Option<&std::path::Path>,
    roots: &CurrentRoots,
    scope: GraphCompatibilityScope<'_>,
) -> Result<graph_provenance::VerifiedGraphSourceContext> {
    let (require_module_scope, tree_config) = match scope {
        GraphCompatibilityScope::AuthoredModuleTree { tree_config } => (true, tree_config),
        GraphCompatibilityScope::ExactSourceUnit => (false, None),
    };
    let scoped_modules_root = match (require_module_scope, modules_root) {
        (true, None) => anyhow::bail!("a module-scoped graph check requires --modules"),
        (_, modules_root) => modules_root,
    };
    let provenance = graph.provenance.as_ref().ok_or_else(|| {
        GraphMismatch::new(
            GraphMismatchReason::MissingProvenance {
                graph: graph_path.to_path_buf(),
            },
            vec!["debundle".to_string(), "run".to_string()],
            None,
        )
    })?;
    let (regenerate_command, regenerate_cwd) = provenance.regeneration_recipe(roots)?;
    if let (true, None) = (require_module_scope, &provenance.modules_directory) {
        return Err(GraphMismatch::new(
            GraphMismatchReason::ModuleScope {
                chunk_id: graph.chunk_id.clone(),
                expected: Vec::new(),
                actual: graph_provenance::absolute(
                    scoped_modules_root.expect("checked by the scope match above"),
                )?,
            },
            regenerate_command.clone(),
            Some(regenerate_cwd.clone()),
        )
        .into());
    }
    // `inspect-source --id unit:*` resolves a saved node without interpreting
    // current module claims, so it skips only this authored-scope check.
    if require_module_scope {
        let modules_root = scoped_modules_root.expect("checked by the scope match above");
        let directory = provenance
            .modules_directory
            .as_ref()
            .expect("checked above");
        let base = roots
            .spec
            .as_ref()
            .unwrap_or(&provenance.roots.spec)
            .join(directory);
        let modules = graph_provenance::absolute(modules_root)?;
        let base = graph_provenance::absolute(&base)?;
        if !modules.starts_with(&base) {
            return Err(GraphMismatch::new(
                GraphMismatchReason::ModulesRoot {
                    expected: base,
                    actual: modules,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        let relative = modules.strip_prefix(&base).expect("checked prefix");
        let scopes = provenance
            .module_roots_by_chunk
            .get(&graph.chunk_id)
            .with_context(|| {
                format!(
                    "graph chunk {:?} has no authored module scope",
                    graph.chunk_id
                )
            })?;
        let exact_chunk_scope = scopes.len() == 1 && relative == scopes[0];
        let full_tree_with_config = relative.as_os_str().is_empty() && tree_config.is_some();
        if !exact_chunk_scope && !full_tree_with_config {
            return Err(GraphMismatch::new(
                GraphMismatchReason::ModuleScope {
                    chunk_id: graph.chunk_id.clone(),
                    expected: scopes.iter().map(|scope| base.join(scope)).collect(),
                    actual: modules,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        if let Some(config) = tree_config {
            let expected = provenance
                .config_path
                .as_ref()
                .map(|path| {
                    roots
                        .spec
                        .as_ref()
                        .unwrap_or(&provenance.roots.spec)
                        .join(path)
                })
                .context("tree graph lacks config path")?;
            let expected = graph_provenance::absolute(&expected)?;
            let actual = graph_provenance::absolute(config)?;
            if expected != actual {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::ConfigPath { expected, actual },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
        }
    }
    let source_context = provenance.verify_chunk_source(roots, &graph.chunk_id)?;
    let expected_path = graph_provenance::absolute(source_context.input_file())?;
    let source_path = source_context.source_path().to_string_lossy();
    let selector_candidate = if let Some(root) = selector_source_root {
        let canonical_root = graph_provenance::absolute(root)?;
        let logical_path = source_context.source_path();
        let candidate = if !logical_path.is_absolute()
            && graph_provenance::validate_relative_path(logical_path).is_ok()
        {
            Some(canonical_root.join(logical_path))
        } else {
            None
        };
        let root_contains_input =
            canonical_root.is_dir() && expected_path.starts_with(&canonical_root);
        let candidate_exists = match candidate.as_ref().map(std::fs::symlink_metadata) {
            Some(Ok(_)) => true,
            Some(Err(error)) if error.kind() != std::io::ErrorKind::NotFound => true,
            _ => false,
        };
        let canonical_candidate = if candidate_exists {
            candidate.as_ref().map(|candidate| {
                graph_provenance::absolute(candidate).unwrap_or_else(|_| candidate.clone())
            })
        } else {
            None
        };
        if !root_contains_input
            || canonical_candidate
                .as_ref()
                .is_some_and(|candidate| candidate != &expected_path)
        {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSourceRoot {
                    chunk_id: graph.chunk_id.clone(),
                    expected: expected_path,
                    actual: canonical_candidate.unwrap_or_else(|| {
                        candidate
                            .clone()
                            .unwrap_or_else(|| canonical_root.join(logical_path))
                    }),
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        candidate.filter(|_| candidate_exists)
    } else {
        modules_root.and_then(|modules_root| {
            anonymous_resolution::find_source_file_candidate(
                &source_path,
                None,
                graph_path,
                modules_root,
            )
        })
    };
    if let Some(candidate) = selector_candidate {
        let canonical_candidate = graph_provenance::absolute(&candidate)?;
        if canonical_candidate != expected_path {
            return Err(GraphMismatch::new(
                GraphMismatchReason::SelectorSourceRoot {
                    chunk_id: graph.chunk_id.clone(),
                    expected: expected_path,
                    actual: canonical_candidate,
                },
                regenerate_command,
                Some(regenerate_cwd),
            )
            .into());
        }
    }
    Ok(source_context)
}
