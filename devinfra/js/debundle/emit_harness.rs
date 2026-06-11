use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::{cell::RefCell, rc::Rc};

use anyhow::{Context, Result, bail};
use lol_html::{RewriteStrSettings, element, end, end_tag, html_content::ContentType, rewrite_str};
use serde::{Deserialize, Serialize};
use url::Url;

use artifact::{
    ArtifactChunkRecord, ChunkBundle, ChunkDecompositionOutput, ChunkId, OutputMetrics,
    chunk_id_for_js_path, get_chunk_entry_path, materialize_artifact_scripts,
    module_path_from_path, normalize_module_path, path_from_module_path, write_json,
};
use identifier_rename_queue::{compute_identifier_rename_queue, write_queue};
use output_layout::DebundleOutputLayout;

/// Harness-relative module specifier for a chunk's emitted entry file,
/// resolved from a snapshot `*.js` script path.
fn runtime_js_href(
    artifact: &ChunkBundle,
    js_path: &str,
    out_dir: &Path,
    runtime_root: &Path,
) -> Result<String> {
    let chunk_name = js_path
        .strip_suffix(".js")
        .with_context(|| format!("Expected a .js path: {js_path}"))?;
    let chunk_id = artifact
        .chunk_table
        .get(chunk_name)
        .with_context(|| format!("Unknown chunk: {chunk_name}"))?;
    let entry_file = get_chunk_entry_path(artifact, chunk_id)
        .with_context(|| format!("Missing chunk entry file for {chunk_name}"))?;
    let entry_path = runtime_root
        .join(chunk_name.split('/').collect::<PathBuf>())
        .join(entry_file.split('/').collect::<PathBuf>());
    Ok(artifact::relative_module_specifier(out_dir, &entry_path))
}

pub struct EmitBrowserHarnessOptions {
    pub asset_summary_path: PathBuf,
    pub out_dir: PathBuf,
    pub snapshot_root: PathBuf,
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

/// `chunk_records` carries the emission set only (the caller drops
/// records of excluded chunks); `excluded_chunk_ids` lists chunks
/// excluded from emission (fully vendor-swapped) — their files are not
/// written, they contribute nothing to the rename queue, and an HTML
/// entry/preload referencing one is rejected the same as a chunk the
/// snapshot manifest never contained.
pub fn emit_browser_harness(
    artifact: &ChunkBundle,
    options: &EmitBrowserHarnessOptions,
    chunk_records: &[ArtifactChunkRecord],
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<()> {
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
        let chunk_id = artifact
            .chunk_table
            .get(&chunk_name)
            .with_context(|| format!("Snapshot manifest does not contain chunk {chunk_name}"))?;
        if !artifact.has_chunk(chunk_id) || excluded_chunk_ids.contains(&chunk_id) {
            bail!("Snapshot manifest does not contain chunk {chunk_name}");
        }
    }

    let layout = DebundleOutputLayout::new(&options.out_dir);
    let app_root = layout.app_root();
    prepare_harness_output_dir(&layout)?;
    let materialized = materialize_artifact_scripts(
        artifact,
        &app_root,
        &layout.tree_root(),
        decomposition_by_chunk,
        excluded_chunk_ids,
    )?;
    let copied_assets = copy_snapshot_assets(&options.snapshot_root, &app_root)?;
    let bootstrap = build_bootstrap(artifact, &entry_scripts, &app_root, &app_root)?;
    let index_html = rewrite_index_html(artifact, &source_html, &app_root, &app_root)?;

    fs::write(app_root.join("index.html"), index_html)?;
    fs::write(app_root.join("bootstrap.js"), bootstrap)?;
    write_json(
        layout.chunks_report(),
        &ChunksManifest {
            chunks: chunk_records,
        },
    )?;
    let queue =
        compute_identifier_rename_queue(artifact, decomposition_by_chunk, excluded_chunk_ids)?;
    write_queue(&layout.rename_queue_report(), &queue)?;
    let runtime = HarnessRuntimeReport {
        app_root: format!("../{}", output_layout::APP_DIR),
        copied_assets,
        entry_scripts,
        module_preloads: preload_entries
            .iter()
            .map(|entry| entry.path.clone())
            .collect(),
        generated: HarnessGeneratedManifest {
            bootstrap: module_path_from_path(&PathBuf::from("bootstrap.js")),
            index_html: module_path_from_path(&PathBuf::from("index.html")),
        },
    };
    write_json(layout.runtime_report(), &runtime)?;
    write_json(
        layout.output_report(),
        &HarnessOutputReport {
            output_metrics: materialized.output_metrics,
        },
    )?;
    write_json(
        layout.source_assets_report(),
        &SourceAssetsReport {
            source_path: module_path_from_path(&options.asset_summary_path),
            asset_summary: asset_summary_value,
        },
    )?;
    write_json(
        layout.provenance_report(),
        &ProvenanceReport {
            source_html_path: module_path_from_path(&source_html_path),
            source_html,
        },
    )?;
    write_json(
        app_root.join("package.json"),
        &PackageManifest {
            package_type: "module",
        },
    )?;
    Ok(())
}

#[derive(Serialize)]
struct ChunksManifest<'a> {
    chunks: &'a [ArtifactChunkRecord],
}

