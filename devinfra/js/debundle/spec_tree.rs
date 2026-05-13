use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Deserialize;

use spec::{
    AnonymousStatement, ChunkRenames, EmitBrowserHarnessConfig, LoadJsChunksArgs, LogicalModule,
    MaterializeLogicalModulesConfig, Member, SwapMark, SwapVendorChunksConfig, TransformSpec,
    VendorLevel, VendorMark, VendorRole, WrapperShape,
};
use spec_modules::{
    collect_module_files, is_deferred_yaml, module_path_from_file, read_module_file,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileSpecTreeOptions {
    pub config_path: PathBuf,
    pub modules_root: PathBuf,
    pub vendor_marks_path: PathBuf,
    pub source_root: Option<PathBuf>,
    pub out_root: PathBuf,
    pub force: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthoringConfig {
    main_chunk_id: String,
    inputs: AuthoringInputs,
    browser_harness: BrowserHarnessPolicy,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthoringInputs {
    root: PathBuf,
    js_list_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct BrowserHarnessPolicy {
    asset_summary_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct VendorMarksFile {
    #[serde(default)]
    vendor_marks: Vec<VendorMarkSource>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct VendorMarkSource {
    chunk_path: String,
    identity: String,
    #[serde(default)]
    role: VendorRole,
    level: VendorLevelSource,
    #[serde(default)]
    package: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    subpath: Option<String>,
    #[serde(default)]
    wrapper_shape: Option<WrapperShape>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum VendorLevelSource {
    Suppress,
    BoundaryRename,
    Swap,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModuleSource {
    chunk_id: String,
    path: String,
    #[serde(default)]
    members: Vec<Member>,
    #[serde(default)]
    anonymous_statements: Vec<AnonymousStatement>,
}

pub fn compile_spec_tree(options: &CompileSpecTreeOptions) -> Result<TransformSpec> {
    let config: AuthoringConfig = read_yaml(&options.config_path)?;
    let source_root = options.source_root.as_deref();
    let input_root = source_path(source_root, config.inputs.root);
    let js_list_path = source_path(source_root, config.inputs.js_list_path);
    let asset_summary_path = source_path(source_root, config.browser_harness.asset_summary_path);
    let layout = OutputLayout::new(options.out_root.clone());
    let (module_sources, deferred_members) =
        load_main_chunk_modules(&options.modules_root, &config.main_chunk_id)?;

    Ok(TransformSpec {
        inputs: LoadJsChunksArgs {
            input_root: input_root.clone(),
            js_list_path,
        },
        vendor: vendor_map(read_yaml::<VendorMarksFile>(&options.vendor_marks_path)?.vendor_marks)?,
        logical_modules: logical_modules_map(module_sources)?,
        residual_modules: BTreeMap::new(),
        chunk_renames: chunk_renames_map(&config.main_chunk_id, deferred_members),
        swap_vendor_chunks: SwapVendorChunksConfig {
            output_manifest_path: Some(layout.vendor_manifest_path.clone()),
            output_wrapper_dir: Some(layout.vendor_wrapper_root.clone()),
            write: true,
        },
        materialize_logical_modules: MaterializeLogicalModulesConfig {
            file: None,
            prune_other_chunks: false,
            force: options.force,
            report_out_dir: Some(layout.reports_root.clone()),
            report_summary_path: Some(layout.reports_root.join("summary.json")),
            target_dir: String::new(),
        },
        write_js_tree: None,
        emit_browser_harness: Some(EmitBrowserHarnessConfig {
            asset_summary_path,
            out_dir: layout.app_root,
            snapshot_root: input_root,
            force: options.force,
        }),
    })
}

fn source_path(source_root: Option<&Path>, path: PathBuf) -> PathBuf {
    if path.is_absolute() {
        path
    } else if let Some(root) = source_root {
        root.join(path)
    } else {
        path
    }
}

fn read_yaml<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    serde_yaml::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

#[derive(Debug, Clone)]
struct OutputLayout {
    app_root: PathBuf,
    reports_root: PathBuf,
    vendor_manifest_path: PathBuf,
    vendor_wrapper_root: PathBuf,
}

impl OutputLayout {
    fn new(app_root: PathBuf) -> Self {
        Self {
            reports_root: app_root.join("analysis/logical_modules"),
            vendor_manifest_path: app_root.join("vendors/manifest.json"),
            vendor_wrapper_root: app_root.join("vendors/generated"),
            app_root,
        }
    }
}

fn load_main_chunk_modules(
    modules_root: &Path,
    main_chunk_id: &str,
) -> Result<(Vec<ModuleSource>, Vec<Member>)> {
    let mut active = Vec::new();
    let mut deferred_members = Vec::new();
    for path in collect_module_files(modules_root)? {
        let is_deferred = is_deferred_yaml(&path);
        let module_path = module_path_from_file(&path, modules_root, is_deferred);
        let data = read_module_file(&path)?;
        if is_deferred {
            // Deferred files don't get materialized; their
            // anonymous_statements (if any) are dropped on the
            // floor — there's no logical module to attach them to.
            deferred_members.extend(data.members);
        } else {
            active.push(ModuleSource {
                chunk_id: main_chunk_id.to_string(),
                path: module_path,
                members: data.members,
                anonymous_statements: data.anonymous_statements,
            });
        }
    }
    active.sort_by(|left, right| left.path.cmp(&right.path));
    Ok((active, deferred_members))
}

fn vendor_map(sources: Vec<VendorMarkSource>) -> Result<BTreeMap<String, VendorMark>> {
    let mut out = BTreeMap::new();
    for source in sources {
        let chunk_path = source.chunk_path.clone();
        let identity = source.identity.clone();
        let role = source.role;
        let level = source.into_vendor_level()?;
        out.insert(
            chunk_path,
            VendorMark {
                identity,
                role,
                level,
            },
        );
    }
    Ok(out)
}

impl VendorMarkSource {
    fn into_vendor_level(self) -> Result<VendorLevel> {
        match self.level {
            VendorLevelSource::Suppress => {
                self.ensure_no_swap_payload()?;
                Ok(VendorLevel::Suppress)
            }
            VendorLevelSource::BoundaryRename => {
                self.ensure_no_swap_payload()?;
                Ok(VendorLevel::BoundaryRename)
            }
            VendorLevelSource::Swap => Ok(VendorLevel::Swap(SwapMark {
                package: self
                    .package
                    .with_context(|| format!("vendor mark {} missing package", self.chunk_path))?,
                version: self
                    .version
                    .with_context(|| format!("vendor mark {} missing version", self.chunk_path))?,
                subpath: self
                    .subpath
                    .with_context(|| format!("vendor mark {} missing subpath", self.chunk_path))?,
                wrapper_shape: self.wrapper_shape,
            })),
        }
    }

    fn ensure_no_swap_payload(&self) -> Result<()> {
        if self.package.is_some()
            || self.version.is_some()
            || self.subpath.is_some()
            || self.wrapper_shape.is_some()
        {
            bail!(
                "vendor mark {} has swap-only fields but level is not swap",
                self.chunk_path
            );
        }
        Ok(())
    }
}

fn logical_modules_map(
    sources: Vec<ModuleSource>,
) -> Result<BTreeMap<String, BTreeMap<String, LogicalModule>>> {
    let mut out = BTreeMap::new();
    for source in sources {
        let previous = out
            .entry(source.chunk_id.clone())
            .or_insert_with(BTreeMap::new)
            .insert(
                source.path.clone(),
                LogicalModule {
                    members: source.members,
                    anonymous_statements: source.anonymous_statements,
                },
            );
        if previous.is_some() {
            bail!(
                "duplicate logical module for chunk {} path {}",
                source.chunk_id,
                source.path
            );
        }
    }
    Ok(out)
}

fn chunk_renames_map(
    main_chunk_id: &str,
    deferred_members: Vec<Member>,
) -> BTreeMap<String, ChunkRenames> {
    if deferred_members.is_empty() {
        return BTreeMap::new();
    }
    BTreeMap::from([(
        main_chunk_id.to_string(),
        ChunkRenames {
            id: None,
            members: deferred_members,
        },
    )])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_yaml::Value;

    fn write_file(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn fixture(root: &Path) -> CompileSpecTreeOptions {
        let config = root.join("spec_config.yaml");
        let modules = root.join("modules");
        let vendor_marks = root.join("sources/vendor/vendor_marks.yaml");
        write_file(
            &config,
            r#"main_chunk_id: static/main
inputs:
  root: snapshots/test
  js_list_path: extracted/js-files.txt
browser_harness:
  asset_summary_path: extracted/asset-summary.json
"#,
        );
        write_file(
            &vendor_marks,
            r#"vendor_marks:
  - level: swap
    chunk_path: static/vendor.js
    identity: example
    role: worker
    package: pkg
    version: 1.2.3
    subpath: dist/index.js
    wrapper_shape: named_from_module_default
"#,
        );
        write_file(
            &modules.join("ui/active.yaml"),
            r#"members:
  - name: No
    selector:
      binding:
        name: No
        kind: variable_declarator
"#,
        );
        write_file(
            &modules.join("deferred.yaml.deferred"),
            r#"members:
  - name: DeferredThing
    selector:
      binding:
        name: d
        kind: function_declaration
"#,
        );
        CompileSpecTreeOptions {
            config_path: config,
            modules_root: modules,
            vendor_marks_path: vendor_marks,
            source_root: None,
            out_root: PathBuf::from("out/override"),
            force: true,
        }
    }

    #[test]
    fn compiles_tree_sources_into_flat_transform_spec() {
        let temp = tempfile::tempdir().unwrap();
        let spec = compile_spec_tree(&fixture(temp.path())).unwrap();

        assert!(spec.logical_modules["static/main"].contains_key("ui/active"));
        assert_eq!(
            spec.logical_modules["static/main"]["ui/active"].members[0]
                .name
                .as_deref(),
            Some("No")
        );
        assert_eq!(
            spec.chunk_renames["static/main"].members[0].name.as_deref(),
            Some("DeferredThing")
        );
        assert_eq!(
            spec.swap_vendor_chunks
                .output_manifest_path
                .as_deref()
                .unwrap(),
            Path::new("out/override/vendors/manifest.json")
        );
        assert_eq!(spec.vendor["static/vendor.js"].identity, "example");
        assert!(spec.materialize_logical_modules.force);
        assert!(spec.emit_browser_harness.unwrap().force);
    }

    #[test]
    fn resolves_config_source_paths_against_source_root() {
        let temp = tempfile::tempdir().unwrap();
        let mut options = fixture(temp.path());
        options.source_root = Some(PathBuf::from("/execroot"));

        let spec = compile_spec_tree(&options).unwrap();

        assert_eq!(
            spec.inputs.input_root,
            Path::new("/execroot/snapshots/test")
        );
        assert_eq!(
            spec.inputs.js_list_path,
            Path::new("/execroot/extracted/js-files.txt")
        );
        assert_eq!(
            spec.emit_browser_harness.unwrap().asset_summary_path,
            Path::new("/execroot/extracted/asset-summary.json")
        );
    }

    #[test]
    fn serialized_spec_omits_retired_and_trivial_fields() {
        let temp = tempfile::tempdir().unwrap();
        let spec = compile_spec_tree(&fixture(temp.path())).unwrap();
        let value: Value = serde_yaml::from_str(&serde_yaml::to_string(&spec).unwrap()).unwrap();

        assert!(value.get("kind").is_none());
        assert!(value.get("schema_version").is_none());
        assert!(value.get("pipeline").is_none());
        assert!(value.get("operations").is_none());
        assert!(value.get("rewrite_chunk_entry_specifiers").is_none());
        assert!(value.get("write_js_tree").is_none());
        assert!(
            value["materialize_logical_modules"]
                .get("target_dir")
                .is_none()
        );
        assert!(
            value["logical_modules"]["static/main"]["ui/active"]["members"][0]
                .get("purity")
                .is_none()
        );
    }

    #[test]
    fn rejects_legacy_binding_kind_spelling() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &options.modules_root.join("active.yaml"),
            r#"members:
  - name: Bad
    selector:
      binding:
        name: bad
        kind: FunctionDeclaration
"#,
        );

        let error = compile_spec_tree(&options).unwrap_err();
        assert!(error.to_string().contains("active.yaml"), "{error:#}");
    }

    #[test]
    fn rejects_explicit_module_path() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &options.modules_root.join("active.yaml"),
            r#"path: active
members: []
"#,
        );

        let error = compile_spec_tree(&options).unwrap_err();
        assert!(format!("{error:#}").contains("unknown field"), "{error:#}");
    }

    #[test]
    fn rejects_explicit_module_chunk_id() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &options.modules_root.join("active.yaml"),
            r#"chunk_id: static/other
members: []
"#,
        );

        let error = compile_spec_tree(&options).unwrap_err();
        assert!(format!("{error:#}").contains("unknown field"), "{error:#}");
    }

    #[test]
    fn rejects_unknown_authoring_config_fields() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &options.config_path,
            r#"main_chunk_id: static/main
inputs:
  root: snapshots/test
  js_list_path: extracted/js-files.txt
browser_harness:
  asset_summary_path: extracted/asset-summary.json
extra_authoring_field: old
"#,
        );

        let error = compile_spec_tree(&options).unwrap_err();
        assert!(format!("{error:#}").contains("unknown field"), "{error:#}");
    }
}
