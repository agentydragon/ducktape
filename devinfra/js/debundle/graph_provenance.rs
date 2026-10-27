//! Exact input bytes and path membership used to produce a saved dependency graph.
//!
//! Readers call `read_bytes` while a pipeline capture is active. This records
//! the bytes returned to the parser, rather than reopening the path after analysis.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

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
    pub fn new(reason: GraphMismatchReason, regenerate_command: Vec<String>) -> Self {
        Self {
            reason,
            regenerate_command,
        }
    }
}

impl fmt::Display for GraphMismatch {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "graph compatibility mismatch: {:?}; regenerate with {}",
            self.reason,
            self.regenerate_command
                .iter()
                .map(|arg| shell_quote(arg))
                .collect::<Vec<_>>()
                .join(" ")
        )
    }
}

impl std::error::Error for GraphMismatch {}

static CAPTURE: OnceLock<Mutex<Option<GraphProvenance>>> = OnceLock::new();

fn capture() -> &'static Mutex<Option<GraphProvenance>> {
    CAPTURE.get_or_init(|| Mutex::new(None))
}

pub struct InputCapture;

impl Drop for InputCapture {
    fn drop(&mut self) {
        capture().lock().expect("capture lock").take();
    }
}

impl InputCapture {
    pub fn start(roots: ProvenanceRoots, modules_directory: Option<PathBuf>) -> Result<Self> {
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
            generation_command: std::env::args().collect(),
        };
        {
            let mut capture = capture().lock().expect("capture lock");
            if capture.is_some() {
                bail!("nested graph input capture");
            }
            *capture = Some(provenance);
            Ok(Self)
        }
    }

    pub fn set_source_root(&self, path: &Path) -> Result<()> {
        let path = absolute(path)?;
        capture()
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
        let mut guard = capture().lock().expect("capture lock");
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
            let mut provenance = capture()
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
    let bytes = fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    {
        let mut guard = capture().lock().expect("capture lock");
        if let Some(provenance) = guard.as_mut() {
            let (root, relative) = provenance.classify(path)?;
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

pub fn record_absent(path: &Path) -> Result<()> {
    let mut guard = capture().lock().expect("capture lock");
    if let Some(provenance) = guard.as_mut() {
        let (root, path) = provenance.classify(path)?;
        provenance.absent_files.push(InputPath { root, path });
    }
    Ok(())
}

pub fn record_chunk_source(
    chunk_id: &str,
    source_path: &str,
    input_file: &Path,
    bytes: &[u8],
) -> Result<()> {
    let mut guard = capture().lock().expect("capture lock");
    if let Some(provenance) = guard.as_mut() {
        let (root, path) = provenance.classify(input_file)?;
        provenance.chunk_sources.push(ChunkSource {
            chunk_id: chunk_id.to_string(),
            source_path: PathBuf::from(source_path),
            input_file: InputPath { root, path },
            sha256: sha256(bytes),
        });
    }
    Ok(())
}

/// Record the exact list returned by the module-tree walk, including paths
/// outside the current graph chunk. Added/deleted YAMLs invalidate the graph.
pub fn record_yaml_membership(directory: &Path, members: &[PathBuf]) -> Result<()> {
    {
        let mut guard = capture().lock().expect("capture lock");
        if let Some(provenance) = guard.as_mut() {
            let (root, relative_dir) = provenance.classify(directory)?;
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
        Ok(())
    }
}

impl GraphProvenance {
    fn classify(&self, path: &Path) -> Result<(InputRoot, PathBuf)> {
        let absolute = absolute(path)?;
        let mut selected: Option<(InputRoot, PathBuf, usize)> = None;
        let mut consider = |kind: InputRoot, base: &Path| {
            if let Ok(relative) = absolute.strip_prefix(base) {
                let depth = base.components().count();
                if selected
                    .as_ref()
                    .is_none_or(|(_, _, previous)| depth > *previous)
                {
                    selected = Some((kind, relative.to_path_buf(), depth));
                }
            }
        };
        consider(InputRoot::Spec, &self.roots.spec);
        consider(InputRoot::Source, &self.roots.source);
        for (name, root) in &self.roots.packages {
            consider(InputRoot::Package(name.clone()), root);
        }
        if let Some((root, relative, _)) = selected {
            return Ok((root, relative));
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

    pub fn verify(&self, current: &CurrentRoots) -> Result<()> {
        let engine = engine_sha256()?;
        if engine != self.engine_sha256 {
            return Err(GraphMismatch::new(
                GraphMismatchReason::Engine {
                    expected: self.engine_sha256.clone(),
                    actual: engine,
                },
                self.generation_command.clone(),
            )
            .into());
        }
        for membership in &self.yaml_memberships {
            let dir = self.resolve(&membership.root, &membership.directory, current)?;
            let actual = collect_yaml(&dir)?;
            if actual != membership.members {
                return Err(GraphMismatch::new(
                    GraphMismatchReason::YamlMembership {
                        directory: dir,
                        expected: membership.members.clone(),
                        actual,
                    },
                    self.generation_command.clone(),
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
                    self.generation_command.clone(),
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
                    self.generation_command.clone(),
                )
                .into());
            }
        }
        Ok(())
    }

    pub fn regeneration_command(&self) -> String {
        self.generation_command
            .iter()
            .map(|arg| shell_quote(arg))
            .collect::<Vec<_>>()
            .join(" ")
    }
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
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .with_context(|| format!("invalid path {}", path.display()))?;
    Ok(fs::canonicalize(parent)?.join(name))
}

pub fn sha256(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
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
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hex(&hasher.finalize()))
}
