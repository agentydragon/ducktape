//! `debundle spec synthesize-selectors` driver: selects module files and rewrites each one.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use spec::LogicalModule;
use spec_modules::{collect_module_files, is_module_yaml, module_path_from_file, read_module_file};

use crate::chunk_index::ChunkSelectorIndex;
use crate::name_binding_rewrite::{
    NameBindingRewriteOptions, rewrite_name_bindings_to_source_match,
};
use crate::report::{
    SelectorCodemodAction, SelectorCodemodModuleResult, SelectorCodemodReport,
    SelectorCodemodReportAction, SelectorCodemodSummary,
};

#[derive(Debug, Clone)]
pub struct SelectorCodemodConfig {
    pub modules_root: PathBuf,
    pub apply: bool,
    pub files: Vec<PathBuf>,
    pub modules: Vec<String>,
    pub module_prefixes: Vec<String>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub source_file: Option<PathBuf>,
    pub items: Vec<String>,
    /// Emit up to N ranked candidate selectors per synthesized item (a menu); the
    /// extras beyond the primary pick are reported as `alternatives`. 1 = today's
    /// single-pick behavior.
    pub candidates: usize,
}

pub fn run_selector_codemod(config: &SelectorCodemodConfig) -> Result<SelectorCodemodReport> {
    run_selector_codemod_with_module_results(config, &mut |_| Ok(()))
}

