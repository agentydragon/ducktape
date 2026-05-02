use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

use artifact::{
    JsPipelineArtifact, chunk_id_for_js_path, manifest_relative_path, materialize_artifact_scripts,
    path_to_posix, split_posix_path,
};
use rewrite_specifiers::runtime_js_href;
use scrambled_id_frequencies::{compute_scrambled_identifier_frequencies, write_queue};

pub struct EmitBrowserHarnessOptions {
    pub asset_summary_path: PathBuf,
    pub force: bool,
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
    tag: String,
}

pub fn emit_browser_harness(
    artifact: &JsPipelineArtifact,
    options: &EmitBrowserHarnessOptions,
) -> Result<()> {
    let asset_summary: AssetSummary = serde_json::from_str(
        &fs::read_to_string(&options.asset_summary_path)
            .with_context(|| format!("reading {}", options.asset_summary_path.display()))?,
    )?;
    let html_path = asset_summary
        .entry_points
        .and_then(|entry_points| entry_points.html)
        .unwrap_or_else(|| "index.html".to_string());
    let source_html_path = options.snapshot_root.join(split_posix_path(&html_path));
    let source_html = fs::read_to_string(&source_html_path)
        .with_context(|| format!("reading {}", source_html_path.display()))?;
    let script_entries = html_script_entries(&source_html)?;
    let preload_entries = html_module_preload_entries(&source_html)?
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
        let chunk_id = chunk_id_for_js_path(path)?;
        if !artifact.chunks.contains_key(&chunk_id) {
            bail!("Snapshot manifest does not contain chunk {chunk_id}");
        }
    }

    prepare_harness_output_dir(&options.out_dir, options.force)?;
    materialize_artifact_scripts(artifact, &options.out_dir)?;
    let copied_assets = copy_snapshot_assets(&options.snapshot_root, &options.out_dir)?;
    let bootstrap = build_bootstrap(artifact, &entry_scripts, &options.out_dir, &options.out_dir)?;
    let index_html =
        rewrite_index_html(artifact, &source_html, &options.out_dir, &options.out_dir)?;

    fs::write(options.out_dir.join("index.html"), index_html)?;
    fs::write(options.out_dir.join("bootstrap.js"), bootstrap)?;
    fs::write(
        options.out_dir.join("chunks.manifest.json"),
        serde_json::to_string_pretty(&serde_json::json!({
            "chunks": artifact
                .root_manifest
                .as_ref()
                .context("emitBrowserHarness requires artifact manifest")?
                .chunks
                .clone(),
        }))? + "\n",
    )?;
    // Make the harness tree self-contained: copy the upstream source HTML
    // and asset-summary into the output dir so the live proxy (and any
    // other consumer reading the manifest) only needs the manifest's own
    // directory in its runfiles, not the unrelated `extracted/` and
    // `snapshots/` input trees. The recursive snapshot copy above already
    // brought in SOURCE.json and other static assets; the source HTML's
    // pre-rewrite copy gets a stable `source.html` name (the snapshot's
    // copy at `<html_path>` is overwritten by the rewritten index above
    // when the two collide).
    let source_html_in_tree = options.out_dir.join("source.html");
    fs::write(&source_html_in_tree, &source_html)?;
    let asset_summary_in_tree = options.out_dir.join("asset-summary.json");
    fs::copy(&options.asset_summary_path, &asset_summary_in_tree)?;
    let manifest_path = options.out_dir.join("manifest.json");
    let rel = |target: &Path| manifest_relative_path(&manifest_path, target);
    // The scrambled-identifier frequency queue is a side output of every
    // harness emit; write it now so its path can be recorded in the
    // manifest.
    let queue = compute_scrambled_identifier_frequencies(artifact)?;
    let queue_path = write_queue(&options.out_dir, &queue)?;
    let manifest = HarnessManifest {
        schema_version: 1,
        source_html: rel(&source_html_in_tree),
        asset_summary_path: rel(&asset_summary_in_tree),
        chunks_manifest_path: rel(&options.out_dir.join("chunks.manifest.json")),
        runtime_root: rel(&options.out_dir),
        out_dir: rel(&options.out_dir),
        copied_assets,
        entry_scripts,
        module_preloads: preload_entries
            .iter()
            .map(|entry| entry.path.clone())
            .collect(),
        scrambled_identifier_frequencies: rel(&queue_path),
        generated: HarnessGeneratedManifest {
            bootstrap: rel(&options.out_dir.join("bootstrap.js")),
            chunks_manifest: rel(&options.out_dir.join("chunks.manifest.json")),
            index_html: rel(&options.out_dir.join("index.html")),
        },
    };
    fs::write(
        &manifest_path,
        serde_json::to_string_pretty(&manifest)? + "\n",
    )?;
    fs::write(
        options.out_dir.join("package.json"),
        "{\n  \"type\": \"module\"\n}\n",
    )?;
    Ok(())
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct HarnessManifest {
    schema_version: u8,
    source_html: String,
    asset_summary_path: String,
    chunks_manifest_path: String,
    runtime_root: String,
    out_dir: String,
    copied_assets: Vec<String>,
    entry_scripts: Vec<String>,
    module_preloads: Vec<String>,
    /// Path to the scrambled-identifier frequency queue JSON (always
    /// emitted as a side output). Manifest-relative.
    scrambled_identifier_frequencies: String,
    generated: HarnessGeneratedManifest,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct HarnessGeneratedManifest {
    bootstrap: String,
    chunks_manifest: String,
    index_html: String,
}

fn build_bootstrap(
    artifact: &JsPipelineArtifact,
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
    artifact: &JsPipelineArtifact,
    source_html: &str,
    out_dir: &Path,
    runtime_root: &Path,
) -> Result<String> {
    let mut html = source_html.to_string();
    let script_entries = html_script_entries(source_html)?;
    let mut script_inserted = false;
    for entry in script_entries {
        let replacement = if script_inserted {
            String::new()
        } else {
            script_inserted = true;
            format!(
                "{}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>",
                harness_monitor_script()
            )
        };
        html = html.replacen(&entry.tag, &replacement, 1);
    }
    for entry in html_module_preload_entries(&html)? {
        if !entry.path.ends_with(".js") {
            continue;
        }
        let href = runtime_js_href(artifact, &entry.path, out_dir, runtime_root)?;
        let rewritten = set_attr(&entry.tag, "href", &href)?;
        html = html.replacen(&entry.tag, &rewritten, 1);
    }
    html = rewrite_root_absolute_urls(&html);
    if !script_inserted {
        let insertion = format!(
            "    {}\n    <script type=\"module\" src=\"./bootstrap.js\"></script>\n  </body>",
            harness_monitor_script()
        );
        html = replace_case_insensitive_once(&html, "</body>", &insertion).unwrap_or_else(|| {
            let mut next = html.clone();
            next.push('\n');
            next.push_str(&insertion);
            next
        });
    }
    let comment = "Generated local harness: loads generated runtime JavaScript from the transformed output tree.";
    if !html.contains(comment) {
        html = replace_case_insensitive_once(
            &html,
            "<head>",
            &format!("<head>\n    <!-- {comment} -->"),
        )
        .unwrap_or(html);
    }
    if html.ends_with('\n') {
        Ok(html)
    } else {
        Ok(format!("{html}\n"))
    }
}

fn html_script_entries(html: &str) -> Result<Vec<HtmlEntry>> {
    html_tags(html, "script")
        .into_iter()
        .filter(|tag| {
            attr_value(tag, "type").is_some_and(|value| value.eq_ignore_ascii_case("module"))
                && attr_value(tag, "src").is_some()
        })
        .map(|tag| {
            let src = attr_value(&tag, "src").context("script tag missing src")?;
            Ok(HtmlEntry {
                path: normalize_url_path(&src)?,
                tag,
            })
        })
        .collect()
}

fn html_module_preload_entries(html: &str) -> Result<Vec<HtmlEntry>> {
    html_tags(html, "link")
        .into_iter()
        .filter(|tag| {
            attr_value(tag, "rel")
                .map(|value| {
                    value
                        .split_ascii_whitespace()
                        .any(|part| part.eq_ignore_ascii_case("modulepreload"))
                })
                .unwrap_or(false)
                && attr_value(tag, "href").is_some()
        })
        .map(|tag| {
            let href = attr_value(&tag, "href").context("preload tag missing href")?;
            Ok(HtmlEntry {
                path: normalize_url_path(&href)?,
                tag,
            })
        })
        .collect()
}

fn html_tags(html: &str, tag_name: &str) -> Vec<String> {
    let mut tags = Vec::new();
    let needle = format!("<{tag_name}");
    let mut search_start = 0usize;
    while let Some(relative_start) = lower_find(&html[search_start..], &needle) {
        let start = search_start + relative_start;
        let Some(relative_end) = html[start..].find('>') else {
            break;
        };
        let end = start + relative_end + 1;
        let mut tag = html[start..end].to_string();
        if tag_name == "script"
            && let Some(close_relative) = lower_find(&html[end..], "</script>")
        {
            let close_end = end + close_relative + "</script>".len();
            tag = html[start..close_end].to_string();
            search_start = close_end;
        } else {
            search_start = end;
        }
        tags.push(tag);
    }
    tags
}

fn attr_value(tag: &str, name: &str) -> Option<String> {
    let lower = tag.to_ascii_lowercase();
    let needle = name.to_ascii_lowercase();
    let mut index = 0usize;
    while let Some(relative) = lower[index..].find(&needle) {
        let start = index + relative;
        let before_ok = start == 0 || !lower.as_bytes()[start - 1].is_ascii_alphanumeric();
        let after = start + needle.len();
        if !before_ok {
            index = after;
            continue;
        }
        let mut cursor = after;
        while cursor < tag.len() && tag.as_bytes()[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if cursor >= tag.len() || tag.as_bytes()[cursor] != b'=' {
            index = after;
            continue;
        }
        cursor += 1;
        while cursor < tag.len() && tag.as_bytes()[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if cursor >= tag.len() {
            return None;
        }
        let quote = tag.as_bytes()[cursor];
        if quote == b'"' || quote == b'\'' {
            cursor += 1;
            let end = tag[cursor..].find(quote as char)? + cursor;
            return Some(tag[cursor..end].to_string());
        }
        let end = tag[cursor..]
            .find(|ch: char| ch.is_ascii_whitespace() || ch == '>')
            .map(|offset| cursor + offset)
            .unwrap_or(tag.len());
        return Some(tag[cursor..end].to_string());
    }
    None
}

fn set_attr(tag: &str, name: &str, value: &str) -> Result<String> {
    let escaped = escape_html_attr(value);
    let lower = tag.to_ascii_lowercase();
    let needle = name.to_ascii_lowercase();
    let start = lower
        .find(&needle)
        .with_context(|| format!("Tag is missing {name}: {tag}"))?;
    let mut cursor = start + needle.len();
    while cursor < tag.len() && tag.as_bytes()[cursor].is_ascii_whitespace() {
        cursor += 1;
    }
    if cursor >= tag.len() || tag.as_bytes()[cursor] != b'=' {
        bail!("Tag is missing {name}: {tag}");
    }
    cursor += 1;
    while cursor < tag.len() && tag.as_bytes()[cursor].is_ascii_whitespace() {
        cursor += 1;
    }
    let quote = tag.as_bytes()[cursor];
    let (value_start, value_end, rendered) = if quote == b'\'' || quote == b'"' {
        let value_start = cursor + 1;
        let value_end = tag[value_start..]
            .find(quote as char)
            .map(|offset| value_start + offset)
            .context("unterminated quoted attribute")?;
        (value_start - 1, value_end + 1, format!("\"{escaped}\""))
    } else {
        let value_start = cursor;
        let value_end = tag[value_start..]
            .find(|ch: char| ch.is_ascii_whitespace() || ch == '>')
            .map(|offset| value_start + offset)
            .unwrap_or(tag.len());
        (value_start, value_end, format!("\"{escaped}\""))
    };
    let mut out = String::new();
    out.push_str(&tag[..value_start]);
    out.push_str(&rendered);
    out.push_str(&tag[value_end..]);
    Ok(out)
}

fn normalize_url_path(url: &str) -> Result<String> {
    if url.is_empty() || url.starts_with("//") || url.contains("://") {
        bail!("Expected a snapshot-relative URL, got {url}");
    }
    let without_hash = url.split('#').next().unwrap_or(url);
    let without_query = without_hash.split('?').next().unwrap_or(without_hash);
    let stripped = without_query
        .strip_prefix('/')
        .unwrap_or(without_query)
        .strip_prefix("./")
        .unwrap_or(without_query.strip_prefix('/').unwrap_or(without_query));
    ::artifact::normalize_relative_path(stripped)
}

fn prepare_harness_output_dir(out_dir: &Path, force: bool) -> Result<()> {
    if out_dir.exists() {
        if !out_dir.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                out_dir.display()
            );
        }
        let removable = fs::read_dir(out_dir)?
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                name != "analysis" && name != "vendors"
            })
            .collect::<Vec<_>>();
        if !removable.is_empty() && !force {
            bail!(
                "Output directory is not empty: {}. Pass --force to replace it.",
                out_dir.display()
            );
        }
        if force {
            for entry in removable {
                let path = entry.path();
                if path.is_dir() {
                    fs::remove_dir_all(path)?;
                } else {
                    fs::remove_file(path)?;
                }
            }
        }
    }
    fs::create_dir_all(out_dir)?;
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
        let relative_posix = path_to_posix(&relative_entry);
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
        fs::copy(entry.path(), out_path)?;
        copied.push(relative_posix);
    }
    copied.sort();
    Ok(())
}

