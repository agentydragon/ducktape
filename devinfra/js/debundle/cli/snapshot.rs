//! Isolated, relocatable copies of the inputs behind one saved graph.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use clap::{Args, Parser, Subcommand};
use graph_provenance::{
    CurrentRoots, GraphProvenance, InputRoot, ProvenanceRoots, read_graph_header,
};
use nix::fcntl::{RenameFlags, renameat2};
use pipeline::TransformSpecSource;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree, require_relative_source_paths};

use crate::{DebundleArgs, DebundleCommand};

const MANIFEST: &str = "snapshot.json";

#[derive(Debug, Args)]
pub(super) struct SnapshotArgs {
    #[command(subcommand)]
    command: SnapshotCommand,
}

#[derive(Debug, Subcommand)]
enum SnapshotCommand {
    /// Copy verified graph inputs and the engine into a new worker directory.
    Create(CreateArgs),
    /// Verify a snapshot's input contents, membership, graph and engine.
    Verify(VerifyArgs),
}

#[derive(Debug, Args)]
struct CreateArgs {
    /// Saved dependency graph whose complete input set will be copied.
    #[arg(long, env = "DEBUNDLE_GRAPH")]
    graph: PathBuf,
    /// New worker directory; an existing destination is always refused.
    #[arg(long)]
    destination: PathBuf,
}

#[derive(Debug, Args)]
struct VerifyArgs {
    /// Worker directory containing snapshot.json.
    directory: PathBuf,
}

#[derive(Debug, Serialize, Deserialize)]
struct SnapshotManifest {
    graph: PathBuf,
    graph_sha256: String,
    engine: PathBuf,
    roots: ProvenanceRoots,
    selector_source_root: PathBuf,
    browser_inputs: Option<Vec<SnapshotFile>>,
    run: SnapshotRun,
}

