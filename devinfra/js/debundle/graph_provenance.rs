//! Exact input bytes and path membership used to produce a saved dependency graph.
//!
//! Readers call `read_bytes` while a pipeline capture is active. This records
//! the bytes returned to the parser, rather than reopening the path after analysis.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::fs;
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};

use anyhow::{Context, Result, bail};
use ring::digest::{self, SHA256};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputRoot {
    Spec,
    Source,
    OwnershipSubset,
    Package(String),
    Absolute,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct InputFile {
    pub root: InputRoot,
    pub path: PathBuf,
    pub sha256: String,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct InputPath {
    pub root: InputRoot,
    pub path: PathBuf,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ChunkSource {
    pub chunk_id: String,
    pub source_path: PathBuf,
    /// Path whose bytes the parser consumed; source_path may be a JS-list alias.
    pub input_file: InputPath,
    pub sha256: String,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct YamlMembership {
    pub root: InputRoot,
    pub directory: PathBuf,
    pub members: Vec<PathBuf>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProvenanceRoots {
    pub spec: PathBuf,
    pub source: PathBuf,
    /// Parent directory for the exact `--ownership-subset` input, when used.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ownership_subset: Option<PathBuf>,
    pub packages: BTreeMap<String, PathBuf>,
}

/// Normalized selected ownership claims recorded in both graph and provenance.
/// Module paths are chunk-local strings; report `unit:N` identifiers are never
/// persisted as authoring coordinates.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct SelectedOwnershipModule {
    pub module: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bindings: Option<std::collections::BTreeSet<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub anonymous_statements: Option<std::collections::BTreeSet<usize>>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct GraphProvenance {
    pub roots: ProvenanceRoots,
    /// Full tree root relative to `roots.spec`; `None` for a flat spec.
    pub modules_directory: Option<PathBuf>,
    pub config_path: Option<PathBuf>,
    pub module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
    /// Normalized selection used for each materialized chunk. Its exact input
    /// file bytes are independently fingerprinted in `files`.
    #[serde(default)]
    pub selected_ownership_by_chunk: BTreeMap<String, Vec<SelectedOwnershipModule>>,
    pub files: Vec<InputFile>,
    /// Package subpaths whose existence as a directory affects analysis.
    /// Snapshot replay recreates these directories without copying their contents.
    #[serde(default)]
    pub present_directories: Vec<InputPath>,
    pub chunk_sources: Vec<ChunkSource>,
    pub yaml_memberships: Vec<YamlMembership>,
    pub engine_sha256: String,
    pub generation_cwd: PathBuf,
    pub generation_command: Vec<String>,
}

/// A saved graph's chunk, its `program_graph` reference as recorded, and the
/// input provenance from that program graph.
#[derive(Debug)]
pub struct GraphHeader {
    pub chunk_id: String,
    pub program_graph: PathBuf,
    pub provenance: Option<GraphProvenance>,
    pub selected_ownership: Option<Vec<SelectedOwnershipModule>>,
}

/// Header projections that skip graph nodes/edges and the program census
/// during compatibility checks. Deserialization still scans each JSON stream,
/// but it does not allocate the large collections or recompute analysis.
#[derive(Deserialize)]
struct ChunkGraphHeader {
    chunk_id: String,
    program_graph: PathBuf,
    #[serde(default)]
    ownership_subset: Option<Vec<SelectedOwnershipModule>>,
}

#[derive(Deserialize)]
struct ProgramGraphHeader {
    provenance: Option<GraphProvenance>,
}

/// Resolve a chunk graph's `program_graph` reference, which is relative to the
/// graph file's directory.
pub fn program_graph_path(graph_path: &Path, program_graph: &Path) -> PathBuf {
    graph_path
        .parent()
        .unwrap_or_else(|| Path::new(""))
        .join(program_graph)
}

fn read_json_header<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    report_io::read_json_report(path)
        .with_context(|| format!("parsing header of {}", path.display()))
}

/// Read the companion program graph and check it describes the chunk's selected owners.
pub fn read_program_provenance(
    path: &Path,
    program_graph: &Path,
    chunk_id: &str,
    ownership_subset: Option<&[SelectedOwnershipModule]>,
) -> Result<Option<GraphProvenance>> {
    let program: ProgramGraphHeader = read_json_header(&program_graph_path(path, program_graph))?;
    if let Some(provenance) = &program.provenance {
        let expected = provenance.selected_ownership_by_chunk.get(chunk_id);
        anyhow::ensure!(
            ownership_subset == expected.map(Vec::as_slice),
            "dependency graph selected ownership for chunk {} does not match its program provenance",
            chunk_id
        );
    } else {
        anyhow::ensure!(
            ownership_subset.is_none(),
            "dependency graph selected ownership has no program provenance"
        );
    }
    Ok(program.provenance)
}

pub fn read_graph_header(path: &Path) -> Result<GraphHeader> {
    let graph: ChunkGraphHeader = read_json_header(path)?;
    let provenance = read_program_provenance(
        path,
        &graph.program_graph,
        &graph.chunk_id,
        graph.ownership_subset.as_deref(),
    )?;
    Ok(GraphHeader {
        selected_ownership: graph.ownership_subset,
        chunk_id: graph.chunk_id,
        program_graph: graph.program_graph,
        provenance,
    })
}

#[derive(Debug, Clone, Default)]
pub struct CurrentRoots {
    pub spec: Option<PathBuf>,
    pub source: Option<PathBuf>,
    pub ownership_subset: Option<PathBuf>,
    pub packages: BTreeMap<String, PathBuf>,
}

/// The source bytes and physical path verified for one graph's chunk.
/// `source_path` remains the graph/selector identity; `input_file` is the
/// physical file that supplied those bytes to analysis.
#[derive(Debug, Clone)]
pub struct VerifiedGraphSourceContext {
    chunk_id: String,
    source_path: PathBuf,
    input_file: PathBuf,
    modules_root: Option<PathBuf>,
    sha256: String,
    source: Arc<str>,
    tree_config: Option<Arc<[u8]>>,
    tree_config_path: Option<PathBuf>,
    selected_ownership: Option<Vec<SelectedOwnershipModule>>,
    ownership_subset: Option<(PathBuf, Arc<[u8]>)>,
    regeneration_command: Vec<String>,
    current_roots: CurrentRoots,
}

impl VerifiedGraphSourceContext {
    pub fn chunk_id(&self) -> &str {
        &self.chunk_id
    }

    pub fn source_path(&self) -> &Path {
        &self.source_path
    }

    pub fn input_file(&self) -> &Path {
        &self.input_file
    }

    /// The full authored modules tree root recorded for this graph, when the
    /// graph came from a tree spec. Callers may receive an exact chunk subtree
    /// as their `--modules` argument; after compatibility verification they
    /// can use this root to interpret the configured chunk scope consistently.
    pub fn modules_root(&self) -> Option<&Path> {
        self.modules_root.as_deref()
    }

    /// Authoring configuration retained from the same fingerprint-verified read.
    pub fn tree_config(&self) -> Option<&[u8]> {
        self.tree_config.as_deref()
    }

    /// Physical path corresponding to the retained, fingerprint-verified bytes.
    pub fn tree_config_path(&self) -> Option<&Path> {
        self.tree_config_path.as_deref()
    }

    pub fn selected_ownership(&self) -> Option<&[SelectedOwnershipModule]> {
        self.selected_ownership.as_deref()
    }

    /// Exact selected-view input from the same read that verified its graph
    /// fingerprint. A staged candidate must not replace these baseline bytes.
    pub fn ownership_subset_input(&self) -> Option<(&Path, &[u8])> {
        self.ownership_subset
            .as_ref()
            .map(|(path, bytes)| (path.as_path(), bytes.as_ref()))
    }

    /// Canonical run arguments with every input root relocated by verification.
    /// Consumers parse these through the ordinary CLI, rather than inventing a
    /// second interpretation of the transform's source and package context.
    pub fn regeneration_command(&self) -> &[String] {
        &self.regeneration_command
    }

    pub fn current_roots(&self) -> &CurrentRoots {
        &self.current_roots
    }

    pub fn sha256(&self) -> &str {
        &self.sha256
    }

    pub fn source(&self) -> &str {
        &self.source
    }

    pub fn matches_source_path(&self, source_path: &str) -> bool {
        self.source_path == Path::new(source_path)
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct GraphMismatch {
    #[serde(flatten)]
    pub reason: GraphMismatchReason,
    pub regenerate_command: Vec<String>,
    pub regenerate_cwd: Option<PathBuf>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum GraphMismatchReason {
    MissingProvenance {
        graph: PathBuf,
    },
    Engine {
        expected: String,
        actual: String,
    },
    File {
        path: PathBuf,
        expected: String,
        actual: String,
    },
    Directory {
        path: PathBuf,
        expected: String,
        actual: String,
    },
    YamlMembership {
        directory: PathBuf,
        expected: Vec<PathBuf>,
        actual: Vec<PathBuf>,
    },
    ModulesRoot {
        expected: PathBuf,
        actual: PathBuf,
    },
    ModuleScope {
        chunk_id: String,
        expected: Vec<PathBuf>,
        actual: PathBuf,
    },
    ConfigPath {
        expected: PathBuf,
        actual: PathBuf,
    },
    SelectorSourceRoot {
        chunk_id: String,
        expected: PathBuf,
        actual: PathBuf,
    },
}

impl GraphMismatch {
    pub fn new(
        reason: GraphMismatchReason,
        regenerate_command: Vec<String>,
        regenerate_cwd: Option<PathBuf>,
    ) -> Self {
        Self {
            reason,
            regenerate_command,
            regenerate_cwd,
        }
    }
}

impl fmt::Display for GraphMismatch {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.reason {
            GraphMismatchReason::Engine { expected, actual } => write!(
                formatter,
                "graph compatibility mismatch: saved graph engine digest is {expected}, running engine digest is {actual}; use the matching saved engine or regenerate with the running engine using: "
            )?,
            reason => write!(
                formatter,
                "graph compatibility mismatch: {reason:?}; regenerate with "
            )?,
        }
        // Arguments come from argv and paths, which cannot hold the NUL byte that
        // is the only input `shlex` refuses to quote.
        if let Some(cwd) = &self.regenerate_cwd {
            let cwd = shlex::try_quote(&cwd.to_string_lossy())
                .map_err(|_| fmt::Error)?
                .into_owned();
            write!(formatter, "cd {cwd} && ")?;
        }
        let command = shlex::try_join(self.regenerate_command.iter().map(String::as_str))
            .map_err(|_| fmt::Error)?;
        write!(formatter, "{command}")
    }
}

impl std::error::Error for GraphMismatch {}

type CaptureState = Arc<Mutex<Option<GraphProvenance>>>;

thread_local! {
    static ACTIVE_CAPTURE: RefCell<Option<CaptureState>> = const { RefCell::new(None) };
}

/// The shared capture state with its lock held. The state is still the
/// caller's `Option`: a handle panics when the capture has ended, while the
/// ambient recorders report it as an error. Poisoning means a recorder
/// panicked mid-capture—a bug rather than a runtime condition—so every site
/// takes the lock through here and says so with the same words.
fn lock_capture(state: &CaptureState) -> MutexGuard<'_, Option<GraphProvenance>> {
    state.lock().expect("capture lock")
}

fn active_capture() -> Option<CaptureState> {
    ACTIVE_CAPTURE.with(|slot| slot.borrow().clone())
}

/// Explicit context passed to Rayon jobs; worker threads do not inherit
/// thread-local state from the pipeline caller.
pub struct CaptureContext(Option<CaptureState>);

pub fn capture_context() -> CaptureContext {
    CaptureContext(active_capture())
}

pub fn with_capture_context<T>(context: &CaptureContext, body: impl FnOnce() -> T) -> T {
    struct Restore(Option<CaptureState>);
    impl Drop for Restore {
        fn drop(&mut self) {
            ACTIVE_CAPTURE.with(|slot| *slot.borrow_mut() = self.0.take());
        }
    }
    let previous = ACTIVE_CAPTURE.with(|slot| slot.replace(context.0.clone()));
    let _restore = Restore(previous);
    body()
}

pub struct InputCapture {
    state: CaptureState,
}

impl Drop for InputCapture {
    fn drop(&mut self) {
        ACTIVE_CAPTURE.with(|slot| {
            let is_current = slot
                .borrow()
                .as_ref()
                .is_some_and(|state| Arc::ptr_eq(state, &self.state));
            if is_current {
                *slot.borrow_mut() = None;
            }
        });
    }
}

impl InputCapture {
    pub fn start(
        roots: ProvenanceRoots,
        modules_directory: Option<PathBuf>,
        generation_command: Vec<String>,
    ) -> Result<Self> {
        let provenance = GraphProvenance {
            roots,
            modules_directory,
            config_path: None,
            module_roots_by_chunk: BTreeMap::new(),
            selected_ownership_by_chunk: BTreeMap::new(),
            files: Vec::new(),
            present_directories: Vec::new(),
            chunk_sources: Vec::new(),
            yaml_memberships: Vec::new(),
            engine_sha256: engine_sha256()?,
            generation_cwd: std::env::current_dir()?,
            generation_command,
        };
        let state = Arc::new(Mutex::new(Some(provenance)));
        ACTIVE_CAPTURE.with(|slot| {
            if slot.borrow().is_some() {
                bail!("nested graph input capture");
            }
            *slot.borrow_mut() = Some(state.clone());
            Ok(Self { state })
        })
    }

    /// Record bytes already read by the caller so capture and parsing use the
    /// same snapshot. The subset is rooted separately because Bazel and local
    /// callers commonly keep it outside the spec tree.
    pub fn record_ownership_subset(&self, path: &Path, bytes: &[u8]) -> Result<()> {
        let path = absolute(path)?;
        let parent = path
            .parent()
            .context("ownership subset input has no parent directory")?
            .to_path_buf();
        let relative = path
            .strip_prefix(&parent)
            .context("ownership subset input is outside its parent directory")?
            .to_path_buf();
        let mut guard = lock_capture(&self.state);
        let provenance = guard.as_mut().expect("capture started");
        provenance.roots.ownership_subset = Some(parent);
        provenance.files.push(InputFile {
            root: InputRoot::OwnershipSubset,
            path: relative,
            sha256: sha256(bytes),
        });
        Ok(())
    }

    pub fn set_selected_ownership(
        &self,
        selected: BTreeMap<String, Vec<SelectedOwnershipModule>>,
    ) -> Result<()> {
        let mut guard = lock_capture(&self.state);
        guard
            .as_mut()
            .expect("capture started")
            .selected_ownership_by_chunk = selected;
        Ok(())
    }

    pub fn set_tree_scope(
        &self,
        config_path: &Path,
        roots: BTreeMap<String, Vec<PathBuf>>,
    ) -> Result<()> {
        let mut guard = lock_capture(&self.state);
        let provenance = guard.as_mut().expect("capture started");
        let config = absolute(config_path)?;
        let relative = config
            .strip_prefix(&provenance.roots.spec)
            .with_context(|| {
                format!(
                    "tree config {} is outside spec root {}",
                    config.display(),
                    provenance.roots.spec.display()
                )
            })?;
        provenance.config_path = Some(relative.to_path_buf());
        provenance.module_roots_by_chunk = roots;
        Ok(())
    }

    pub fn finish(self) -> Result<GraphProvenance> {
        let provenance = self.finish_inputs()?;
        provenance.verify(&CurrentRoots::default())?;
        Ok(provenance)
    }

    /// The captured inputs without `finish`'s replay check, for `debundle
    /// analyze`: its provenance keys an analysis artifact (engine, parser
    /// inputs) and is never replayed as a `debundle run`.
    pub fn finish_inputs(self) -> Result<GraphProvenance> {
        let mut provenance = lock_capture(&self.state).take().expect("capture started");
        provenance
            .files
            .sort_by(|a, b| (&a.root, &a.path).cmp(&(&b.root, &b.path)));
        provenance
            .present_directories
            .sort_by(|a, b| (&a.root, &a.path).cmp(&(&b.root, &b.path)));
        provenance
            .chunk_sources
            .sort_by(|a, b| a.chunk_id.cmp(&b.chunk_id));
        provenance
            .yaml_memberships
            .sort_by(|a, b| (&a.root, &a.directory).cmp(&(&b.root, &b.directory)));
        Ok(provenance)
    }
}

pub fn read_bytes(path: &Path) -> Result<Vec<u8>> {
    read_bytes_with_role(path, ReadRole::Spec)
}

#[derive(Clone, Copy)]
enum ReadRole<'a> {
    Spec,
    Source,
    Package(&'a str),
}

fn read_bytes_with_role(path: &Path, role: ReadRole<'_>) -> Result<Vec<u8>> {
    let bytes = fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    if let Some(state) = active_capture() {
        let mut guard = lock_capture(&state);
        let provenance = guard
            .as_mut()
            .context("graph capture ended before input read")?;
        let (root, relative) = match role {
            ReadRole::Spec => provenance.classify_spec(path)?,
            ReadRole::Source => provenance.classify_source(path)?,
            ReadRole::Package(name) => provenance.classify_package(name, path)?,
        };
        let entry = InputFile {
            root,
            path: relative,
            sha256: sha256(&bytes),
        };
        provenance.record_file(entry, path)?;
    }
    Ok(bytes)
}

/// Text for bytes already recorded as an input, naming the path the caller read.
fn to_text(bytes: Vec<u8>, path: &Path) -> Result<String> {
    String::from_utf8(bytes).with_context(|| format!("{} is not UTF-8", path.display()))
}

pub fn read_to_string(path: &Path) -> Result<String> {
    to_text(read_bytes(path)?, path)
}

/// Source-list and JS chunk reads carry an explicit role when spec and source
/// roots overlap, so moving only the source root does not silently reuse them.
pub fn read_source_to_string(path: &Path) -> Result<String> {
    to_text(read_bytes_with_role(path, ReadRole::Source)?, path)
}

/// Package reads retain the resolver's semantic package name when multiple
/// names happen to point at the same physical directory.
pub fn read_package_to_string(name: &str, path: &Path) -> Result<String> {
    to_text(read_bytes_with_role(path, ReadRole::Package(name))?, path)
}

/// Capture the package subpath kind and, for files, its opaque bytes even when
/// the pipeline does not need to inspect those bytes (for example, an import
/// target validated for a bundled vendor replacement).
pub fn record_package_input(name: &str, path: &Path) -> Result<()> {
    let Some(state) = active_capture() else {
        return Ok(());
    };
    let (root, relative) = {
        let guard = lock_capture(&state);
        guard
            .as_ref()
            .context("graph capture ended before package input check")?
            .classify_package(name, path)?
    };

    let metadata = fs::metadata(path).with_context(|| format!("reading {}", path.display()))?;
    if metadata.is_dir() {
        let mut guard = lock_capture(&state);
        let provenance = guard
            .as_mut()
            .context("graph capture ended before package input check")?;
        let entry = InputPath {
            root,
            path: relative,
        };
        if !provenance.present_directories.contains(&entry) {
            provenance.present_directories.push(entry);
        }
        return Ok(());
    }
    anyhow::ensure!(
        metadata.is_file(),
        "package subpath is not a regular file or directory: {}",
        path.display()
    );

    let mut file = fs::File::open(path).with_context(|| format!("reading {}", path.display()))?;
    let entry = InputFile {
        root,
        path: relative,
        sha256: sha256_reader(&mut file).with_context(|| format!("hashing {}", path.display()))?,
    };
    let mut guard = lock_capture(&state);
    let provenance = guard
        .as_mut()
        .context("graph capture ended before package input check")?;
    provenance.record_file(entry, path)
}

pub fn record_chunk_source(
    chunk_id: &str,
    source_path: &str,
    input_file: &Path,
    bytes: &[u8],
) -> Result<()> {
    if let Some(state) = active_capture() {
        let mut guard = lock_capture(&state);
        let provenance = guard
            .as_mut()
            .context("graph capture ended before source mapping")?;
        let (root, path) = provenance.classify_source(input_file)?;
        provenance.chunk_sources.push(ChunkSource {
            chunk_id: chunk_id.to_string(),
            source_path: PathBuf::from(source_path),
            input_file: InputPath { root, path },
            sha256: sha256(bytes),
        });
    }
    Ok(())
}

/// Discover every YAML input under the directory. Verification uses the same
/// relative paths even when a caller only parses a subset as module specs.
pub fn yaml_files(directory: &Path) -> Result<Vec<PathBuf>> {
    let members = relative_yaml_files(directory)?;
    if let Some(state) = active_capture() {
        let mut guard = lock_capture(&state);
        let provenance = guard
            .as_mut()
            .context("graph capture ended before YAML membership check")?;
        let (root, relative_dir) = provenance.classify_spec(directory)?;
        provenance.yaml_memberships.push(YamlMembership {
            root,
            directory: relative_dir,
            members: members.clone(),
        });
    }
    Ok(members
        .into_iter()
        .map(|path| directory.join(path))
        .collect())
}

impl GraphProvenance {
    /// Record one captured input file. The same root and path must keep the same
    /// bytes for the whole run: a different hash means the tree moved while the
    /// pipeline was reading it, and the captured graph would not replay.
    fn record_file(&mut self, entry: InputFile, path: &Path) -> Result<()> {
        let already_recorded = self
            .files
            .iter()
            .position(|file| file.root == entry.root && file.path == entry.path);
        match already_recorded {
            Some(index) if self.files[index].sha256 != entry.sha256 => bail!(
                "input {} changed during graph analysis; rerun on a stable snapshot",
                path.display()
            ),
            Some(_) => Ok(()),
            None => {
                self.files.push(entry);
                Ok(())
            }
        }
    }

    /// The authored flat-spec or tree-config path recorded when the graph was
    /// captured. Flat specs do not populate `config_path`; their canonical
    /// `--spec` argument is retained in the generation command and is already
    /// the source of truth for replay.
    pub fn author_spec_path(&self) -> Result<PathBuf> {
        if let Some(config_path) = &self.config_path {
            return Ok(self.roots.spec.join(config_path));
        }
        let spec_arg = self
            .generation_command
            .iter()
            .position(|argument| argument == "--spec")
            .and_then(|index| self.generation_command.get(index + 1))
            .context("graph provenance has no recorded flat spec path for author input evidence")?;
        Ok(PathBuf::from(spec_arg))
    }

    /// Where a read belongs relative to one captured root: bytes inside the
    /// root are recorded relative to it, and anything else - a symlink leading
    /// outside, a file that predates the capture - is recorded under its actual
    /// absolute path, which isolated snapshots reject.
    fn classify_under(
        &self,
        root: InputRoot,
        root_dir: &Path,
        path: &Path,
    ) -> Result<(InputRoot, PathBuf)> {
        let absolute = absolute(path)?;
        if let Ok(relative) = absolute.strip_prefix(root_dir) {
            return Ok((root, relative.to_path_buf()));
        }
        Ok((InputRoot::Absolute, absolute))
    }

    fn classify_package(&self, name: &str, path: &Path) -> Result<(InputRoot, PathBuf)> {
        let (root_name, root) = self
            .roots
            .packages
            .get_key_value(name)
            .or_else(|| self.roots.packages.get_key_value(SHARED_PACKAGES_ROOT))
            .with_context(|| format!("missing captured root for package {name}"))?;
        self.classify_under(InputRoot::Package(root_name.clone()), root, path)
    }

    fn classify_source(&self, path: &Path) -> Result<(InputRoot, PathBuf)> {
        self.classify_under(InputRoot::Source, &self.roots.source, path)
    }

    fn classify_spec(&self, path: &Path) -> Result<(InputRoot, PathBuf)> {
        self.classify_under(InputRoot::Spec, &self.roots.spec, path)
    }

    pub fn resolve(
        &self,
        root: &InputRoot,
        path: &Path,
        current: &CurrentRoots,
    ) -> Result<PathBuf> {
        if matches!(root, InputRoot::Absolute) {
            anyhow::ensure!(
                path.is_absolute(),
                "absolute graph input path is relative: {}",
                path.display()
            );
            return Ok(path.to_path_buf());
        }
        validate_relative_path(path)?;
        let base = match root {
            InputRoot::Spec => current.spec.as_ref().unwrap_or(&self.roots.spec),
            InputRoot::Source => current.source.as_ref().unwrap_or(&self.roots.source),
            InputRoot::OwnershipSubset => current
                .ownership_subset
                .as_ref()
                .or(self.roots.ownership_subset.as_ref())
                .context("missing ownership subset root")?,
            InputRoot::Package(name) => current
                .packages
                .get(name)
                .or_else(|| self.roots.packages.get(name))
                .with_context(|| format!("missing package root {name}"))?,
            InputRoot::Absolute => unreachable!(),
        };
        Ok(base.join(path))
    }

    /// Replay the recorded effective run against explicitly relocated roots.
    /// The stored argv uses absolute paths and is the source of the effective
    /// options; only declared path arguments are translated.
    pub fn regeneration_recipe(&self, current: &CurrentRoots) -> Result<(Vec<String>, PathBuf)> {
        for name in current.packages.keys() {
            anyhow::ensure!(
                self.roots.packages.contains_key(name),
                "graph package root {name} was not captured; available names: {}",
                self.roots
                    .packages
                    .keys()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
        anyhow::ensure!(
            self.generation_command
                .get(1)
                .is_some_and(|arg| arg == "run"),
            "graph has no replayable debundle run command"
        );
        let relocated = current.spec.is_some()
            || current.source.is_some()
            || current.ownership_subset.is_some()
            || !current.packages.is_empty();
        let cwd = if relocated {
            absolute(current.spec.as_deref().unwrap_or(&self.roots.spec))?
        } else {
            self.generation_cwd.clone()
        };
        let mut argv = self.generation_command.clone();
        argv[0] = absolute(&std::env::current_exe()?)?
            .to_string_lossy()
            .into_owned();
        let mut index = 2;
        while index < argv.len() {
            let flag = argv[index].clone();
            if matches!(
                flag.as_str(),
                "--tree-config"
                    | "--tree-modules"
                    | "--tree-vendor-marks"
                    | "--spec"
                    | "--tree-source-root"
                    | "--ownership-subset"
                    | "--out-root"
                    | "--package-root"
                    | "--packages-root"
            ) {
                let value = argv
                    .get(index + 1)
                    .with_context(|| format!("missing value for {flag}"))?
                    .clone();
                let translated = match flag.as_str() {
                    "--tree-source-root" => {
                        absolute(current.source.as_deref().unwrap_or(&self.roots.source))?
                    }
                    "--ownership-subset" => {
                        let original = absolute(Path::new(&value))?;
                        let root = self
                            .roots
                            .ownership_subset
                            .as_deref()
                            .context("ownership subset root was not captured")?;
                        let relative = original.strip_prefix(root).with_context(|| {
                            format!(
                                "recorded ownership subset {} is outside captured root {}",
                                original.display(),
                                root.display()
                            )
                        })?;
                        absolute(current.ownership_subset.as_deref().unwrap_or(root))?
                            .join(relative)
                    }
                    "--package-root" => {
                        let (name, root) = value
                            .split_once('=')
                            .context("invalid recorded --package-root")?;
                        let root = current
                            .packages
                            .get(name)
                            .map(PathBuf::as_path)
                            .unwrap_or(Path::new(root));
                        argv[index + 1] = format!("{name}={}", absolute(root)?.display());
                        index += 2;
                        continue;
                    }
                    "--packages-root" => current
                        .packages
                        .get(SHARED_PACKAGES_ROOT)
                        .map(PathBuf::as_path)
                        .map(absolute)
                        .transpose()?
                        .unwrap_or(absolute(Path::new(&value))?),
                    "--out-root" => {
                        let original = absolute(Path::new(&value))?;
                        let mapped = self.translate_rooted_path(&original, current)?;
                        let previous = match mapped {
                            Some(mapped) => mapped,
                            None => match current.spec.as_deref() {
                                Some(spec) => absolute(spec)?.join("debundle-output"),
                                None => original,
                            },
                        };
                        fresh_regeneration_output(&previous)?
                    }
                    _ => {
                        let original = absolute(Path::new(&value))?;
                        let relative =
                            original.strip_prefix(&self.roots.spec).with_context(|| {
                                format!(
                                    "recorded spec input {} is outside the spec root",
                                    original.display()
                                )
                            })?;
                        absolute(current.spec.as_deref().unwrap_or(&self.roots.spec))?
                            .join(relative)
                    }
                };
                argv[index + 1] = translated.to_string_lossy().into_owned();
                index += 2;
            } else {
                index += 1;
            }
        }
        Ok((argv, cwd))
    }

    fn translate_rooted_path(
        &self,
        path: &Path,
        current: &CurrentRoots,
    ) -> Result<Option<PathBuf>> {
        let path = absolute(path)?;
        let mut selected: Option<(usize, PathBuf)> = None;
        let mut consider = |old: &Path, new: Option<&Path>| -> Result<()> {
            if let Some(new) = new
                && let Ok(relative) = path.strip_prefix(old)
            {
                let depth = old.components().count();
                if selected
                    .as_ref()
                    .is_none_or(|(previous, _)| depth > *previous)
                {
                    selected = Some((depth, absolute(new)?.join(relative)));
                }
            }
            Ok(())
        };
        consider(&self.roots.spec, current.spec.as_deref())?;
        consider(&self.roots.source, current.source.as_deref())?;
        if let Some(old) = &self.roots.ownership_subset {
            consider(old, current.ownership_subset.as_deref())?;
        }
        for (name, old) in &self.roots.packages {
            consider(old, current.packages.get(name).map(PathBuf::as_path))?;
        }
        Ok(selected.map(|(_, translated)| translated))
    }

    pub fn verify(&self, current: &CurrentRoots) -> Result<()> {
        self.verify_with_source_context(current, None).map(|_| ())
    }

    pub fn verify_chunk_source(
        &self,
        current: &CurrentRoots,
        chunk_id: &str,
    ) -> Result<VerifiedGraphSourceContext> {
        self.verify_with_source_context(current, Some(chunk_id))?
            .context("verified graph source context was not requested")
    }

    fn verify_with_source_context(
        &self,
        current: &CurrentRoots,
        chunk_id: Option<&str>,
    ) -> Result<Option<VerifiedGraphSourceContext>> {
        let (regenerate_command, regenerate_cwd) = self.regeneration_recipe(current)?;
        let mismatch = |reason| {
            anyhow::Error::from(GraphMismatch::new(
                reason,
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            ))
        };
        let require_directory = |path: &Path| -> Result<()> {
            if !path.is_dir() {
                return Err(mismatch(GraphMismatchReason::Directory {
                    path: path.to_owned(),
                    expected: "directory".to_string(),
                    actual: if path.exists() { "file" } else { "missing" }.to_string(),
                }));
            }
            Ok(())
        };
        let engine = engine_sha256()?;
        if engine != self.engine_sha256 {
            return Err(mismatch(GraphMismatchReason::Engine {
                expected: self.engine_sha256.clone(),
                actual: engine,
            }));
        }
        let mut sources_by_chunk = BTreeMap::new();
        for source in &self.chunk_sources {
            anyhow::ensure!(
                sources_by_chunk
                    .insert(source.chunk_id.as_str(), source)
                    .is_none(),
                "graph provenance has multiple source mappings for chunk {}",
                source.chunk_id
            );
        }
        let source = if let Some(chunk_id) = chunk_id {
            let source = sources_by_chunk.get(chunk_id).copied().with_context(|| {
                format!("graph provenance lacks source mapping for chunk {chunk_id}")
            })?;
            let mut matching_files = self.files.iter().filter(|file| {
                file.root == source.input_file.root && file.path == source.input_file.path
            });
            let input_file = matching_files.next().with_context(|| {
                format!(
                    "captured source input {} is missing from graph fingerprints",
                    source.input_file.path.display()
                )
            })?;
            anyhow::ensure!(
                matching_files.next().is_none(),
                "captured source input {} has multiple graph fingerprints",
                source.input_file.path.display()
            );
            anyhow::ensure!(
                input_file.sha256 == source.sha256,
                "captured source digest differs from its graph input fingerprint for chunk {chunk_id}"
            );
            Some((source, input_file))
        } else {
            None
        };
        let mut verified_source = None;
        let mut verified_tree_config = None;
        let mut verified_ownership_subset = None;
        for membership in &self.yaml_memberships {
            let dir = self.resolve(&membership.root, &membership.directory, current)?;
            require_directory(&dir)?;
            let actual = relative_yaml_files(&dir)?;
            if actual != membership.members {
                return Err(mismatch(GraphMismatchReason::YamlMembership {
                    directory: dir,
                    expected: membership.members.clone(),
                    actual,
                }));
            }
        }
        for directory in &self.present_directories {
            let path = self.resolve(&directory.root, &directory.path, current)?;
            require_directory(&path)?;
        }
        for file in &self.files {
            let path = self.resolve(&file.root, &file.path, current)?;
            let (actual, contents) = match fs::read(&path) {
                Ok(contents) => (sha256(&contents), Some(contents)),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    ("missing".to_string(), None)
                }
                Err(err) => return Err(err).with_context(|| format!("reading {}", path.display())),
            };
            if file.root == InputRoot::Spec && self.config_path.as_ref() == Some(&file.path) {
                verified_tree_config = contents.as_deref().map(Arc::from);
            }
            if file.root == InputRoot::OwnershipSubset {
                anyhow::ensure!(
                    verified_ownership_subset.is_none(),
                    "graph provenance has multiple selected-view inputs"
                );
                if let Some(contents) = &contents {
                    verified_ownership_subset =
                        Some((absolute(&path)?, Arc::from(contents.as_slice())));
                }
            }
            if actual != file.sha256 {
                return Err(mismatch(GraphMismatchReason::File {
                    path,
                    expected: file.sha256.clone(),
                    actual,
                }));
            }
            // The selected fingerprint was checked against the chunk's digest
            // above. Retain the same bytes we just hashed and their physical path.
            if source.is_some_and(|(_, selected)| std::ptr::eq(selected, file)) {
                verified_source = contents.map(|bytes| (path, bytes));
            }
        }
        let Some((source, _)) = source else {
            return Ok(None);
        };
        let (input_file, bytes) = verified_source.with_context(|| {
            format!(
                "verified graph input did not retain source bytes for chunk {}",
                source.chunk_id
            )
        })?;
        let modules_root = self
            .modules_directory
            .as_deref()
            .map(|directory| self.resolve(&InputRoot::Spec, directory, current))
            .transpose()?
            .map(|path| absolute(&path))
            .transpose()?;
        Ok(Some(VerifiedGraphSourceContext {
            chunk_id: source.chunk_id.clone(),
            source_path: source.source_path.clone(),
            input_file: absolute(&input_file)?,
            modules_root,
            tree_config: verified_tree_config,
            tree_config_path: self
                .config_path
                .as_deref()
                .map(|path| self.resolve(&InputRoot::Spec, path, current))
                .transpose()?
                .map(|path| absolute(&path))
                .transpose()?,
            selected_ownership: self
                .selected_ownership_by_chunk
                .get(&source.chunk_id)
                .cloned(),
            ownership_subset: verified_ownership_subset,
            regeneration_command: regenerate_command,
            current_roots: current.clone(),
            sha256: source.sha256.clone(),
            source: Arc::from(
                String::from_utf8(bytes).context("captured JavaScript source is not UTF-8")?,
            ),
        }))
    }
}

fn fresh_regeneration_output(previous: &Path) -> Result<PathBuf> {
    let parent = previous
        .parent()
        .context("recorded output root has no parent directory")?;
    let name = previous
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "debundle-output".to_string());
    for suffix in 0..1000 {
        let candidate = if suffix == 0 {
            parent.join(format!("{name}-regenerated"))
        } else {
            parent.join(format!("{name}-regenerated-{suffix}"))
        };
        if !candidate.try_exists()? {
            return Ok(candidate);
        }
    }
    anyhow::bail!(
        "cannot find an unused regeneration output beside {}",
        previous.display()
    )
}

pub fn validate_relative_path(path: &Path) -> Result<()> {
    use std::path::Component;
    anyhow::ensure!(
        !path.is_absolute()
            && path
                .components()
                .all(|part| matches!(part, Component::Normal(_))),
        "invalid relative graph input path {}",
        path.display()
    );
    Ok(())
}

fn relative_yaml_files(root: &Path) -> Result<Vec<PathBuf>> {
    fn walk(root: &Path, dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
        for entry in fs::read_dir(dir).with_context(|| format!("reading {}", dir.display()))? {
            let path = entry?.path();
            if path.is_dir() {
                walk(root, &path, out)?;
                continue;
            }
            // The module loader accepts any UTF-8 name ending in ".yaml",
            // including the dotfile for which Path::extension() returns None.
            if path.file_name().is_some_and(|name| name == ".yaml")
                || matches!(
                    path.extension().and_then(|e| e.to_str()),
                    Some("yaml" | "yml")
                )
            {
                out.push(path.strip_prefix(root)?.to_path_buf());
            }
        }
        Ok(())
    }
    let mut out = Vec::new();
    walk(root, root, &mut out)?;
    out.sort();
    Ok(out)
}

/// Key used for the shared package fallback in captured and relocated provenance roots.
pub const SHARED_PACKAGES_ROOT: &str = "__packages_root__";

pub fn capture_package_roots(
    named: &HashMap<String, PathBuf>,
    shared: Option<&Path>,
) -> Result<BTreeMap<String, PathBuf>> {
    anyhow::ensure!(
        !named.contains_key(SHARED_PACKAGES_ROOT),
        "package name {SHARED_PACKAGES_ROOT} is reserved for the shared packages root in graph provenance"
    );
    let mut packages = BTreeMap::new();
    for (name, path) in named {
        packages.insert(name.clone(), absolute(path)?);
    }
    if let Some(path) = shared {
        packages.insert(SHARED_PACKAGES_ROOT.to_owned(), absolute(path)?);
    }
    Ok(packages)
}

pub fn absolute(path: &Path) -> Result<PathBuf> {
    if path.exists() {
        return fs::canonicalize(path)
            .with_context(|| format!("canonicalizing {}", path.display()));
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .with_context(|| format!("invalid path {}", path.display()))?;
    Ok(absolute(parent)?.join(name))
}

pub fn sha256(bytes: &[u8]) -> String {
    hex(digest::digest(&SHA256, bytes).as_ref())
}

pub fn sha256_reader(reader: impl Read) -> io::Result<String> {
    sha256_copy(reader, io::sink())
}

pub fn sha256_copy(mut reader: impl Read, mut writer: impl Write) -> io::Result<String> {
    let mut hasher = digest::Context::new(&SHA256);
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
        writer.write_all(&buffer[..count])?;
    }
    Ok(hex(hasher.finish().as_ref()))
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

fn engine_sha256() -> Result<String> {
    // The running engine is immutable for this process. Rehash source/package
    // inputs on every verification, but hash the executable only once. A new
    // process still computes its own digest before reusing saved provenance.
    static ENGINE: OnceLock<String> = OnceLock::new();
    if let Some(digest) = ENGINE.get() {
        return Ok(digest.clone());
    }
    let file = fs::File::open(std::env::current_exe()?)?;
    let digest = sha256_reader(BufReader::new(file)).context("hashing debundle executable")?;
    Ok(ENGINE.get_or_init(|| digest).clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};

    #[test]
    fn sha256_matches_standard_vectors() {
        assert_eq!(
            sha256(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha256_copy_hashes_and_copies_short_reads() -> std::io::Result<()> {
        struct ShortReader<R> {
            inner: R,
            max_read: usize,
        }

        impl<R: Read> Read for ShortReader<R> {
            fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
                let limit = buffer.len().min(self.max_read);
                self.inner.read(&mut buffer[..limit])
            }
        }

        let bytes: Vec<u8> = (0..200_000).map(|byte| (byte % 251) as u8).collect();
        let reader = ShortReader {
            inner: std::io::Cursor::new(&bytes),
            max_read: 997,
        };
        let mut copied = Vec::new();

        let actual = sha256_copy(reader, &mut copied)?;

        assert_eq!(copied, bytes);
        assert_eq!(
            actual,
            "e24bc62381f1224fbbb74688663f8f9743b9680b193edd666835e97b06e730eb"
        );
        Ok(())
    }

    #[test]
    fn sha256_copy_propagates_reader_and_writer_errors() {
        struct FailingReader;

        impl Read for FailingReader {
            fn read(&mut self, _buffer: &mut [u8]) -> std::io::Result<usize> {
                Err(std::io::Error::from(std::io::ErrorKind::ConnectionReset))
            }
        }

        struct FailingWriter;

        impl Write for FailingWriter {
            fn write(&mut self, _buffer: &[u8]) -> std::io::Result<usize> {
                Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe))
            }

            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }

        assert_eq!(
            sha256_copy(FailingReader, std::io::sink())
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::ConnectionReset
        );
        assert_eq!(
            sha256_copy(&b"input"[..], FailingWriter)
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::BrokenPipe
        );
    }

    #[test]
    fn older_provenance_defaults_package_directory_inputs() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let capture = InputCapture::start(
            ProvenanceRoots {
                spec: temp.path().to_path_buf(),
                source: temp.path().to_path_buf(),
                ownership_subset: None,
                packages: BTreeMap::new(),
            },
            None,
            vec![
                std::env::current_exe()?.to_string_lossy().into_owned(),
                "run".to_string(),
                "--spec".to_string(),
                temp.path().join("spec.yaml").to_string_lossy().into_owned(),
            ],
        )?;
        let provenance = capture.finish()?;
        let mut value = serde_json::to_value(provenance)?;
        value
            .as_object_mut()
            .context("serialized provenance is not an object")?
            .remove("present_directories");
        let older: GraphProvenance = serde_json::from_value(value)?;
        assert!(older.present_directories.is_empty());
        Ok(())
    }

    #[test]
    fn package_name_role_survives_equal_original_roots() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let package = temp.path().join("shared");
        fs::create_dir_all(&package)?;
        let metadata = package.join("package.json");
        fs::write(&metadata, "{}")?;
        let roots = ProvenanceRoots {
            spec: temp.path().to_path_buf(),
            source: temp.path().to_path_buf(),
            ownership_subset: None,
            packages: BTreeMap::from([
                ("a".to_string(), package.clone()),
                ("b".to_string(), package.clone()),
            ]),
        };
        let capture = InputCapture::start(
            roots,
            None,
            vec![
                std::env::current_exe()?.to_string_lossy().into_owned(),
                "run".to_string(),
                "--spec".to_string(),
                temp.path().join("spec.yaml").to_string_lossy().into_owned(),
            ],
        )?;
        assert_eq!(read_package_to_string("b", &metadata)?, "{}");
        let provenance = capture.finish()?;
        assert_eq!(
            provenance.files[0].root,
            InputRoot::Package("b".to_string())
        );
        let moved = temp.path().join("moved");
        fs::create_dir_all(&moved)?;
        fs::write(moved.join("package.json"), "{\"changed\":true}")?;
        let mismatch = provenance
            .verify(&CurrentRoots {
                packages: BTreeMap::from([("b".to_string(), moved.clone())]),
                ..CurrentRoots::default()
            })
            .unwrap_err();
        assert!(matches!(
            mismatch.downcast_ref::<GraphMismatch>().map(|m| &m.reason),
            Some(GraphMismatchReason::File { path, .. }) if path == &moved.join("package.json")
        ));
        let unknown = provenance
            .verify(&CurrentRoots {
                packages: BTreeMap::from([("typo".to_string(), moved)]),
                ..CurrentRoots::default()
            })
            .unwrap_err();
        assert!(unknown.to_string().contains("was not captured"));
        Ok(())
    }

    #[test]
    fn authored_yaml_keeps_spec_role_inside_deeper_source_root() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let modules = temp.path().join("modules/main");
        fs::create_dir_all(&modules)?;
        let yaml = modules.join("a.yaml");
        fs::write(&yaml, "members: []\n")?;
        let capture = InputCapture::start(
            ProvenanceRoots {
                spec: temp.path().to_path_buf(),
                source: modules.clone(),
                ownership_subset: None,
                packages: BTreeMap::new(),
            },
            Some(PathBuf::from("modules")),
            vec![
                std::env::current_exe()?.to_string_lossy().into_owned(),
                "run".to_string(),
                "--spec".to_string(),
                temp.path().join("spec.yaml").to_string_lossy().into_owned(),
            ],
        )?;
        read_to_string(&yaml)?;
        assert_eq!(yaml_files(&modules)?, [yaml]);
        let provenance = capture.finish()?;
        assert_eq!(provenance.files[0].root, InputRoot::Spec);
        assert_eq!(provenance.yaml_memberships[0].root, InputRoot::Spec);
        Ok(())
    }

    #[test]
    fn simultaneous_captures_keep_worker_reads_in_their_own_runs() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let barrier = Arc::new(Barrier::new(2));
        let mut workers = Vec::new();
        for index in 0..2 {
            let root = temp.path().join(format!("run-{index}"));
            fs::create_dir_all(&root)?;
            fs::write(root.join("source.js"), format!("const value = {index};"))?;
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || -> Result<(String, String)> {
                let capture = InputCapture::start(
                    ProvenanceRoots {
                        spec: root.clone(),
                        source: root.clone(),
                        ownership_subset: None,
                        packages: BTreeMap::new(),
                    },
                    None,
                    vec!["debundle".to_string(), "run".to_string()],
                )?;
                barrier.wait();
                let context = capture_context();
                let path = root.join("source.js");
                let read = std::thread::spawn(move || {
                    with_capture_context(&context, || read_bytes(&path))
                });
                let bytes = read.join().expect("worker thread")?;
                barrier.wait();
                let provenance = capture.finish()?;
                Ok((
                    sha256(&bytes),
                    provenance
                        .files
                        .first()
                        .expect("recorded source")
                        .sha256
                        .clone(),
                ))
            }));
        }
        for worker in workers {
            let (read_digest, captured_digest) = worker.join().expect("pipeline thread")?;
            assert_eq!(read_digest, captured_digest);
        }
        Ok(())
    }
}
