//! CLI verbs `debundle bindings comment <sym>` and
//! `debundle modules comment <module>`: edit the `comment:` field on a
//! single member entry or on a module YAML.
//!
//! This module is intentionally generic over `serde_yaml::Value` so it
//! does not need the analysis crate. The two `comment:` fields it
//! touches:
//!
//! * Per-member: `<modules>/.../*.yaml :: members[i].comment`. The
//!   member is located by matching the supplied `<sym>` against either
//!   `members[i].selector.binding.name` (minified form) or
//!   `members[i].name` (readable form). If both happen to match
//!   different members, the command refuses with the full match list.
//! * Module-level: `<modules>/<module>.yaml :: comment`. The module
//!   path argument is filesystem-relative (no `.yaml` suffix), same
//!   shape `spec_modules::module_path_from_file` emits.
//!
//! Three modes are shared by both verbs:
//!
//! * Positional `"text"` — replace the existing comment with the
//!   literal arg.
//! * `--edit` — spawn `$EDITOR` (fallback `$VISUAL`, then `vi`) on a
//!   tempfile prepopulated with the current comment; pick up the
//!   result.
//! * `--clear` — remove the `comment:` field entirely.
//! * No arg — read the current comment and print (text or JSON
//!   depending on `--format` / tty).
//!
//! Writes preserve key order by mutating the `serde_yaml::Mapping`
//! in place and re-serializing the same `Value`. `--dry-run` skips
//! the write but still prints the verdict. `--no-verify` is accepted
//! and a no-op (comments don't participate in factorization).

use std::fs;
use std::io::{IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, anyhow, bail};
use clap::{Args as ClapArgs, ValueEnum};
use serde_yaml::Value;

use crate::yaml_edit::{read_yaml, write_yaml_if_semantic_changed, yaml_semantically_changed};

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
pub enum Format {
    Text,
    Json,
}

/// Args for `debundle bindings comment <sym> [...]`.
#[derive(Debug, ClapArgs)]
pub struct BindingCommentArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    modules_root: PathBuf,

    /// Binding identifier: minified name (e.g. `XOe`) or readable
    /// name (e.g. `PluginSettingsAccessor`).
    sym: String,

    /// Replacement comment text. Mutually exclusive with `--edit`
    /// and `--clear`. Omit all three to read the current comment.
    text: Option<String>,

    /// Spawn `$EDITOR` (fallback `$VISUAL`, then `vi`) on a tempfile
    /// pre-populated with the current comment.
    #[arg(long, conflicts_with_all = ["clear", "text"])]
    edit: bool,

    /// Remove the `comment:` field entirely.
    #[arg(long, conflicts_with_all = ["edit", "text"])]
    clear: bool,

    /// Output format for read mode. Default `text` on tty, `json`
    /// on pipe.
    #[arg(long, value_enum)]
    format: Option<Format>,

    /// Validate (or simulate) but do not modify any file.
    #[arg(long)]
    dry_run: bool,

    /// Accepted for symmetry with mutating commands; no-op for
    /// comment edits (comments do not affect factorization).
    #[arg(long)]
    no_verify: bool,
}

/// Args for `debundle modules comment <module> [...]`.
#[derive(Debug, ClapArgs)]
pub struct ModuleCommentArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    modules_root: PathBuf,

    /// Module path relative to `--modules` (no `.yaml` suffix), e.g.
    /// `runtime/plugins`.
    module: String,

    /// Replacement comment text. Mutually exclusive with `--edit`
    /// and `--clear`. Omit all three to read the current comment.
    text: Option<String>,

    /// Spawn `$EDITOR` on a tempfile pre-populated with the current
    /// comment.
    #[arg(long, conflicts_with_all = ["clear", "text"])]
    edit: bool,

    /// Remove the `comment:` field entirely.
    #[arg(long, conflicts_with_all = ["edit", "text"])]
    clear: bool,

    /// Output format for read mode.
    #[arg(long, value_enum)]
    format: Option<Format>,

    /// Validate (or simulate) but do not modify any file.
    #[arg(long)]
    dry_run: bool,

    /// Accepted for symmetry; no-op for comment edits.
    #[arg(long)]
    no_verify: bool,
}