#[derive(Debug, Serialize, Deserialize)]
struct SnapshotFile {
    path: PathBuf,
    sha256: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct SnapshotRun {
    config: PathBuf,
    modules: PathBuf,
    vendor_marks: PathBuf,
    package_roots: BTreeMap<String, PathBuf>,
    packages_root: Option<PathBuf>,
    fail_fast: bool,
}

#[derive(Serialize)]
struct SnapshotOutcome {
    directory: PathBuf,
    graph: PathBuf,
    engine: PathBuf,
    spec_root: PathBuf,
    graph_source_root: PathBuf,
    graph_package_roots: BTreeMap<String, PathBuf>,
    source_root: PathBuf,
    tree_config: PathBuf,
    modules: PathBuf,
    regeneration_argv: Vec<String>,
}

pub(super) fn run(args: SnapshotArgs, current: CurrentRoots) -> Result<()> {
    let outcome = match args.command {
        SnapshotCommand::Create(args) => create(args, current)?,
        SnapshotCommand::Verify(args) => verify(&args.directory)?,
    };
    println!("{}", serde_json::to_string_pretty(&outcome)?);
    Ok(())
}

fn create(args: CreateArgs, current: CurrentRoots) -> Result<SnapshotOutcome> {
    let requested = std::path::absolute(&args.destination)?;
    let parent = fs::canonicalize(
        requested
            .parent()
            .context("snapshot destination has no parent")?,
    )?;
    let destination = parent.join(
        requested
            .file_name()
            .context("snapshot destination has no name")?,
    );
    ensure!(
        !destination.exists(),
        "snapshot destination already exists: {}",
        destination.display()
    );
    let graph = read_graph_header(&args.graph)?;
    let provenance = graph
        .provenance
        .as_ref()
        .context("graph has no input provenance; regenerate it before creating a snapshot")?;
    provenance.verify(&current)?;
    ensure!(
        provenance
            .files
            .iter()
            .map(|file| &file.root)
            .chain(provenance.absent_files.iter().map(|file| &file.root))
            .chain(
                provenance
                    .yaml_memberships
                    .iter()
                    .map(|membership| &membership.root)
            )
            .all(|root| *root != InputRoot::Absolute),
        "snapshot contains inputs outside its declared roots; regenerate with explicit source and package roots"
    );
    let run = snapshot_run(provenance)?;

    let parent = parent.as_path();
    ensure!(
        parent.is_dir(),
        "snapshot destination parent must exist: {}",
        parent.display()
    );
    let staging = tempfile::Builder::new()
        .prefix(".debundle-snapshot-")
        .tempdir_in(parent)?;
    let roots = ProvenanceRoots {
        spec: placement(&provenance.roots.spec)?,
        source: placement(&provenance.roots.source)?,
        packages: provenance
            .roots
            .packages
            .iter()
            .map(|(name, root)| Ok((name.clone(), placement(root)?)))
            .collect::<Result<_>>()?,
    };
    for root in [&roots.spec, &roots.source]
        .into_iter()
        .chain(roots.packages.values())
    {
        fs::create_dir_all(staging.path().join(root))?;
    }
    for file in &provenance.files {
        let original = provenance.resolve(&file.root, &file.path, &CurrentRoots::default())?;
        let source = provenance.resolve(&file.root, &file.path, &current)?;
        copy_verified(
            &source,
            &staging.path().join(placement(&original)?),
            &file.sha256,
        )?;
    }
    // Empty module roots still participate in membership verification.
    for membership in &provenance.yaml_memberships {
        let original = provenance.resolve(
            &membership.root,
            &membership.directory,
            &CurrentRoots::default(),
        )?;
        fs::create_dir_all(staging.path().join(placement(&original)?))?;
    }
    let options = CompileSpecTreeOptions {
        config_path: staging.path().join(&run.config),
        modules_root: staging.path().join(&run.modules),
        vendor_marks_path: staging.path().join(&run.vendor_marks),
        source_root: Some(staging.path().join(&roots.source)),
        out_root: staging.path().join("output"),
    };
    require_relative_source_paths(&options)?;
    let spec = compile_spec_tree(&options)?;
    let selector_source_root =
        relative_in_snapshot(staging.path(), &fs::canonicalize(&spec.inputs.input_root)?)?;
    let browser_inputs = if let Some(browser) = &spec.emit_browser_harness {
        let placed = &selector_source_root;
        let source = actual_input(placed, provenance, &current)?;
        ensure!(
            !staging.path().starts_with(fs::canonicalize(&source)?),
            "snapshot destination must be outside the browser source tree"
        );
        let mut extra = BTreeMap::new();
        for relative in source_files(&source)? {
            copy_supplemental(
                &source.join(&relative),
                &placed.join(relative),
                staging.path(),
                &mut extra,
            )?;
        }
        let summary = relative_in_snapshot(staging.path(), &browser.asset_summary_path)?;
        let actual_summary = actual_input(&summary, provenance, &current)?;
        copy_supplemental(&actual_summary, &summary, staging.path(), &mut extra)?;
        Some(
            extra
                .into_iter()
                .map(|(path, sha256)| SnapshotFile { path, sha256 })
                .collect(),
        )
    } else {
        None
    };
    let engine = PathBuf::from("engine/debundle");
    copy_verified(
        &std::env::current_exe()?,
        &staging.path().join(&engine),
        &provenance.engine_sha256,
    )?;
    let graph_path = PathBuf::from("analysis/dependency_graph.json");
    let graph_sha256 = copy_file(&args.graph, &staging.path().join(&graph_path), None)?;
    let manifest = SnapshotManifest {
        graph: graph_path,
        graph_sha256,
        engine,
        browser_inputs,
        selector_source_root,
        roots,
        run,
    };
    write_file(
        &staging.path().join(MANIFEST),
        &serde_json::to_vec_pretty(&manifest)?,
    )?;
    verify(staging.path())?;
    let directory = fs::File::open(parent)?;
    renameat2(
        &directory,
        staging.path(),
        &directory,
        &destination,
        RenameFlags::RENAME_NOREPLACE,
    )
    .with_context(|| format!("publishing snapshot {}", destination.display()))?;
    outcome(&destination, &manifest)
}

fn snapshot_run(provenance: &GraphProvenance) -> Result<SnapshotRun> {
    let args = DebundleArgs::try_parse_from(&provenance.generation_command)?;
    let DebundleCommand::Run(mut args) = args.command else {
        bail!("snapshot creation requires a graph generated by debundle run");
    };
    args.tree_source_root = Some(provenance.roots.source.clone());
    args.out_root = Some(PathBuf::from("output"));
    let fail_fast = args.fail_fast;
    let cli = args.resolve()?;
    let TransformSpecSource::Tree(options) = cli.spec_source else {
        bail!("snapshot creation currently requires a tree-shaped spec");
    };
    let locate =
        |path: &Path| -> Result<PathBuf> { placement(&provenance.generation_cwd.join(path)) };
    Ok(SnapshotRun {
        config: locate(&options.config_path)?,
        modules: locate(&options.modules_root)?,
        vendor_marks: locate(&options.vendor_marks_path)?,
        package_roots: cli
            .package_roots
            .iter()
            .map(|(name, root)| Ok((name.clone(), locate(root)?)))
            .collect::<Result<_>>()?,
        packages_root: cli.packages_root.as_deref().map(locate).transpose()?,
        fail_fast,
    })
}

fn actual_input(
    placed: &Path,
    provenance: &GraphProvenance,
    current: &CurrentRoots,
) -> Result<PathBuf> {
    let original = Path::new("/").join(placed.strip_prefix("inputs")?);
    for (old, new) in [
        (&provenance.roots.source, current.source.as_ref()),
        (&provenance.roots.spec, current.spec.as_ref()),
    ] {
        if let Ok(relative) = original.strip_prefix(old) {
            return Ok(new.unwrap_or(old).join(relative));
        }
    }
    bail!(
        "spec input {} is outside the captured roots",
        original.display()
    )
}

fn relative_in_snapshot(root: &Path, path: &Path) -> Result<PathBuf> {
    let relative = path
        .strip_prefix(root)
        .context("snapshot input escapes its directory")?;
    let mut normalized = PathBuf::new();
    for component in relative.components() {
        match component {
            Component::Normal(name) => normalized.push(name),
            Component::CurDir => {}
            Component::ParentDir => {
                ensure!(normalized.pop(), "snapshot input escapes its directory");
            }
            Component::RootDir | Component::Prefix(_) => bail!("snapshot input is not relative"),
        }
    }
    Ok(normalized)
}

fn source_files(root: &Path) -> Result<Vec<PathBuf>> {
    fn walk(root: &Path, directory: &Path, paths: &mut Vec<PathBuf>) -> Result<()> {
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                walk(root, &entry.path(), paths)?;
            } else if kind.is_file() {
                paths.push(entry.path().strip_prefix(root)?.to_path_buf());
            } else {
                bail!(
                    "snapshot source must contain concrete files/directories: {}",
                    entry.path().display()
                );
            }
        }
        Ok(())
    }
    let mut paths = Vec::new();
    walk(root, root, &mut paths)?;
    paths.sort();
    Ok(paths)
}

