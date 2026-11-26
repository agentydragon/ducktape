//! Incremental command evidence and input snapshots within an owned case.

use super::failure_workspace::{CaseMetadata, copy_tree};
use super::process::{self, ProcessError};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};

const COMMANDS: &str = ".case-commands";
const INPUT_DIRECTORIES: [&str; 4] = ["snapshot", "modules", "extracted", "dependencies"];
static COMMAND_COUNTER: AtomicUsize = AtomicUsize::new(0);

#[derive(Deserialize, Serialize)]
pub struct RecordedCommand {
    pub id: String,
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, Option<String>>,
    pub clear_env: bool,
    pub debundle: bool,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub spawn_error: Option<String>,
    pub snapshot_errors: Vec<String>,
    pub external_inputs: BTreeMap<PathBuf, PathBuf>,
    pub outputs: Vec<OutputSnapshot>,
    pub graph_roots: Option<graph_provenance::ProvenanceRoots>,
}

#[derive(Deserialize, Serialize)]
pub struct OutputSnapshot {
    relative: PathBuf,
    was_directory: Option<bool>,
}

#[derive(Deserialize, Serialize)]
struct ToolIdentity {
    program: PathBuf,
    sha256: String,
}

fn json_error(error: impl std::error::Error + Send + Sync + 'static) -> io::Error {
    io::Error::other(error)
}

fn write_json(path: &Path, value: &impl Serialize) -> io::Result<()> {
    fs::write(path, serde_json::to_vec_pretty(value).map_err(json_error)?)
}

fn below(root: &Path, path: &Path) -> Option<PathBuf> {
    path.strip_prefix(root)
        .ok()
        .filter(|relative| {
            relative
                .components()
                .all(|component| matches!(component, Component::Normal(_)))
        })
        .map(Path::to_path_buf)
}

fn external_path(record: &mut RecordedCommand, path: &Path, namespace: &str) -> PathBuf {
    if let Some(relative) = record.external_inputs.iter().find_map(|(root, relative)| {
        below(root, path).map(|suffix| {
            if suffix.as_os_str().is_empty() {
                relative.clone()
            } else {
                relative.join(suffix)
            }
        })
    }) {
        return relative;
    }
    let relative = PathBuf::from(namespace).join(record.external_inputs.len().to_string());
    record
        .external_inputs
        .insert(path.to_path_buf(), relative.clone());
    relative
}

fn contains_journal(path: &Path, directory: &Path) -> bool {
    path.canonicalize()
        .is_ok_and(|path| directory.starts_with(path))
}