/// Mode dispatched by `apply_*_command`.
#[derive(Debug, Clone)]
pub enum CommentMode {
    /// Print the current comment.
    Read,
    /// Replace the comment with the given literal text.
    Set(String),
    /// Spawn `$EDITOR` on a tempfile preloaded with the current text.
    Edit,
    /// Remove the `comment:` field.
    Clear,
}

impl CommentMode {
    fn from_flags(text: Option<String>, edit: bool, clear: bool) -> Result<Self> {
        match (text, edit, clear) {
            (Some(t), false, false) => Ok(Self::Set(t)),
            (None, true, false) => Ok(Self::Edit),
            (None, false, true) => Ok(Self::Clear),
            (None, false, false) => Ok(Self::Read),
            _ => bail!("--edit, --clear, and positional comment text are mutually exclusive"),
        }
    }
}

fn resolve_format(format: Option<Format>) -> Format {
    if let Some(f) = format {
        return f;
    }
    if std::io::stdout().is_terminal() {
        Format::Text
    } else {
        Format::Json
    }
}

/// Public entry point for the inner `comment` verb under either the
/// `bindings` or `modules` namespace. Composed by the top-level
/// `cli.rs` so the new `modules` clap node can sit alongside `merge`
/// / `propose` without duplicating the comment YAML logic.
pub fn run_binding_comment_cmd(args: BindingCommentArgs) -> Result<()> {
    run_binding_comment(args)
}

/// See [`run_binding_comment_cmd`].
pub fn run_module_comment_cmd(args: ModuleCommentArgs) -> Result<()> {
    run_module_comment(args)
}

// ---------------------------------------------------------------------
// Binding comment
// ---------------------------------------------------------------------

fn run_binding_comment(args: BindingCommentArgs) -> Result<()> {
    let _ = args.no_verify; // accepted, no-op
    let mode = CommentMode::from_flags(args.text, args.edit, args.clear)?;
    let outcome = apply_binding_comment(&args.modules_root, &args.sym, mode, args.dry_run)?;
    let format = resolve_format(args.format);
    print_outcome(&outcome, format);
    Ok(())
}

