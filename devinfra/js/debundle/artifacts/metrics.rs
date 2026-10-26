//! Output-size and decomposition metrics, plus file/directory dependency
//! summaries derived from finalized artifact outputs.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::Path;

use analysis::DepKind;
use anyhow::Result;
use rayon::prelude::*;
use serde::Serialize;

use crate::ChunkId;
use crate::paths::{join_module_path, module_path_dirname};
use crate::reports::{ChunkDecompositionOutput, ModuleRef, SelectedModuleLowering, write_json};
use output_layout::{report_path_for_directory, report_path_for_file};

#[derive(Debug, Clone, Serialize)]
pub struct OutputMetrics {
    pub total: OutputSize,
    pub top_level_entry: OutputSize,
    pub named_modules: OutputSize,
    pub residual_modules: OutputSize,
    pub other_files: OutputSize,
    pub named_module_fraction: OutputFraction,
    pub residual_module_fraction: OutputFraction,
    pub top_level_entry_fraction: OutputFraction,
    pub largest_files_by_bytes: Vec<OutputFileMetric>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputSize {
    pub files: usize,
    pub bytes: usize,
    pub lines: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputFraction {
    pub bytes: f64,
    pub lines: f64,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputRole {
    TopLevelEntry,
    NamedModule,
    ResidualModule,
    Other,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputFileMetric {
    pub file: String,
    pub role: OutputRole,
    pub bytes: usize,
    pub lines: usize,
    /// The materialized module emitted to this file, when the file is
    /// a module (entry/runtime/other files carry `None`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module: Option<ModuleRef>,
}

impl OutputMetrics {
    pub(crate) fn from_file_metrics(metrics: impl IntoIterator<Item = OutputFileMetric>) -> Self {
        let metrics = metrics.into_iter().collect::<Vec<_>>();
        let total = OutputSize::sum(metrics.iter());
        let top_level_entry = size_for_role(&metrics, OutputRole::TopLevelEntry);
        let named_modules = size_for_role(&metrics, OutputRole::NamedModule);
        let residual_modules = size_for_role(&metrics, OutputRole::ResidualModule);
        let other_files = size_for_role(&metrics, OutputRole::Other);
        OutputMetrics {
            named_module_fraction: output_fraction(&named_modules, &total),
            residual_module_fraction: output_fraction(&residual_modules, &total),
            top_level_entry_fraction: output_fraction(&top_level_entry, &total),
            total,
            top_level_entry,
            named_modules,
            residual_modules,
            other_files,
            largest_files_by_bytes: largest_files_by_bytes(metrics),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DecompositionMetrics {
    pub module_count: usize,
    pub total_symbols_defined: usize,
    pub total_exported_symbols: usize,
    pub export_ratio: f64,
    pub loc_distribution: LocDistribution,
    pub entropy: f64,
    pub per_module: Vec<ModuleDecompositionMetrics>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DirectoryManifestIndex {
    pub path: String,
    pub directories: Vec<String>,
    pub files: Vec<String>,
    pub chunks: Vec<String>,
    /// Modules whose emitted file lives under this directory, as
    /// chunk-qualified [`ModuleRef`]s.
    pub modules: Vec<ModuleRef>,
    pub defined_symbols: BTreeMap<String, usize>,
    pub loc: usize,
    pub incoming: DirectoryBoundarySummary,
    pub outgoing: DirectoryBoundarySummary,
}

#[derive(Debug, Clone, Serialize)]
pub struct DirectoryBoundarySummary {
    pub edge_count: usize,
    pub edge_count_by_kind: EdgeHistogram,
    pub symbols: EdgeHistogram,
    pub files: EdgeHistogram,
    pub edges: Vec<DirectoryDependencyEdgeManifest>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FileDependencyManifest {
    pub path: String,
    pub role: OutputRole,
    pub bytes: usize,
    pub lines: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunk_id: Option<String>,
    /// The materialized module emitted to this file, when the file is
    /// a module, as a chunk-qualified [`ModuleRef`].
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module: Option<ModuleRef>,
    pub incoming: FileBoundarySummary,
    pub outgoing: FileBoundarySummary,
}

#[derive(Debug, Clone, Serialize)]
pub struct FileBoundarySummary {
    pub edge_count: usize,
    pub edge_count_by_kind: EdgeHistogram,
    pub symbols: EdgeHistogram,
    pub files: EdgeHistogram,
}

/// Histogram of counts keyed by a string label (kind, symbol, or file).
/// Emitted as a JSON array of `[key, count]` pairs to skip serde's
/// `BTreeMap` machinery — `DirectoryDependencyEdgeManifest`'s per-edge
/// maps were a measured hotspot inside `write_tree_reports` (see
/// `perf/proposer.md`), and the same shape is shared by
/// `DirectoryBoundarySummary` and `FileBoundarySummary`. Pairs are
/// lexicographically sorted by key so the output is stable for diffing.
pub type EdgeHistogram = Vec<(String, usize)>;

#[derive(Debug, Clone, Serialize)]
pub struct DirectoryDependencyEdgeManifest {
    pub source_dir: String,
    pub target_dir: String,
    pub edge_count_by_kind: EdgeHistogram,
    pub symbols: EdgeHistogram,
    pub source_files: EdgeHistogram,
    pub target_files: EdgeHistogram,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionMetrics {
    pub module: ModuleRef,
    pub loc: usize,
    pub exported_symbol_count: usize,
    pub is_residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LocDistribution {
    pub p50: usize,
    pub p90: usize,
    pub max: usize,
    pub min: usize,
}

impl DecompositionMetrics {
    pub fn compute(
        lowerings: &[SelectedModuleLowering],
        file_metrics: &[OutputFileMetric],
    ) -> Self {
        let mut per_module: Vec<ModuleDecompositionMetrics> = Vec::new();
        let mut total_exported = 0usize;
        let mut total_defined = 0usize;

        for lowering in lowerings {
            let module_ref = lowering.module_ref();
            let loc: usize = file_metrics
                .iter()
                .filter(|f| f.module.as_ref() == Some(&module_ref))
                .map(|f| f.lines)
                .sum();
            total_exported += lowering.exported_names.len();
            total_defined += lowering.binding_names.len();
            per_module.push(ModuleDecompositionMetrics {
                module: module_ref,
                loc,
                exported_symbol_count: lowering.exported_names.len(),
                is_residual: lowering.residual,
            });
        }

        let module_count = per_module.len();
        let export_ratio = if total_defined > 0 {
            total_exported as f64 / total_defined as f64
        } else {
            0.0
        };

        let loc_distribution = compute_loc_distribution(&per_module);
        let entropy = compute_normalized_entropy(&per_module);

        Self {
            module_count,
            total_symbols_defined: total_defined,
            total_exported_symbols: total_exported,
            export_ratio,
            loc_distribution,
            entropy,
            per_module,
        }
    }
}

fn compute_loc_distribution(modules: &[ModuleDecompositionMetrics]) -> LocDistribution {
    let mut locs: Vec<usize> = modules.iter().map(|m| m.loc).collect();
    if locs.is_empty() {
        return LocDistribution {
            p50: 0,
            p90: 0,
            max: 0,
            min: 0,
        };
    }
    locs.sort_unstable();
    let len = locs.len();
    LocDistribution {
        p50: locs[len / 2],
        p90: locs[len * 9 / 10],
        max: locs[len - 1],
        min: locs[0],
    }
}

fn compute_normalized_entropy(modules: &[ModuleDecompositionMetrics]) -> f64 {
    if modules.len() <= 1 {
        return 0.0;
    }
    let total_loc: usize = modules.iter().map(|m| m.loc).sum();
    if total_loc == 0 {
        return 0.0;
    }
    let entropy: f64 = modules
        .iter()
        .filter(|m| m.loc > 0)
        .map(|m| {
            let p = m.loc as f64 / total_loc as f64;
            -p * p.ln()
        })
        .sum();
    let n = modules.len() as f64;
    entropy / n.ln()
}

impl OutputSize {
    fn from_file(file: &OutputFileMetric) -> Self {
        Self {
            files: 1,
            bytes: file.bytes,
            lines: file.lines,
        }
    }

    fn sum<'a>(files: impl IntoIterator<Item = &'a OutputFileMetric>) -> Self {
        files.into_iter().map(Self::from_file).fold(
            Self {
                files: 0,
                bytes: 0,
                lines: 0,
            },
            |left, right| Self {
                files: left.files + right.files,
                bytes: left.bytes + right.bytes,
                lines: left.lines + right.lines,
            },
        )
    }
}

pub(crate) fn write_tree_reports(
    report_tree_root: &Path,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    file_metrics: &[OutputFileMetric],
) -> Result<()> {
    fs::create_dir_all(report_tree_root)?;

    let file_manifests = build_file_dependency_manifests(decomposition_by_chunk, file_metrics);
    file_manifests.into_par_iter().try_for_each(|manifest| {
        write_json(
            report_path_for_file(report_tree_root, &manifest.path),
            &manifest,
        )
    })?;

    let manifests = build_directory_dependency_manifests(decomposition_by_chunk, file_metrics);
    manifests.into_par_iter().try_for_each(|manifest| {
        write_json(
            report_path_for_directory(report_tree_root, &manifest.path),
            &manifest,
        )
    })?;
    Ok(())
}

fn build_directory_dependency_manifests(
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    file_metrics: &[OutputFileMetric],
) -> Vec<DirectoryManifestIndex> {
    let loc_by_module: BTreeMap<&ModuleRef, usize> = file_metrics
        .iter()
        .filter_map(|metric| Some((metric.module.as_ref()?, metric.lines)))
        .collect();
    let mut directories = BTreeMap::<String, DirectoryAccumulator>::new();

    // Per-edge invocations of `module_path_dirname` and
    // `directory_ancestors_for_file` recompute the same path manipulations
    // many times — the same source/target files recur across many
    // facts. Memoize both keyed on the input path so each unique path
    // pays the path-parsing cost exactly once per call to this function.
    let mut dirname_cache: HashMap<String, String> = HashMap::new();
    let mut ancestors_cache: HashMap<String, Vec<String>> = HashMap::new();

    for metric in file_metrics {
        let ancestors = memoize_ancestors(&mut ancestors_cache, &metric.file).to_vec();
        for dir in ancestors {
            directories
                .entry(dir.clone())
                .or_default()
                .add_file(&dir, metric);
        }
    }

    for decomposition in decomposition_by_chunk.values() {
        for lowering in &decomposition.selected_module_lowerings {
            let file = join_module_path(&[&lowering.chunk_id, &lowering.target_file]);
            let module_ref = lowering.module_ref();
            let loc = loc_by_module.get(&module_ref).copied().unwrap_or(0);
            let ancestors = memoize_ancestors(&mut ancestors_cache, &file).to_vec();
            for dir in ancestors {
                let accumulator = directories.entry(dir).or_default();
                accumulator.modules.insert(module_ref.clone());
                for binding_name in &lowering.binding_names {
                    let symbol = format!("{file}#{binding_name}");
                    *accumulator.defined_symbols.entry(symbol).or_default() += 1;
                }
                accumulator.loc += loc;
            }
        }
    }

    for decomposition in decomposition_by_chunk.values() {
        for fact in &decomposition.directory_dependency_facts {
            let source_dir = memoize_dirname(&mut dirname_cache, &fact.source_file).clone();
            let target_dir = memoize_dirname(&mut dirname_cache, &fact.target_file).clone();
            let source_ancestors =
                memoize_ancestors(&mut ancestors_cache, &fact.source_file).to_vec();
            for dir in source_ancestors {
                if path_is_in_directory(&fact.target_file, &dir) {
                    continue;
                }
                let accumulator = directories.entry(dir.clone()).or_default();
                accumulator.outgoing.add_fact(DirectionalDirectoryFact {
                    source_dir: dir,
                    target_dir: target_dir.clone(),
                    source_file: &fact.source_file,
                    target_file: &fact.target_file,
                    external_file: &fact.target_file,
                    edge_kind: fact.edge_kind,
                    symbol: fact.symbol.as_deref(),
                });
            }
            let target_ancestors =
                memoize_ancestors(&mut ancestors_cache, &fact.target_file).to_vec();
            for dir in target_ancestors {
                if path_is_in_directory(&fact.source_file, &dir) {
                    continue;
                }
                let accumulator = directories.entry(dir.clone()).or_default();
                accumulator.incoming.add_fact(DirectionalDirectoryFact {
                    source_dir: source_dir.clone(),
                    target_dir: dir,
                    source_file: &fact.source_file,
                    target_file: &fact.target_file,
                    external_file: &fact.source_file,
                    edge_kind: fact.edge_kind,
                    symbol: fact.symbol.as_deref(),
                });
            }
        }
    }

    directories
        .into_iter()
        .map(|(directory, accumulator)| accumulator.into_manifest(directory))
        .collect()
}

fn memoize_dirname<'cache>(
    cache: &'cache mut HashMap<String, String>,
    file: &str,
) -> &'cache String {
    if !cache.contains_key(file) {
        cache.insert(file.to_string(), module_path_dirname(file));
    }
    cache.get(file).expect("just inserted")
}

fn memoize_ancestors<'cache>(
    cache: &'cache mut HashMap<String, Vec<String>>,
    file: &str,
) -> &'cache [String] {
    if !cache.contains_key(file) {
        cache.insert(file.to_string(), directory_ancestors_for_file(file));
    }
    cache.get(file).expect("just inserted")
}

fn build_file_dependency_manifests(
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    file_metrics: &[OutputFileMetric],
) -> Vec<FileDependencyManifest> {
    let mut boundaries = BTreeMap::<String, FileAccumulator>::new();
    for decomposition in decomposition_by_chunk.values() {
        for fact in &decomposition.directory_dependency_facts {
            if fact.source_file == fact.target_file {
                continue;
            }
            boundaries
                .entry(fact.source_file.clone())
                .or_default()
                .outgoing
                .add_fact(&fact.target_file, fact.edge_kind, fact.symbol.as_deref());
            boundaries
                .entry(fact.target_file.clone())
                .or_default()
                .incoming
                .add_fact(&fact.source_file, fact.edge_kind, fact.symbol.as_deref());
        }
    }
    file_metrics
        .iter()
        .map(|metric| {
            let boundary = boundaries.remove(&metric.file).unwrap_or_default();
            FileDependencyManifest {
                path: metric.file.clone(),
                role: metric.role,
                bytes: metric.bytes,
                lines: metric.lines,
                chunk_id: metric.file.split('/').next().map(str::to_string),
                module: metric.module.clone(),
                incoming: boundary.incoming.into_file_summary(),
                outgoing: boundary.outgoing.into_file_summary(),
            }
        })
        .collect()
}

#[derive(Default)]
struct DirectoryAccumulator {
    directories: BTreeSet<String>,
    files: BTreeSet<String>,
    chunk_ids: BTreeSet<String>,
    modules: BTreeSet<ModuleRef>,
    defined_symbols: BTreeMap<String, usize>,
    loc: usize,
    incoming: DirectionalDirectoryAccumulator,
    outgoing: DirectionalDirectoryAccumulator,
}

impl DirectoryAccumulator {
    fn add_file(&mut self, directory: &str, metric: &OutputFileMetric) {
        if let Some((child, is_directory)) = direct_child_path(directory, &metric.file) {
            if is_directory {
                self.directories.insert(child);
            } else {
                self.files.insert(child);
            }
        }
        if let Some(chunk_id) = metric.file.split('/').next() {
            self.chunk_ids.insert(chunk_id.to_string());
        }
    }

    fn into_manifest(self, path: String) -> DirectoryManifestIndex {
        let incoming = self.incoming.into_summary().into();
        let outgoing = self.outgoing.into_summary().into();
        DirectoryManifestIndex {
            path,
            directories: self.directories.into_iter().collect(),
            files: self.files.into_iter().collect(),
            chunks: self.chunk_ids.into_iter().collect(),
            modules: self.modules.into_iter().collect(),
            defined_symbols: self.defined_symbols,
            loc: self.loc,
            incoming,
            outgoing,
        }
    }
}

#[derive(Default)]
struct DirectionalDirectoryAccumulator {
    edge_count: usize,
    edge_count_by_kind: BTreeMap<String, usize>,
    symbols: BTreeMap<String, usize>,
    external_files: BTreeMap<String, usize>,
    edges: BTreeMap<(String, String), DirectoryEdgeAccumulator>,
}

struct DirectionalDirectoryFact<'a> {
    source_dir: String,
    target_dir: String,
    source_file: &'a str,
    target_file: &'a str,
    external_file: &'a str,
    edge_kind: DepKind,
    symbol: Option<&'a str>,
}

impl DirectionalDirectoryAccumulator {
    fn add_fact(&mut self, fact: DirectionalDirectoryFact<'_>) {
        let kind: &'static str = fact.edge_kind.into();
        self.edge_count += 1;
        *self.edge_count_by_kind.entry(kind.to_string()).or_default() += 1;
        if let Some(symbol) = fact.symbol {
            *self.symbols.entry(symbol.to_string()).or_default() += 1;
        }
        *self
            .external_files
            .entry(fact.external_file.to_string())
            .or_default() += 1;
        self.edges
            .entry((fact.source_dir.clone(), fact.target_dir.clone()))
            .or_insert_with(|| DirectoryEdgeAccumulator {
                source_dir: fact.source_dir,
                target_dir: fact.target_dir,
                ..Default::default()
            })
            .add_fact(fact.source_file, fact.target_file, kind, fact.symbol);
    }

    fn into_summary(self) -> DirectionalSummary {
        DirectionalSummary {
            edge_count: self.edge_count,
            edge_count_by_kind: self.edge_count_by_kind,
            symbols: self.symbols,
            external_files: self.external_files,
            edges: self
                .edges
                .into_values()
                .map(|edge| edge.into_manifest())
                .collect(),
        }
    }
}

#[derive(Default)]
struct DirectoryEdgeAccumulator {
    source_dir: String,
    target_dir: String,
    edge_count: usize,
    edge_count_by_kind: BTreeMap<String, usize>,
    symbols: BTreeMap<String, usize>,
    source_files: BTreeMap<String, usize>,
    target_files: BTreeMap<String, usize>,
}

impl DirectoryEdgeAccumulator {
    fn add_fact(&mut self, source_file: &str, target_file: &str, kind: &str, symbol: Option<&str>) {
        self.edge_count += 1;
        *self.edge_count_by_kind.entry(kind.to_string()).or_default() += 1;
        *self
            .source_files
            .entry(source_file.to_string())
            .or_default() += 1;
        *self
            .target_files
            .entry(target_file.to_string())
            .or_default() += 1;
        if let Some(symbol) = symbol {
            *self.symbols.entry(symbol.to_string()).or_default() += 1;
        }
    }

    fn into_manifest(self) -> DirectoryDependencyEdgeManifest {
        DirectoryDependencyEdgeManifest {
            source_dir: self.source_dir,
            target_dir: self.target_dir,
            edge_count_by_kind: histogram_into_pairs(self.edge_count_by_kind),
            symbols: histogram_into_pairs(self.symbols),
            source_files: histogram_into_pairs(self.source_files),
            target_files: histogram_into_pairs(self.target_files),
        }
    }
}

/// Convert a `BTreeMap` histogram into a sorted `Vec<(key, count)>` pair
/// list. Equivalent ordering to iterating the `BTreeMap`, but cheaper to
/// serialize since serde emits a plain JSON array instead of opening and
/// closing a map for each per-edge field.
fn histogram_into_pairs(map: BTreeMap<String, usize>) -> EdgeHistogram {
    map.into_iter().collect()
}

struct DirectionalSummary {
    edge_count: usize,
    edge_count_by_kind: BTreeMap<String, usize>,
    symbols: BTreeMap<String, usize>,
    external_files: BTreeMap<String, usize>,
    edges: Vec<DirectoryDependencyEdgeManifest>,
}

impl From<DirectionalSummary> for DirectoryBoundarySummary {
    fn from(summary: DirectionalSummary) -> Self {
        Self {
            edge_count: summary.edge_count,
            edge_count_by_kind: histogram_into_pairs(summary.edge_count_by_kind),
            symbols: histogram_into_pairs(summary.symbols),
            files: histogram_into_pairs(summary.external_files),
            edges: summary.edges,
        }
    }
}

#[derive(Default)]
struct FileAccumulator {
    incoming: DirectionalFileAccumulator,
    outgoing: DirectionalFileAccumulator,
}

#[derive(Default)]
struct DirectionalFileAccumulator {
    edge_count: usize,
    edge_count_by_kind: BTreeMap<String, usize>,
    symbols: BTreeMap<String, usize>,
    files: BTreeMap<String, usize>,
}

impl DirectionalFileAccumulator {
    fn add_fact(&mut self, external_file: &str, edge_kind: DepKind, symbol: Option<&str>) {
        let kind: &'static str = edge_kind.into();
        self.edge_count += 1;
        *self.edge_count_by_kind.entry(kind.to_string()).or_default() += 1;
        if let Some(symbol) = symbol {
            *self.symbols.entry(symbol.to_string()).or_default() += 1;
        }
        *self.files.entry(external_file.to_string()).or_default() += 1;
    }

    fn into_file_summary(self) -> FileBoundarySummary {
        FileBoundarySummary {
            edge_count: self.edge_count,
            edge_count_by_kind: histogram_into_pairs(self.edge_count_by_kind),
            symbols: histogram_into_pairs(self.symbols),
            files: histogram_into_pairs(self.files),
        }
    }
}

fn directory_ancestors_for_file(file: &str) -> Vec<String> {
    let dir = module_path_dirname(file);
    let mut ancestors = vec![String::new()];
    if dir.is_empty() {
        return ancestors;
    }
    let mut current = String::new();
    for part in dir.split('/').filter(|part| !part.is_empty()) {
        if current.is_empty() {
            current.push_str(part);
        } else {
            current.push('/');
            current.push_str(part);
        }
        ancestors.push(current.clone());
    }
    ancestors
}

fn path_is_in_directory(path: &str, directory: &str) -> bool {
    directory.is_empty()
        || path == directory
        || path
            .strip_prefix(directory)
            .is_some_and(|rest| rest.starts_with('/'))
}

fn direct_child_path(directory: &str, file: &str) -> Option<(String, bool)> {
    let rest = if directory.is_empty() {
        file
    } else {
        file.strip_prefix(directory)?.strip_prefix('/')?
    };
    let mut parts = rest.split('/');
    let first = parts.next()?;
    Some((first.to_string(), parts.next().is_some()))
}

fn output_fraction(part: &OutputSize, total: &OutputSize) -> OutputFraction {
    OutputFraction {
        bytes: fraction(part.bytes, total.bytes),
        lines: fraction(part.lines, total.lines),
    }
}

fn size_for_role(files: &[OutputFileMetric], role: OutputRole) -> OutputSize {
    OutputSize::sum(files.iter().filter(|file| file.role == role))
}

fn largest_files_by_bytes(files: Vec<OutputFileMetric>) -> Vec<OutputFileMetric> {
    let mut sorted = files;
    sorted.sort_by(|left, right| {
        right
            .bytes
            .cmp(&left.bytes)
            .then_with(|| left.file.cmp(&right.file))
    });
    sorted.into_iter().take(20).collect()
}

fn fraction(part: usize, total: usize) -> f64 {
    if total == 0 {
        0.0
    } else {
        part as f64 / total as f64
    }
}
