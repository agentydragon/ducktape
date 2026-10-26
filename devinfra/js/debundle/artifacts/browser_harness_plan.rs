//! Browser loading decisions prepared before final program acceptance.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::{cell::RefCell, rc::Rc};

use anyhow::{Context, Result, bail};
use artifact::{
    ChunkBundle, ChunkId, chunk_id_for_js_path, get_chunk_entry_path, module_path_from_path,
    normalize_module_path, path_from_module_path,
};
use lol_html::{RewriteStrSettings, element, end, end_tag, html_content::ContentType, rewrite_str};
use output_layout::DebundleOutputLayout;
use serde::Deserialize;
use spec::EmitBrowserHarnessConfig;
use url::Url;

const HARNESS_MONITOR_SCRIPT: &str = include_str!("harness_monitor.html");

/// Concrete loading topology and asset destinations for an accepted browser tree.
/// The containing validated program exposes this plan only by immutable reference.
pub struct BrowserHarnessPlan {
    pub out_dir: PathBuf,
    pub snapshot_root: PathBuf,
    pub asset_paths: Vec<PathBuf>,
    pub asset_summary_path: PathBuf,
    pub asset_summary: serde_json::Value,
    pub source_html_path: PathBuf,
    pub source_html: String,
    pub index_html: String,
    pub bootstrap: String,
    pub entry_scripts: Vec<String>,
    pub module_preloads: Vec<String>,
}

impl BrowserHarnessPlan {
    pub(super) fn prepare(
        artifact: &ChunkBundle,
        excluded_chunk_ids: &BTreeSet<ChunkId>,
        options: &EmitBrowserHarnessConfig,
    ) -> Result<Self> {
        let asset_summary_raw = fs::read_to_string(&options.asset_summary_path)
            .with_context(|| format!("reading {}", options.asset_summary_path.display()))?;
        let asset_summary_value: serde_json::Value = serde_json::from_str(&asset_summary_raw)?;
        let asset_summary: AssetSummary = serde_json::from_str(&asset_summary_raw)?;
        let html_path = asset_summary
            .entry_points
            .and_then(|entry_points| entry_points.html)
            .unwrap_or_else(|| "index.html".to_string());
        let source_html_path = options
            .snapshot_root
            .join(path_from_module_path(&html_path));
        let source_html = fs::read_to_string(&source_html_path)
            .with_context(|| format!("reading {}", source_html_path.display()))?;
        let html_entries = collect_html_entries(&source_html)?;
        let script_entries = html_entries.module_scripts;
        let preload_entries = html_entries
            .module_preloads
            .into_iter()
            .filter(|entry| entry.path.ends_with(".js"))
            .collect::<Vec<_>>();
        let mut entry_scripts = Vec::<String>::new();
        for entry in &script_entries {
            if entry.path.ends_with(".js") && !entry_scripts.contains(&entry.path) {
                entry_scripts.push(entry.path.clone());
            }
        }
        if entry_scripts.is_empty() {
            bail!(
                "No module script entry found in {}",
                source_html_path.display()
            );
        }
        for path in entry_scripts
            .iter()
            .chain(preload_entries.iter().map(|entry| &entry.path))
        {
            let chunk_name = chunk_id_for_js_path(path)?;
            let chunk_id = artifact.chunk_table.get(&chunk_name).with_context(|| {
                format!("Snapshot manifest does not contain chunk {chunk_name}")
            })?;
            if !artifact.has_chunk(chunk_id) || excluded_chunk_ids.contains(&chunk_id) {
                bail!("Snapshot manifest does not contain chunk {chunk_name}");
            }
        }

        let app_root = DebundleOutputLayout::new(&options.out_dir).app_root();
        let bootstrap = build_bootstrap(artifact, &entry_scripts, &app_root)?;
        let index_html = rewrite_index_html(artifact, &source_html, &app_root)?;
        let mut asset_paths = Vec::new();
        collect_snapshot_assets(&options.snapshot_root, Path::new(""), &mut asset_paths)?;
        asset_paths.sort();
        Ok(Self {
            out_dir: options.out_dir.clone(),
            snapshot_root: options.snapshot_root.clone(),
            asset_paths,
            asset_summary_path: options.asset_summary_path.clone(),
            asset_summary: asset_summary_value,
            source_html_path,
            source_html,
            index_html,
            bootstrap,
            entry_scripts,
            module_preloads: preload_entries
                .into_iter()
                .map(|entry| entry.path)
                .collect(),
        })
    }
}

