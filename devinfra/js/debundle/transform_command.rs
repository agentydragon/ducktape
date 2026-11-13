//! Command syntax and environment policy for transform and projection requests.

use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use clap::{ArgMatches, Args as ClapArgs, parser::ValueSource};
use package_args::{collect_package_roots, parse_package_root_kv};
use pipeline::{RunOutput, TransformRequest, TransformSpecSource, project_analysis};
use spec_tree::CompileSpecTreeOptions;

/// Arguments for the standalone projection executable and
/// `debundle spec project-analysis`.
#[derive(Debug, ClapArgs)]
pub struct ProjectAnalysisArgs {
    #[command(flatten)]
    transform: TransformArgs,
    /// Where to write the projection.
    #[arg(long)]
    out: PathBuf,
}

impl ProjectAnalysisArgs {
    /// Resolve the shared transform arguments and write the canonical
    /// analysis projection.
    pub fn run(self) -> Result<()> {
        command_diagnostics::run(|diagnostics| {
            project_analysis(&self.transform.context.resolve()?, &self.out, diagnostics)
        })
    }
}

/// Spec, source and package inputs shared by runs and assessment-only callers.
#[derive(ClapArgs, Debug, Clone, Default)]
pub struct TransformContextArgs {
    /// Path to a flat transform spec YAML. Pass exactly one of `--spec`
    /// / `--tree-config`.
    #[arg(long)]
    pub spec: Option<PathBuf>,
    /// Path to a tree-shaped authoring config YAML. Requires
    /// `--tree-modules`, `--tree-vendor-marks`, and `--out-root`.
    #[arg(long = "tree-config")]
    pub tree_config: Option<PathBuf>,
    /// Root directory containing tree-shaped logical module YAML files.
    #[arg(long = "tree-modules")]
    pub tree_modules: Option<PathBuf>,
    #[command(flatten)]
    pub auxiliary: TransformContextAuxArgs,
}

/// Common source, output and package inputs shared by transform context
/// consumers that already own the tree config and module root arguments.
#[derive(ClapArgs, Debug, Clone, Default)]
pub struct TransformContextAuxArgs {
    /// Path to tree-shaped vendor marks YAML.
    #[arg(long = "tree-vendor-marks")]
    pub tree_vendor_marks: Option<PathBuf>,
    /// Root for source-relative paths embedded in the tree-shaped config YAML.
    /// Deliberately NOT `DEBUNDLE_SOURCE_ROOT`: that env var is the query
    /// commands' upstream-snapshot root, a different directory in real corpora.
    #[arg(long = "tree-source-root", env = "DEBUNDLE_TREE_SOURCE_ROOT")]
    pub tree_source_root: Option<PathBuf>,
    /// Output root used when compiling tree-shaped authoring sources.
    #[arg(long = "out-root", env = "DEBUNDLE_OUT")]
    pub out_root: Option<PathBuf>,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
}

impl TransformContextArgs {
    /// Collapse `--package-root` pairs into a `HashMap` and validate spec source.
    pub fn resolve(self) -> Result<TransformRequest> {
        let spec_source = resolve_spec_source(&self)?;
        Ok(TransformRequest {
            spec_source,
            package_roots: collect_package_roots(self.auxiliary.package_roots)?
                .into_iter()
                .collect(),
            packages_root: self.auxiliary.packages_root,
        })
    }

    pub fn has_explicit_auxiliary_inputs(&self) -> bool {
        self.tree_modules.is_some() || self.auxiliary.has_explicit_inputs()
    }
}

impl TransformContextAuxArgs {
    pub fn has_explicit_inputs(&self) -> bool {
        self.tree_vendor_marks.is_some()
            || self.out_root.is_some()
            || !self.package_roots.is_empty()
            || self.packages_root.is_some()
    }

    /// Whether any transform input is present, including environment values.
    /// `modules apply` and `modules refine` use this after their command
    /// registrations disable the source/output environment defaults.
    pub fn has_any_inputs(&self) -> bool {
        self.tree_vendor_marks.is_some()
            || self.tree_source_root.is_some()
            || self.out_root.is_some()
            || !self.package_roots.is_empty()
            || self.packages_root.is_some()
    }
}

