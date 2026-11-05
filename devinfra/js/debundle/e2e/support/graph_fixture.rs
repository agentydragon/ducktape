//! Real JS/spec inputs for graph-backed query and edit workflows.

use super::*;
use debundle_e2e_support::*;
use std::collections::BTreeMap;
use std::fs;
use std::process::Command;
use tempfile::TempDir;

pub struct GraphFixture {
    run: TreeRun,
    pub modules: PathBuf,
    pub graph: PathBuf,
}

impl GraphFixture {
    pub fn root(&self) -> &Path {
        self.run.root()
    }
    /// Module paths are relative to this fixture's single `main` chunk tree.
    pub fn new(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::run(source, modules, true)
    }

    /// One-chunk full tree for edit tests that need a canonical staged
    /// assessment before a verified ownership write.
    pub fn new_full_tree(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::full_tree(source, modules, true)
    }

    /// Source facts can assess a repair even when the original assignment
    /// is rejected. This never certifies that original assignment.
    pub fn rejected_full_tree(source: &str, modules: &[(&str, &str)]) -> Self {
        Self::full_tree(source, modules, false)
    }

    fn full_tree(source: &str, modules: &[(&str, &str)], succeeds: bool) -> Self {
        let run = run_tree_graph_fixture(
            &TreeFixture {
                chunks: &[("main", source)],
                module_roots: &[],
                modules,
            },
            &[],
        );
        assert_eq!(
            run.result.status.success(),
            succeeds,
            "{}",
            run.result.stderr
        );
        Self {
            graph: run.report_root.join("main/dependency_graph.json.gz"),
            modules: run.root().join("modules"),
            run,
        }
    }

    /// Stage typed facts from the original full-tree source/projection. The
    /// edit command assesses its own exact candidate documents against these
    /// facts; this artifact is never synthesized from the requested edit.
    pub fn stage_analysis(&self) -> PathBuf {
        let root = self.run.root();
        assert_eq!(
            self.modules,
            root.join("modules"),
            "full tree fixture required"
        );
        let projection = root.join("edit-analysis.projection");
        let artifact = root.join("edit-analysis.dba");
        let project = Command::new(debundler_path())
            .args(["spec", "project-analysis", "--tree-config"])
            .arg(root.join("spec_config.yaml"))
            .arg("--tree-modules")
            .arg(&self.modules)
            .arg("--tree-vendor-marks")
            .arg(root.join("vendor_marks.yaml"))
            .arg("--tree-source-root")
            .arg(root)
            .arg("--out-root")
            .arg(root.join("edit-project-out"))
            .arg("--out")
            .arg(&projection)
            .output()
            .unwrap();
        assert!(
            project.status.success(),
            "{}",
            String::from_utf8_lossy(&project.stderr)
        );
        let analyze = Command::new(analyzer_path())
            .arg("--projection")
            .arg(&projection)
            .arg("--tree-source-root")
            .arg(root)
            .arg("--out")
            .arg(&artifact)
            .output()
            .unwrap();
        assert!(
            analyze.status.success(),
            "{}",
            String::from_utf8_lossy(&analyze.stderr)
        );
        artifact
    }

    pub fn process_with_assessment(&self, args: &[&str], analysis: &Path) -> Command {
        let root = self.run.root();
        let mut command = self.process(args);
        command
            .arg("--tree-config")
            .arg(root.join("spec_config.yaml"))
            .arg("--tree-vendor-marks")
            .arg(root.join("vendor_marks.yaml"))
            .arg("--tree-source-root")
            .arg(root)
            .arg("--out-root")
            .arg(root.join("edit-out"))
            .arg("--analysis")
            .arg(analysis);
        if args.first() == Some(&"bindings")
            || args.get(1) == Some(&"merge")
            || args.get(1) == Some(&"delete")
        {
            command.arg("--tree-modules").arg(&self.modules);
        }
        command
    }

