//! `debundle binding ...` — query/edit a single binding.
//!
//! Subcommands:
//!
//! * `describe` — JSON record covering current spec home, source
//!   location, owner ids, declared statement shape, atomic-unit
//!   membership.
//! * `show-code` — print the binding's emitted-in-source body
//!   (delegates to the same source-slice logic peel uses).
//! * `assign` — move the binding into a specific module's YAML.
//! * `unassign` — remove the binding from its current module.
//!
//! `assign` / `unassign` write the spec edit and exit; they don't
//! regen. Run the regen command (`bazelisk run
//! //tana/re/web/<v>:regen_js`) yourself, then read back the new
//! reports.

use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use clap::{Args, Subcommand};
use serde::Serialize;

use analysis::{
    AtomicUnitReport, BindingReport, ModuleReportRef, OwnerGraphNodeReport, OwnerGraphReport,
    SourceLocation, StatementKind,
};
use spec::{BindingSelector, Member, MemberSelector};
use spec_modules::{
    collect_module_files, default_binding_patches_path, is_module_yaml, module_path_from_file,
    read_binding_patches_file, read_module_file,
};

use super::io::print_json;

#[derive(Debug, Subcommand)]
pub enum BindingCommand {
    /// Print a JSON record covering current home, source span,
    /// owners, and atomic-unit membership for one binding.
    Describe(DescribeArgs),
    /// Print the source body the binding's owner statement(s)
    /// occupy in the input chunk.
    #[command(name = "show-code")]
    ShowCode(ShowCodeArgs),
    /// Move one or more bindings into named modules in a single
    /// atomic batch. Single-op shape (`<name> <module>`) is the
    /// backward-compatible drop-in for `assign`; batch syntax
    /// (`--op X=foo --op Y=foo`, `X=foo Y=foo`, `--batch ops.txt`)
    /// lets cycle-aware multi-move plans land together.
    Move(super::move_batch::MoveArgs),
    /// Move the binding into a named module. Single-op alias for
    /// `move`; kept for backward compatibility.
    Assign(AssignArgs),
    /// Remove the binding from its current module. After
    /// re-running the pipeline, it ends up in the residual.
    /// Single-op alias for `move <name>=-`.
    Unassign(UnassignArgs),
}

#[derive(Debug, Clone, Args)]
pub struct CommonArgs {
    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph")]
    pub owner_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules")]
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, Args)]
pub struct DescribeArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Binding name (the value of `selector.binding.name` in the spec).
    pub name: String,
}

#[derive(Debug, Clone, Args)]
pub struct ShowCodeArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Binding name (the value of `selector.binding.name` in the spec).
    pub name: String,

    /// Extra source lines to include around the owner span.
    #[arg(long = "context-lines", default_value_t = 0)]
    pub context_lines: usize,

    /// Root used to resolve relative `source_location.source_path`
    /// values when the owner graph stores chunk-relative paths.
    #[arg(long = "source-root")]
    pub source_root: Option<PathBuf>,
}

#[derive(Debug, Clone, Args)]
pub struct AssignArgs {
    /// Optional path to `owner_graph.json` (debundler analysis
    /// output). When supplied, the same batch-validation check
    /// `binding move` runs is applied here too. Omit to skip
    /// validation; pair with `--force` if a graph is on hand but
    /// you intentionally want to bypass.
    #[arg(long = "graph")]
    pub owner_graph_path: Option<PathBuf>,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules")]
    pub modules_root: PathBuf,

    /// Binding name (the value of `selector.binding.name` in the spec).
    pub name: String,

    /// Destination module path (e.g. `runtime/plugins`). Created if
    /// the YAML does not exist.
    pub module: String,

    /// Public export name to set on the new member. Defaults to the
    /// binding name (no `name:` key emitted).
    #[arg(long = "rename")]
    pub rename: Option<String>,

    /// Print the planned write but do not modify any files.
    #[arg(long = "dry-run", default_value_t = false)]
    pub dry_run: bool,

    /// Bypass realizability validation. Equivalent to `binding move
    /// --force <name>=<module>`.
    #[arg(long = "force", default_value_t = false)]
    pub force: bool,
}

#[derive(Debug, Clone, Args)]
pub struct UnassignArgs {
    /// Optional path to `owner_graph.json`; see `AssignArgs::graph`.
    #[arg(long = "graph")]
    pub owner_graph_path: Option<PathBuf>,

