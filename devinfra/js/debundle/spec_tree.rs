use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Deserialize;

use output_layout::DebundleOutputLayout;
use spec::{
    AnonymousStatement, BindingAnnotation, BundledPartialSwapBundle, BundledPartialSwapMark,
    BundledPartialSwapPackage, ChunkExportPurity, ChunkRenameMember, ChunkRenameSelector,
    ChunkRenames, EmitBrowserHarnessConfig, LoadJsChunksArgs, LogicalModule,
    MaterializeLogicalModulesConfig, Member, OwnerGraphOptions, PartialSwapMark,
    PartialSwapPackage, PartialSwapSymbol, SourceMatchClaim, SwapMark, SwapVendorChunksConfig,
    TransformSpec, UnassignedMode, VendorLevel, VendorMark, VendorRole, WrapperShape,
    WriteJsTreeConfig,
};
use spec_modules::{
    collect_module_files, load_binding_patch_members, module_path_from_file, read_module_file,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompileSpecTreeOptions {
    pub config_path: PathBuf,
    pub modules_root: PathBuf,
    pub vendor_marks_path: PathBuf,
    pub source_root: Option<PathBuf>,
    pub out_root: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthoringConfig {
    main_chunk_id: String,
    /// Optional per-chunk module roots, relative to `--tree-modules`.
    /// When omitted, every module below `--tree-modules` belongs to
    /// `main_chunk_id`, preserving the original single-chunk authoring shape.
    /// When present, only the listed subtrees are loaded and each subtree's
    /// modules belong to its map key.
    #[serde(default)]
    module_roots: BTreeMap<String, PathBuf>,
    inputs: AuthoringInputs,
    /// Optional: only meaningful for a browser-delivered chunk. Omit for a
    /// target with no HTML entry point (e.g. a Node CLI bundle) to skip the
    /// browser-harness emit step entirely.
    #[serde(default)]
    browser_harness: Option<BrowserHarnessPolicy>,
    /// Write the materialized JS tree, independent of `browser_harness`.
    /// A browser target gets this for free as a side effect of
    /// `emit_browser_harness` and doesn't need to also set this; a target
    /// with no HTML entry point (e.g. a Node CLI bundle) needs this to get
    /// any emitted JS output at all. Default false.
    #[serde(default)]
    write_js_tree: bool,
    /// Per-chunk `unassigned_mode` policy. Required: every chunk this
    /// authoring tree materialises must appear here. The downstream
    /// pipeline validator enforces the same invariant on the compiled
    /// `TransformSpec`; declaring it in the authoring config keeps
    /// the policy next to the modules tree it governs.
    unassigned_mode: BTreeMap<String, UnassignedMode>,
    /// Per-chunk opt-ins for conditionally-correct analyses (see
    /// <docs/design.md> → "Conditionally-correct
    /// optimizations"). Default empty — each chunk uses the
    /// strictly-conservative analysis path unless it opts in here.
    #[serde(default)]
    chunk_analysis_options: BTreeMap<String, OwnerGraphOptions>,
    /// Author-asserted pure exports, keyed by defining chunk. See
    /// `TransformSpec::chunk_export_purity`.
    #[serde(default)]
    chunk_export_purity: BTreeMap<String, ChunkExportPurity>,
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
    /// `swap`-only: upstream named exports asserted as aliases of the
    /// package default (see `spec::SwapMark::default_export_aliases`).
    #[serde(default)]
    default_export_aliases: Vec<String>,
    /// `partial_swap` / `bundled_partial_swap`: per-package upstream
    /// coordinates. Bundled swaps also require `bundle_export`.
    #[serde(default)]
    packages: Option<BTreeMap<String, VendorPackageSource>>,
    /// `partial_swap` / `bundled_partial_swap`: per-chunk-export
    /// upstream-export mapping.
    #[serde(default)]
    symbols: Option<BTreeMap<String, PartialSwapSymbol>>,
    /// `bundled_partial_swap`-only: caller-supplied ESM bundle blob.
    #[serde(default)]
    bundle: Option<BundledPartialSwapBundle>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum VendorLevelSource {
    Suppress,
    BoundaryRename,
    Swap,
    PartialSwap,
    BundledPartialSwap,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct VendorPackageSource {
    version: String,
    subpath: String,
    #[serde(default)]
    namespace: Option<String>,
    #[serde(default)]
    bundle_export: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModuleSource {
    chunk_id: String,
    path: String,
    #[serde(default)]
    members: Vec<Member>,
    #[serde(default)]
    source_matches: Vec<SourceMatchClaim>,
    #[serde(default)]
    annotations: BTreeMap<String, BindingAnnotation>,
    #[serde(default)]
    anonymous_statements: Vec<AnonymousStatement>,
    #[serde(default)]
    comment: Option<String>,
    #[serde(default)]
    note: Option<String>,
}

pub fn compile_spec_tree(options: &CompileSpecTreeOptions) -> Result<TransformSpec> {
    let config: AuthoringConfig = read_yaml(&options.config_path)?;
    let source_root = options.source_root.as_deref();
    let input_root = source_path(source_root, config.inputs.root);
    let js_list_path = source_path(source_root, config.inputs.js_list_path);
    let layout = OutputLayout::new(options.out_root.clone());
    let module_sources = load_module_sources(
        &options.modules_root,
        &config.main_chunk_id,
        &config.module_roots,
    )?;
    let binding_patch_members = load_binding_patch_members(&options.modules_root)?
        .into_iter()
        .filter(|member| !is_trivial_binding_patch(member))
        .collect();

    Ok(TransformSpec {
        inputs: LoadJsChunksArgs {
            input_root: input_root.clone(),
            js_list_path,
        },
        vendor: vendor_map(
            read_yaml::<VendorMarksFile>(&options.vendor_marks_path)?.vendor_marks,
            source_root,
        )?,
        logical_modules: logical_modules_map(module_sources)?,
        chunk_renames: chunk_renames_map(&config.main_chunk_id, binding_patch_members),
        unassigned_mode: config.unassigned_mode,
        chunk_analysis_options: config.chunk_analysis_options,
        chunk_export_purity: config.chunk_export_purity,
        swap_vendor_chunks: SwapVendorChunksConfig {
            output_manifest_path: Some(layout.vendor_manifest_path.clone()),
            output_wrapper_dir: Some(layout.vendor_wrapper_root.clone()),
            write: true,
        },
        materialize_logical_modules: MaterializeLogicalModulesConfig {
            file: None,
            prune_other_chunks: false,
            report_out_dir: Some(layout.report_tree_root.clone()),
            target_dir: String::new(),
        },
        write_js_tree: config.write_js_tree.then(|| WriteJsTreeConfig {
            out_dir: layout.output_root.clone(),
        }),
        emit_browser_harness: config.browser_harness.map(|browser_harness| {
            EmitBrowserHarnessConfig {
                asset_summary_path: source_path(source_root, browser_harness.asset_summary_path),
                out_dir: layout.output_root,
                snapshot_root: input_root,
            }
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
    output_root: PathBuf,
    report_tree_root: PathBuf,
    vendor_manifest_path: PathBuf,
    vendor_wrapper_root: PathBuf,
}

impl OutputLayout {
    fn new(root: PathBuf) -> Self {
        let debundle = DebundleOutputLayout::new(&root);
        Self {
            output_root: debundle.root().to_path_buf(),
            report_tree_root: debundle.tree_root(),
            vendor_manifest_path: debundle.vendor_swaps_report(),
            vendor_wrapper_root: debundle.app_root().join("vendors/generated"),
        }
    }
}

fn load_module_sources(
    modules_root: &Path,
    main_chunk_id: &str,
    module_roots: &BTreeMap<String, PathBuf>,
) -> Result<Vec<ModuleSource>> {
    if module_roots.is_empty() {
        return load_chunk_modules(modules_root, main_chunk_id);
    }

    validate_module_roots(module_roots)?;
    let mut active = Vec::new();
    for (chunk_id, relative_root) in module_roots {
        active.extend(
            load_chunk_modules(&modules_root.join(relative_root), chunk_id).with_context(|| {
                format!(
                    "loading module_roots entry `{chunk_id}` from {}",
                    relative_root.display()
                )
            })?,
        );
    }
    active.sort_by(|left, right| (&left.chunk_id, &left.path).cmp(&(&right.chunk_id, &right.path)));
    Ok(active)
}

fn validate_module_roots(module_roots: &BTreeMap<String, PathBuf>) -> Result<()> {
    let mut roots: Vec<(&str, &Path)> = Vec::new();
    for (chunk_id, root) in module_roots {
        if chunk_id.is_empty() {
            bail!("module_roots contains an empty chunk id");
        }
        if root.as_os_str().is_empty()
            || root.is_absolute()
            || root
                .components()
                .any(|component| !matches!(component, std::path::Component::Normal(_)))
        {
            bail!(
                "module_roots entry `{chunk_id}` must be a non-empty normalized relative path, got `{}`",
                root.display()
            );
        }
        roots.push((chunk_id, root));
    }

    for (index, (left_chunk, left_root)) in roots.iter().enumerate() {
        for (right_chunk, right_root) in roots.iter().skip(index + 1) {
            if left_root == right_root {
                bail!(
                    "module_roots entries `{left_chunk}` and `{right_chunk}` use the same root `{}`",
                    left_root.display()
                );
            }
            if left_root.starts_with(right_root) || right_root.starts_with(left_root) {
                bail!(
                    "module_roots entries `{left_chunk}` (`{}`) and `{right_chunk}` (`{}`) overlap",
                    left_root.display(),
                    right_root.display()
                );
            }
        }
    }
    Ok(())
}

fn load_chunk_modules(modules_root: &Path, chunk_id: &str) -> Result<Vec<ModuleSource>> {
    let mut active = Vec::new();
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        let data = read_module_file(&path)?;
        active.push(ModuleSource {
            chunk_id: chunk_id.to_string(),
            path: module_path,
            members: data.members,
            source_matches: data.source_matches,
            annotations: data.annotations,
            anonymous_statements: data.anonymous_statements,
            comment: data.comment,
            note: data.note,
        });
    }
    active.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(active)
}

fn is_trivial_binding_patch(member: &Member) -> bool {
    let Some(binding) = &member.selector.binding else {
        return false;
    };
    let binding_name = &binding.name;
    member
        .name
        .as_ref()
        .is_none_or(|export_name| export_name == binding_name)
}

fn vendor_map(
    sources: Vec<VendorMarkSource>,
    source_root: Option<&Path>,
) -> Result<BTreeMap<String, VendorMark>> {
    // Multiple vendor marks on the same `chunk_path` would silently clobber
    // each other in the resulting `BTreeMap`. The downstream consumers
    // (the vendor resolution plan and everything it feeds) only
    // see the survivor, so the dropped entries' packages and symbols never
    // make it into the importmap or the rewriter — the spec author sees a
    // green build but only one of the two marks actually takes effect.
    // Bail with a clear message instead. If a chunk genuinely needs two
    // partial-swap clusters, merge them into one entry (union of
    // `packages` + `symbols`).
    let mut out: BTreeMap<String, VendorMark> = BTreeMap::new();
    for source in sources {
        let chunk_path = source.chunk_path.clone();
        let identity = source.identity.clone();
        let role = source.role;
        let level = source.into_vendor_level(source_root)?;
        if let Some(prior) = out.get(&chunk_path) {
            bail!(
                "vendor_marks: duplicate chunk_path `{chunk_path}` (first entry: {}, second entry: {identity}). \
                 Merge the two entries into one — multiple marks on the same chunk silently clobber each other.",
                prior.identity,
            );
        }
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
    fn into_vendor_level(self, source_root: Option<&Path>) -> Result<VendorLevel> {
        match self.level {
            VendorLevelSource::Suppress => {
                self.ensure_no_swap_payload()?;
                self.ensure_no_partial_swap_payload()?;
                Ok(VendorLevel::Suppress)
            }
            VendorLevelSource::BoundaryRename => {
                self.ensure_no_swap_payload()?;
                self.ensure_no_partial_swap_payload()?;
                Ok(VendorLevel::BoundaryRename)
            }
            VendorLevelSource::Swap => {
                self.ensure_no_partial_swap_payload()?;
                Ok(VendorLevel::Swap(SwapMark {
                    package: self.package.with_context(|| {
                        format!("vendor mark {} missing package", self.chunk_path)
                    })?,
                    version: self.version.with_context(|| {
                        format!("vendor mark {} missing version", self.chunk_path)
                    })?,
                    subpath: self.subpath.with_context(|| {
                        format!("vendor mark {} missing subpath", self.chunk_path)
                    })?,
                    wrapper_shape: self.wrapper_shape,
                    default_export_aliases: self.default_export_aliases,
                }))
            }
            VendorLevelSource::PartialSwap => {
                self.ensure_no_swap_payload()?;
                let packages = self.packages.clone().with_context(|| {
                    format!(
                        "vendor mark {} (partial_swap) missing `packages`",
                        self.chunk_path
                    )
                })?;
                let symbols = self.symbols.clone().with_context(|| {
                    format!(
                        "vendor mark {} (partial_swap) missing `symbols`",
                        self.chunk_path
                    )
                })?;
                for (chunk_export, symbol) in &symbols {
                    if !packages.contains_key(&symbol.package) {
                        bail!(
                            "vendor mark {} (partial_swap) symbol {} references unknown package `{}`",
                            self.chunk_path,
                            chunk_export,
                            symbol.package
                        );
                    }
                }
                Ok(VendorLevel::PartialSwap(PartialSwapMark {
                    packages: packages
                        .into_iter()
                        .map(|(name, package)| {
                            Ok((name, package.into_partial_swap_package(&self.chunk_path)?))
                        })
                        .collect::<Result<_>>()?,
                    symbols,
                }))
            }
            VendorLevelSource::BundledPartialSwap => {
                self.ensure_no_swap_payload()?;
                let bundle = self.bundle.clone().with_context(|| {
                    format!(
                        "vendor mark {} (bundled_partial_swap) missing `bundle`",
                        self.chunk_path
                    )
                })?;
                let packages = self.packages.clone().with_context(|| {
                    format!(
                        "vendor mark {} (bundled_partial_swap) missing `packages`",
                        self.chunk_path
                    )
                })?;
                let symbols = self.symbols.clone().with_context(|| {
                    format!(
                        "vendor mark {} (bundled_partial_swap) missing `symbols`",
                        self.chunk_path
                    )
                })?;
                for (chunk_export, symbol) in &symbols {
                    if !packages.contains_key(&symbol.package) {
                        bail!(
                            "vendor mark {} (bundled_partial_swap) symbol {} references unknown package `{}`",
                            self.chunk_path,
                            chunk_export,
                            symbol.package
                        );
                    }
                }
                Ok(VendorLevel::BundledPartialSwap(BundledPartialSwapMark {
                    bundle: BundledPartialSwapBundle {
                        path: source_path(source_root, bundle.path),
                    },
                    packages: packages
                        .into_iter()
                        .map(|(name, package)| {
                            Ok((
                                name,
                                package.into_bundled_partial_swap_package(&self.chunk_path)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                    symbols,
                }))
            }
        }
    }

    fn ensure_no_swap_payload(&self) -> Result<()> {
        if self.package.is_some()
            || self.version.is_some()
            || self.subpath.is_some()
            || self.wrapper_shape.is_some()
            || !self.default_export_aliases.is_empty()
        {
            bail!(
                "vendor mark {} has swap-only fields but level is not swap",
                self.chunk_path
            );
        }
        Ok(())
    }

    fn ensure_no_partial_swap_payload(&self) -> Result<()> {
        if self.packages.is_some() || self.symbols.is_some() || self.bundle.is_some() {
            bail!(
                "vendor mark {} has partial-swap-only fields (`bundle`/`packages`/`symbols`) but level is not partial_swap or bundled_partial_swap",
                self.chunk_path
            );
        }
        Ok(())
    }
}

impl VendorPackageSource {
    fn into_partial_swap_package(self, chunk_path: &str) -> Result<PartialSwapPackage> {
        if self.bundle_export.is_some() {
            bail!(
                "vendor mark {chunk_path} (partial_swap) package has bundled_partial_swap-only field `bundle_export`"
            );
        }
        Ok(PartialSwapPackage {
            version: self.version,
            subpath: self.subpath,
            namespace: self.namespace,
        })
    }

    fn into_bundled_partial_swap_package(
        self,
        chunk_path: &str,
    ) -> Result<BundledPartialSwapPackage> {
        let bundle_export = self.bundle_export.with_context(|| {
            format!(
                "vendor mark {chunk_path} (bundled_partial_swap) package missing `bundle_export`"
            )
        })?;
        Ok(BundledPartialSwapPackage {
            version: self.version,
            subpath: self.subpath,
            bundle_export,
            namespace: self.namespace,
        })
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
                    source_matches: source.source_matches,
                    annotations: source.annotations,
                    anonymous_statements: source.anonymous_statements,
                    comment: source.comment,
                    note: source.note,
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
    binding_patch_members: Vec<Member>,
) -> BTreeMap<String, ChunkRenames> {
    let members = binding_patch_members
        .into_iter()
        .filter_map(|member| {
            Some(ChunkRenameMember {
                name: member.name,
                selector: ChunkRenameSelector {
                    binding: member.selector.binding?,
                },
            })
        })
        .collect::<Vec<_>>();
    if members.is_empty() {
        return BTreeMap::new();
    }
    BTreeMap::from([(
        main_chunk_id.to_string(),
        ChunkRenames {
            members,
            annotations: BTreeMap::new(),
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
unassigned_mode:
  static/main:
    kind: inline_in_entry
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
            &root.join("binding_patches.yaml"),
            r#"members:
  - name: PatchedThing
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
        }
    }

    #[test]
    fn legacy_single_chunk_tree_compiles_unchanged() {
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
            Some("PatchedThing")
        );
        assert_eq!(
            spec.swap_vendor_chunks
                .output_manifest_path
                .as_deref()
                .unwrap(),
            Path::new("out/override/reports/vendor_swaps.json")
        );
        assert_eq!(spec.vendor["static/vendor.js"].identity, "example");
        assert!(spec.emit_browser_harness.is_some());
    }

    #[test]
    fn compiles_multiple_chunk_module_roots_into_one_flat_spec() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let config = root.join("spec_config.yaml");
        let modules = root.join("modules");
        let vendor_marks = root.join("vendor_marks.yaml");
        write_file(
            &config,
            r#"main_chunk_id: cli
module_roots:
  cli: chunks/cli
  print: chunks/print
inputs:
  root: snapshot
  js_list_path: extracted/js-files.txt
unassigned_mode:
  cli:
    kind: inline_in_entry
  print:
    kind: catchall_file
    target_path: residual/print
"#,
        );
        write_file(&vendor_marks, "vendor_marks: []\n");
        write_file(
            &modules.join("chunks/cli/runtime/session.yaml"),
            r#"source_matches:
  - match: |
      function selectedCli() {
        return "cli";
      }
    bindings: [selectedCli]
"#,
        );
        write_file(
            &modules.join("chunks/print/protocol/stream.yaml"),
            r#"members:
  - name: StructuredStream
    selector:
      binding:
        name: minifiedStream
        kind: class_declaration
"#,
        );
        write_file(
            &modules.join("not_mapped.yaml"),
            "members: [{ selector: { binding: { name: ignored } } }]\n",
        );

        let spec = compile_spec_tree(&CompileSpecTreeOptions {
            config_path: config,
            modules_root: modules,
            vendor_marks_path: vendor_marks,
            source_root: None,
            out_root: root.join("out"),
        })
        .unwrap();

        assert_eq!(spec.logical_modules.len(), 2);
        assert_eq!(
            spec.logical_modules
                .values()
                .map(BTreeMap::len)
                .sum::<usize>(),
            2
        );
        assert!(spec.logical_modules["cli"].contains_key("runtime/session"));
        assert!(spec.logical_modules["print"].contains_key("protocol/stream"));
        assert_eq!(
            spec.logical_modules["cli"]["runtime/session"].source_matches[0].bindings[0].local(),
            "selectedCli"
        );
        assert!(matches!(
            spec.unassigned_mode["cli"],
            UnassignedMode::InlineInEntry
        ));
        assert!(matches!(
            spec.unassigned_mode["print"],
            UnassignedMode::CatchallFile { .. }
        ));
    }

    #[test]
    fn rejects_invalid_multi_chunk_module_roots() {
        let cases = [
            (
                "duplicate",
                "  cli: chunks/shared\n  print: chunks/shared\n",
                "use the same root",
            ),
            (
                "overlap",
                "  cli: chunks\n  print: chunks/print\n",
                "overlap",
            ),
            (
                "traversal",
                "  cli: ../outside\n  print: chunks/print\n",
                "normalized relative path",
            ),
            (
                "absolute",
                "  cli: /tmp/cli\n  print: chunks/print\n",
                "normalized relative path",
            ),
            (
                "empty",
                "  cli: ''\n  print: chunks/print\n",
                "normalized relative path",
            ),
        ];

        for (case, module_roots, expected) in cases {
            let temp = tempfile::tempdir().unwrap();
            let root = temp.path();
            let config = root.join("spec_config.yaml");
            let modules = root.join("modules");
            let vendor_marks = root.join("vendor_marks.yaml");
            write_file(
                &config,
                &format!(
                    "main_chunk_id: cli\nmodule_roots:\n{module_roots}inputs:\n  root: snapshot\n  js_list_path: extracted/js-files.txt\nunassigned_mode:\n  cli: {{ kind: inline_in_entry }}\n  print: {{ kind: inline_in_entry }}\n"
                ),
            );
            fs::create_dir_all(&modules).unwrap();
            write_file(&vendor_marks, "vendor_marks: []\n");

            let error = compile_spec_tree(&CompileSpecTreeOptions {
                config_path: config,
                modules_root: modules,
                vendor_marks_path: vendor_marks,
                source_root: None,
                out_root: root.join("out"),
            })
            .expect_err(case);
            let message = format!("{error:#}");
            assert!(
                message.contains(expected),
                "{case}: expected `{expected}` in `{message}`"
            );
        }
    }

    #[test]
    fn drops_trivial_binding_patches() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &temp.path().join("binding_patches.yaml"),
            r#"members:
  - name: same
    selector:
      binding:
        name: same
        kind: variable_declarator
  - name: UsefulName
    selector:
      binding:
        name: min
        kind: function_declaration
"#,
        );

        let spec = compile_spec_tree(&options).unwrap();

        assert_eq!(spec.chunk_renames["static/main"].members.len(), 1);
        assert_eq!(
            spec.chunk_renames["static/main"].members[0].name.as_deref(),
            Some("UsefulName")
        );
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
    fn omits_browser_harness_when_not_configured() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let config = root.join("spec_config.yaml");
        let modules = root.join("modules");
        let vendor_marks = root.join("sources/vendor/vendor_marks.yaml");
        write_file(
            &config,
            r#"main_chunk_id: static/main
inputs:
  root: snapshots/test
  js_list_path: extracted/js-files.txt
unassigned_mode:
  static/main:
    kind: inline_in_entry
"#,
        );
        fs::create_dir_all(&modules).unwrap();
        write_file(&vendor_marks, "vendor_marks: []\n");

        let spec = compile_spec_tree(&CompileSpecTreeOptions {
            config_path: config,
            modules_root: modules,
            vendor_marks_path: vendor_marks,
            source_root: None,
            out_root: PathBuf::from("out/override"),
        })
        .unwrap();

        assert!(spec.emit_browser_harness.is_none());
        assert!(spec.write_js_tree.is_none());
    }

    #[test]
    fn enables_write_js_tree_when_configured() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let config = root.join("spec_config.yaml");
        let modules = root.join("modules");
        let vendor_marks = root.join("sources/vendor/vendor_marks.yaml");
        write_file(
            &config,
            r#"main_chunk_id: static/main
inputs:
  root: snapshots/test
  js_list_path: extracted/js-files.txt
write_js_tree: true
unassigned_mode:
  static/main:
    kind: inline_in_entry
"#,
        );
        fs::create_dir_all(&modules).unwrap();
        write_file(&vendor_marks, "vendor_marks: []\n");

        let spec = compile_spec_tree(&CompileSpecTreeOptions {
            config_path: config,
            modules_root: modules,
            vendor_marks_path: vendor_marks,
            source_root: None,
            out_root: PathBuf::from("out/override"),
        })
        .unwrap();

        assert!(spec.emit_browser_harness.is_none());
        assert_eq!(
            spec.write_js_tree.unwrap().out_dir,
            Path::new("out/override")
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
    fn rejects_duplicate_vendor_chunk_path() {
        let temp = tempfile::tempdir().unwrap();
        let options = fixture(temp.path());
        write_file(
            &options.vendor_marks_path,
            r#"vendor_marks:
  - level: partial_swap
    chunk_path: static/vendor.js
    identity: first cluster
    packages:
      pkg-a:
        version: 1.0.0
        subpath: index.js
    symbols:
      a: { package: pkg-a, kind: namespace }
  - level: partial_swap
    chunk_path: static/vendor.js
    identity: second cluster
    packages:
      pkg-b:
        version: 2.0.0
        subpath: index.js
    symbols:
      b: { package: pkg-b, kind: namespace }
"#,
        );
        let err = compile_spec_tree(&options).unwrap_err();
        let msg = format!("{err:#}");
        assert!(
            msg.contains("duplicate chunk_path `static/vendor.js`"),
            "expected duplicate-chunk_path error, got: {msg}",
        );
        assert!(
            msg.contains("first cluster"),
            "missing first identity: {msg}"
        );
        assert!(
            msg.contains("second cluster"),
            "missing second identity: {msg}"
        );
    }
}