fn rewrite_root_absolute_urls(html: &str) -> String {
    let mut out = String::with_capacity(html.len());
    let mut cursor = 0usize;
    while let Some(relative) = lower_find(&html[cursor..], "=\"/") {
        let start = cursor + relative;
        out.push_str(&html[cursor..start + 2]);
        out.push('.');
        cursor = start + 2;
    }
    out.push_str(&html[cursor..]);
    out
}

fn replace_case_insensitive_once(text: &str, needle: &str, replacement: &str) -> Option<String> {
    let start = lower_find(text, needle)?;
    let mut out = String::new();
    out.push_str(&text[..start]);
    out.push_str(replacement);
    out.push_str(&text[start + needle.len()..]);
    Some(out)
}

fn lower_find(text: &str, needle: &str) -> Option<usize> {
    text.to_ascii_lowercase().find(&needle.to_ascii_lowercase())
}

fn escape_html_attr(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('"', "&quot;")
        .replace('<', "&lt;")
}

fn harness_monitor_script() -> &'static str {
    r#"<script>
      globalThis.__debundleHarness = { errors: [] };
      (() => {
        const state = globalThis.__debundleHarness;
        const render = (message) => {
          const body = document.body;
          if (!body) {
            return;
          }
          let node = document.getElementById("debundle-harness-error");
          if (!node) {
            node = document.createElement("pre");
            node.id = "debundle-harness-error";
            node.style.cssText = "position:fixed;inset:0;z-index:2147483647;margin:0;padding:16px;white-space:pre-wrap;background:#2b0000;color:#ffd8d8;font:13px/1.4 monospace;";
            body.appendChild(node);
          }
          node.textContent = message;
        };
        const messageFor = (kind, value) => {
          if (value && value.stack) {
            return value.stack;
          }
          if (value && typeof value === "object") {
            try {
              return JSON.stringify(value);
            } catch {
              return String(value);
            }
          }
          return String(value ?? kind);
        };
        const record = (kind, value, visible) => {
          const message = messageFor(kind, value);
          state.errors.push({ kind, message });
          document.documentElement.dataset.debundleHarnessLastEvent = message;
          if (kind === "error") {
            document.documentElement.dataset.debundleHarnessError = message;
          }
          if (visible) {
            if (document.readyState === "loading") {
              addEventListener("DOMContentLoaded", () => render(message), { once: true });
            } else {
              render(message);
            }
          }
        };
        addEventListener("error", (event) => record("error", event.error ?? event.message, true));
        addEventListener("unhandledrejection", (event) => record("unhandledrejection", event.reason, false));
        addEventListener("DOMContentLoaded", () => {
          document.documentElement.dataset.debundleHarnessDomContentLoaded = "true";
        });
        addEventListener("load", () => {
          document.documentElement.dataset.debundleHarnessLoaded = "true";
        });
      })();
    </script>"#
}
