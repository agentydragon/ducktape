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
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, anyhow, bail};
use clap::Args as ClapArgs;
use serde::Serialize;
use serde_yaml::Value;
use spec::ModulePath;

use crate::binding::resolve_unambiguous;
use crate::yaml_edit::{read_yaml, write_yaml_if_semantic_changed, yaml_semantically_changed};

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
    format: Option<peel::OutputFormat>,

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
    format: Option<peel::OutputFormat>,

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
    let format = peel::OutputFormat::resolve(args.format);
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
    /// mode, what it currently is). `None` means the field is absent;
    /// `Some("")` is an explicit empty comment — the two are distinct
    /// (CLI_DOGFOOD #7), and serialize as `null` vs `""`.
    pub comment: Option<String>,
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

/// JSON wire shape for a comment read/edit outcome. The locator key is
/// `sym` for bindings and `module` for modules (`#[serde(flatten)]` on a
/// `kind`-discriminated locator), matching the per-binding / per-module
/// verb namespaces.
#[derive(Debug, Serialize)]
struct CommentOutcomeJson<'a> {
    #[serde(flatten)]
    locator: Locator<'a>,
    /// `null` when the comment field is unset, distinct from `""`.
    comment: Option<&'a str>,
    action: &'a str,
}

#[derive(Debug, Serialize)]
enum Locator<'a> {
    #[serde(rename = "sym")]
    Binding(&'a str),
    #[serde(rename = "module")]
    Module(&'a str),
}

fn print_outcome(outcome: &CommentOutcome, format: peel::OutputFormat) {
    match format {
        peel::OutputFormat::Text => {
            // Text keeps the bare-line shape: an unset comment prints an
            // empty line. The null/"" distinction lives in JSON.
            println!("{}", outcome.comment.as_deref().unwrap_or(""));
        }
        peel::OutputFormat::Json | peel::OutputFormat::Ndjson => {
            let locator = match outcome.kind {
                OutcomeKind::Binding => Locator::Binding(&outcome.locator),
                OutcomeKind::Module => Locator::Module(&outcome.locator),
            };
            let payload = CommentOutcomeJson {
                locator,
                comment: outcome.comment.as_deref(),
                action: outcome.action,
            };
            println!(
                "{}",
                serde_json::to_string(&payload).expect("comment outcome serializes")
            );
        }
    }
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
    // Same `<sym>` resolution (and refusal shape on zero/ambiguous
    // matches) as `bindings assign` / `bindings rename`.
    let hit = resolve_unambiguous(modules_root, sym)?;
    let (file, member_index) = (hit.file, hit.member_index);
    let mut doc = read_yaml(&file)?;
    let current: Option<String> = current_member_comment(&doc, member_index)?;

    let (new_action, new_comment, dirty) = match mode {
        CommentMode::Read => ("read", current.clone(), false),
        CommentMode::Set(text) => {
            set_member_comment(&mut doc, member_index, Some(text.clone()))?;
            ("set", Some(text), true)
        }
        CommentMode::Edit => {
            let effective_current = current.clone().unwrap_or_default();
            let edited = run_editor(&effective_current)?;
            if edited == effective_current {
                ("unchanged", current.clone(), false)
            } else if edited.is_empty() {
                set_member_comment(&mut doc, member_index, None)?;
                ("cleared", None, true)
            } else {
                set_member_comment(&mut doc, member_index, Some(edited.clone()))?;
                ("set", Some(edited), true)
            }
        }
        CommentMode::Clear => {
            if current.is_some() {
                set_member_comment(&mut doc, member_index, None)?;
                ("cleared", None, true)
            } else {
                ("unchanged", None, false)
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
    let format = peel::OutputFormat::resolve(args.format);
    print_outcome(&outcome, format);
    Ok(())
}

pub fn apply_module_comment(
    modules_root: &Path,
    module: &str,
    mode: CommentMode,
    dry_run: bool,
) -> Result<CommentOutcome> {
    let file = module_path_to_yaml(modules_root, module)?;
    if !file.exists() {
        bail!("module YAML not found: {}", file.display());
    }
    let mut doc = read_yaml(&file)?;
    let current: Option<String> = current_module_comment(&doc);

    let (new_action, new_comment, dirty) = match mode {
        CommentMode::Read => ("read", current.clone(), false),
        CommentMode::Set(text) => {
            set_module_comment(&mut doc, Some(text.clone()))?;
            ("set", Some(text), true)
        }
        CommentMode::Edit => {
            let effective_current = current.clone().unwrap_or_default();
            let edited = run_editor(&effective_current)?;
            if edited == effective_current {
                ("unchanged", current.clone(), false)
            } else if edited.is_empty() {
                set_module_comment(&mut doc, None)?;
                ("cleared", None, true)
            } else {
                set_module_comment(&mut doc, Some(edited.clone()))?;
                ("set", Some(edited), true)
            }
        }
        CommentMode::Clear => {
            if current.is_some() {
                set_module_comment(&mut doc, None)?;
                ("cleared", None, true)
            } else {
                ("unchanged", None, false)
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

/// Resolve a module-path argument to its on-disk YAML through the
/// same [`ModulePath::parse`] canonicalization (lowercasing) the spec
/// pipeline applies, so `UI/Widgets` and `ui/widgets` address the
/// same file.
fn module_path_to_yaml(modules_root: &Path, module: &str) -> Result<PathBuf> {
    let raw = module.strip_suffix(".yaml").unwrap_or(module);
    let canonical =
        ModulePath::parse(raw, "").map_err(|err| anyhow!("invalid module path: {err}"))?;
    Ok(modules_root.join(format!("{canonical}.yaml")))
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
        assert_eq!(set.comment.as_deref(), Some("acquires plugin settings"));

        let body = read(root, "ui/widgets.yaml");
        let doc: Value = serde_yaml::from_str(&body).unwrap();
        assert_eq!(
            doc["members"][0]["comment"].as_str(),
            Some("acquires plugin settings")
        );

        let got = apply_binding_comment(root, "XOe", CommentMode::Read, false).unwrap();
        assert_eq!(got.comment.as_deref(), Some("acquires plugin settings"));
        assert_eq!(got.action, "read");

        let cleared = apply_binding_comment(root, "XOe", CommentMode::Clear, false).unwrap();
        assert_eq!(cleared.action, "cleared");
        assert_eq!(cleared.comment, None);
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
        assert_eq!(out.comment.as_deref(), Some("will not stick"));
        assert_eq!(read(root, "m.yaml"), original);
    }

    #[test]
    fn read_distinguishes_unset_from_explicit_empty_comment() {
        // CLI_DOGFOOD #7: an absent `comment:` reads as `None` (JSON
        // `null`); an explicit `comment: ""` reads as `Some("")`. The
        // two must not collapse to the same value.
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "m.yaml",
            "members:\n  - selector: { binding: { name: Unset } }\n  - comment: \"\"\n    selector: { binding: { name: Empty } }\n",
        );
        let unset = apply_binding_comment(root, "Unset", CommentMode::Read, false).unwrap();
        assert_eq!(unset.comment, None);
        let empty = apply_binding_comment(root, "Empty", CommentMode::Read, false).unwrap();
        assert_eq!(empty.comment.as_deref(), Some(""));
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
        assert_eq!(got.comment.as_deref(), Some("plugin glue"));

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