/// Report a module as soon as its candidates and optional YAML edit are done.
/// The final report remains available for text/JSON callers; NDJSON callers
/// can use this hook to emit useful partial output during long scans.
pub fn run_selector_codemod_with_module_results(
    config: &SelectorCodemodConfig,
    on_module: &mut dyn FnMut(&SelectorCodemodModuleResult<'_>) -> Result<()>,
) -> Result<SelectorCodemodReport> {
    js_ast::with_swc_globals(|| run_selector_codemod_impl(config, on_module))
}

fn run_selector_codemod_impl(
    config: &SelectorCodemodConfig,
    on_module: &mut dyn FnMut(&SelectorCodemodModuleResult<'_>) -> Result<()>,
) -> Result<SelectorCodemodReport> {
    let selected_items = config
        .items
        .iter()
        .map(|item| parse_synthesis_item(item))
        .collect::<Result<BTreeSet<_>>>()?;
    let selected_item_exports = selected_item_exports_by_module(&selected_items);
    let selected_files = config
        .files
        .iter()
        .map(|path| resolve_file_filter(&config.modules_root, path))
        .collect::<BTreeSet<_>>();
    let selected_modules = config.modules.iter().cloned().collect::<BTreeSet<_>>();
    let files = collect_candidate_module_files(
        config,
        &selected_files,
        &selected_modules,
        &selected_item_exports,
    )
    .with_context(|| format!("selecting files under {}", config.modules_root.display()))?;
    let synthesis_module = load_synthesis_module(config)?;
    let synthesis_index = ChunkSelectorIndex::new(&synthesis_module.module);

    let mut candidates = Vec::new();
    let mut summary = SelectorCodemodSummary {
        dry_run: !config.apply,
        ..SelectorCodemodSummary::default()
    };

    for file in files {
        summary.files_scanned += 1;
        let module = module_path_from_file(&file, &config.modules_root);
        if !module_selected(
            &file,
            &module,
            &selected_files,
            &selected_modules,
            &config.module_prefixes,
        ) {
            continue;
        }
        summary.modules_scanned += 1;
        let candidate_start = candidates.len();

        let mut doc = serde_yaml::to_value(read_module_file(&file)?)?;
        let mut file_changed = false;
        let root = doc
            .as_mapping_mut()
            .expect("LogicalModule serializes as a YAML mapping");
        let selected_exports = selected_item_exports.get(&module);
        if !selected_item_exports.is_empty() && selected_exports.is_none() {
            continue;
        }
        let outcomes = rewrite_name_bindings_to_source_match(
            &module,
            &file,
            root,
            &synthesis_index,
            selected_exports,
            NameBindingRewriteOptions {
                apply: config.apply,
                candidates: config.candidates,
            },
        )?;
        summary.members_scanned += outcomes.members_scanned;
        summary.name_binding_members += outcomes.members_seen;
        summary.synthesized_groups += outcomes.groups_changed;
        for candidate in outcomes.candidates {
            if matches!(
                candidate.action,
                SelectorCodemodAction::WouldChange | SelectorCodemodAction::Changed
            ) {
                file_changed = true;
                summary.changed_candidates += 1;
            } else {
                summary.skipped_candidates += 1;
            }
            candidates.push(candidate);
        }

        let file_written = if config.apply && file_changed {
            let rewritten: LogicalModule = serde_yaml::from_value(doc)
                .with_context(|| format!("validating rewritten {}", file.display()))?;
            yaml_edit::write_yaml_atomic(&file, &serde_yaml::to_value(rewritten)?)?;
            true
        } else {
            false
        };
        if file_written {
            summary.files_written.push(file.display().to_string());
        }
        on_module(&SelectorCodemodModuleResult {
            module: &module,
            file: file.display().to_string(),
            candidates: &candidates[candidate_start..],
            file_written,
        })?;
    }

    Ok(SelectorCodemodReport {
        action: if config.apply {
            SelectorCodemodReportAction::Applied
        } else {
            SelectorCodemodReportAction::DryRun
        },
        candidates,
        summary,
    })
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct SynthesisItem {
    module: String,
    export_name: String,
}

fn load_synthesis_module(config: &SelectorCodemodConfig) -> Result<js_ast::ParsedJsModule> {
    let source_file = source_input::optional_chunk_source_file(
        config.source_file.as_deref(),
        config.source_root.as_deref(),
        config.chunk.as_deref(),
    )?
    .context("synthesize-selectors requires --source-file or --source-root + --chunk")?;
    let source = fs::read_to_string(&source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    js_ast::parse_js_module(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))
}

fn parse_synthesis_item(raw: &str) -> Result<SynthesisItem> {
    let Some((module, export_name)) = raw.rsplit_once(':') else {
        bail!("--item must be `module/path:ExportName`, got `{raw}`");
    };
    if module.is_empty() || export_name.is_empty() {
        bail!("--item must be `module/path:ExportName`, got `{raw}`");
    }
    Ok(SynthesisItem {
        module: module.to_string(),
        export_name: export_name.to_string(),
    })
}

fn selected_item_exports_by_module(
    selected_items: &BTreeSet<SynthesisItem>,
) -> BTreeMap<String, BTreeSet<String>> {
    let mut by_module: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for item in selected_items {
        by_module
            .entry(item.module.clone())
            .or_default()
            .insert(item.export_name.clone());
    }
    by_module
}

fn collect_candidate_module_files(
    config: &SelectorCodemodConfig,
    selected_files: &BTreeSet<PathBuf>,
    selected_modules: &BTreeSet<String>,
    selected_item_exports: &BTreeMap<String, BTreeSet<String>>,
) -> Result<Vec<PathBuf>> {
    let has_explicit_module_filters = !selected_files.is_empty()
        || !selected_modules.is_empty()
        || !config.module_prefixes.is_empty();
    if !has_explicit_module_filters && selected_item_exports.is_empty() {
        return collect_module_files(&config.modules_root);
    }

    let mut candidates = BTreeSet::new();
    for file in selected_files {
        add_existing_module_file(&mut candidates, file);
    }
    for module in selected_modules {
        add_existing_module_file(
            &mut candidates,
            &module_file_path(&config.modules_root, module),
        );
    }
    for module in selected_item_exports.keys() {
        add_existing_module_file(
            &mut candidates,
            &module_file_path(&config.modules_root, module),
        );
    }
    if selected_item_exports.is_empty() {
        for prefix in &config.module_prefixes {
            add_module_prefix_files(&mut candidates, &config.modules_root, prefix)?;
        }
    }

    Ok(candidates
        .into_iter()
        .filter(|file| {
            let module = module_path_from_file(file, &config.modules_root);
            module_selected(
                file,
                &module,
                selected_files,
                selected_modules,
                &config.module_prefixes,
            ) && (selected_item_exports.is_empty() || selected_item_exports.contains_key(&module))
        })
        .collect())
}

fn module_file_path(modules_root: &Path, module: &str) -> PathBuf {
    modules_root.join(format!("{module}.yaml"))
}

fn add_existing_module_file(candidates: &mut BTreeSet<PathBuf>, path: &Path) {
    if path.is_file() && is_module_yaml(path) {
        candidates.insert(path.to_path_buf());
    }
}

fn add_module_prefix_files(
    candidates: &mut BTreeSet<PathBuf>,
    modules_root: &Path,
    prefix: &str,
) -> Result<()> {
    if prefix.is_empty() {
        return Ok(());
    }
    add_existing_module_file(candidates, &module_file_path(modules_root, prefix));
    let dir = modules_root.join(prefix);
    if dir.is_dir() {
        candidates.extend(collect_module_files(&dir)?);
    }
    Ok(())
}

fn module_selected(
    file: &Path,
    module: &str,
    selected_files: &BTreeSet<PathBuf>,
    selected_modules: &BTreeSet<String>,
    module_prefixes: &[String],
) -> bool {
    if selected_files.is_empty() && selected_modules.is_empty() && module_prefixes.is_empty() {
        return true;
    }
    if selected_files.contains(file)
        || selected_files.contains(&PathBuf::from(format!("{module}.yaml")))
    {
        return true;
    }
    if selected_modules.contains(module) {
        return true;
    }
    module_prefixes
        .iter()
        .any(|prefix| module == prefix || module.starts_with(&format!("{prefix}/")))
}

fn resolve_file_filter(modules_root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        return path.to_path_buf();
    }
    let under_modules = modules_root.join(path);
    if under_modules.exists() {
        under_modules
    } else {
        path.to_path_buf()
    }
}