/// Run-only output and diagnostic flags layered over transform inputs.
#[derive(ClapArgs, Debug, Clone)]
pub struct TransformArgs {
    #[command(flatten)]
    pub context: TransformContextArgs,
    /// Run pipeline parse/facts/gate checks without writing emitted JS
    /// or accept-path reports. A gate rejection still writes its evidence
    /// under reports/tree/<chunk>/. Add --graphs for graph-backed gate queries.
    #[arg(long)]
    pub dry_run: bool,
    /// When the realizability gate rejects the spec, still write the
    /// logical-module tree for reading — marked unrunnable by a
    /// `GATE_REJECTED` file at the output root and a banner comment on each
    /// rejected chunk's entry file — without the browser harness or vendor
    /// assets. The gate reports are written as usual and the run still exits
    /// nonzero. Accepted specs are unaffected.
    #[arg(long = "emit-on-rejection", conflicts_with = "dry_run")]
    pub emit_on_rejection: bool,
    /// Stop at the first supported diagnostic instead of collecting all
    /// findings from the pass. Broad runs otherwise aggregate every
    /// supported diagnostic — currently unresolved source-match
    /// selectors and duplicate binding claims, with
    /// module/export/origin evidence — before failing.
    #[arg(long)]
    pub fail_fast: bool,
}

impl TransformArgs {
    /// `--dry-run` / `--emit-on-rejection` as one output mode; Clap already
    /// refuses both together.
    pub fn run_output(&self) -> Result<RunOutput> {
        match (self.dry_run, self.emit_on_rejection) {
            (false, false) => Ok(RunOutput::Accepted),
            (true, false) => Ok(RunOutput::DryRun),
            (false, true) => Ok(RunOutput::EmitOnRejection),
            (true, true) => bail!("--dry-run and --emit-on-rejection are mutually exclusive"),
        }
    }
}

fn resolve_spec_source(args: &TransformContextArgs) -> Result<TransformSpecSource> {
    match (&args.spec, &args.tree_config) {
        (Some(_), Some(_)) => {
            bail!("pass either --spec or --tree-config, not both");
        }
        (Some(path), None) => Ok(TransformSpecSource::Flat { path: path.clone() }),
        (None, Some(config_path)) => {
            let modules_root = required_tree_arg("--tree-modules", &args.tree_modules)?;
            let vendor_marks_path =
                required_tree_arg("--tree-vendor-marks", &args.auxiliary.tree_vendor_marks)?;
            let out_root = required_tree_arg("--out-root", &args.auxiliary.out_root)?;
            Ok(TransformSpecSource::Tree(CompileSpecTreeOptions {
                config_path: config_path.clone(),
                modules_root,
                vendor_marks_path,
                source_root: args.auxiliary.tree_source_root.clone(),
                out_root,
            }))
        }
        (None, None) => {
            bail!("pass either --spec or --tree-config");
        }
    }
}

fn required_tree_arg(name: &str, value: &Option<PathBuf>) -> Result<PathBuf> {
    value
        .clone()
        .with_context(|| format!("{name} is required with --tree-config"))
}

const TREE_INPUT_FLAGS: &[(&str, &str)] = &[
    ("tree_config", "--tree-config"),
    ("tree_modules", "--tree-modules"),
    ("tree_vendor_marks", "--tree-vendor-marks"),
    ("tree_source_root", "--tree-source-root"),
    ("out_root", "--out-root"),
];

fn is_flat_context(matches: &ArgMatches) -> bool {
    let context_group = <TransformContextArgs as clap::Args>::group_id()
        .expect("transform context has a Clap argument group");
    matches.try_contains_id(context_group.as_str()).is_ok()
        && matches
            .try_get_one::<PathBuf>("spec")
            .ok()
            .flatten()
            .is_some()
}