#[derive(Serialize)]
struct PackageManifest {
    #[serde(rename = "type")]
    package_type: &'static str,
}

#[derive(Serialize)]
struct HarnessRuntimeReport {
    app_root: String,
    copied_assets: Vec<String>,
    entry_scripts: Vec<String>,
    module_preloads: Vec<String>,
    generated: HarnessGeneratedManifest,
}

#[derive(Serialize)]
struct HarnessGeneratedManifest {
    bootstrap: String,
    index_html: String,
}

#[derive(Serialize)]
struct HarnessOutputReport {
    output_metrics: OutputMetrics,
}

#[derive(Serialize)]
struct SourceAssetsReport {
    source_path: String,
    asset_summary: serde_json::Value,
}

#[derive(Serialize)]
struct ProvenanceReport {
    source_html_path: String,
    source_html: String,
}

fn build_bootstrap(
    artifact: &ChunkBundle,
    entry_scripts: &[String],
    out_dir: &Path,
    runtime_root: &Path,
) -> Result<String> {
    let mut lines = vec![
        "// Generated by //devinfra/js/debundle:debundle.".to_string(),
        "// Loads original HTML module script entries from split output.".to_string(),
        String::new(),
    ];
    for entry_script in entry_scripts {
        let href = runtime_js_href(artifact, entry_script, out_dir, runtime_root)?;
        lines.push(format!("import {};", serde_json::to_string(&href)?));
    }
    lines.push(String::new());
    Ok(lines.join("\n"))
}

