//! Deterministic discovery and authoring-scope selection for graph corpora.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use spec_tree::ModuleTreeScope;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct GraphCorpusScope {
    /// Graphs whose chunks are configured by the authoring tree.
    pub configured: BTreeMap<String, PathBuf>,
    /// Discovered graphs excluded because their chunks have no configured roots.
    pub skipped_unconfigured: Vec<UnconfiguredGraph>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(super) struct UnconfiguredGraph {
    pub chunk_id: String,
    pub path: PathBuf,
}

/// Discover graph files without following symlinks and index them by their
/// embedded chunk ID. File paths, not directory iteration order, determine
/// which duplicate is reported first.
pub(super) fn discover_graphs(root: &Path) -> Result<BTreeMap<String, PathBuf>> {
    let root_metadata = fs::symlink_metadata(root)
        .with_context(|| format!("reading reports root {}", root.display()))?;
    if !root_metadata.file_type().is_dir() {
        bail!("reports root {} is not a directory", root.display());
    }

    let mut pending_directories = vec![root.to_path_buf()];
    let mut graph_paths = Vec::new();
    while let Some(directory) = pending_directories.pop() {
        let entries = fs::read_dir(&directory)
            .with_context(|| format!("reading reports directory {}", directory.display()))?
            .collect::<std::io::Result<Vec<_>>>()
            .with_context(|| format!("reading entries in {}", directory.display()))?;
        let mut entries = entries;
        entries.sort_by_key(|entry| entry.path());
        for entry in entries.into_iter().rev() {
            let path = entry.path();
            let file_type = entry
                .file_type()
                .with_context(|| format!("reading file type for {}", path.display()))?;
            if file_type.is_dir() {
                pending_directories.push(path);
            } else if file_type.is_file() && is_dependency_graph(&path) {
                graph_paths.push(path);
            }
        }
    }
    graph_paths.sort();
    if graph_paths.is_empty() {
        bail!(
            "no dependency_graph.json or dependency_graph.json.gz files found under {}",
            root.display()
        );
    }

    let mut graphs = BTreeMap::new();
    for path in graph_paths {
        let header = graph_provenance::read_graph_header(&path)?;
        if let Some(previous) = graphs.insert(header.chunk_id.clone(), path.clone()) {
            bail!(
                "reports root contains multiple dependency graphs for chunk {:?}: {} and {}",
                header.chunk_id,
                previous.display(),
                path.display()
            );
        }
    }
    Ok(graphs)
}

fn is_dependency_graph(path: &Path) -> bool {
    path.file_name()
        .is_some_and(|name| name == "dependency_graph.json" || name == "dependency_graph.json.gz")
}

