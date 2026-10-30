//! Resolved command provenance for replay and delegation records.

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::io::IsTerminal;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::builder::ArgAction;
use clap::parser::ValueSource;
use clap::{ArgMatches, Command};
use serde::Serialize;

/// One parsed argument and the value source that won Clap's CLI-over-env
/// precedence. Paths remain relative to `cwd`, which is included in the
/// record and applied by `shell_command`.
#[derive(Debug, Clone, Serialize)]
pub struct ResolvedArgument {
    pub name: String,
    pub source: ArgumentSource,
    pub value: ArgumentValue,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArgumentSource {
    CommandLine,
    Environment { name: String },
    Default,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArgumentValue {
    Paths(Vec<PathBuf>),
    PackageRoots(Vec<PackageRoot>),
    Values(Vec<String>),
    Flag(bool),
}

#[derive(Debug, Clone, Serialize)]
pub struct PackageRoot {
    pub package: String,
    pub path: PathBuf,
}

/// The operation class and checks requested by a command. These describe the
/// call being made; they never claim that validation completed successfully.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InvocationMode {
    GraphSnapshotRead,
    DiagnosticSnapshotRead,
    FreshPipeline,
    SnapshotCreation,
    SnapshotVerification,
    ValidatedEdit,
    UnvalidatedEdit,
    SourceSnapshotRead,
    ReadOnlyAnalysis,
    Conditional,
    Unclassified,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RequestedProof {
    None,
    GraphSnapshotInput,
    DiagnosticSnapshotInput,
    FullPipelineRequested,
    PostEditGateRequested,
    NamesOnlyCheckRequested,
    SelectorValidationRequested,
    SourceValidationRequested,
    SnapshotCreationRequested,
    SnapshotVerificationRequested,
    Conditional,
    Unclassified,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case", tag = "scope")]
pub enum WriteScope {
    /// A transform's output paths are declared inside the supplied flat spec.
    FlatSpec { spec: PathBuf },
    /// Tree transforms write into their explicitly selected output root.
    TreeOutput { root: PathBuf },
    /// A spec authoring command may update YAML below this module tree.
    ModuleTree { root: PathBuf },
    /// The CLI writes this invocation record before dispatching the command.
    InvocationRecord { path: PathBuf },
    /// `spec validate` captures reports in a temporary directory that is
    /// removed when the command returns.
    TemporaryCapture,
    /// A gate may create or clear rejection reports beside this graph.
    ConditionalGateReports { graph: PathBuf },
    /// A dry-run pipeline may write rejection reports into the configured
    /// report area but never emits the transformed tree.
    ConditionalPipelineReports {
        spec: Option<PathBuf>,
        output_root: Option<PathBuf>,
    },
    /// `snapshot create` writes a self-contained snapshot at this destination.
    WorkerSnapshot { destination: PathBuf },
    /// An authoring command writes a draft for review at this path, never
    /// into the spec.
    Draft { path: PathBuf },
}

/// Common, typed record for the effective command inputs and requested work.
/// `argv` is suitable for replay with `cwd`; `shell_command` additionally
/// quotes each word for a POSIX shell.
#[derive(Debug, Clone, Serialize)]
pub struct ResolvedInvocation {
    pub cwd: PathBuf,
    pub executable: PathBuf,
    pub command: Vec<String>,
    pub argv: Vec<String>,
    pub shell_command: String,
    pub arguments: Vec<ResolvedArgument>,
    pub mode: InvocationMode,
    pub requested_proof: RequestedProof,
    pub write_scope: Vec<WriteScope>,
    /// False for `--print-invocation`; true when the command will continue.
    pub will_execute: bool,
}

struct CommandNode<'a> {
    command: &'a Command,
    matches: &'a ArgMatches,
}

impl ResolvedInvocation {
    /// Resolve a parsed Clap tree without running its operation.
    pub fn capture(
        command: &Command,
        matches: &ArgMatches,
        raw_argv: &[OsString],
        print_only: bool,
        invocation_out: Option<&Path>,
    ) -> Result<Self> {
        let cwd = std::env::current_dir().context("reading current directory")?;
        let executable = std::env::current_exe().context("resolving debundle executable")?;
        let nodes = command_nodes(command, matches)?;
        let command_path = nodes
            .iter()
            .skip(1)
            .map(|node| node.command.get_name().to_string())
            .collect::<Vec<_>>();
        let arguments = resolved_arguments(&nodes)?;
        let mut argv = raw_argv
            .iter()
            .skip(1)
            .map(|arg| string_arg(arg))
            .collect::<Result<Vec<_>>>()?;
        let insertion_index = strip_invocation_options(&mut argv);
        append_env_and_default_arguments(&mut argv, insertion_index, &nodes, &arguments)?;

        let mode = classify_mode(&command_path, &arguments);
        let requested_proof = classify_proof(&command_path, &arguments, mode);
        let mut write_scope = classify_write_scope(&command_path, &arguments, mode);
        if let Some(path) = invocation_out {
            write_scope.push(WriteScope::InvocationRecord {
                path: absolute_from(&cwd, path),
            });
        }

        let mut full_argv = Vec::with_capacity(argv.len() + 1);
        full_argv.push(path_string(&executable)?);
        full_argv.extend(argv);
        let shell_command = shell_command(&cwd, &full_argv)?;

        Ok(Self {
            cwd,
            executable,
            command: command_path,
            argv: full_argv,
            shell_command,
            arguments,
            mode,
            requested_proof,
            write_scope,
            will_execute: !print_only,
        })
    }
}

fn command_nodes<'a>(
    command: &'a Command,
    matches: &'a ArgMatches,
) -> Result<Vec<CommandNode<'a>>> {
    let mut nodes = vec![CommandNode { command, matches }];
    let mut current_command = command;
    let mut current_matches = matches;
    while let Some((name, child_matches)) = current_matches.subcommand() {
        let child_command = current_command
            .find_subcommand(name)
            .with_context(|| format!("Clap metadata does not contain selected command {name:?}"))?;
        nodes.push(CommandNode {
            command: child_command,
            matches: child_matches,
        });
        current_command = child_command;
        current_matches = child_matches;
    }
    Ok(nodes)
}

