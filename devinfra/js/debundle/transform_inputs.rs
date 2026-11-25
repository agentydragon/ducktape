//! Spec loading and command inputs shared by placement resolution and the transform pipeline.
use anyhow::{Context, Result, bail};
use clap::{ArgMatches, Args as ClapArgs, parser::ValueSource};
use package_args::{PackageRootArgs, collect_package_roots};
use serde::{Deserialize, Serialize};
use spec::TransformSpec;
use spec_modules::read_flat_transform_spec;
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree_with_roots};
use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformRequest {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

pub fn load_transform_spec_source(source: &TransformSpecSource) -> Result<TransformSpec> {
    Ok(load_transform_spec_with_roots(source)?.0)
}

pub fn load_transform_spec_with_roots(
    source: &TransformSpecSource,
) -> Result<(TransformSpec, BTreeMap<String, Vec<PathBuf>>)> {
    match source {
        TransformSpecSource::Flat { path } => {
            Ok((read_flat_transform_spec(path)?, BTreeMap::new()))
        }
        TransformSpecSource::Tree(options) => compile_spec_tree_with_roots(options),
    }
}

/// Spec, source and package inputs shared by runs and assessment-only callers.
#[derive(ClapArgs, Debug, Clone, Default)]
pub struct TransformContextArgs {
    #[command(flatten)]
    pub source: SpecSourceArgs,
    #[command(flatten)]
    pub package_sources: PackageRootArgs,
}

#[derive(ClapArgs, Debug, Clone, Default)]
pub struct SpecSourceArgs {
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
    pub tree: TreePathArgs,
}

/// Common source, output and package inputs shared by transform context
/// consumers that already own the tree config and module root arguments.
#[derive(ClapArgs, Debug, Clone, Default)]
pub struct TransformContextAuxArgs {
    #[command(flatten)]
    pub tree: TreePathArgs,
    #[command(flatten)]
    pub package_sources: PackageRootArgs,
}

#[derive(ClapArgs, Debug, Clone, Default)]
pub struct TreePathArgs {
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
}

impl TransformContextArgs {
    /// Collapse `--package-root` pairs into a `HashMap` and validate spec source.
    pub fn resolve(self) -> Result<TransformRequest> {
        let spec_source = self.source.resolve()?;
        Ok(TransformRequest {
            spec_source,
            package_roots: collect_package_roots(self.package_sources.package_roots)?
                .into_iter()
                .collect(),
            packages_root: self.package_sources.packages_root,
        })
    }

    pub fn has_explicit_auxiliary_inputs(&self) -> bool {
        self.source.tree_modules.is_some()
            || self.source.tree.has_explicit_inputs()
            || !self.package_sources.package_roots.is_empty()
            || self.package_sources.packages_root.is_some()
    }
}

impl TreePathArgs {
    fn has_explicit_inputs(&self) -> bool {
        self.tree_vendor_marks.is_some() || self.out_root.is_some()
    }
}

impl TransformContextAuxArgs {
    pub fn has_any_inputs(&self) -> bool {
        self.tree.tree_source_root.is_some()
            || self.tree.has_explicit_inputs()
            || !self.package_sources.package_roots.is_empty()
            || self.package_sources.packages_root.is_some()
    }
}

impl SpecSourceArgs {
    pub fn resolve(&self) -> Result<TransformSpecSource> {
        match (&self.spec, &self.tree_config) {
            (Some(_), Some(_)) => {
                bail!("pass either --spec or --tree-config, not both");
            }
            (Some(path), None) => Ok(TransformSpecSource::Flat { path: path.clone() }),
            (None, Some(config_path)) => {
                let modules_root = required_tree_arg("--tree-modules", &self.tree_modules)?;
                let vendor_marks_path =
                    required_tree_arg("--tree-vendor-marks", &self.tree.tree_vendor_marks)?;
                let out_root = required_tree_arg("--out-root", &self.tree.out_root)?;
                Ok(TransformSpecSource::Tree(CompileSpecTreeOptions {
                    config_path: config_path.clone(),
                    modules_root,
                    vendor_marks_path,
                    source_root: self.tree.tree_source_root.clone(),
                    out_root,
                }))
            }
            (None, None) => {
                bail!("pass either --spec or --tree-config");
            }
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
    let context_group = <SpecSourceArgs as clap::Args>::group_id()
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