/// Limit the discovered graph corpus to chunks the configured tree authors.
/// Every configured chunk must have one graph; other discovered chunks are
/// returned explicitly so a report can state its scope.
pub(super) fn scope_graphs_to_tree(
    graphs: &BTreeMap<String, PathBuf>,
    tree_config: &Path,
) -> Result<GraphCorpusScope> {
    let configured_ids = ModuleTreeScope::configured_chunk_ids(tree_config)?;
    let missing = configured_ids
        .iter()
        .filter(|chunk_id| !graphs.contains_key(*chunk_id))
        .cloned()
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        bail!(
            "reports root is missing configured graph inputs for chunks: {}",
            missing.join(", ")
        );
    }

    let mut configured = BTreeMap::new();
    let mut skipped_unconfigured = Vec::new();
    for (chunk_id, path) in graphs {
        if configured_ids.contains(chunk_id) {
            configured.insert(chunk_id.clone(), path.clone());
        } else {
            skipped_unconfigured.push(UnconfiguredGraph {
                chunk_id: chunk_id.clone(),
                path: path.clone(),
            });
        }
    }
    Ok(GraphCorpusScope {
        configured,
        skipped_unconfigured,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use report_io::write_json_gzip;
    use serde_json::json;

    fn write_graph(root: &Path, relative_graph: &Path, chunk_id: &str, gzip: bool) -> PathBuf {
        let graph_path = root.join(relative_graph);
        let program_path = graph_path.parent().unwrap().join(if gzip {
            "program_graph.json.gz"
        } else {
            "program_graph.json"
        });
        fs::create_dir_all(graph_path.parent().unwrap()).unwrap();
        let graph = json!({
            "chunk_id": chunk_id,
            "program_graph": program_path.file_name().unwrap().to_str().unwrap(),
        });
        let program = json!({ "provenance": null });
        if gzip {
            write_json_gzip(&graph_path, &graph).unwrap();
            write_json_gzip(&program_path, &program).unwrap();
        } else {
            fs::write(&graph_path, serde_json::to_vec(&graph).unwrap()).unwrap();
            fs::write(&program_path, serde_json::to_vec(&program).unwrap()).unwrap();
        }
        graph_path
    }

    #[test]
    fn discovers_plain_and_gzip_graphs_in_sorted_chunk_order_without_following_symlinks() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("reports");
        let z_graph = write_graph(
            &root,
            Path::new("z/deep/dependency_graph.json.gz"),
            "z",
            true,
        );
        let a_graph = write_graph(&root, Path::new("a/dependency_graph.json"), "a", false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;

            symlink(root.join("z"), root.join("linked-z")).unwrap();
            symlink(&z_graph, root.join("dependency_graph.json.gz")).unwrap();
        }

        let graphs = discover_graphs(&root).unwrap();
        assert_eq!(
            graphs.keys().map(String::as_str).collect::<Vec<_>>(),
            ["a", "z"]
        );
        assert_eq!(graphs["a"], a_graph);
        assert_eq!(graphs["z"], z_graph);
    }

    #[test]
    fn duplicate_chunk_ids_report_both_paths_in_stable_order() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("reports");
        write_graph(&root, Path::new("a/dependency_graph.json"), "same", false);
        write_graph(&root, Path::new("z/dependency_graph.json.gz"), "same", true);

        let error = discover_graphs(&root).unwrap_err();
        let message = format!("{error:#}");
        assert!(message.contains("chunk \"same\""), "{message}");
        assert!(
            message.find("a/dependency_graph.json").unwrap()
                < message.find("z/dependency_graph.json.gz").unwrap(),
            "{message}"
        );
    }

    #[test]
    fn rejects_a_reports_root_without_graph_files() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("reports");
        fs::create_dir_all(&root).unwrap();

        let error = discover_graphs(&root).unwrap_err();
        assert!(
            format!("{error:#}").contains("no dependency_graph.json or dependency_graph.json.gz"),
            "{error:#}"
        );
    }

    #[test]
    fn configured_scope_requires_each_configured_graph_and_lists_skipped_chunks() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("tree.yaml");
        fs::write(
            &config,
            "main_chunk_id: ignored\nmodule_roots:\n  cli: cli\n  print: print\n  shared: print\ninputs:\n  root: snapshot\n  js_list_path: extracted/js-files.txt\nunassigned_mode:\n  cli: { kind: inline_in_entry }\n  print: { kind: inline_in_entry }\n",
        )
        .unwrap();
        let graphs = BTreeMap::from([
            (
                "cli".to_string(),
                PathBuf::from("cli/dependency_graph.json.gz"),
            ),
            (
                "print".to_string(),
                PathBuf::from("print/dependency_graph.json.gz"),
            ),
            (
                "unconfigured".to_string(),
                PathBuf::from("other/dependency_graph.json.gz"),
            ),
        ]);

        let scope = scope_graphs_to_tree(&graphs, &config).unwrap();
        assert_eq!(
            scope
                .configured
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            ["cli", "print"]
        );
        assert_eq!(scope.skipped_unconfigured.len(), 1);
        assert_eq!(scope.skipped_unconfigured[0].chunk_id, "unconfigured");
        assert_eq!(scope.skipped_unconfigured[0].path, graphs["unconfigured"]);

        let incomplete = BTreeMap::from([("cli".to_string(), graphs["cli"].clone())]);
        let error = scope_graphs_to_tree(&incomplete, &config).unwrap_err();
        assert!(
            format!("{error:#}").contains("missing configured graph inputs for chunks: print"),
            "{error:#}"
        );
    }
}