fn copy_supplemental(
    source: &Path,
    relative: &Path,
    staging: &Path,
    extra: &mut BTreeMap<PathBuf, String>,
) -> Result<()> {
    let target = staging.join(relative);
    if target.exists() {
        ensure!(
            digest_file(source)? == digest_file(&target)?,
            "snapshot source changed while copying: {}",
            source.display()
        );
    } else {
        let digest = copy_file(source, &target, None)?;
        extra.insert(relative.to_path_buf(), digest);
    }
    Ok(())
}

fn placement(path: &Path) -> Result<PathBuf> {
    ensure!(
        path.is_absolute(),
        "captured path must be absolute: {}",
        path.display()
    );
    let mut result = PathBuf::from("inputs");
    for component in path.components() {
        match component {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(name) => result.push(name),
            Component::ParentDir => {
                ensure!(result != Path::new("inputs"), "captured path escapes root");
                result.pop();
            }
            Component::Prefix(_) => bail!("unsupported captured path {}", path.display()),
        }
    }
    Ok(result)
}

fn write_file(path: &Path, bytes: &[u8]) -> Result<()> {
    fs::create_dir_all(path.parent().context("file has no parent")?)?;
    fs::write(path, bytes).with_context(|| format!("writing {}", path.display()))
}

fn copy_verified(source: &Path, destination: &Path, expected: &str) -> Result<()> {
    copy_file(source, destination, Some(expected))?;
    Ok(())
}

fn copy_file(source: &Path, destination: &Path, expected: Option<&str>) -> Result<String> {
    fs::create_dir_all(
        destination
            .parent()
            .context("snapshot file has no parent")?,
    )?;
    let mut input =
        fs::File::open(source).with_context(|| format!("reading {}", source.display()))?;
    let mut output = fs::File::create(destination)?;
    let digest = digest_reader(&mut input, Some(&mut output))?;
    if let Some(expected) = expected {
        ensure!(
            digest == expected,
            "snapshot input changed while copying: {}",
            source.display()
        );
    }
    fs::set_permissions(destination, fs::metadata(source)?.permissions())?;
    Ok(digest)
}