fn collect_snapshot_assets(root: &Path, relative: &Path, assets: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root.join(relative))? {
        let entry = entry?;
        let path = relative.join(entry.file_name());
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            collect_snapshot_assets(root, &path, assets)?;
        } else if file_type.is_file() {
            let name = module_path_from_path(&path);
            if !name.ends_with(".js") && !name.ends_with(".js.map") {
                assets.push(path);
            }
        }
    }
    Ok(())
}

/// Harness-relative module specifier for a chunk's emitted entry file,
/// resolved from a snapshot `*.js` script path.
fn runtime_js_href(artifact: &ChunkBundle, js_path: &str, app_root: &Path) -> Result<String> {
    let chunk_name = js_path
        .strip_suffix(".js")
        .with_context(|| format!("Expected a .js path: {js_path}"))?;
    let chunk_id = artifact
        .chunk_table
        .get(chunk_name)
        .with_context(|| format!("Unknown chunk: {chunk_name}"))?;
    let entry_file = get_chunk_entry_path(artifact, chunk_id)
        .with_context(|| format!("Missing chunk entry file for {chunk_name}"))?;
    let entry_path = app_root
        .join(chunk_name.split('/').collect::<PathBuf>())
        .join(entry_file.split('/').collect::<PathBuf>());
    Ok(artifact::relative_module_specifier(app_root, &entry_path))
}

#[derive(Debug, Deserialize)]
struct AssetSummary {
    #[serde(rename = "entryPoints")]
    entry_points: Option<EntryPoints>,
}

#[derive(Debug, Deserialize)]
struct EntryPoints {
    html: Option<String>,
}

#[derive(Debug, Clone)]
struct HtmlEntry {
    path: String,
}

#[derive(Debug, Clone, Default)]
struct HtmlEntries {
    module_scripts: Vec<HtmlEntry>,
    module_preloads: Vec<HtmlEntry>,
}

fn build_bootstrap(
    artifact: &ChunkBundle,
    entry_scripts: &[String],
    app_root: &Path,
) -> Result<String> {
    let mut lines = vec![
        "// Generated by //devinfra/js/debundle:debundle.".to_string(),
        "// Loads original HTML module script entries from split output.".to_string(),
        String::new(),
    ];
    for entry_script in entry_scripts {
        let href = runtime_js_href(artifact, entry_script, app_root)?;
        lines.push(format!("import {};", serde_json::to_string(&href)?));
    }
    lines.push(String::new());
    Ok(lines.join("\n"))
}

fn rewrite_index_html(
    artifact: &ChunkBundle,
    source_html: &str,
    app_root: &Path,
) -> Result<String> {
    let comment = "Generated local harness: loads generated runtime JavaScript from the transformed output tree.";
    let state = Rc::new(RefCell::new(HtmlRewriteState::default()));
    let first_script_replacement = format!(
        "{HARNESS_MONITOR_SCRIPT}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>"
    );
    let body_script_insertion = format!(
        "    {HARNESS_MONITOR_SCRIPT}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>\n  "
    );
    let bodyless_script_insertion = format!("{body_script_insertion}</body>");
    let should_insert_comment = !source_html.contains(comment);
    let comment_html = format!("\n    <!-- {comment} -->");

    let html = rewrite_str(
        source_html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!("script[type][src]", {
                let state = Rc::clone(&state);
                let first_script_replacement = first_script_replacement.clone();
                move |element| {
                    if !attribute_eq_ignore_ascii_case(element, "type", "module") {
                        return Ok(());
                    }
                    let src = element
                        .get_attribute("src")
                        .context("script tag missing src")?;
                    let _ = normalize_url_path(&src)?;
                    let mut state = state.borrow_mut();
                    if state.script_inserted {
                        element.remove();
                    } else {
                        state.script_inserted = true;
                        element.replace(&first_script_replacement, ContentType::Html);
                    }
                    Ok(())
                }
            }))
            .append_element_content_handler(element!("link[rel][href]", move |element| {
                if !attribute_contains_token_ignore_ascii_case(element, "rel", "modulepreload") {
                    return Ok(());
                }
                let href = element
                    .get_attribute("href")
                    .context("preload tag missing href")?;
                let path = normalize_url_path(&href)?;
                if path.ends_with(".js") {
                    let href = runtime_js_href(artifact, &path, app_root)?;
                    element.set_attribute("href", &href)?;
                }
                Ok(())
            }))
            .append_element_content_handler(element!("head", {
                let state = Rc::clone(&state);
                move |element| {
                    if should_insert_comment {
                        let mut state = state.borrow_mut();
                        if !state.head_comment_inserted {
                            state.head_comment_inserted = true;
                            element.prepend(&comment_html, ContentType::Html);
                        }
                    }
                    Ok(())
                }
            }))
            .append_element_content_handler(element!("body", {
                let state = Rc::clone(&state);
                let body_script_insertion = body_script_insertion.clone();
                move |element| {
                    state.borrow_mut().body_seen = true;
                    let state = Rc::clone(&state);
                    let body_script_insertion = body_script_insertion.clone();
                    element.on_end_tag(end_tag!(move |end_tag| {
                        let mut state = state.borrow_mut();
                        if !state.script_inserted {
                            state.script_inserted = true;
                            end_tag.before(&body_script_insertion, ContentType::Html);
                        }
                        Ok(())
                    }))
                }
            }))
            .append_element_content_handler(element!("*", |element| {
                let rewrites = element
                    .attributes()
                    .iter()
                    .filter_map(|attribute| {
                        root_absolute_harness_url(&attribute.value())
                            .map(|value| (attribute.name_preserve_case(), value))
                    })
                    .collect::<Vec<_>>();
                for (name, value) in rewrites {
                    element.set_attribute(&name, &value)?;
                }
                Ok(())
            }))
            .append_document_content_handler(end!({
                let state = Rc::clone(&state);
                move |document_end| {
                    let mut state = state.borrow_mut();
                    if !state.script_inserted && !state.body_seen {
                        state.script_inserted = true;
                        document_end
                            .append(&format!("\n{bodyless_script_insertion}"), ContentType::Html);
                    }
                    Ok(())
                }
            })),
    )
    .context("rewriting index HTML")?;

    if html.ends_with('\n') {
        Ok(html)
    } else {
        Ok(format!("{html}\n"))
    }
}