// `run --reports-only` consumes `--out-root` in both input modes and
// separately checks that the two output roots agree before any work starts.
fn is_flat_reports_output(matches: &ArgMatches, id: &str) -> bool {
    id == "out_root"
        && matches
            .try_get_one::<PathBuf>("reports_only")
            .ok()
            .flatten()
            .is_some()
}

/// Validate explicit mode conflicts while Clap retains value origins. Keep
/// inherited defaults available to tree mode; recurse only into the selected
/// command. Both executable entry points call this before dispatch.
pub fn validate_transform_arg_matches(matches: &ArgMatches) -> Result<()> {
    if is_flat_context(matches) {
        for &(id, flag) in TREE_INPUT_FLAGS {
            if !is_flat_reports_output(matches, id)
                && matches.try_contains_id(id).is_ok()
                && matches.value_source(id) == Some(ValueSource::CommandLine)
            {
                bail!("{flag} cannot be used with --spec; use --tree-config for tree inputs");
            }
        }
    }
    if let Some((_, selected)) = matches.subcommand() {
        validate_transform_arg_matches(selected)?;
    }
    Ok(())
}

/// Inactive defaults are omitted from invocation projections. Rendering them
/// as explicit replay flags would conflict with the selected flat mode.
pub fn is_inactive_transform_default(matches: &ArgMatches, id: &str) -> bool {
    is_flat_context(matches)
        && !is_flat_reports_output(matches, id)
        && TREE_INPUT_FLAGS.iter().any(|&(input, _)| input == id)
        && matches.value_source(id) != Some(ValueSource::CommandLine)
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{CommandFactory, FromArgMatches, Parser};

    #[derive(Parser)]
    struct Args {
        #[command(flatten)]
        context: TransformContextArgs,
    }

    fn resolve(argv: &[&str]) -> Result<TransformRequest> {
        // Inherited values have a different origin from explicit flags, as do
        // the existing environment-backed defaults on the real command.
        let matches = Args::command()
            .mut_arg("out_root", |arg| {
                arg.env(None::<&str>).default_value("inherited-output")
            })
            .mut_arg("tree_source_root", |arg| {
                arg.env(None::<&str>).default_value("inherited-source")
            })
            .try_get_matches_from(argv)?;
        validate_transform_arg_matches(&matches)?;
        Args::from_arg_matches(&matches)?.context.resolve()
    }

    #[test]
    fn flat_mode_refuses_every_explicit_tree_input() {
        for flag in [
            "--tree-config",
            "--tree-modules",
            "--tree-vendor-marks",
            "--tree-source-root",
            "--out-root",
        ] {
            let error =
                resolve(&["debundle", "--spec", "flat.yaml", flag, "explicit"]).unwrap_err();
            assert!(error.to_string().contains(flag), "{error:#}");
        }
    }

    #[test]
    fn flat_mode_keeps_shared_package_inputs_and_ignores_inherited_tree_defaults() {
        let request = resolve(&[
            "debundle",
            "--spec",
            "flat.yaml",
            "--package-root",
            "pkg=package-source",
            "--packages-root",
            "all-packages",
        ])
        .unwrap();
        assert_eq!(
            request.spec_source,
            TransformSpecSource::Flat {
                path: "flat.yaml".into()
            }
        );
        assert_eq!(
            request.package_roots.get("pkg"),
            Some(&PathBuf::from("package-source"))
        );
        assert_eq!(request.packages_root, Some("all-packages".into()));
    }

    #[test]
    fn tree_mode_keeps_inherited_source_and_output_defaults() {
        let request = resolve(&[
            "debundle",
            "--tree-config",
            "tree.yaml",
            "--tree-modules",
            "modules",
            "--tree-vendor-marks",
            "vendors.yaml",
        ])
        .unwrap();
        let TransformSpecSource::Tree(tree) = request.spec_source else {
            panic!("expected tree inputs")
        };
        assert_eq!(tree.out_root, PathBuf::from("inherited-output"));
        assert_eq!(tree.source_root, Some("inherited-source".into()));
    }
}