    /// Root of emitted-module `*.yaml` spec files.
    #[arg(long = "modules")]
    pub modules_root: PathBuf,

    /// Binding name (the value of `selector.binding.name` in the spec).
    pub name: String,

    /// Print the planned change but do not modify any files.
    #[arg(long = "dry-run", default_value_t = false)]
    pub dry_run: bool,

    /// Bypass realizability validation. Same semantics as
    /// `binding move --force`.
    #[arg(long = "force", default_value_t = false)]
    pub force: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct DescribeReport {
    pub binding: String,
    pub current_home: Option<BindingHome>,
    pub owners: Vec<OwnerInfo>,
    pub atomic_units: Vec<UnitInfo>,
    pub destination: Option<ModuleReportRef>,
    pub export_name: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BindingHome {
    pub source: HomeSource,
    pub module_path: String,
    pub file: String,
    pub renamed_to: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HomeSource {
    Module,
    BindingPatch,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerInfo {
    pub owner_id: String,
    pub statement_kind: StatementKind,
    pub source_location: Option<SourceLocation>,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnitInfo {
    pub unit_id: String,
    pub members: Vec<BindingReport>,
    pub size_lines_estimate: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ShowCodeReport {
    pub binding: String,
    pub slices: Vec<SourceSlice>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SourceSlice {
    pub source_path: String,
    pub resolved_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub context_start_line: usize,
    pub context_end_line: usize,
    pub text: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AssignReport {
    pub binding: String,
    pub previous_home: Option<BindingHome>,
    pub new_home: BindingHome,
    pub created_destination_file: bool,
    pub dry_run: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnassignReport {
    pub binding: String,
    pub previous_home: Option<BindingHome>,
    pub dry_run: bool,
}

pub fn run(command: BindingCommand) -> Result<()> {
    match command {
        BindingCommand::Describe(args) => print_json(&describe(&args)?),
        BindingCommand::ShowCode(args) => print_json(&show_code(&args)?),
        BindingCommand::Move(args) => super::move_batch::run(args),
        BindingCommand::Assign(args) => print_json(&assign(&args)?),
        BindingCommand::Unassign(args) => print_json(&unassign(&args)?),
    }
}

pub fn describe(args: &DescribeArgs) -> Result<DescribeReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let owners = owners_for_binding(&graph, &args.name);
    let atomic_units = units_for_binding(&graph, &args.name);
    let current_home = find_home(&args.common.modules_root, &args.name)?;
    let destination = owners.first().map(|owner| owner.destination.clone());

    let export_name = current_home
        .as_ref()
        .and_then(|home| home.renamed_to.clone())
        .or_else(|| {
            owners.iter().find_map(|owner| {
                owner
                    .declared_bindings
                    .iter()
                    .find(|b| b.binding == args.name)
                    .map(|b| b.export_name.to_string())
            })
        });

    Ok(DescribeReport {
        binding: args.name.clone(),
        current_home,
        owners: owners
            .into_iter()
            .map(|owner| OwnerInfo {
                owner_id: owner.id,
                statement_kind: owner.statement_kind,
                source_location: owner.source_location,
            })
            .collect(),
        atomic_units: atomic_units
            .into_iter()
            .map(|unit| UnitInfo {
                unit_id: unit.id,
                members: unit.members,
                size_lines_estimate: unit.size_lines_estimate,
            })
            .collect(),
        destination,
        export_name,
    })
}

pub fn show_code(args: &ShowCodeArgs) -> Result<ShowCodeReport> {
    let graph = load_graph(&args.common.owner_graph_path)?;
    let owners = owners_for_binding(&graph, &args.name);
    if owners.is_empty() {
        bail!(
            "binding {:?} not found in any owner in {}",
            args.name,
            args.common.owner_graph_path.display()
        );
    }
    let spans = collect_spans(&owners)?;
    let mut slices = Vec::new();
    for (source_path, location) in spans {
        let resolved = resolve_source_file(
            &source_path,
            args.source_root.as_deref(),
            &args.common.owner_graph_path,
            &args.common.modules_root,
        )?;
        let (context_start_line, context_end_line, text) = read_source_text(
            &resolved,
            location.start_line,
            location.end_line,
            args.context_lines,
        )
        .with_context(|| format!("reading source slice from {}", resolved.display()))?;
        slices.push(SourceSlice {
            source_path,
            resolved_path: resolved.display().to_string(),
            start_line: location.start_line,
            end_line: location.end_line,
            context_start_line,
            context_end_line,
            text,
        });
    }
    Ok(ShowCodeReport {
        binding: args.name.clone(),
        slices,
    })
}

pub fn assign(args: &AssignArgs) -> Result<AssignReport> {
    if args.module.starts_with('/') || args.module.contains("..") {
        bail!("destination module must be a relative path under modules root");
    }

    let previous_home = find_home(&args.modules_root, &args.name)?;
    let destination_file = args.modules_root.join(format!("{}.yaml", args.module));
    let created_destination_file = !destination_file.exists();

    let new_member = Member {
        name: args.rename.clone(),
        selector: MemberSelector {
            binding: BindingSelector {
                name: args.name.clone(),
                kind: None,
            },
        },
        purity: spec::MemberPurity::Default,
        effect: spec::MemberEffect::Default,
        pure_members: Vec::new(),
    };

    let new_home = BindingHome {
        source: HomeSource::Module,
        module_path: args.module.clone(),
        file: destination_file.display().to_string(),
        renamed_to: args.rename.clone(),
    };

    // Single-op invocations route through the batch's validator so
    // `assign --graph ...` enforces the same realizability check as
    // `binding move`.
    if let Some(graph_path) = &args.owner_graph_path {
        if !args.force {
            let op = super::move_batch::MoveOp {
                name: args.name.clone(),
                destination: Some(args.module.clone()),
            };
            super::move_batch::validate_single_op(
                std::slice::from_ref(&op),
                graph_path,
                &args.modules_root,
            )?;
        }
    }

    if !args.dry_run {
        // Remove from previous home (if any).
        if let Some(home) = &previous_home {
            remove_binding_from_file(Path::new(&home.file), &args.name, home.source)?;
        }
        // Append to destination.
        append_member_to_module(&destination_file, &new_member)?;
    }

    Ok(AssignReport {
        binding: args.name.clone(),
        previous_home,
        new_home,
        created_destination_file,
        dry_run: args.dry_run,
    })
}

pub fn unassign(args: &UnassignArgs) -> Result<UnassignReport> {
    let previous_home = find_home(&args.modules_root, &args.name)?;
    let Some(home) = &previous_home else {
        bail!(
            "binding {:?} is not currently assigned to any module under {}",
            args.name,
            args.modules_root.display()
        );
    };
    if let Some(graph_path) = &args.owner_graph_path {
        if !args.force {
            let op = super::move_batch::MoveOp {
                name: args.name.clone(),
                destination: None,
            };
            super::move_batch::validate_single_op(
                std::slice::from_ref(&op),
                graph_path,
                &args.modules_root,
            )?;
        }
    }
    if !args.dry_run {
        remove_binding_from_file(Path::new(&home.file), &args.name, home.source)?;
    }
    Ok(UnassignReport {
        binding: args.name.clone(),
        previous_home,
        dry_run: args.dry_run,
    })
}

fn load_graph(path: &Path) -> Result<OwnerGraphReport> {
    serde_json::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

fn owners_for_binding(graph: &OwnerGraphReport, name: &str) -> Vec<OwnerGraphNodeReport> {
    graph
        .nodes
        .iter()
        .filter(|node| node.declared_bindings.iter().any(|b| b.binding == name))
        .cloned()
        .collect()
}

fn units_for_binding(graph: &OwnerGraphReport, name: &str) -> Vec<AtomicUnitReport> {
    graph
        .atomic_graph
        .nodes
        .iter()
        .filter(|unit| unit.members.iter().any(|m| m.binding == name))
        .cloned()
        .collect()
}

pub fn find_home(modules_root: &Path, name: &str) -> Result<Option<BindingHome>> {
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            if member.selector.binding.name == name {
                return Ok(Some(BindingHome {
                    source: HomeSource::Module,
                    module_path,
                    file: path.display().to_string(),
                    renamed_to: member.name,
                }));
            }
        }
    }
    let patches_path = default_binding_patches_path(modules_root);
    if patches_path.exists() {
        for member in read_binding_patches_file(&patches_path)?.members {
            if member.selector.binding.name == name {
                return Ok(Some(BindingHome {
                    source: HomeSource::BindingPatch,
                    module_path: "binding_patches".to_string(),
                    file: patches_path.display().to_string(),
                    renamed_to: member.name,
                }));
            }
        }
    }
    Ok(None)
}

fn append_member_to_module(file: &Path, member: &Member) -> Result<()> {
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    }
    let existing = if file.exists() {
        Some(read_module_file(file)?)
    } else {
        None
    };
    let mut members = existing
        .as_ref()
        .map(|m| m.members.clone())
        .unwrap_or_default();
    members.push(member.clone());
    let anonymous = existing
        .as_ref()
        .map(|m| m.anonymous_statements.clone())
        .unwrap_or_default();
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([
        (
            serde_yaml::Value::String("members".into()),
            serde_yaml::to_value(&members)?,
        ),
        (
            serde_yaml::Value::String("anonymous_statements".into()),
            serde_yaml::to_value(&anonymous)?,
        ),
    ]))?;
    // Drop the `anonymous_statements: []` line if empty for cleaner files.
    let cleaned = if anonymous.is_empty() {
        document.replace("anonymous_statements: []\n", "")
    } else {
        document
    };
    fs::write(file, cleaned).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn remove_binding_from_file(file: &Path, name: &str, source: HomeSource) -> Result<()> {
    match source {
        HomeSource::Module => remove_binding_from_module_file(file, name),
        HomeSource::BindingPatch => remove_binding_from_patches_file(file, name),
    }
}

fn remove_binding_from_module_file(file: &Path, name: &str) -> Result<()> {
    let module = read_module_file(file)?;
    let before = module.members.len();
    let members: Vec<Member> = module
        .members
        .into_iter()
        .filter(|m| m.selector.binding.name != name)
        .collect();
    if members.len() == before {
        return Ok(());
    }
    if members.is_empty() && module.anonymous_statements.is_empty() {
        // Drop the now-empty file rather than leaving an
        // unreferenced YAML behind. Authors who want to keep it
        // can re-create it with the spec edit they actually want.
        fs::remove_file(file).with_context(|| format!("removing empty {}", file.display()))?;
        return Ok(());
    }
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([
        (
            serde_yaml::Value::String("members".into()),
            serde_yaml::to_value(&members)?,
        ),
        (
            serde_yaml::Value::String("anonymous_statements".into()),
            serde_yaml::to_value(&module.anonymous_statements)?,
        ),
    ]))?;
    let cleaned = if module.anonymous_statements.is_empty() {
        document.replace("anonymous_statements: []\n", "")
    } else {
        document
    };
    fs::write(file, cleaned).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn remove_binding_from_patches_file(file: &Path, name: &str) -> Result<()> {
    let patches = read_binding_patches_file(file)?;
    let members: Vec<Member> = patches
        .members
        .into_iter()
        .filter(|m| m.selector.binding.name != name)
        .collect();
    let document = serde_yaml::to_string(&serde_yaml::Mapping::from_iter([(
        serde_yaml::Value::String("members".into()),
        serde_yaml::to_value(&members)?,
    )]))?;
    fs::write(file, document).with_context(|| format!("writing {}", file.display()))?;
    Ok(())
}

fn collect_spans(
    owners: &[OwnerGraphNodeReport],
) -> Result<std::collections::BTreeMap<String, SourceLocation>> {
    let mut spans: std::collections::BTreeMap<String, SourceLocation> =
        std::collections::BTreeMap::new();
    for owner in owners {
        let Some(location) = &owner.source_location else {
            continue;
        };
        spans
            .entry(location.source_path.clone())
            .and_modify(|span| span.expand_to(location))
            .or_insert_with(|| location.clone());
    }
    if spans.is_empty() {
        bail!("selected owners do not have source locations");
    }
    Ok(spans)
}

fn resolve_source_file(
    source_path: &str,
    source_root: Option<&Path>,
    owner_graph_path: &Path,
    modules_root: &Path,
) -> Result<PathBuf> {
    let mut candidates = Vec::new();
    let source = PathBuf::from(source_path);
    if source.is_absolute() {
        candidates.push(source);
    } else {
        if let Some(root) = source_root {
            candidates.push(root.join(source_path));
        }
        if let Ok(cwd) = env::current_dir() {
            candidates.push(cwd.join(source_path));
        }
        push_candidate(&mut candidates, owner_graph_path.parent(), source_path);
        push_candidate(
            &mut candidates,
            owner_graph_path.parent().and_then(Path::parent),
            source_path,
        );
        push_candidate(&mut candidates, modules_root.parent(), source_path);
        push_candidate(
            &mut candidates,
            modules_root.parent().and_then(Path::parent),
            source_path,
        );
    }
    dedup_paths(&mut candidates);
    for candidate in &candidates {
        if candidate.is_file() {
            return Ok(candidate.clone());
        }
    }
    Err(anyhow!(
        "could not resolve source path {source_path:?}; pass --source-root. Tried: {}",
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

fn push_candidate(candidates: &mut Vec<PathBuf>, root: Option<&Path>, source_path: &str) {
    if let Some(root) = root {
        candidates.push(root.join(source_path));
    }
}

fn dedup_paths(paths: &mut Vec<PathBuf>) {
    let mut seen = BTreeSet::new();
    paths.retain(|p| seen.insert(p.display().to_string()));
}

fn read_source_text(
    path: &Path,
    start_line: usize,
    end_line: usize,
    context_lines: usize,
) -> Result<(usize, usize, String)> {
    let body = fs::read_to_string(path)?;
    let lines: Vec<&str> = body.lines().collect();
    if lines.is_empty() {
        return Ok((1, 0, String::new()));
    }
    let context_start_line = start_line.saturating_sub(context_lines).max(1);
    let context_end_line = end_line.saturating_add(context_lines).min(lines.len());
    let start_index = context_start_line.saturating_sub(1).min(lines.len());
    let end_index = context_end_line.min(lines.len());
    let text = if start_index <= end_index {
        lines[start_index..end_index].join("\n")
    } else {
        String::new()
    };
    Ok((context_start_line, context_end_line, text))
}

// Re-export helpers so the parent module's `mod tests` block can
// reach in.
#[allow(dead_code)]
pub(crate) fn _is_module_yaml(path: &Path) -> bool {
    is_module_yaml(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        AtomicGraphReport, BindingReport, ModuleReportRef, OwnerGraphEdgeReport,
        OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity, StatementKind,
        StatementOrdinal,
    };
    use swc_atoms::Atom;
    use tempfile::TempDir;

    fn write(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn binding_report(binding: &str, export_name: &str) -> BindingReport {
        BindingReport {
            binding: Atom::from(binding),
            export_name: Atom::from(export_name),
        }
    }

    fn owner(id: &str, binding: &str) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(1),
            source_location: Some(SourceLocation {
                source_path: "static/app.js".to_string(),
                start_line: 10,
                end_line: 20,
            }),
            declared_bindings: vec![binding_report(binding, binding)],
            statement_kind: StatementKind::ClassDecl,
            purity: Purity::Pure,
            destination: ModuleReportRef {
                id: "static/app::residual/unhandled".to_string(),
                label: "residual/unhandled".to_string(),
                residual: true,
                index: None,
                target_file: None,
            },
        }
    }

    fn graph_with_owner(binding: &str) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes: vec![owner("owner:0", binding)],
            edges: Vec::<OwnerGraphEdgeReport>::new(),
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        }
    }

    fn fixture(binding: &str) -> (TempDir, PathBuf, PathBuf) {
        let dir = TempDir::new().unwrap();
        let graph_path = dir.path().join("owner_graph.json");
        let modules_root = dir.path().join("spec/modules");
        write(
            &graph_path,
            &serde_json::to_string(&graph_with_owner(binding)).unwrap(),
        );
        fs::create_dir_all(&modules_root).unwrap();
        write(&dir.path().join("static/app.js"), &"line\n".repeat(30));
        (dir, graph_path, modules_root)
    }

    #[test]
    fn describe_reports_unassigned_when_not_in_spec() {
        let (_dir, graph_path, modules_root) = fixture("XOe");
        let report = describe(&DescribeArgs {
            common: CommonArgs {
                owner_graph_path: graph_path,
                modules_root,
            },
            name: "XOe".to_string(),
        })
        .unwrap();
        assert_eq!(report.binding, "XOe");
        assert!(report.current_home.is_none());
        assert_eq!(report.owners.len(), 1);
        assert_eq!(report.export_name.as_deref(), Some("XOe"));
    }

    #[test]
    fn describe_reports_module_home_when_assigned() {
        let (_dir, graph_path, modules_root) = fixture("XOe");
        write(
            &modules_root.join("runtime/plugins.yaml"),
            "members:\n  - name: PluginSettingsAccessor\n    selector:\n      binding:\n        name: XOe\n",
        );
        let report = describe(&DescribeArgs {
            common: CommonArgs {
                owner_graph_path: graph_path,
                modules_root,
            },
            name: "XOe".to_string(),
        })
        .unwrap();
        let home = report.current_home.unwrap();
        assert_eq!(home.source, HomeSource::Module);
        assert_eq!(home.module_path, "runtime/plugins");
        assert_eq!(home.renamed_to.as_deref(), Some("PluginSettingsAccessor"));
        assert_eq!(
            report.export_name.as_deref(),
            Some("PluginSettingsAccessor")
        );
    }

    #[test]
    fn assign_writes_new_module_and_clears_previous_home() {
        let (_dir, _graph_path, modules_root) = fixture("XOe");
        write(
            &modules_root.join("runtime/old.yaml"),
            "members:\n  - selector:\n      binding:\n        name: XOe\n",
        );
        let report = assign(&AssignArgs {
            owner_graph_path: None,
            modules_root: modules_root.clone(),
            name: "XOe".to_string(),
            module: "runtime/plugins".to_string(),
            rename: Some("PluginSettingsAccessor".to_string()),
            dry_run: false,
            force: false,
        })
        .unwrap();
        assert!(report.created_destination_file);
        assert_eq!(report.new_home.module_path, "runtime/plugins");
        // Previous file dropped because it became empty.
        assert!(!modules_root.join("runtime/old.yaml").exists());
        // New file present.
        let new = read_module_file(&modules_root.join("runtime/plugins.yaml")).unwrap();
        assert_eq!(new.members.len(), 1);
        assert_eq!(new.members[0].selector.binding.name, "XOe");
        assert_eq!(
            new.members[0].name.as_deref(),
            Some("PluginSettingsAccessor")
        );
    }

    #[test]
    fn assign_dry_run_does_not_touch_files() {
        let (_dir, _graph_path, modules_root) = fixture("XOe");
        let report = assign(&AssignArgs {
            owner_graph_path: None,
            modules_root: modules_root.clone(),
            name: "XOe".to_string(),
            module: "runtime/plugins".to_string(),
            rename: None,
            dry_run: true,
            force: false,
        })
        .unwrap();
        assert!(report.dry_run);
        assert!(!modules_root.join("runtime/plugins.yaml").exists());
    }

    #[test]
    fn assign_rejects_absolute_destination() {
        let (_dir, _graph_path, modules_root) = fixture("XOe");
        let err = assign(&AssignArgs {
            owner_graph_path: None,
            modules_root,
            name: "XOe".to_string(),
            module: "/etc/passwd".to_string(),
            rename: None,
            dry_run: false,
            force: false,
        })
        .err();
        assert!(err.is_some());
    }

    #[test]
    fn unassign_removes_member_and_returns_previous_home() {
        let (_dir, _graph_path, modules_root) = fixture("XOe");
        write(
            &modules_root.join("runtime/plugins.yaml"),
            "members:\n  - selector:\n      binding:\n        name: XOe\n  - selector:\n      binding:\n        name: keep_me\n",
        );
        let report = unassign(&UnassignArgs {
            owner_graph_path: None,
            modules_root: modules_root.clone(),
            name: "XOe".to_string(),
            dry_run: false,
            force: false,
        })
        .unwrap();
        assert_eq!(report.previous_home.unwrap().module_path, "runtime/plugins");
        let remaining = read_module_file(&modules_root.join("runtime/plugins.yaml")).unwrap();
        assert_eq!(remaining.members.len(), 1);
        assert_eq!(remaining.members[0].selector.binding.name, "keep_me");
    }

    #[test]
    fn unassign_errors_when_binding_is_unassigned() {
        let (_dir, _graph_path, modules_root) = fixture("XOe");
        let err = unassign(&UnassignArgs {
            owner_graph_path: None,
            modules_root,
            name: "XOe".to_string(),
            dry_run: false,
            force: false,
        })
        .err();
        assert!(err.is_some());
    }

    #[test]
    fn show_code_returns_owner_span_text() {
        let (dir, graph_path, modules_root) = fixture("XOe");
        let report = show_code(&ShowCodeArgs {
            common: CommonArgs {
                owner_graph_path: graph_path,
                modules_root,
            },
            name: "XOe".to_string(),
            context_lines: 0,
            source_root: Some(dir.path().to_path_buf()),
        })
        .unwrap();
        assert_eq!(report.slices.len(), 1);
        assert_eq!(report.slices[0].start_line, 10);
        assert_eq!(report.slices[0].end_line, 20);
        assert_eq!(report.slices[0].context_start_line, 10);
    }
}