    pub fn json_with_assessment(&self, args: &[&str], analysis: &Path) -> Value {
        let output = self
            .process_with_assessment(args, analysis)
            .args(["--format", "json"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        parse_stdout_json(&output)
    }

    pub fn assert_success_with_assessment(&self, args: &[&str], analysis: &Path) {
        let output = self
            .process_with_assessment(args, analysis)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    pub fn assert_rejected_unchanged_with_assessment(
        &self,
        args: &[&str],
        analysis: &Path,
        diagnostics: &[&str],
    ) -> Value {
        fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
            let mut files = BTreeMap::new();
            for entry in fs::read_dir(root).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    files.extend(snapshot(&path));
                } else {
                    files.insert(path.clone(), fs::read(path).unwrap());
                }
            }
            files
        }
        let before = snapshot(&self.modules);
        let preview = self
            .process_with_assessment(args, analysis)
            .arg("--dry-run")
            .args(["--format", "json"])
            .output()
            .unwrap();
        assert!(
            preview.status.success(),
            "preview failed: {}",
            String::from_utf8_lossy(&preview.stderr)
        );
        let preview_report = parse_stdout_json(&preview);
        assert_eq!(preview_report["action"], "dry-run", "{preview_report}");
        assert_eq!(preview_report["gate"], "claims_checked", "{preview_report}");
        assert!(
            matches!(
                preview_report["effect_relocation_assessment"]["status"].as_str(),
                Some(
                    "blocked_by_effect"
                        | "blocked_by_placement"
                        | "blocked_by_effect_and_placement"
                        | "incomplete"
                )
            ),
            "{preview_report}"
        );
        assert_eq!(snapshot(&self.modules), before, "preview changed spec");
        let output = self
            .process_with_assessment(args, analysis)
            .args(["--format", "json"])
            .output()
            .unwrap();
        let rejection = parse_stdout_json(&output);
        assert_eq!(
            rejection["rejection"], preview_report["effect_relocation_assessment"],
            "{rejection}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            !output.status.success(),
            "{args:?} unexpectedly succeeded: {stderr}"
        );
        for diagnostic in diagnostics {
            assert!(stderr.contains(diagnostic), "{args:?}: {stderr}");
        }
        assert_eq!(snapshot(&self.modules), before, "{args:?}: spec changed");
        rejection["rejection"].clone()
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
        )
    }

    fn run(source: &str, modules: &[(&str, &str)], succeeds: bool) -> Self {
        Self::run_chunks(&[("main", source)], modules, succeeds)
    }

    fn run_chunks(chunks: &[(&str, &str)], modules: &[(&str, &str)], succeeds: bool) -> Self {
        let files: Vec<_> = modules
            .iter()
            .map(|(path, yaml)| (format!("main/{path}"), *yaml))
            .collect();
        let files: Vec<_> = files
            .iter()
            .map(|(path, yaml)| (path.as_str(), *yaml))
            .collect();
        let run = run_tree_graph_fixture(
            &TreeFixture {
                chunks,
                module_roots: &[("main", "main")],
                modules: &files,
            },
            &[],
        );
        assert_eq!(
            run.result.status.success(),
            succeeds,
            "{}",
            run.result.stderr
        );
        let modules_root = run.root().join("modules/main");
        Self {
            graph: run.report_root.join("main/dependency_graph.json.gz"),
            modules: modules_root,
            run,
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

    pub fn acyclic_pair_full_tree() -> Self {
        Self::new_full_tree(
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
        fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
            let mut files = BTreeMap::new();
            for entry in fs::read_dir(root).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    files.extend(snapshot(&path));
                } else {
                    files.insert(path.clone(), fs::read(path).unwrap());
                }
            }
            files
        }
        let before = snapshot(&self.modules);
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
            let out = command.output().expect("run fixture command");
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
                snapshot(&self.modules),
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
        self.process(args).output().expect("run fixture command")
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

    fn rerun(&self) -> TempDir {
        let rerun = TempDir::new_in(self.run.root()).expect("create fresh output directory");
        let out = self.run_current_spec(rerun.path());
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        rerun
    }

    /// Run validation against the current input spec, independent of the
    /// saved graph used by CLI edit commands.
    pub fn assert_current_pipeline_rejects(&self, diagnostics: &[&str]) {
        let output_root = TempDir::new_in(self.run.root())
            .expect("create current-spec validation output directory");
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

    fn run_current_spec(&self, out_root: &Path) -> std::process::Output {
        let root = self.run.root();
        Command::new(debundler_path())
            .arg("run")
            .arg("--graphs")
            .arg("--tree-config")
            .arg(root.join("spec_config.yaml"))
            .arg("--tree-modules")
            .arg(root.join("modules"))
            .arg("--tree-vendor-marks")
            .arg(root.join("vendor_marks.yaml"))
            .arg("--tree-source-root")
            .arg(root)
            .arg("--out-root")
            .arg(out_root)
            .output()
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
}