/// Outcome of a single comment edit / read, suitable for printing.
#[derive(Debug, Clone)]
pub struct CommentOutcome {
    /// The locator we operated on (sym for bindings, module path for
    /// modules). The printed JSON uses `sym` or `module` based on
    /// `kind`.
    pub locator: String,
    pub kind: OutcomeKind,
    /// What the comment looks like after the operation (or, in read
    /// mode, what it currently is). Empty string means absent.
    pub comment: String,
    /// One of "read", "set", "cleared", "unchanged", "dry-run".
    pub action: &'static str,
    /// Absolute path of the YAML touched (or that would be touched).
    pub path: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutcomeKind {
    Binding,
    Module,
}

fn print_outcome(outcome: &CommentOutcome, format: Format) {
    match format {
        Format::Text => {
            println!("{}", outcome.comment);
        }
        Format::Json => {
            let key = match outcome.kind {
                OutcomeKind::Binding => "sym",
                OutcomeKind::Module => "module",
            };
            let json = format!(
                "{{\"{}\":{},\"comment\":{},\"action\":{}}}",
                key,
                json_string(&outcome.locator),
                json_string(&outcome.comment),
                json_string(outcome.action),
            );
            println!("{json}");
        }
    }
}

fn json_string(s: &str) -> String {
    // Minimal JSON string escaper. We don't import serde_json just
    // for this; the inputs are short and well-behaved.
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Find the member matching `sym` and apply `mode`. Returns the
/// outcome with the post-state comment (or current comment in read
/// mode).
pub fn apply_binding_comment(
    modules_root: &Path,
    sym: &str,
    mode: CommentMode,
    dry_run: bool,
) -> Result<CommentOutcome> {
    let matches = find_binding_matches(modules_root, sym)?;
    let hit = match matches.len() {
        0 => bail!(
            "no binding named \"{sym}\" found under {}",
            modules_root.display()
        ),
        1 => matches.into_iter().next().expect("len==1"),
        _ => {
            let locations: Vec<String> = matches
                .iter()
                .map(|m| {
                    format!(
                        "  {} (binding={}, name={})",
                        m.file.display(),
                        m.binding_name,
                        m.readable_name.as_deref().unwrap_or("-")
                    )
                })
                .collect();
            bail!(
                "ambiguous binding identifier \"{sym}\": {} matches:\n{}",
                matches.len(),
                locations.join("\n")
            );
        }
    };

    let BindingMatch {
        file, member_index, ..
    } = hit;
    let mut doc = read_yaml(&file)?;
    let current = current_member_comment(&doc, member_index)?.unwrap_or_default();

    let (new_action, new_comment, dirty) = match mode {
        CommentMode::Read => ("read", current.clone(), false),
        CommentMode::Set(text) => {
            set_member_comment(&mut doc, member_index, Some(text.clone()))?;
            ("set", text, true)
        }
        CommentMode::Edit => {
            let edited = run_editor(&current)?;
            if edited == current {
                ("unchanged", current, false)
            } else if edited.is_empty() {
                set_member_comment(&mut doc, member_index, None)?;
                ("cleared", String::new(), true)
            } else {
                set_member_comment(&mut doc, member_index, Some(edited.clone()))?;
                ("set", edited, true)
            }
        }
        CommentMode::Clear => {
            let was_present = current_member_comment(&doc, member_index)?.is_some();
            if was_present {
                set_member_comment(&mut doc, member_index, None)?;
                ("cleared", String::new(), true)
            } else {
                ("unchanged", String::new(), false)
            }
        }
    };

    let changed = dirty && yaml_semantically_changed(&file, &doc)?;
    let action = if dirty && !changed {
        "unchanged"
    } else if changed && dry_run {
        "dry-run"
    } else {
        new_action
    };
    if changed && !dry_run {
        write_yaml_if_semantic_changed(&file, &doc)?;
    }

    Ok(CommentOutcome {
        locator: sym.to_string(),
        kind: OutcomeKind::Binding,
        comment: new_comment,
        action,
        path: file,
    })
}

#[derive(Debug, Clone)]
struct BindingMatch {
    file: PathBuf,
    member_index: usize,
    binding_name: String,
    readable_name: Option<String>,
}

fn find_binding_matches(modules_root: &Path, sym: &str) -> Result<Vec<BindingMatch>> {
    let files = collect_yaml_files(modules_root)?;
    let mut out = Vec::new();
    for file in files {
        let doc = read_yaml(&file)?;
        let Some(members) = doc.as_mapping().and_then(|m| m.get(yk("members"))) else {
            continue;
        };
        let Some(seq) = members.as_sequence() else {
            continue;
        };
        for (idx, member) in seq.iter().enumerate() {
            let Some(map) = member.as_mapping() else {
                continue;
            };
            let binding_name = map
                .get(yk("selector"))
                .and_then(Value::as_mapping)
                .and_then(|s| s.get(yk("binding")))
                .and_then(Value::as_mapping)
                .and_then(|b| b.get(yk("name")))
                .and_then(Value::as_str)
                .map(str::to_string);
            let readable_name = map
                .get(yk("name"))
                .and_then(Value::as_str)
                .map(str::to_string);
            let binding_match = binding_name.as_deref() == Some(sym);
            let readable_match = readable_name.as_deref() == Some(sym);
            if binding_match || readable_match {
                out.push(BindingMatch {
                    file: file.clone(),
                    member_index: idx,
                    binding_name: binding_name.unwrap_or_default(),
                    readable_name,
                });
            }
        }
    }
    Ok(out)
}

fn current_member_comment(doc: &Value, index: usize) -> Result<Option<String>> {
    let members = doc
        .as_mapping()
        .and_then(|m| m.get(yk("members")))
        .and_then(Value::as_sequence)
        .ok_or_else(|| anyhow!("module YAML missing members sequence"))?;
    let member = members
        .get(index)
        .ok_or_else(|| anyhow!("member index {index} out of range"))?;
    let Some(map) = member.as_mapping() else {
        return Ok(None);
    };
    Ok(map
        .get(yk("comment"))
        .and_then(Value::as_str)
        .map(str::to_string))
}

fn set_member_comment(doc: &mut Value, index: usize, value: Option<String>) -> Result<()> {
    let members = doc
        .as_mapping_mut()
        .and_then(|m| m.get_mut(yk("members")))
        .and_then(Value::as_sequence_mut)
        .ok_or_else(|| anyhow!("module YAML missing members sequence"))?;
    let member = members
        .get_mut(index)
        .ok_or_else(|| anyhow!("member index {index} out of range"))?;
    let map = member
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("member entry is not a mapping"))?;
    match value {
        Some(text) => {
            map.insert(yk("comment"), Value::String(text));
        }
        None => {
            map.remove(yk("comment"));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Module comment
// ---------------------------------------------------------------------

fn run_module_comment(args: ModuleCommentArgs) -> Result<()> {
    let _ = args.no_verify;
    let mode = CommentMode::from_flags(args.text, args.edit, args.clear)?;
    let outcome = apply_module_comment(&args.modules_root, &args.module, mode, args.dry_run)?;
    let format = resolve_format(args.format);
    print_outcome(&outcome, format);
    Ok(())
}

pub fn apply_module_comment(
    modules_root: &Path,
    module: &str,
    mode: CommentMode,
    dry_run: bool,
) -> Result<CommentOutcome> {
    let file = module_path_to_yaml(modules_root, module);
    if !file.exists() {
        bail!("module YAML not found: {}", file.display());
    }
    let mut doc = read_yaml(&file)?;
    let current = current_module_comment(&doc).unwrap_or_default();

    let (new_action, new_comment, dirty) = match mode {
        CommentMode::Read => ("read", current.clone(), false),
        CommentMode::Set(text) => {
            set_module_comment(&mut doc, Some(text.clone()))?;
            ("set", text, true)
        }
        CommentMode::Edit => {
            let edited = run_editor(&current)?;
            if edited == current {
                ("unchanged", current, false)
            } else if edited.is_empty() {
                set_module_comment(&mut doc, None)?;
                ("cleared", String::new(), true)
            } else {
                set_module_comment(&mut doc, Some(edited.clone()))?;
                ("set", edited, true)
            }
        }
        CommentMode::Clear => {
            let was_present = current_module_comment(&doc).is_some();
            if was_present {
                set_module_comment(&mut doc, None)?;
                ("cleared", String::new(), true)
            } else {
                ("unchanged", String::new(), false)
            }
        }
    };

    let changed = dirty && yaml_semantically_changed(&file, &doc)?;
    let action = if dirty && !changed {
        "unchanged"
    } else if changed && dry_run {
        "dry-run"
    } else {
        new_action
    };
    if changed && !dry_run {
        write_yaml_if_semantic_changed(&file, &doc)?;
    }

    Ok(CommentOutcome {
        locator: module.to_string(),
        kind: OutcomeKind::Module,
        comment: new_comment,
        action,
        path: file,
    })
}

fn module_path_to_yaml(modules_root: &Path, module: &str) -> PathBuf {
    let mut rel = PathBuf::from(module);
    if rel.extension().and_then(|s| s.to_str()) != Some("yaml") {
        rel.set_extension("yaml");
    }
    modules_root.join(rel)
}

fn current_module_comment(doc: &Value) -> Option<String> {
    doc.as_mapping()
        .and_then(|m| m.get(yk("comment")))
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn set_module_comment(doc: &mut Value, value: Option<String>) -> Result<()> {
    let map = doc
        .as_mapping_mut()
        .ok_or_else(|| anyhow!("module YAML is not a mapping"))?;
    match value {
        Some(text) => {
            map.insert(yk("comment"), Value::String(text));
        }
        None => {
            map.remove(yk("comment"));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------

fn yk(s: &str) -> Value {
    Value::String(s.to_string())
}

fn collect_yaml_files(root: &Path) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    collect_into(root, &mut out)?;
    out.sort();
    Ok(out)
}

fn collect_into(root: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root).with_context(|| format!("reading {}", root.display()))? {
        let entry = entry.with_context(|| format!("walking {}", root.display()))?;
        let path = entry.path();
        if path.is_dir() {
            collect_into(&path, out)?;
        } else if path.extension().and_then(|s| s.to_str()) == Some("yaml") {
            out.push(path);
        }
    }
    Ok(())
}

/// Spawn the user's editor on a tempfile pre-populated with `current`.
/// Returns the trimmed-trailing-newline contents on exit.
fn run_editor(current: &str) -> Result<String> {
    let editor = std::env::var("EDITOR")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("VISUAL").ok().filter(|s| !s.is_empty()))
        .unwrap_or_else(|| "vi".to_string());

    let dir = std::env::temp_dir();
    let pid = std::process::id();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let path = dir.join(format!("debundle-comment-{pid}-{nanos}.txt"));

    {
        let mut f = fs::File::create(&path)
            .with_context(|| format!("creating tempfile {}", path.display()))?;
        f.write_all(current.as_bytes())
            .with_context(|| format!("writing tempfile {}", path.display()))?;
    }

    // Split editor on whitespace so users can set `EDITOR="code -w"`.
    let mut parts = editor.split_whitespace();
    let prog = parts.next().ok_or_else(|| anyhow!("empty $EDITOR"))?;
    let extra: Vec<&str> = parts.collect();
    let status = Command::new(prog)
        .args(&extra)
        .arg(&path)
        .status()
        .with_context(|| format!("spawning editor {editor}"))?;
    if !status.success() {
        bail!("editor {editor} exited with status {status}");
    }

    let mut s = String::new();
    fs::File::open(&path)
        .with_context(|| format!("reopening tempfile {}", path.display()))?
        .read_to_string(&mut s)
        .with_context(|| format!("reading tempfile {}", path.display()))?;
    let _ = fs::remove_file(&path);
    // Strip a single trailing newline (most editors append one).
    if let Some(stripped) = s.strip_suffix('\n') {
        s = stripped.to_string();
    }
    Ok(s)
}

// ---------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let p = root.join(rel);
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(p, body).unwrap();
    }

    fn read(root: &Path, rel: &str) -> String {
        fs::read_to_string(root.join(rel)).unwrap()
    }

    #[test]
    fn set_then_read_then_clear_binding_comment() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "ui/widgets.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );

        let set = apply_binding_comment(
            root,
            "XOe",
            CommentMode::Set("acquires plugin settings".into()),
            false,
        )
        .unwrap();
        assert_eq!(set.action, "set");
        assert_eq!(set.comment, "acquires plugin settings");

        let body = read(root, "ui/widgets.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(
            doc["members"][0]["comment"].as_str(),
            Some("acquires plugin settings")
        );

        let got = apply_binding_comment(root, "XOe", CommentMode::Read, false).unwrap();
        assert_eq!(got.comment, "acquires plugin settings");
        assert_eq!(got.action, "read");

        let cleared = apply_binding_comment(root, "XOe", CommentMode::Clear, false).unwrap();
        assert_eq!(cleared.action, "cleared");
        assert_eq!(cleared.comment, "");
        let body = read(root, "ui/widgets.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert!(
            doc["members"][0]
                .as_mapping()
                .unwrap()
                .get(yk("comment"))
                .is_none()
        );
    }

    #[test]
    fn binding_resolves_by_readable_name() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "runtime/plugins.yaml",
            "members:\n  - name: PluginSettingsAccessor\n    selector: { binding: { name: XOe } }\n",
        );
        let set = apply_binding_comment(
            root,
            "PluginSettingsAccessor",
            CommentMode::Set("readable hit".into()),
            false,
        )
        .unwrap();
        assert_eq!(set.action, "set");
        let body = read(root, "runtime/plugins.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(doc["members"][0]["comment"].as_str(), Some("readable hit"));
    }

    #[test]
    fn ambiguous_binding_refuses_with_list() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        // Same `sym` matches as a minified name in one file and as a
        // readable name in another.
        write(
            root,
            "a.yaml",
            "members:\n  - selector: { binding: { name: Foo } }\n",
        );
        write(
            root,
            "b.yaml",
            "members:\n  - name: Foo\n    selector: { binding: { name: YYY } }\n",
        );
        let err =
            apply_binding_comment(root, "Foo", CommentMode::Set("nope".into()), false).unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains("ambiguous"), "msg={msg}");
        assert!(msg.contains("a.yaml"), "msg={msg}");
        assert!(msg.contains("b.yaml"), "msg={msg}");
        // Neither file was modified.
        let a = read(root, "a.yaml");
        let b = read(root, "b.yaml");
        assert!(!a.contains("comment:"), "a={a}");
        assert!(!b.contains("comment:"), "b={b}");
    }

    #[test]
    fn unknown_binding_errors() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "m.yaml", "members: []\n");
        let err = apply_binding_comment(root, "Nope", CommentMode::Read, false).unwrap_err();
        assert!(format!("{err}").contains("no binding named"));
    }

    #[test]
    fn dry_run_does_not_write_binding() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "m.yaml",
            "members:\n  - selector: { binding: { name: XOe } }\n",
        );
        let original = read(root, "m.yaml");
        let out =
            apply_binding_comment(root, "XOe", CommentMode::Set("will not stick".into()), true)
                .unwrap();
        assert_eq!(out.action, "dry-run");
        assert_eq!(out.comment, "will not stick");
        assert_eq!(read(root, "m.yaml"), original);
    }

    #[test]
    fn setting_same_binding_comment_preserves_formatting() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        let original = "# hand formatted\nmembers: [ { selector: { binding: { name: XOe } }, comment: keep } ]\n";
        write(root, "m.yaml", original);

        let out =
            apply_binding_comment(root, "XOe", CommentMode::Set("keep".into()), false).unwrap();

        assert_eq!(out.action, "unchanged");
        assert_eq!(read(root, "m.yaml"), original);
    }

    #[test]
    fn set_then_read_then_clear_module_comment() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "runtime/plugins.yaml", "members: []\n");

        let set = apply_module_comment(
            root,
            "runtime/plugins",
            CommentMode::Set("plugin glue".into()),
            false,
        )
        .unwrap();
        assert_eq!(set.action, "set");
        let body = read(root, "runtime/plugins.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(doc["comment"].as_str(), Some("plugin glue"));

        let got = apply_module_comment(root, "runtime/plugins", CommentMode::Read, false).unwrap();
        assert_eq!(got.comment, "plugin glue");

        let cleared =
            apply_module_comment(root, "runtime/plugins", CommentMode::Clear, false).unwrap();
        assert_eq!(cleared.action, "cleared");
        let body = read(root, "runtime/plugins.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert!(doc.as_mapping().unwrap().get(yk("comment")).is_none());
    }

    #[test]
    fn missing_module_errors() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        let err = apply_module_comment(root, "no/such", CommentMode::Read, false).unwrap_err();
        assert!(format!("{err}").contains("module YAML not found"));
    }

    #[test]
    fn from_flags_rejects_conflicts() {
        assert!(CommentMode::from_flags(Some("x".into()), true, false).is_err());
        assert!(CommentMode::from_flags(Some("x".into()), false, true).is_err());
        assert!(CommentMode::from_flags(None, true, true).is_err());
    }
}