#[derive(Debug, Default)]
struct HtmlRewriteState {
    script_inserted: bool,
    body_seen: bool,
    head_comment_inserted: bool,
}

fn collect_html_entries(html: &str) -> Result<HtmlEntries> {
    let entries = Rc::new(RefCell::new(HtmlEntries::default()));
    rewrite_str(
        html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!("script[type][src]", {
                let entries = Rc::clone(&entries);
                move |element| {
                    if attribute_eq_ignore_ascii_case(element, "type", "module") {
                        let src = element
                            .get_attribute("src")
                            .context("script tag missing src")?;
                        entries.borrow_mut().module_scripts.push(HtmlEntry {
                            path: normalize_url_path(&src)?,
                        });
                    }
                    Ok(())
                }
            }))
            .append_element_content_handler(element!("link[rel][href]", {
                let entries = Rc::clone(&entries);
                move |element| {
                    if attribute_contains_token_ignore_ascii_case(element, "rel", "modulepreload") {
                        let href = element
                            .get_attribute("href")
                            .context("preload tag missing href")?;
                        entries.borrow_mut().module_preloads.push(HtmlEntry {
                            path: normalize_url_path(&href)?,
                        });
                    }
                    Ok(())
                }
            })),
    )
    .context("collecting HTML entry tags")?;
    Ok(entries.borrow().clone())
}

fn attribute_eq_ignore_ascii_case(
    element: &lol_html::html_content::Element<'_, '_>,
    name: &str,
    expected: &str,
) -> bool {
    element
        .get_attribute(name)
        .is_some_and(|value| value.eq_ignore_ascii_case(expected))
}

fn attribute_contains_token_ignore_ascii_case(
    element: &lol_html::html_content::Element<'_, '_>,
    name: &str,
    expected: &str,
) -> bool {
    element
        .get_attribute(name)
        .map(|value| {
            value
                .split_ascii_whitespace()
                .any(|part| part.eq_ignore_ascii_case(expected))
        })
        .unwrap_or(false)
}

fn root_absolute_harness_url(value: &str) -> Option<String> {
    if value.starts_with('/') && !value.starts_with("//") {
        Some(format!(".{value}"))
    } else {
        None
    }
}

fn normalize_url_path(url: &str) -> Result<String> {
    if url.is_empty() {
        bail!("Expected a snapshot-relative URL, got {url}");
    }
    let base = Url::parse("https://debundle.invalid/")
        .expect("static debundle harness URL base must parse");
    let parsed = base
        .join(url)
        .with_context(|| format!("parsing snapshot-relative URL {url:?}"))?;
    if parsed.scheme() != "https" || parsed.host_str() != Some("debundle.invalid") {
        bail!("Expected a snapshot-relative URL, got {url}");
    }
    let stripped = parsed.path().strip_prefix('/').unwrap_or(parsed.path());
    normalize_module_path(stripped)
}