fn resolved_arguments(nodes: &[CommandNode<'_>]) -> Result<Vec<ResolvedArgument>> {
    let mut seen = BTreeSet::new();
    let mut arguments = Vec::new();
    for node in nodes {
        for argument in node.command.get_arguments() {
            let name = argument.get_id().as_str();
            if matches!(
                name,
                "print_invocation" | "invocation_out" | "help" | "version"
            ) || !seen.insert(name.to_string())
            {
                continue;
            }
            let source = node.matches.value_source(name);
            if source.is_none()
                && !matches!(
                    argument.get_action(),
                    ArgAction::SetTrue | ArgAction::SetFalse
                )
                && name != "format"
            {
                continue;
            }
            let source = match source {
                Some(ValueSource::CommandLine) => ArgumentSource::CommandLine,
                Some(ValueSource::EnvVariable) => {
                    let env_name = argument
                        .get_env()
                        .context("Clap reports an environment source without an env name")?;
                    ArgumentSource::Environment {
                        name: os_string(env_name, "environment variable name")?,
                    }
                }
                Some(ValueSource::DefaultValue) | None => ArgumentSource::Default,
                Some(_) => bail!("unsupported Clap value source for argument {name:?}"),
            };
            let value = if matches!(
                argument.get_action(),
                ArgAction::SetTrue | ArgAction::SetFalse
            ) {
                ArgumentValue::Flag(node.matches.get_one::<bool>(name).copied().unwrap_or(false))
            } else if name == "format" && node.matches.value_source(name).is_none() {
                ArgumentValue::Values(vec![if std::io::stdout().is_terminal() {
                    "text".to_string()
                } else {
                    "json".to_string()
                }])
            } else {
                let values = node
                    .matches
                    .get_raw(name)
                    .map(|raw| {
                        raw.map(|value| os_string(value, "argument value"))
                            .collect::<Result<Vec<_>>>()
                    })
                    .transpose()?
                    .unwrap_or_default();
                if matches!(name, "package_roots" | "graph_package_roots") {
                    let roots = values
                        .into_iter()
                        .map(|value| {
                            let (package, path) = value.split_once('=').with_context(|| {
                                format!("Clap accepted malformed package root {value:?}")
                            })?;
                            Ok(PackageRoot {
                                package: package.to_string(),
                                path: PathBuf::from(path),
                            })
                        })
                        .collect::<Result<Vec<_>>>()?;
                    ArgumentValue::PackageRoots(roots)
                } else if is_path_argument(name, argument.get_env()) {
                    ArgumentValue::Paths(values.into_iter().map(PathBuf::from).collect())
                } else {
                    ArgumentValue::Values(values)
                }
            };
            arguments.push(ResolvedArgument {
                name: name.to_string(),
                source,
                value,
            });
        }
    }
    arguments.sort_by(|left, right| left.name.cmp(&right.name));
    Ok(arguments)
}

/// Remove only invocation switches before Clap's `--` positional-only marker.
/// Preserve option-looking data after `--`, and insert replay materialization
/// before the marker so env/default options remain options when reparsed.
fn strip_invocation_options(argv: &mut Vec<String>) -> usize {
    let mut index = 0;
    let mut insertion_index = argv
        .iter()
        .position(|arg| arg == "--")
        .unwrap_or(argv.len());
    while index < insertion_index {
        if argv[index] == "--print-invocation" {
            argv.remove(index);
            insertion_index -= 1;
        } else if argv[index] == "--invocation-out" {
            argv.remove(index);
            insertion_index -= 1;
            if index < argv.len() {
                argv.remove(index);
                insertion_index -= 1;
            }
        } else if argv[index].starts_with("--invocation-out=") {
            argv.remove(index);
            insertion_index -= 1;
        } else {
            index += 1;
        }
    }
    insertion_index
}

fn append_env_and_default_arguments(
    argv: &mut Vec<String>,
    insertion_index: usize,
    nodes: &[CommandNode<'_>],
    arguments: &[ResolvedArgument],
) -> Result<()> {
    let by_name: std::collections::BTreeMap<_, _> = arguments
        .iter()
        .map(|argument| (argument.name.as_str(), argument))
        .collect();
    let mut seen = BTreeSet::new();
    let mut resolved_argv = Vec::new();
    for node in nodes {
        for argument in node.command.get_arguments() {
            let name = argument.get_id().as_str();
            if !seen.insert(name.to_string()) {
                continue;
            }
            let Some(resolved) = by_name.get(name) else {
                continue;
            };
            if !matches!(
                &resolved.source,
                ArgumentSource::Environment { .. } | ArgumentSource::Default
            ) {
                continue;
            }
            let Some(flag) = argument
                .get_long()
                .map(|long| format!("--{long}"))
                .or_else(|| argument.get_short().map(|short| format!("-{short}")))
            else {
                if matches!(resolved.source, ArgumentSource::Environment { .. }) {
                    bail!("cannot replay environment value for positional argument {name:?}");
                }
                continue;
            };
            match &resolved.value {
                ArgumentValue::Flag(true) => resolved_argv.push(flag),
                ArgumentValue::Flag(false) => {}
                ArgumentValue::Values(values) if values.is_empty() => {}
                ArgumentValue::Values(values) => {
                    append_option_values(&mut resolved_argv, &flag, values)
                }
                ArgumentValue::Paths(values) => {
                    let strings = values
                        .iter()
                        .map(|value| path_string(value))
                        .collect::<Result<Vec<_>>>()?;
                    append_option_values(&mut resolved_argv, &flag, &strings);
                }
                ArgumentValue::PackageRoots(roots) => {
                    let values = roots
                        .iter()
                        .map(|root| Ok(format!("{}={}", root.package, path_string(&root.path)?)))
                        .collect::<Result<Vec<_>>>()?;
                    append_option_values(&mut resolved_argv, &flag, &values);
                }
            }
        }
    }
    argv.splice(insertion_index..insertion_index, resolved_argv);
    Ok(())
}

fn append_option_values(argv: &mut Vec<String>, flag: &str, values: &[String]) {
    for value in values {
        if value.starts_with('-') {
            argv.push(format!("{flag}={value}"));
        } else {
            argv.push(flag.to_string());
            argv.push(value.clone());
        }
    }
}

fn classify_mode(command: &[String], arguments: &[ResolvedArgument]) -> InvocationMode {
    match command {
        [run] if run == "run" => InvocationMode::FreshPipeline,
        [spec, audit] if spec == "spec" && audit == "audit-contracts" => {
            InvocationMode::FreshPipeline
        }
        [spec, validate] if spec == "spec" && validate == "validate" => {
            if ["modules_root", "source_file", "source_root", "chunk"]
                .iter()
                .any(|name| has_argument(arguments, name))
            {
                InvocationMode::SourceSnapshotRead
            } else {
                InvocationMode::FreshPipeline
            }
        }
        [bindings, verb]
            if bindings == "bindings"
                && ["assign", "unassign", "rename"].contains(&verb.as_str()) =>
        {
            if flag_value(arguments, "no_verify") {
                InvocationMode::UnvalidatedEdit
            } else {
                InvocationMode::ValidatedEdit
            }
        }
        [modules, verb]
            if modules == "modules"
                && ["apply", "refine", "merge", "delete"].contains(&verb.as_str()) =>
        {
            if flag_value(arguments, "no_verify") {
                InvocationMode::UnvalidatedEdit
            } else {
                InvocationMode::ValidatedEdit
            }
        }
        [spec, synthesize]
            if spec == "spec"
                && synthesize == "synthesize-selectors"
                && flag_value(arguments, "apply") =>
        {
            InvocationMode::ValidatedEdit
        }
        [snapshot, action] if snapshot == "snapshot" && action == "verify" => {
            InvocationMode::SnapshotVerification
        }
        [snapshot, action] if snapshot == "snapshot" && action == "create" => {
            InvocationMode::SnapshotCreation
        }
        [namespace, comment]
            if ["bindings", "modules"].contains(&namespace.as_str()) && comment == "comment" =>
        {
            if has_argument(arguments, "text")
                || flag_value(arguments, "edit")
                || flag_value(arguments, "clear")
            {
                InvocationMode::Conditional
            } else {
                InvocationMode::ReadOnlyAnalysis
            }
        }
        // These commands accept DEBUNDLE_GRAPH for multiple shapes, but the
        // selected operation determines whether it actually reads the graph.
        [inspect] if inspect == "inspect-source" && has_argument(arguments, "id") => {
            InvocationMode::GraphSnapshotRead
        }
        [inspect] if inspect == "inspect-source" => InvocationMode::SourceSnapshotRead,
        [gate, verb] if gate == "gate" && ["list", "cut"].contains(&verb.as_str()) => {
            InvocationMode::DiagnosticSnapshotRead
        }
        [gate, verb] if gate == "gate" && verb == "describe" => InvocationMode::GraphSnapshotRead,
        // Reads a saved graph and its rejection report, but its product is
        // a draft the author reviews: analysis, not a snapshot view.
        [spec, propose] if spec == "spec" && propose == "propose-commutations" => {
            InvocationMode::ReadOnlyAnalysis
        }
        _ if has_argument(arguments, "dependency_graph_path")
            || has_argument(arguments, "graph") =>
        {
            InvocationMode::GraphSnapshotRead
        }
        _ if has_argument(arguments, "source_root") || has_argument(arguments, "source_file") => {
            InvocationMode::SourceSnapshotRead
        }
        [spec, synthesize] if spec == "spec" && synthesize == "synthesize-selectors" => {
            InvocationMode::ReadOnlyAnalysis
        }
        [atoms] if atoms == "atoms" => InvocationMode::ReadOnlyAnalysis,
        [coverage] if coverage == "coverage" => InvocationMode::ReadOnlyAnalysis,
        [graph_summary] if graph_summary == "graph-summary" => InvocationMode::ReadOnlyAnalysis,
        [describe] if describe == "describe" => InvocationMode::ReadOnlyAnalysis,
        [inspect] if inspect == "inspect-source" => InvocationMode::ReadOnlyAnalysis,
        [scc] if scc == "scc" => InvocationMode::ReadOnlyAnalysis,
        [cluster] if cluster == "cluster" => InvocationMode::ReadOnlyAnalysis,
        [bindings, list] if bindings == "bindings" && list == "list" => {
            InvocationMode::ReadOnlyAnalysis
        }
        [modules, verb]
            if modules == "modules"
                && ["list", "propose", "decompose"].contains(&verb.as_str()) =>
        {
            InvocationMode::ReadOnlyAnalysis
        }
        [spec, verb]
            if spec == "spec"
                && ["stats", "selector-debt", "match-selector"].contains(&verb.as_str()) =>
        {
            InvocationMode::ReadOnlyAnalysis
        }
        [gate, verb] if gate == "gate" && ["list", "describe", "cut"].contains(&verb.as_str()) => {
            InvocationMode::ReadOnlyAnalysis
        }
        _ => InvocationMode::Unclassified,
    }
}

fn classify_proof(
    command: &[String],
    _arguments: &[ResolvedArgument],
    mode: InvocationMode,
) -> RequestedProof {
    match mode {
        InvocationMode::FreshPipeline => RequestedProof::FullPipelineRequested,
        InvocationMode::SnapshotCreation => RequestedProof::SnapshotCreationRequested,
        InvocationMode::SnapshotVerification => RequestedProof::SnapshotVerificationRequested,
        InvocationMode::GraphSnapshotRead => RequestedProof::GraphSnapshotInput,
        InvocationMode::DiagnosticSnapshotRead => RequestedProof::DiagnosticSnapshotInput,
        InvocationMode::SourceSnapshotRead
            if command.last().is_some_and(|part| part == "validate") =>
        {
            RequestedProof::SourceValidationRequested
        }
        InvocationMode::ValidatedEdit => {
            if command == ["bindings", "rename"] {
                RequestedProof::NamesOnlyCheckRequested
            } else if command == ["spec", "synthesize-selectors"] {
                RequestedProof::SelectorValidationRequested
            } else {
                RequestedProof::PostEditGateRequested
            }
        }
        InvocationMode::UnvalidatedEdit => RequestedProof::None,
        InvocationMode::Conditional => RequestedProof::Conditional,
        InvocationMode::SourceSnapshotRead | InvocationMode::ReadOnlyAnalysis => {
            RequestedProof::None
        }
        InvocationMode::Unclassified => RequestedProof::Unclassified,
    }
}

fn classify_write_scope(
    command: &[String],
    arguments: &[ResolvedArgument],
    mode: InvocationMode,
) -> Vec<WriteScope> {
    let mut scopes = Vec::new();
    let get_path = |names: &[&str]| {
        arguments.iter().find_map(|argument| {
            if names.contains(&argument.name.as_str())
                && let ArgumentValue::Paths(paths) = &argument.value
            {
                return paths.first().cloned();
            }
            None
        })
    };
    // Probe mode suppresses even rejection graphs: their authored-file
    // provenance would not describe the in-memory contract edit.
    if command == ["spec", "audit-contracts"] {
        return scopes;
    }
    if command == ["run"] {
        if flag_value(arguments, "dry_run") {
            scopes.push(WriteScope::ConditionalPipelineReports {
                spec: get_path(&["spec", "tree_config"]),
                output_root: get_path(&["out_root"]),
            });
        } else if let Some(root) = get_path(&["out_root"]) {
            scopes.push(WriteScope::TreeOutput { root });
        } else if let Some(spec) = get_path(&["spec"]) {
            scopes.push(WriteScope::FlatSpec { spec });
        }
    } else if command == ["spec", "validate"] && mode == InvocationMode::FreshPipeline {
        scopes.push(WriteScope::TemporaryCapture);
    } else if matches!(
        mode,
        InvocationMode::ValidatedEdit
            | InvocationMode::UnvalidatedEdit
            | InvocationMode::Conditional
    ) {
        if !flag_value(arguments, "dry_run")
            && let Some(root) = get_path(&["modules_root", "modules"])
        {
            scopes.push(WriteScope::ModuleTree { root });
        }
        if mode == InvocationMode::ValidatedEdit
            && let Some(graph) = get_path(&["dependency_graph_path", "graph"])
        {
            scopes.push(WriteScope::ConditionalGateReports { graph });
        }
    }
    if command == ["snapshot", "create"]
        && let Some(destination) = get_path(&["destination"])
    {
        scopes.push(WriteScope::WorkerSnapshot { destination });
    }
    if command == ["spec", "propose-commutations"]
        && let Some(path) = get_path(&["out_path"])
    {
        scopes.push(WriteScope::Draft { path });
    }
    scopes
}

fn has_argument(arguments: &[ResolvedArgument], name: &str) -> bool {
    arguments.iter().any(|argument| argument.name == name)
}

fn flag_value(arguments: &[ResolvedArgument], name: &str) -> bool {
    arguments.iter().any(|argument| {
        argument.name == name && matches!(&argument.value, ArgumentValue::Flag(true))
    })
}

fn is_path_argument(name: &str, env: Option<&OsStr>) -> bool {
    let name = name.to_ascii_lowercase();
    let env_name = env.map(OsStr::to_string_lossy).unwrap_or_default();
    matches!(
        name.as_str(),
        "chunk"
            | "destination"
            | "directory"
            | "file"
            | "files"
            | "graph"
            | "modules"
            | "modules_root"
            | "out_root"
            | "packages_root"
            | "path"
            | "paths"
            | "source_file"
            | "source_root"
            | "spec"
            | "spec_file"
            | "snapshot_dir"
            | "tree_config"
            | "tree_modules"
            | "tree_source_root"
            | "tree_vendor_marks"
    ) || ["_path", "_file", "_root", "_dir", "_config", "_destination"]
        .iter()
        .any(|suffix| name.ends_with(suffix))
        || env_name.contains("GRAPH")
        || env_name.contains("ROOT")
}

fn shell_command(cwd: &Path, argv: &[String]) -> Result<String> {
    let command = argv
        .iter()
        .map(|value| shell_quote(value))
        .collect::<Result<Vec<_>>>()?
        .join(" ");
    Ok(format!(
        "cd {} && exec {command}",
        shell_quote(&path_string(cwd)?)?
    ))
}

fn shell_quote(value: &str) -> Result<String> {
    shlex::try_quote(value)
        .map(|quoted| quoted.into_owned())
        .context("argument cannot be represented in a POSIX shell command")
}

fn absolute_from(cwd: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        cwd.join(path)
    }
}

fn string_arg(value: &OsStr) -> Result<String> {
    os_string(value, "command-line argument")
}

fn os_string(value: &OsStr, label: &str) -> Result<String> {
    value
        .to_str()
        .map(str::to_owned)
        .with_context(|| format!("resolved invocation cannot represent non-UTF-8 {label}"))
}

fn path_string(path: &Path) -> Result<String> {
    path.to_str().map(str::to_owned).with_context(|| {
        format!(
            "resolved invocation cannot represent non-UTF-8 path {}",
            path.display()
        )
    })
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::path::PathBuf;

    use clap::{Arg, Command, value_parser};

    use super::{ArgumentSource, ArgumentValue, ResolvedInvocation};

    struct RestoreEnvironment {
        name: &'static str,
        value: Option<OsString>,
    }

    impl Drop for RestoreEnvironment {
        fn drop(&mut self) {
            if let Some(value) = self.value.take() {
                unsafe { std::env::set_var(self.name, value) };
            } else {
                unsafe { std::env::remove_var(self.name) };
            }
        }
    }

    #[test]
    fn global_environment_and_default_options_are_materialized_once() {
        const ENV_NAME: &str = "DEBUNDLE_INVOCATION_TEST_ROOT";
        let _restore = RestoreEnvironment {
            name: ENV_NAME,
            value: std::env::var_os(ENV_NAME),
        };
        // This variable is unique to this test and restored before returning.
        unsafe { std::env::set_var(ENV_NAME, "/tmp/input root") };

        let command = Command::new("debundle")
            .arg(
                Arg::new("test_root")
                    .long("test-root")
                    .global(true)
                    .env(ENV_NAME)
                    .value_parser(value_parser!(PathBuf)),
            )
            .arg(
                Arg::new("test_depth")
                    .long("test-depth")
                    .global(true)
                    .default_value("7"),
            )
            .subcommand(Command::new("child"));
        let raw_argv = vec!["debundle".into(), "child".into()];
        let matches = command.clone().get_matches_from(raw_argv.clone());
        let record = ResolvedInvocation::capture(&command, &matches, &raw_argv, true, None)
            .expect("resolve synthetic command");

        assert_eq!(
            record
                .argv
                .windows(2)
                .filter(|pair| pair[0] == "--test-root")
                .count(),
            1
        );
        assert_eq!(
            record
                .argv
                .windows(2)
                .filter(|pair| pair[0] == "--test-depth")
                .count(),
            1
        );
        assert!(
            record
                .argv
                .windows(2)
                .any(|pair| { pair[0] == "--test-root" && pair[1] == "/tmp/input root" })
        );
        assert!(
            record
                .argv
                .windows(2)
                .any(|pair| { pair[0] == "--test-depth" && pair[1] == "7" })
        );
        let root = record
            .arguments
            .iter()
            .find(|argument| argument.name == "test_root")
            .expect("environment-backed root recorded");
        assert!(matches!(&root.value, ArgumentValue::Paths(_)));
        assert!(matches!(&root.source, ArgumentSource::Environment { .. }));
        let depth = record
            .arguments
            .iter()
            .find(|argument| argument.name == "test_depth")
            .expect("default recorded");
        assert!(matches!(&depth.source, ArgumentSource::Default));
        assert!(matches!(&depth.value, ArgumentValue::Values(_)));
    }
}
