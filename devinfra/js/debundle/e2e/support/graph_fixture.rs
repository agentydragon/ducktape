//! Real JS/spec inputs for graph-backed query and edit workflows.

use super::*;
use debundle_e2e_support::*;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::process::Command;

pub struct GraphFixture {
    run: TreeRun,
    pub modules: PathBuf,
    pub graph: PathBuf,
}

impl GraphFixture {
    pub fn save_json(&self, name: &str, value: &serde_json::Value) -> String {
        let path = self.root().join(name);
        fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
        path.to_str().unwrap().to_owned()
    }

    pub fn tree_config(&self) -> PathBuf {
        self.root().join("spec_config.yaml")
    }

    /// Write the author's complete final priority without deriving or repairing it.
    pub fn write_final_import_order(&self, owners: &[&str]) -> PathBuf {
        let path = self.root().join("final_import_order.json");
        fs::write(&path, serde_json::to_vec(owners).unwrap()).unwrap();
        path
    }

    /// Explicitly edit the current configured priority before refreshing a graph.
    pub fn set_import_order(&self, owners: &[&str]) {
        let path = self.root().join("spec_config.yaml");
        let mut config: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(&path).unwrap()).unwrap();
        config["import_order"]["main"] = serde_yaml::to_value(owners).unwrap();
        fs::write(path, serde_yaml::to_string(&config).unwrap()).unwrap();
    }

    pub fn root(&self) -> &Path {
        self.run.root()
    }

    /// Physical module-tree root configured for this fixture's graph chunk.
    pub fn module_tree_root(&self) -> String {
        let config: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(self.root().join("spec_config.yaml")).unwrap())
                .unwrap();
        let report = self.dependency_graph();
        let Some(roots) = config
            .get("module_roots")
            .and_then(serde_yaml::Value::as_mapping)
        else {
            return String::new();
        };
        roots
            .iter()
            .find_map(|(root, chunk)| {
                (chunk.as_str() == Some(&report.chunk_id))
                    .then(|| root.as_str().unwrap().to_owned())
            })
            .expect("fixture chunk has a configured module tree root")
    }

    /// Convert a logical destination to the configured physical tree path.
    pub fn physical_module_path(&self, module: &str) -> String {
        let root = self.module_tree_root();
        if root.is_empty() || std::path::Path::new(module).starts_with(&root) {
            module.to_owned()
        } else {
            std::path::Path::new(&root)
                .join(module)
                .display()
                .to_string()
        }
    }

    /// Strip this chunk's configured physical tree prefix from a destination.
    pub fn logical_module_path(&self, module: &str) -> String {
        let root = self.module_tree_root();
        let logical = if root.is_empty() {
            module
        } else {
            std::path::Path::new(module)
                .strip_prefix(&root)
                .ok()
                .and_then(std::path::Path::to_str)
                .unwrap_or(module)
        };
        spec::ModulePath::parse(logical, "")
            .expect("fixture module path is valid")
            .as_str()
            .to_owned()
    }

    /// Persist a batch with each logical destination converted to its physical
    /// tree path for `modules apply --tree-config`.
    pub fn write_physical_assignment_batch(
        &self,
        name: &str,
        assignments: &serde_json::Value,
    ) -> PathBuf {
        let mut assignments = assignments.clone();
        for change in assignments.as_array_mut().unwrap() {
            change["module"] = self
                .physical_module_path(change["module"].as_str().unwrap())
                .into();
        }
        let path = self.root().join(name);
        fs::write(&path, serde_json::to_vec(&assignments).unwrap()).unwrap();
        path
    }

    /// Write the exact final nonentry owner priority for this fixture.
    /// Existing owners retain baseline relative order; new owners follow the
    /// caller-supplied sequence. This makes owner removal and append order
    /// explicit in each test instead of guessing from the mutation batch.
    pub fn write_import_order(
        &self,
        name: &str,
        final_modules: &BTreeSet<String>,
        appended_modules: &[String],
    ) -> PathBuf {
        let report = self.dependency_graph();
        let mut order: Vec<String> = report
            .import_order
            .iter()
            .filter(|path| final_modules.contains(path.as_str()))
            .map(ToString::to_string)
            .collect();
        order.extend(appended_modules.iter().cloned());
        assert_eq!(
            order.iter().collect::<BTreeSet<_>>(),
            final_modules.iter().collect::<BTreeSet<_>>(),
            "baseline plus explicit appended order must cover the final module set exactly"
        );
        assert_eq!(
            order.len(),
            final_modules.len(),
            "import order has duplicates"
        );
        let path = self.root().join(name);
        fs::write(&path, serde_json::to_vec(&order).unwrap()).unwrap();
        path
    }

    /// Write the exact final order for a source-unit assignment batch while
    /// preserving baseline order for surviving owners and appending new ones.
    pub fn write_assignment_import_order(
        &self,
        name: &str,
        assignments: &serde_json::Value,
    ) -> PathBuf {
        let report = self.dependency_graph();
        let changes: BTreeMap<_, _> = assignments
            .as_array()
            .unwrap()
            .iter()
            .map(|change| {
                (
                    change["unit_id"].as_str().unwrap().to_owned(),
                    self.logical_module_path(change["module"].as_str().unwrap()),
                )
            })
            .collect();
        let baseline: BTreeSet<_> = report
            .import_order
            .iter()
            .map(ToString::to_string)
            .collect();
        // Authored modules remain emitted shells until explicit deletion.
        // Generated atomic-group modules are emitted only while they still own
        // at least one source unit after the batch, and the emitter renumbers
        // those surviving groups densely.
        let mut final_owners = Vec::with_capacity(report.nodes.len());
        for unit in &report.nodes {
            let current = report.module(&unit.destination).unwrap().path.as_str();
            let final_owner = changes
                .get(&unit.id)
                .cloned()
                .or_else(|| baseline.contains(current).then(|| current.to_owned()));
            final_owners
                .push(final_owner.map(|owner| spec::ModulePath::parse(&owner, "").unwrap()));
        }
        let generated_remap =
            peel::assignment::reindex_generated_atomic_group_owners(&mut final_owners).unwrap();
        let remap = |path: &spec::ModulePath| -> Option<String> {
            if path.as_str().starts_with("__auto/atomic_groups/") {
                generated_remap.get(path).map(ToString::to_string)
            } else {
                Some(path.to_string())
            }
        };

        let mut final_modules: BTreeSet<_> = baseline
            .iter()
            .filter(|module| !module.starts_with("__auto/atomic_groups/"))
            .cloned()
            .collect();
        final_modules.extend(final_owners.iter().flatten().map(ToString::to_string));

        let mut order: Vec<_> = report
            .import_order
            .iter()
            .filter_map(remap)
            .filter(|path| final_modules.contains(path))
            .collect();
        let appended: BTreeSet<_> = final_owners
            .iter()
            .flatten()
            .map(ToString::to_string)
            .filter(|owner| !order.contains(owner))
            .collect();
        order.extend(appended);
        assert_eq!(
            order.iter().collect::<BTreeSet<_>>(),
            final_modules.iter().collect::<BTreeSet<_>>(),
            "source-unit assignment order must cover the materialized module set exactly"
        );
        assert_eq!(
            order.len(),
            final_modules.len(),
            "import order has duplicates"
        );
        let path = self.root().join(name);
        fs::write(&path, serde_json::to_vec(&order).unwrap()).unwrap();
        path
    }
    /// Module paths are relative to this fixture's single `main` chunk tree.
    pub fn new(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::run(source, modules, true)
    }

    /// A rejected real spec still emits graph and gate diagnostic artifacts.
    pub fn rejected(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::run(source, modules, false)
    }

    pub fn with_dependency(source: &str, dependency: &str, modules: &[(&str, &str)]) -> Self {
        Self::run_chunks(
            &[("main", source), ("dependency", dependency)],
            modules,
            true,
            false,
        )
    }

    fn run(source: &str, modules: &[(&str, &str)], succeeds: bool) -> Self {
        Self::run_chunks(&[("main", source)], modules, succeeds, false)
    }

    pub fn per_atomic_group(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::run_chunks(&[("main", source)], modules, true, true)
    }

    fn run_chunks(
        chunks: &[(&str, &str)],
        modules: &[(&str, &str)],
        succeeds: bool,
        per_atomic_group: bool,
    ) -> Self {
        let files: Vec<_> = modules
            .iter()
            .map(|(path, yaml)| (format!("main/{path}"), *yaml))
            .collect();
        let files: Vec<_> = files
            .iter()
            .map(|(path, yaml)| (path.as_str(), *yaml))
            .collect();
        let workspace = write_tree_fixture(&TreeFixture {
            chunks,
            module_roots: &[("main", "main")],
            modules: &files,
        });
        let run = workspace.run(&["--graphs"]);
        if !per_atomic_group {
            assert_eq!(
                run.result.status.success(),
                succeeds,
                "{}",
                run.result.stderr
            );
        }
        let fixture = Self::from_tree_run(run, "modules/main");
        if per_atomic_group {
            // This intermediate inline assignment may fail the gate. Its
            // diagnostic graph still identifies the generated groups; the
            // configured per-atom assignment below must then pass normally.
            let baseline = fixture.dependency_graph();
            let generated_groups = baseline
                .atomic_graph
                .nodes
                .iter()
                .filter(|group| {
                    group
                        .destinations
                        .iter()
                        .any(|destination| baseline.is_unassigned_or_entry(destination))
                })
                .count();
            let config_path = fixture.root().join("spec_config.yaml");
            let mut config: serde_yaml::Value =
                serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
            config["unassigned_mode"]["main"] =
                serde_yaml::from_str("{kind: per_atomic_group}").unwrap();
            let mut priority: Vec<_> = baseline
                .import_order
                .iter()
                .map(ToString::to_string)
                .collect();
            priority.extend(
                (0..generated_groups).map(|index| format!("__auto/atomic_groups/{index:04}")),
            );
            config["import_order"]["main"] = serde_yaml::to_value(priority).unwrap();
            fs::write(config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
            fixture.refresh_graph();
        }
        fixture
    }

    /// Wrap a tree run using a custom module root relative to its case directory.
    /// The owned run keeps its case and command journal alive.
    pub fn from_tree_run(run: TreeRun, modules_root: impl AsRef<Path>) -> Self {
        let graph = run.report_root.join("main/dependency_graph.json.gz");
        let modules = run.root().join(modules_root);
        Self {
            run,
            modules,
            graph,
        }
    }

    /// A real rebind forces these two declarations into one atomic group.
    pub fn atomic_pair() -> Self {
        Self::new(
            "let alpha = 0;\nfunction beta() { alpha = 1; }\nconsole.log(alpha, typeof beta);\n",
            &[(
                "home/atom.yaml",
                "members: [{selector: {binding: {name: alpha}}}, {selector: {binding: {name: beta}}}]\n",
            )],
        )
    }

    pub fn acyclic_pair() -> Self {
        Self::new(
            "const beta = 1;\nconst alpha = beta + 1;\nconsole.log(alpha);\n",
            &[
                (
                    "a.yaml",
                    "members: [{selector: {binding: {name: alpha}}}]\n",
                ),
                ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
            ],
        )
    }

    /// Merging the endpoints a/b makes a cycle through the middle c.
    pub fn dependency_chain() -> Self {
        Self::new(
            "const beta = 1;\nconst gamma = beta + 1;\nconst alpha = gamma + 1;\nconsole.log(alpha);\n",
            &[
                (
                    "a.yaml",
                    "members: [{selector: {binding: {name: alpha}}}]\n",
                ),
                ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
                (
                    "c.yaml",
                    "members: [{selector: {binding: {name: gamma}}}]\n",
                ),
            ],
        )
    }

    pub fn source_path(&self) -> PathBuf {
        self.run.root().join("snapshot/main.js")
    }

    pub fn assert_success(&self, args: &[&str]) {
        let out = self.command(args);
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    /// Both edit modes must refuse for the intended reason, without changing
    /// any spec bytes or creating/deleting files (including nested modules).
    pub fn assert_rejected_unchanged(&self, args: &[&str], diagnostics: &[&str]) {
        self.assert_rejected_unchanged_with_graph(args, diagnostics, true);
    }

    /// Test an explicitly unverified edit without supplying a saved graph.
    pub fn assert_rejected_unchanged_without_graph(&self, args: &[&str], diagnostics: &[&str]) {
        self.assert_rejected_unchanged_with_graph(args, diagnostics, false);
    }

    fn assert_rejected_unchanged_with_graph(
        &self,
        args: &[&str],
        diagnostics: &[&str],
        with_graph: bool,
    ) {
        let before = read_tree(&self.modules);
        let mut codes = Vec::new();
        for dry_run in [true, false] {
            let mut args = args.to_vec();
            if dry_run {
                args.push("--dry-run");
            }
            let mut command = self.process(&args);
            if !with_graph {
                command.env_remove("DEBUNDLE_GRAPH");
            }
            let out =
                debundle_fixture_output(&mut command, self.root()).expect("run fixture command");
            let stderr = String::from_utf8_lossy(&out.stderr);
            // `debundle` exits 1 when it refuses an edit and clap exits 2 on a usage error; a
            // panic exits 101 and is no refusal.
            assert!(
                matches!(out.status.code(), Some(1 | 2)),
                "{args:?}: expected rejection: {stderr}"
            );
            for diagnostic in diagnostics {
                assert!(stderr.contains(diagnostic), "{args:?}: {stderr}");
            }
            assert_eq!(
                read_tree(&self.modules),
                before,
                "{args:?}: spec changed after refusal"
            );
            codes.push(out.status.code());
        }
        assert_eq!(codes[0], codes[1], "dry-run and apply must agree");
    }

    /// Configure only this child process, including optional editor overrides.
    pub fn process(&self, args: &[&str]) -> Command {
        let mut command = Command::new(debundler_path());
        command
            .args(args)
            .env("DEBUNDLE_MODULES", &self.modules)
            .env("DEBUNDLE_GRAPH", &self.graph)
            .env("DEBUNDLE_SOURCE_ROOT", self.run.root().join("snapshot"));
        command
    }

    pub fn command(&self, args: &[&str]) -> std::process::Output {
        debundle_fixture_output(&mut self.process(args), self.root()).expect("run fixture command")
    }

    pub fn json(&self, args: &[&str]) -> Value {
        let mut args = args.to_vec();
        args.extend(["--format", "json"]);
        let out = self.command(&args);
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        parse_stdout_json(&out)
    }

    /// Re-run the edited spec, then execute its emitted entry under Node.
    pub fn assert_runs(&self, expected: &str) {
        let rerun = self.rerun();
        assert_node_output(&rerun.path().join("app/main/entry.js"), expected, "");
    }

    /// Refresh the saved graph after deliberate changes to valid authoring inputs.
    pub fn refresh_graph(&self) {
        let rerun = self.rerun();
        let fresh_graph = rerun
            .path()
            .join("reports/tree/main/dependency_graph.json.gz");
        fs::copy(
            program_graph_path(&fresh_graph),
            program_graph_path(&self.graph),
        )
        .expect("refresh fixture program graph from the real pipeline");
        fs::copy(&fresh_graph, &self.graph).expect("refresh fixture graph from the real pipeline");
    }

    /// Run the current tree spec through the real pipeline in an owned child workspace.
    pub fn rerun(&self) -> CaseWorkspace {
        let rerun = self.run.workspace().child();
        let out = self.run_current_spec(rerun.path());
        process::assert_success(&out);
        rerun
    }

    /// Run validation against the current input spec, independent of the
    /// saved graph used by CLI edit commands.
    pub fn assert_current_pipeline_rejects(&self, diagnostics: &[&str]) {
        let output_root = self.run.workspace().child();
        let out = self.run_current_spec(output_root.path());
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(
            out.status.code(),
            Some(1),
            "current spec should fail validation: {stderr}"
        );
        for diagnostic in diagnostics {
            assert!(stderr.contains(diagnostic), "{stderr}");
        }
        assert!(
            !output_root.path().join("app/main/entry.js").exists(),
            "rejected current spec emitted an accepted app entry"
        );
    }

    /// Independent assignment oracle: resolve and materialize the on-disk spec
    /// through the normal CLI, then check accepted output under Node. No saved
    /// graph or proposal/check implementation participates in this verdict.
    pub fn current_spec_runs(&self, expected: &str) -> bool {
        let output_root = self.run.workspace().child();
        let out = self.run_current_spec(output_root.path());
        if out.status.success() {
            assert_node_output(&output_root.path().join("app/main/entry.js"), expected, "");
            true
        } else {
            let stderr = String::from_utf8_lossy(&out.stderr);
            assert_eq!(out.status.code(), Some(1), "{stderr}");
            assert!(
                stderr.contains("unrealizable") || stderr.contains("atomic group"),
                "oracle fixture failed outside the assignment gate: {stderr}"
            );
            assert!(!output_root.path().join("app/main/entry.js").exists());
            false
        }
    }

    fn run_current_spec(&self, out_root: &Path) -> std::process::Output {
        let root = self.run.root();
        debundle_fixture_output(
            Command::new(debundler_path())
                .arg("run")
                .arg("--graphs")
                .args(tree_spec_args(root))
                .arg("--out-root")
                .arg(out_root),
            root,
        )
        .unwrap()
    }

    pub fn dependency_graph(&self) -> DependencyGraphReport {
        let report = read_gzip_json(&self.graph);
        assert_no_purity_disagreements(&report);
        report
    }

    pub fn program_graph(&self) -> ProgramGraphReport {
        read_program_graph(&self.graph)
    }

    /// Saved-graph source-unit id that declares `binding`.
    pub fn source_unit_for_binding(&self, binding: &str) -> String {
        let [unit] = self.source_units_for_bindings([binding]);
        unit
    }

    /// Look up related bindings from one current graph snapshot.
    pub fn source_units_for_bindings<const N: usize>(&self, bindings: [&str; N]) -> [String; N] {
        let graph = self.dependency_graph();
        bindings.map(|binding| source_unit_for_binding(&graph, binding).to_string())
    }

    /// Saved-graph source-unit id whose statement starts on source `line`.
    pub fn source_unit_for_line(&self, line: usize) -> String {
        source_unit_for_line(&self.dependency_graph(), line).to_string()
    }
}
