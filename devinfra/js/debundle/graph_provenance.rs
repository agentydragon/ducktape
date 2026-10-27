//! Exact input bytes and path membership used to produce a saved dependency graph.
//!
//! Readers call `read_bytes` while a pipeline capture is active. This records
//! the bytes returned to the parser, rather than reopening the path after analysis.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail};
use ring::digest::{self, SHA256};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputRoot {
    Spec,
    Source,
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
    pub packages: BTreeMap<String, PathBuf>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct GraphProvenance {
    pub roots: ProvenanceRoots,
    /// Full tree root relative to `roots.spec`; `None` for a flat spec.
    pub modules_directory: Option<PathBuf>,
    pub config_path: Option<PathBuf>,
    pub module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
    pub files: Vec<InputFile>,
    pub absent_files: Vec<InputPath>,
    pub chunk_sources: Vec<ChunkSource>,
    pub yaml_memberships: Vec<YamlMembership>,
    pub engine_sha256: String,
    pub generation_cwd: PathBuf,
    pub generation_command: Vec<String>,
}

/// Header projection that skips graph nodes/edges during compatibility checks.
/// Deserialization still scans the JSON stream, but it does not allocate the
/// large graph collections or recompute dependency analysis.
#[derive(Debug, Deserialize)]
pub struct GraphHeader {
    pub chunk_id: String,
    pub provenance: Option<GraphProvenance>,
}

pub fn read_graph_header(path: &Path) -> Result<GraphHeader> {
    let file = fs::File::open(path).with_context(|| format!("reading graph {}", path.display()))?;
    serde_json::from_reader(BufReader::new(file))
        .with_context(|| format!("parsing graph header {}", path.display()))
}

#[derive(Debug, Clone, Default)]
pub struct CurrentRoots {
    pub spec: Option<PathBuf>,
    pub source: Option<PathBuf>,
    pub packages: BTreeMap<String, PathBuf>,
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
    SelectorSource {
        path: PathBuf,
        chunk_id: String,
        expected: String,
        actual: String,
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
        write!(
            formatter,
            "graph compatibility mismatch: {:?}; regenerate with ",
            self.reason
        )?;
        if let Some(cwd) = &self.regenerate_cwd {
            write!(formatter, "cd {} && ", shell_quote(&cwd.to_string_lossy()))?;
        }
        write!(
            formatter,
            "{}",
            self.regenerate_command
                .iter()
                .map(|arg| shell_quote(arg))
                .collect::<Vec<_>>()
                .join(" ")
        )
    }
}

impl std::error::Error for GraphMismatch {}

type CaptureState = Arc<Mutex<Option<GraphProvenance>>>;

thread_local! {
    static ACTIVE_CAPTURE: RefCell<Option<CaptureState>> = const { RefCell::new(None) };
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
            files: Vec::new(),
            absent_files: Vec::new(),
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

    pub fn set_source_root(&self, path: &Path) -> Result<()> {
        let path = absolute(path)?;
        self.state
            .lock()
            .expect("capture lock")
            .as_mut()
            .expect("capture started")
            .roots
            .source = path;
        Ok(())
    }

    pub fn set_tree_scope(
        &self,
        config_path: &Path,
        roots: BTreeMap<String, Vec<PathBuf>>,
    ) -> Result<()> {
        let mut guard = self.state.lock().expect("capture lock");
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
        {
            let mut provenance = self
                .state
                .lock()
                .expect("capture lock")
                .take()
                .expect("capture started");
            provenance
                .files
                .sort_by(|a, b| (&a.root, &a.path).cmp(&(&b.root, &b.path)));
            provenance
                .absent_files
                .sort_by(|a, b| (&a.root, &a.path).cmp(&(&b.root, &b.path)));
            provenance
                .chunk_sources
                .sort_by(|a, b| a.chunk_id.cmp(&b.chunk_id));
            provenance
                .yaml_memberships
                .sort_by(|a, b| (&a.root, &a.directory).cmp(&(&b.root, &b.directory)));
            provenance.verify(&CurrentRoots::default())?;
            Ok(provenance)
        }
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
        let mut guard = state.lock().expect("capture lock");
        {
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
            if let Some(previous) = provenance
                .files
                .iter()
                .find(|f| f.root == entry.root && f.path == entry.path)
            {
                if previous.sha256 != entry.sha256 {
                    bail!(
                        "input {} changed during graph analysis; rerun on a stable snapshot",
                        path.display()
                    );
                }
            } else {
                provenance.files.push(entry);
            }
        }
    }
    Ok(bytes)
}

pub fn read_to_string(path: &Path) -> Result<String> {
    String::from_utf8(read_bytes(path)?).with_context(|| format!("{} is not UTF-8", path.display()))
}

/// Source-list and JS chunk reads carry an explicit role when spec and source
/// roots overlap, so moving only the source root does not silently reuse them.
pub fn read_source_to_string(path: &Path) -> Result<String> {
    String::from_utf8(read_bytes_with_role(path, ReadRole::Source)?)
        .with_context(|| format!("{} is not UTF-8", path.display()))
}

/// Package reads retain the resolver's semantic package name when multiple
/// names happen to point at the same physical directory.
pub fn read_package_to_string(name: &str, path: &Path) -> Result<String> {
    String::from_utf8(read_bytes_with_role(path, ReadRole::Package(name))?)
        .with_context(|| format!("{} is not UTF-8", path.display()))
}

pub fn record_absent(path: &Path) -> Result<()> {
    if let Some(state) = active_capture() {
        let mut guard = state.lock().expect("capture lock");
        {
            let provenance = guard
                .as_mut()
                .context("graph capture ended before absent-input check")?;
            let (root, path) = provenance.classify_spec(path)?;
            provenance.absent_files.push(InputPath { root, path });
        }
    }
    Ok(())
}

pub fn record_chunk_source(
    chunk_id: &str,
    source_path: &str,
    input_file: &Path,
    bytes: &[u8],
) -> Result<()> {
    if let Some(state) = active_capture() {
        let mut guard = state.lock().expect("capture lock");
        {
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
    }
    Ok(())
}

/// Record the exact list returned by the module-tree walk, including paths
/// outside the current graph chunk. Added/deleted YAMLs invalidate the graph.
pub fn record_yaml_membership(directory: &Path, members: &[PathBuf]) -> Result<()> {
    if let Some(state) = active_capture() {
        let mut guard = state.lock().expect("capture lock");
        {
            let provenance = guard
                .as_mut()
                .context("graph capture ended before YAML membership check")?;
            let (root, relative_dir) = provenance.classify_spec(directory)?;
            let mut relative_members = members
                .iter()
                .map(|p| {
                    p.strip_prefix(directory)
                        .map(Path::to_path_buf)
                        .with_context(|| {
                            format!("{} is outside {}", p.display(), directory.display())
                        })
                })
                .collect::<Result<Vec<_>>>()?;
            relative_members.sort();
            provenance.yaml_memberships.push(YamlMembership {
                root,
                directory: relative_dir,
                members: relative_members,
            });
        }
    }
    Ok(())
}

impl GraphProvenance {
    fn classify_package(&self, name: &str, path: &Path) -> Result<(InputRoot, PathBuf)> {
        let absolute = absolute(path)?;
        let (root_name, root) = self
            .roots
            .packages
            .get_key_value(name)
            .or_else(|| self.roots.packages.get_key_value("__packages_root__"))
            .with_context(|| format!("missing captured root for package {name}"))?;
        if let Ok(relative) = absolute.strip_prefix(root) {
            return Ok((
                InputRoot::Package(root_name.clone()),
                relative.to_path_buf(),
            ));
        }
        // A symlink can lead outside the package directory. Record the bytes
        // under the actual absolute path, which isolated snapshots reject.
        Ok((InputRoot::Absolute, absolute))
    }

    fn classify_source(&self, path: &Path) -> Result<(InputRoot, PathBuf)> {
        let absolute = absolute(path)?;
        if let Ok(relative) = absolute.strip_prefix(&self.roots.source) {
            return Ok((InputRoot::Source, relative.to_path_buf()));
        }
        Ok((InputRoot::Absolute, absolute))
    }

    fn classify_spec(&self, path: &Path) -> Result<(InputRoot, PathBuf)> {
        let absolute = absolute(path)?;
        if let Ok(relative) = absolute.strip_prefix(&self.roots.spec) {
            return Ok((InputRoot::Spec, relative.to_path_buf()));
        }
        Ok((InputRoot::Absolute, absolute))
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
        let relocated =
            current.spec.is_some() || current.source.is_some() || !current.packages.is_empty();
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
                        .get("__packages_root__")
                        .map(PathBuf::as_path)
                        .map(absolute)
                        .transpose()?
                        .unwrap_or(absolute(Path::new(&value))?),
                    "--out-root" => {
                        let original = absolute(Path::new(&value))?;
                        let mapped = self.translate_rooted_path(&original, current)?;
                        fresh_regeneration_output(&mapped)?
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

    fn translate_rooted_path(&self, path: &Path, current: &CurrentRoots) -> Result<PathBuf> {
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
        for (name, old) in &self.roots.packages {
            consider(old, current.packages.get(name).map(PathBuf::as_path))?;
        }
        Ok(selected.map_or(path, |(_, translated)| translated))
    }

    pub fn verify(&self, current: &CurrentRoots) -> Result<()> {
        let (regenerate_command, regenerate_cwd) = self.regeneration_recipe(current)?;
        let engine = engine_sha256()?;
        if engine != self.engine_sha256 {
            return Err(GraphMismatch::new(
                GraphMismatchReason::Engine {
                    expected: self.engine_sha256.clone(),
                    actual: engine,
                },
                regenerate_command.clone(),
                Some(regenerate_cwd.clone()),
            )
            .into());
        }
        for membership in &self.yaml_memberships {
            let dir = self.resolve(&membership.root, &membership.directory, current)?;
            if !dir.is_dir() {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::Directory {
                        path: dir.clone(),
                        expected: "directory".to_string(),
                        actual: if dir.exists() { "file" } else { "missing" }.to_string(),
                    },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
            let actual = collect_yaml(&dir)?;
            if actual != membership.members {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::YamlMembership {
                        directory: dir,
                        expected: membership.members.clone(),
                        actual,
                    },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
        }
        for file in &self.files {
            let path = self.resolve(&file.root, &file.path, current)?;
            let actual = match fs::read(&path) {
                Ok(bytes) => sha256(&bytes),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => "missing".to_string(),
                Err(err) => return Err(err).with_context(|| format!("reading {}", path.display())),
            };
            if actual != file.sha256 {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::File {
                        path,
                        expected: file.sha256.clone(),
                        actual,
                    },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
        }
        for absent in &self.absent_files {
            let path = self.resolve(&absent.root, &absent.path, current)?;
            if path.exists() {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::File {
                        path,
                        expected: "absent".to_string(),
                        actual: "present".to_string(),
                    },
                    regenerate_command.clone(),
                    Some(regenerate_cwd.clone()),
                )
                .into());
            }
        }
        Ok(())
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

fn shell_quote(value: &str) -> String {
    if value
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "_./:=+-".contains(c))
    {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

pub fn collect_yaml(root: &Path) -> Result<Vec<PathBuf>> {
    fn walk(root: &Path, dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
        for entry in fs::read_dir(dir).with_context(|| format!("reading {}", dir.display()))? {
            let path = entry?.path();
            if path.is_dir() {
                walk(root, &path, out)?;
            } else if matches!(
                path.extension().and_then(|e| e.to_str()),
                Some("yaml" | "yml")
            ) {
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
    let file = fs::File::open(std::env::current_exe()?)?;
    let mut reader = BufReader::new(file);
    let mut hasher = digest::Context::new(&SHA256);
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hex(hasher.finish().as_ref()))
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
    fn package_name_role_survives_equal_original_roots() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let package = temp.path().join("shared");
        fs::create_dir_all(&package)?;
        let metadata = package.join("package.json");
        fs::write(&metadata, "{}")?;
        let roots = ProvenanceRoots {
            spec: temp.path().to_path_buf(),
            source: temp.path().to_path_buf(),
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
        record_yaml_membership(&modules, &[yaml])?;
        record_absent(&modules.join("b.yaml"))?;
        let provenance = capture.finish()?;
        assert_eq!(provenance.files[0].root, InputRoot::Spec);
        assert_eq!(provenance.yaml_memberships[0].root, InputRoot::Spec);
        assert_eq!(provenance.absent_files[0].root, InputRoot::Spec);
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