fn case_root(hint: &Path) -> Option<&Path> {
    hint.ancestors().find(|path| {
        fs::read(path.join("case.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<CaseMetadata>(&bytes).ok())
            .is_some_and(|metadata| metadata.original_root == *path)
    })
}

/// A command builder already carries concrete fixture paths. Find its owned
/// input/output path without introducing a process-wide current workspace.
pub(super) fn command_hint(command: &Command) -> PathBuf {
    command
        .get_args()
        .filter_map(|arg| arg.to_str())
        .find_map(|arg| {
            let path = Path::new(arg.split_once('=').map_or(arg, |(_, path)| path));
            case_root(path).map(Path::to_path_buf)
        })
        .unwrap_or_default()
}

/// A concrete path below a case associates the process with its owner. There is
/// no global or thread-local current-case state; unrelated commands use the raw
/// process runner without creating evidence directories.
pub fn fixture_output(command: &mut Command, hint: &Path) -> Result<Output, ProcessError> {
    output(command, hint, false, false)
}

pub fn debundle_fixture_output(command: &mut Command, hint: &Path) -> Result<Output, ProcessError> {
    output(command, hint, true, false)
}

pub(super) fn isolated_debundle_output(
    command: &mut Command,
    hint: &Path,
) -> Result<Output, ProcessError> {
    output(command, hint, true, true)
}

fn output(
    command: &mut Command,
    hint: &Path,
    debundle: bool,
    clear_env: bool,
) -> Result<Output, ProcessError> {
    let journal =
        case_root(hint).and_then(|root| match begin(root, command, debundle, clear_env) {
            Ok(record) => Some(record),
            Err(error) => {
                let _ = writeln!(io::stderr(), "could not record case command: {error}");
                None
            }
        });
    let output = process::output(command);
    if let Some((directory, mut record)) = journal {
        let result = (|| -> io::Result<()> {
            match &output {
                Ok(output) => {
                    fs::write(directory.join("stdout"), &output.stdout)?;
                    fs::write(directory.join("stderr"), &output.stderr)?;
                    record.exit_code = output.status.code();
                    record.signal = output.status.signal();
                }
                Err(error) => record.spawn_error = Some(error.to_string()),
            }
            write_json(&directory.join("command.json"), &record)
        })();
        if let Err(error) = result {
            let _ = writeln!(
                io::stderr(),
                "could not finish case command record: {error}"
            );
        }
    }
    let output = output?;
    if debundle {
        process::assert_debundle_invariants(&output);
    }
    Ok(output)
}

fn begin(
    root: &Path,
    command: &Command,
    debundle: bool,
    clear_env: bool,
) -> io::Result<(PathBuf, RecordedCommand)> {
    let id = format!("{:08}", COMMAND_COUNTER.fetch_add(1, Ordering::Relaxed));
    let directory = root.join(COMMANDS).join(&id);
    fs::create_dir_all(&directory)?;
    let mut env: BTreeMap<_, _> = command
        .get_envs()
        .map(|(key, value)| {
            (
                key.to_string_lossy().into_owned(),
                value.map(|value| value.to_string_lossy().into_owned()),
            )
        })
        .collect();
    if !clear_env && let Ok(value) = std::env::var("DEBUNDLE_TV_ORACLE") {
        env.entry("DEBUNDLE_TV_ORACLE".into())
            .or_insert(Some(value));
    }
    let mut record = RecordedCommand {
        id,
        program: command.get_program().into(),
        args: command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect(),
        cwd: command
            .get_current_dir()
            .map(Path::to_path_buf)
            .unwrap_or(std::env::current_dir()?),
        env,
        clear_env,
        debundle,
        exit_code: None,
        signal: None,
        spawn_error: None,
        snapshot_errors: vec![],
        external_inputs: BTreeMap::new(),
        outputs: vec![],
        graph_roots: None,
    };
    if debundle {
        snapshot_inputs(root, &directory, &mut record);
        snapshot_outputs(root, &directory, &mut record);
    }
    write_json(&directory.join("command.json"), &record)?;
    Ok((directory, record))
}

fn copy_snapshot(source: &Path, destination: &Path, was_directory: Option<bool>) -> io::Result<()> {
    match was_directory {
        Some(true) => copy_tree(source, destination),
        Some(false) => {
            fs::create_dir_all(destination.parent().unwrap())?;
            fs::copy(source, destination)?;
            Ok(())
        }
        None => Ok(()),
    }
}

fn snapshot_outputs(root: &Path, directory: &Path, record: &mut RecordedCommand) {
    let mut outputs = vec![];
    for pair in record.args.windows(2) {
        match pair[0].as_str() {
            "--out-root" | "--out" => outputs.push(PathBuf::from(&pair[1])),
            "--spec" => {
                if let Ok(bytes) = fs::read(&pair[1])
                    && let Ok(spec) = serde_yaml::from_slice::<serde_json::Value>(&bytes)
                {
                    for pointer in [
                        "/write_js_tree/out_dir",
                        "/materialize_logical_modules/report_out_dir",
                        "/emit_browser_harness/out_dir",
                    ] {
                        if let Some(path) = spec.pointer(pointer).and_then(|value| value.as_str()) {
                            outputs.push(PathBuf::from(path));
                        }
                    }
                }
            }
            _ => {}
        }
    }
    outputs.sort();
    outputs.dedup();
    let mut parents = vec![];
    for output in outputs {
        if parents
            .iter()
            .any(|parent: &PathBuf| output.starts_with(parent))
        {
            continue;
        }
        parents.push(output);
    }
    for output in parents {
        let output = record.cwd.join(output);
        if contains_journal(&output, directory) {
            record.snapshot_errors.push(format!(
                "output {} contains the command journal",
                output.display()
            ));
            continue;
        }
        let relative = match below(root, &output) {
            Some(relative) => relative,
            None => external_path(record, &output, "external-output"),
        };
        let destination = directory
            .join("outputs")
            .join(record.outputs.len().to_string());
        let was_directory = output.exists().then(|| output.is_dir());
        if let Err(error) = copy_snapshot(&output, &destination, was_directory) {
            record
                .snapshot_errors
                .push(format!("{}: {error}", output.display()));
        }
        record.outputs.push(OutputSnapshot {
            relative,
            was_directory,
        });
    }
}

fn snapshot_inputs(root: &Path, directory: &Path, record: &mut RecordedCommand) {
    // Source and author-owned spec data are small public fixtures. Do not copy
    // generated output trees on every launch. Graphs/artifacts are copied only
    // when this command actually consumes them.
    let mut inputs = INPUT_DIRECTORIES.map(|path| root.join(path)).to_vec();
    if let Ok(entries) = fs::read_dir(root) {
        inputs.extend(entries.flatten().map(|entry| entry.path()).filter(|path| {
            path.is_file()
                && path.extension().is_some_and(|extension| {
                    matches!(extension.to_str(), Some("yaml" | "yml" | "txt"))
                })
        }));
    }
    for pair in record.args.windows(2) {
        let path = match pair[0].as_str() {
            "--spec"
            | "--analysis"
            | "--graph"
            | "--projection"
            | "--tree-config"
            | "--tree-modules"
            | "--tree-vendor-marks"
            | "--packages-root" => Some(pair[1].as_str()),
            "--package-root" => pair[1].split_once('=').map(|(_, path)| path),
            _ => None,
        };
        if let Some(path) = path {
            inputs.push(PathBuf::from(path));
        }
    }
    for name in [
        "DEBUNDLE_GRAPH",
        "DEBUNDLE_ANALYSIS",
        "DEBUNDLE_CONFIG",
        "DEBUNDLE_VENDOR_MARKS",
    ] {
        if let Some(Some(path)) = record.env.get(name) {
            inputs.push(PathBuf::from(path));
        }
    }
    let graph = record
        .args
        .windows(2)
        .find(|pair| pair[0] == "--graph")
        .map(|pair| PathBuf::from(&pair[1]))
        .or_else(|| {
            record
                .env
                .get("DEBUNDLE_GRAPH")
                .and_then(|value| value.as_ref())
                .map(PathBuf::from)
        });
    if let Some(graph) = graph
        && let Ok(header) = graph_provenance::read_graph_header(&graph)
    {
        if let Ok(program) =
            graph_provenance::program_graph_path(&graph, &header.program_graph).canonicalize()
        {
            inputs.push(program);
        }
        if let Some(provenance) = header.provenance {
            inputs.extend(provenance.roots.packages.values().cloned());
            record.graph_roots = Some(provenance.roots);
        }
    }
    // Valid input blocks can name source trees and loader manifests outside the
    // owner. Inspect this block separately so malformed unrelated spec keys
    // remain available to the CLI's own validation.
    for input in inputs.clone() {
        if input
            .extension()
            .is_some_and(|extension| extension == "yaml")
            && let Ok(bytes) = fs::read(&input)
            && let Ok(spec) = serde_yaml::from_slice::<serde_json::Value>(&bytes)
            && let Some(value) = spec.get("inputs")
            && let Ok(paths) = serde_json::from_value::<spec::LoadJsChunksArgs>(value.clone())
        {
            inputs.extend([paths.input_root, paths.js_list_path]);
            inputs.extend(paths.embedded_loaders_path);
        }
    }
    inputs.sort();
    inputs.dedup();
    for input in inputs {
        let input = record.cwd.join(input);
        if contains_journal(&input, directory) {
            record.snapshot_errors.push(format!(
                "input {} contains the command journal",
                input.display()
            ));
            continue;
        }
        if !input.exists() {
            continue;
        }
        let relative = match below(root, &input) {
            Some(path) => path,
            None => external_path(record, &input, "external"),
        };
        let destination = directory.join("inputs").join(relative);
        if let Err(error) = copy_snapshot(&input, &destination, Some(input.is_dir())) {
            record
                .snapshot_errors
                .push(format!("{}: {error}", input.display()));
        }
    }
}

pub fn recorded_commands(case: &Path) -> io::Result<Vec<RecordedCommand>> {
    let mut commands = vec![];
    for entry in fs::read_dir(case.join(COMMANDS))? {
        let path = entry?.path().join("command.json");
        commands
            .push(serde_json::from_slice::<RecordedCommand>(&fs::read(path)?).map_err(json_error)?);
    }
    commands.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(commands)
}

pub(super) fn retain_tool_identities(case: &Path) -> io::Result<()> {
    if !case.join(COMMANDS).is_dir() {
        return Ok(());
    }
    let mut tools = BTreeMap::new();
    for record in recorded_commands(case)? {
        if !tools.contains_key(&record.program) {
            let sha256 = graph_provenance::sha256_reader(fs::File::open(&record.program)?)?;
            tools.insert(
                record.program.clone(),
                ToolIdentity {
                    program: record.program,
                    sha256,
                },
            );
        }
    }
    write_json(
        &case.join("tools.json"),
        &tools.into_values().collect::<Vec<_>>(),
    )
}

/// Replay one recorded command in a fresh copy, restoring its pre-command
/// source/spec/artifact inputs. Supply the executable explicitly; verify its
/// identity unless the caller deliberately opts into a tool change.
pub fn replay_command(
    case: &Path,
    id: &str,
    destination: &Path,
    binary: &Path,
    allow_tool_change: bool,
) -> io::Result<Output> {
    let record = recorded_commands(case)?
        .into_iter()
        .find(|record| record.id == id)
        .ok_or_else(|| io::Error::other(format!("no recorded command {id}")))?;
    if !record.snapshot_errors.is_empty() {
        return Err(io::Error::other(format!(
            "incomplete input snapshot: {:?}",
            record.snapshot_errors
        )));
    }
    let tools: Vec<ToolIdentity> =
        serde_json::from_slice(&fs::read(case.join("tools.json"))?).map_err(json_error)?;
    let identity = tools
        .iter()
        .find(|tool| tool.program == record.program)
        .ok_or_else(|| io::Error::other("recorded executable identity is missing"))?;
    if !allow_tool_change
        && graph_provenance::sha256_reader(fs::File::open(binary)?)? != identity.sha256
    {
        return Err(io::Error::other(
            "replay executable differs; pass --allow-tool-change to compare versions",
        ));
    }
    let case = case.canonicalize()?;
    if destination.exists() {
        return Err(io::Error::other("replay destination must be fresh"));
    }
    fs::create_dir_all(
        destination
            .parent()
            .ok_or_else(|| io::Error::other("destination needs a parent"))?,
    )?;
    let destination = destination
        .parent()
        .unwrap()
        .canonicalize()?
        .join(destination.file_name().unwrap());
    if destination.starts_with(&case) {
        return Err(io::Error::other(
            "replay destination must be outside retained evidence",
        ));
    }
    copy_tree(&case, &destination)?;
    let inputs = case.join(COMMANDS).join(id).join("inputs");
    if inputs.is_dir() {
        // Restore complete source/module directories, including deletions, before
        // overlaying command-specific files and external dependencies.
        for name in INPUT_DIRECTORIES {
            if inputs.join(name).is_dir() && destination.join(name).is_dir() {
                fs::remove_dir_all(destination.join(name))?;
            }
        }
        copy_tree(&inputs, &destination)?;
    }
    for (index, output) in record.outputs.iter().enumerate() {
        if output.relative.is_absolute()
            || output
                .relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(io::Error::other(
                "output snapshot path must stay below the replay workspace",
            ));
        }
        let path = destination.join(&output.relative);
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if metadata.is_dir() {
                fs::remove_dir_all(&path)?;
            } else {
                fs::remove_file(&path)?;
            }
        }
        let snapshot = case
            .join(COMMANDS)
            .join(id)
            .join("outputs")
            .join(index.to_string());
        copy_snapshot(&snapshot, &path, output.was_directory)?;
    }
    let metadata: CaseMetadata =
        serde_json::from_slice(&fs::read(case.join("case.json"))?).map_err(json_error)?;
    let relocate = |value: &str| -> String {
        let path = Path::new(value);
        if let Some(relative) = record
            .external_inputs
            .iter()
            .find_map(|(original, relative)| {
                below(original, path).map(|suffix| {
                    if suffix.as_os_str().is_empty() {
                        relative.clone()
                    } else {
                        relative.join(suffix)
                    }
                })
            })
        {
            destination.join(relative).to_string_lossy().into_owned()
        } else if let Some(relative) = below(&metadata.original_root, path) {
            destination.join(relative).to_string_lossy().into_owned()
        } else {
            value.to_owned()
        }
    };
    relocate_specs(&destination, &relocate)?;
    let mut command = Command::new(binary);
    if record.clear_env {
        command.env_clear();
    }
    command.current_dir(if record.cwd.starts_with(&metadata.original_root) {
        PathBuf::from(relocate(&record.cwd.to_string_lossy()))
    } else {
        destination.clone()
    });
    for (name, value) in &record.env {
        if let Some(value) = value {
            command.env(name, relocate(value));
        } else {
            command.env_remove(name);
        }
    }
    if let Some(roots) = &record.graph_roots {
        for (flag, env, root) in [
            ("--spec-root", "DEBUNDLE_SPEC_ROOT", &roots.spec),
            (
                "--graph-source-root",
                "DEBUNDLE_GRAPH_SOURCE_ROOT",
                &roots.source,
            ),
        ] {
            if !record.args.iter().any(|arg| arg == flag) && !record.env.contains_key(env) {
                command.env(env, relocate(&root.to_string_lossy()));
            }
        }
        if !record.args.iter().any(|arg| arg == "--graph-package-root") {
            for (name, root) in &roots.packages {
                command.args([
                    "--graph-package-root",
                    &format!("{name}={}", relocate(&root.to_string_lossy())),
                ]);
            }
        }
    }
    for (index, arg) in record.args.iter().enumerate() {
        let package = index > 0
            && matches!(
                record.args[index - 1].as_str(),
                "--package-root" | "--graph-package-root"
            );
        command.arg(
            if (package || arg.starts_with("--"))
                && let Some((package, path)) = arg.split_once('=')
            {
                format!("{package}={}", relocate(path))
            } else {
                relocate(arg)
            },
        );
    }
    let output = process::output(&mut command).map_err(json_error)?;
    if record.debundle {
        process::assert_debundle_invariants(&output);
    }
    Ok(output)
}

