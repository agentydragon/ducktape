//! CLI binding and module comment workflows. Binding comments live in the
//! canonical `annotations.<readable-or-minified-name>.comment` field; module
//! comments live in `comment`. Read, literal replacement, `$EDITOR`, and clear
//! modes share one transition function. Changed YAML is reserialized; textual
//! layout and YAML comments are not an editing contract.

use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, anyhow, bail};
use clap::Args as ClapArgs;
use serde::Serialize;
use spec::{BindingAnnotation, LogicalModule, ModulePath};
use spec_modules::read_module_file;

use crate::binding::resolve_unambiguous;
use yaml_edit::write_yaml_atomic;

/// Args for `debundle bindings comment <sym> [...]`.
#[derive(Debug, ClapArgs)]
pub struct BindingCommentArgs {
    /// Binding identifier, in minified or readable form.
    sym: String,
    #[command(flatten)]
    comment: CommentArgs,
}

/// Args for `debundle modules comment <module> [...]`.
#[derive(Debug, ClapArgs)]
pub struct ModuleCommentArgs {
    /// Module path relative to `--modules`, e.g. `runtime/plugins`.
    module: String,
    #[command(flatten)]
    comment: CommentArgs,
}

/// Shared read/edit options; the binding or module locator precedes the text.
#[derive(Debug, ClapArgs)]
struct CommentArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    modules_root: PathBuf,

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
}

/// Read/edit operation shared by binding and module comment commands.
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

pub fn run_binding_comment_cmd(args: BindingCommentArgs) -> Result<()> {
    let mode = CommentMode::from_flags(args.comment.text, args.comment.edit, args.comment.clear)?;
    let outcome = apply_binding_comment(
        &args.comment.modules_root,
        &args.sym,
        mode,
        args.comment.dry_run,
    )?;
    let format = peel::OutputFormat::resolve(args.comment.format);
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
    let file = hit.file;
    let mut doc = read_module_file(&file)?;
    let name = hit.name.readable().unwrap_or_else(|| hit.name.minified());
    let current = doc.annotations.get(name).and_then(|a| a.comment.clone());
    let (action, comment, dirty) = edit_comment(current, mode)?;
    if dirty {
        let annotation = doc.annotations.entry(name.to_string()).or_default();
        annotation.comment = comment.clone();
        // Clearing the only annotation must not leave a phantom module keeper.
        if annotation == &BindingAnnotation::default() {
            doc.annotations.remove(name);
        }
    }
    let action = persist_comment(&file, &doc, action, dirty, dry_run)?;
    Ok(CommentOutcome {
        locator: sym.to_string(),
        kind: OutcomeKind::Binding,
        comment,
        action,
        path: file,
    })
}

// ---------------------------------------------------------------------
// Module comment
// ---------------------------------------------------------------------

pub fn run_module_comment_cmd(args: ModuleCommentArgs) -> Result<()> {
    let mode = CommentMode::from_flags(args.comment.text, args.comment.edit, args.comment.clear)?;
    let outcome = apply_module_comment(
        &args.comment.modules_root,
        &args.module,
        mode,
        args.comment.dry_run,
    )?;
    let format = peel::OutputFormat::resolve(args.comment.format);
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
    let mut doc = read_module_file(&file)?;
    let (action, comment, dirty) = edit_comment(doc.comment.clone(), mode)?;
    doc.comment = comment.clone();
    let action = persist_comment(&file, &doc, action, dirty, dry_run)?;
    Ok(CommentOutcome {
        locator: module.to_string(),
        kind: OutcomeKind::Module,
        comment,
        action,
        path: file,
    })
}

fn edit_comment(
    current: Option<String>,
    mode: CommentMode,
) -> Result<(&'static str, Option<String>, bool)> {
    let replacement = match mode {
        CommentMode::Read => return Ok(("read", current, false)),
        CommentMode::Set(text) => Some(text),
        CommentMode::Clear => None,
        CommentMode::Edit => {
            let text = current.as_deref().unwrap_or("");
            let edited = run_editor(text)?;
            if edited == text {
                return Ok(("unchanged", current, false));
            }
            (!edited.is_empty()).then_some(edited)
        }
    };
    if replacement == current {
        return Ok(("unchanged", current, false));
    }
    let action = if replacement.is_some() {
        "set"
    } else {
        "cleared"
    };
    Ok((action, replacement, true))
}

fn persist_comment(
    file: &Path,
    doc: &LogicalModule,
    action: &'static str,
    dirty: bool,
    dry_run: bool,
) -> Result<&'static str> {
    // edit_comment already compared the original and replacement metadata.
    if dirty && !dry_run {
        write_yaml_atomic(file, &serde_yaml::to_value(doc)?)?;
    }
    Ok(if dirty && dry_run { "dry-run" } else { action })
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