fn digest_reader(input: &mut impl Read, mut output: Option<&mut fs::File>) -> Result<String> {
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        if let Some(output) = &mut output {
            output.write_all(&buffer[..count])?;
        }
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

fn digest_file(path: &Path) -> Result<String> {
    digest_reader(&mut fs::File::open(path)?, None)
}

fn verify(directory: &Path) -> Result<SnapshotOutcome> {
    let directory = fs::canonicalize(directory)?;
    let manifest: SnapshotManifest = serde_json::from_slice(&fs::read(directory.join(MANIFEST))?)?;
    let graph_path = contained(&directory, &manifest.graph)?;
    ensure!(
        digest_file(&graph_path)? == manifest.graph_sha256,
        "snapshot graph contents changed"
    );
    let graph = read_graph_header(&graph_path)?;
    let provenance = graph
        .provenance
        .as_ref()
        .context("snapshot graph has no provenance")?;
    ensure!(
        digest_file(&contained(&directory, &manifest.engine)?)? == provenance.engine_sha256,
        "snapshot engine contents changed"
    );
    let current = CurrentRoots {
        spec: Some(contained(&directory, &manifest.roots.spec)?),
        source: Some(contained(&directory, &manifest.roots.source)?),
        packages: manifest
            .roots
            .packages
            .iter()
            .map(|(name, root)| Ok((name.clone(), contained(&directory, root)?)))
            .collect::<Result<_>>()?,
    };
    for file in &provenance.files {
        ensure!(
            file.root != InputRoot::Absolute,
            "snapshot graph has an unrelocated absolute input"
        );
        let path = provenance.resolve(&file.root, &file.path, &current)?;
        let relative = path
            .strip_prefix(&directory)
            .context("snapshot input escapes its directory")?;
        contained(&directory, relative)?;
    }
    for path in [
        &manifest.run.config,
        &manifest.run.modules,
        &manifest.run.vendor_marks,
        &manifest.selector_source_root,
    ]
    .into_iter()
    .chain(manifest.run.package_roots.values())
    .chain(manifest.run.packages_root.iter())
    {
        contained(&directory, path)?;
    }
    provenance.verify(&current)?;
    if let Some(extra) = &manifest.browser_inputs {
        for file in extra {
            ensure!(
                digest_file(&contained(&directory, &file.path)?)? == file.sha256,
                "snapshot browser input changed: {}",
                file.path.display()
            );
        }
        let source = directory.join(&manifest.selector_source_root);
        let expected: BTreeSet<_> = provenance
            .files
            .iter()
            .map(|file| provenance.resolve(&file.root, &file.path, &current))
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .chain(extra.iter().map(|file| directory.join(&file.path)))
            .filter_map(|path| path.strip_prefix(&source).ok().map(Path::to_path_buf))
            .collect();
        ensure!(
            source_files(&source)?.into_iter().collect::<BTreeSet<_>>() == expected,
            "snapshot browser source membership changed"
        );
    }
    outcome(&directory, &manifest)
}

fn contained(root: &Path, relative: &Path) -> Result<PathBuf> {
    ensure!(
        relative
            .components()
            .all(|part| matches!(part, Component::Normal(_) | Component::CurDir)),
        "snapshot path must stay within its directory: {}",
        relative.display()
    );
    let path = fs::canonicalize(root.join(relative))
        .with_context(|| format!("resolving snapshot path {}", relative.display()))?;
    ensure!(
        path.starts_with(root),
        "snapshot path resolves outside its directory: {}",
        relative.display()
    );
    Ok(path)
}

fn outcome(directory: &Path, manifest: &SnapshotManifest) -> Result<SnapshotOutcome> {
    let path =
        |relative: &Path| -> String { directory.join(relative).to_string_lossy().into_owned() };
    let mut argv = vec![
        path(&manifest.engine),
        "run".to_owned(),
        "--tree-config".to_owned(),
        path(&manifest.run.config),
        "--tree-modules".to_owned(),
        path(&manifest.run.modules),
        "--tree-vendor-marks".to_owned(),
        path(&manifest.run.vendor_marks),
        "--tree-source-root".to_owned(),
        path(&manifest.roots.source),
        "--out-root".to_owned(),
        path(Path::new("output")),
    ];
    for (name, root) in &manifest.run.package_roots {
        argv.extend([
            "--package-root".to_owned(),
            format!("{name}={}", path(root)),
        ]);
    }
    if let Some(root) = &manifest.run.packages_root {
        argv.extend(["--packages-root".to_owned(), path(root)]);
    }
    if manifest.run.fail_fast {
        argv.push("--fail-fast".to_owned());
    }
    Ok(SnapshotOutcome {
        directory: directory.to_path_buf(),
        graph: directory.join(&manifest.graph),
        engine: directory.join(&manifest.engine),
        spec_root: directory.join(&manifest.roots.spec),
        graph_source_root: directory.join(&manifest.roots.source),
        graph_package_roots: manifest
            .roots
            .packages
            .iter()
            .map(|(name, root)| (name.clone(), directory.join(root)))
            .collect(),
        source_root: directory.join(&manifest.selector_source_root),
        tree_config: directory.join(&manifest.run.config),
        modules: directory.join(&manifest.run.modules),
        regeneration_argv: argv,
    })
}