fn relocate_specs(root: &Path, relocate: &impl Fn(&str) -> String) -> io::Result<()> {
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let path = entry.path();
            if entry.file_type()?.is_dir() {
                if entry.file_name() != COMMANDS {
                    pending.push(path);
                }
                continue;
            }
            if !path
                .extension()
                .is_some_and(|extension| extension == "yaml" || extension == "yml")
            {
                continue;
            }
            let bytes = fs::read(&path)?;
            let Ok(mut spec) = serde_yaml::from_slice::<serde_json::Value>(&bytes) else {
                continue;
            };
            // Only schema path fields change. Preserve raw unknown keys and JS
            // strings, even when their contents resemble a temporary path.
            let fields = [
                "/inputs/input_root",
                "/inputs/js_list_path",
                "/inputs/embedded_loaders_path",
                "/materialize_logical_modules/report_out_dir",
                "/write_js_tree/out_dir",
                "/emit_browser_harness/out_dir",
                "/emit_browser_harness/snapshot_root",
                "/emit_browser_harness/asset_summary_path",
            ];
            let mut changed = false;
            for field in fields {
                if let Some(value) = spec.pointer_mut(field)
                    && let Some(path) = value.as_str()
                {
                    let next = relocate(path);
                    changed |= next != path;
                    *value = next.into();
                }
            }
            if changed {
                fs::write(path, serde_yaml::to_string(&spec).map_err(json_error)?)?;
            }
        }
    }
    Ok(())
}