fn rewrite_index_html(
    artifact: &ChunkBundle,
    source_html: &str,
    out_dir: &Path,
    runtime_root: &Path,
) -> Result<String> {
    let comment = "Generated local harness: loads generated runtime JavaScript from the transformed output tree.";
    let state = Rc::new(RefCell::new(HtmlRewriteState::default()));
    let first_script_replacement = format!(
        "{}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>",
        harness_monitor_script()
    );
    let body_script_insertion = format!(
        "    {}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>\n  ",
        harness_monitor_script()
    );
    let bodyless_script_insertion = format!("{body_script_insertion}</body>");
    let should_insert_comment = !source_html.contains(comment);
    let comment_html = format!("\n    <!-- {comment} -->");

    let html = rewrite_str(
        source_html,
        RewriteStrSettings {
            element_content_handlers: vec![
                element!("script[type][src]", {
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
                }),
                element!("link[rel][href]", move |element| {
                    if !attribute_contains_token_ignore_ascii_case(element, "rel", "modulepreload")
                    {
                        return Ok(());
                    }
                    let href = element
                        .get_attribute("href")
                        .context("preload tag missing href")?;
                    let path = normalize_url_path(&href)?;
                    if path.ends_with(".js") {
                        let href = runtime_js_href(artifact, &path, out_dir, runtime_root)?;
                        element.set_attribute("href", &href)?;
                    }
                    Ok(())
                }),
                element!("head", {
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
                }),
                element!("body", {
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
                }),
                element!("*", |element| {
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
                }),
            ],
            document_content_handlers: vec![end!({
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
            })],
            ..RewriteStrSettings::new()
        },
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
        RewriteStrSettings {
            element_content_handlers: vec![
                element!("script[type][src]", {
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
                }),
                element!("link[rel][href]", {
                    let entries = Rc::clone(&entries);
                    move |element| {
                        if attribute_contains_token_ignore_ascii_case(
                            element,
                            "rel",
                            "modulepreload",
                        ) {
                            let href = element
                                .get_attribute("href")
                                .context("preload tag missing href")?;
                            entries.borrow_mut().module_preloads.push(HtmlEntry {
                                path: normalize_url_path(&href)?,
                            });
                        }
                        Ok(())
                    }
                }),
            ],
            ..RewriteStrSettings::new()
        },
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

fn prepare_harness_output_dir(layout: &DebundleOutputLayout) -> Result<()> {
    if layout.root().exists() && !layout.root().is_dir() {
        bail!(
            "Output path exists and is not a directory: {}",
            layout.root().display()
        );
    }
    let app_root = layout.app_root();
    fs::create_dir_all(app_root)?;
    fs::create_dir_all(layout.reports_root())?;
    Ok(())
}

fn copy_snapshot_assets(snapshot_root: &Path, out_dir: &Path) -> Result<Vec<String>> {
    let mut copied = Vec::new();
    copy_snapshot_assets_recursive(snapshot_root, out_dir, Path::new(""), &mut copied)?;
    Ok(copied)
}

fn copy_snapshot_assets_recursive(
    snapshot_root: &Path,
    out_dir: &Path,
    relative_dir: &Path,
    copied: &mut Vec<String>,
) -> Result<()> {
    let absolute_dir = snapshot_root.join(relative_dir);
    for entry in fs::read_dir(&absolute_dir)? {
        let entry = entry?;
        let relative_entry = relative_dir.join(entry.file_name());
        let relative_posix = module_path_from_path(&relative_entry);
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            copy_snapshot_assets_recursive(snapshot_root, out_dir, &relative_entry, copied)?;
            continue;
        }
        if !file_type.is_file()
            || relative_posix.ends_with(".js")
            || relative_posix.ends_with(".js.map")
        {
            continue;
        }
        let out_path = out_dir.join(&relative_entry);
        if let Some(parent) = out_path.parent() {
            fs::create_dir_all(parent)?;
        }
        copy_output_file(&entry.path(), &out_path)?;
        copied.push(relative_posix);
    }
    copied.sort();
    Ok(())
}

fn copy_output_file(source: &Path, target: &Path) -> Result<()> {
    fs::copy(source, target)?;
    make_owner_writable(target)?;
    Ok(())
}

#[cfg(unix)]
fn make_owner_writable(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::metadata(path)?;
    let mut permissions = metadata.permissions();
    permissions.set_mode(permissions.mode() | 0o200);
    fs::set_permissions(path, permissions)?;
    Ok(())
}

#[cfg(not(unix))]
fn make_owner_writable(path: &Path) -> Result<()> {
    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_readonly(false);
    fs::set_permissions(path, permissions)?;
    Ok(())
}

fn harness_monitor_script() -> &'static str {
    include_str!("live_proxy/harness_monitor.html")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn copied_snapshot_assets_remain_rewritable() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let snapshot = temp.path().join("snapshot");
        let out = temp.path().join("out");
        fs::create_dir_all(&snapshot)?;
        let source_html = snapshot.join("index.html");
        fs::write(&source_html, "<html></html>")?;

        #[cfg(unix)]
        fs::set_permissions(&source_html, fs::Permissions::from_mode(0o555))?;

        let copied = copy_snapshot_assets(&snapshot, &out)?;

        assert_eq!(copied, vec!["index.html"]);
        fs::write(out.join("index.html"), "<html>rewritten</html>")?;
        Ok(())
    }
}
