//! Catch `SyntaxError: Duplicate export of '<name>'` at pipeline time.
//!
//! ES modules reject any file that declares the same public export name
//! twice. Browsers and Node both bail at module-link time (before any
//! body evaluates) — but the failure surfaces differently depending on
//! the runtime:
//!
//! - Node: `SyntaxError: Duplicate export of '<name>'`, line/column.
//! - Chromium: a synthetic `pageerror` event whose `Error` carries an
//!   empty stack and message. No console output, no failed network
//!   requests, and the module's static `import` graph never starts
//!   fetching. The page silently hangs blank.
//!
//! The second failure mode is what motivated this stage. A pipeline
//! regen that flips two unrelated emit paths into both contributing
//! the same public name (e.g. `BackgroundPattern as av` from chunk
//! renames + `av` from the auto-grown residual export block) produces
//! a chunk that link-fails inside the browser smoke without leaving a
//! useful trace. Failing here turns that into a build-time error that
//! names the file, the colliding public name, and the line of each
//! offending `export …` statement.
//!
//! The check is purely a static read over the in-memory artifact —
//! every JS file with an AST body is walked once and each export
//! statement's public name(s) are recorded. Files stored as raw
//! `JsFileBody::Source` (no AST) are skipped: those didn't go through
//! materialization or strip, so this pass has no leverage on them and
//! the upstream chunk shape itself is the upstream's contract.
//!
//! This stage doesn't *fix* an underlying generation bug — it makes
//! the failure mode unmissable when an emit-side path regresses.
//! (The original offender, `auto_grown_residual_exports` in
//! `lowering/exports.rs`, now checks candidates against
//! `pre_existing_public_export_names` and suffix-mints a
//! non-colliding name.)

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, bail};
use binding_targets::{declaration_name_strings, module_export_name};
use swc_common::Spanned;
use swc_ecma_ast::*;

use artifact::{ChunkBundle, ChunkId, EmissionFiles};
use js_ast::{ParsedJsModule, SourceLineIndex};

/// `excluded_chunk_ids`: chunks excluded from the emission set (fully
/// vendor-swapped) — never emitted, so their export surfaces are not
/// this check's business.
pub fn validate_emitted_exports(
    files: &EmissionFiles,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<()> {
    validate_bundle_exports(files.files(), excluded_chunk_ids)
}

fn validate_bundle_exports(
    artifact: &ChunkBundle,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<()> {
    validate_modules(
        artifact
            .chunks
            .iter()
            .filter(|chunk| !excluded_chunk_ids.contains(&chunk.chunk_id))
            .flat_map(|chunk| {
                let name = artifact.chunk_table.name(chunk.chunk_id);
                chunk
                    .js
                    .files
                    .iter()
                    .map(move |file| (name, file.path.as_str(), file.ast()))
            }),
    )
}

/// Validate parsed output files without depending on chunk analysis reports.
fn validate_modules<'a>(
    files: impl IntoIterator<Item = (&'a str, &'a str, Option<&'a ParsedJsModule>)>,
) -> Result<()> {
    let mut findings: Vec<FileFinding> = Vec::new();
    for (chunk, file, ast) in files {
        let Some(ast) = ast else { continue };
        let duplicates = duplicates_in_module(&ast.module, &ast.line_index());
        if !duplicates.is_empty() {
            findings.push(FileFinding {
                chunk: chunk.to_string(),
                file: file.to_string(),
                duplicates,
            });
        }
    }
    if findings.is_empty() {
        return Ok(());
    }

    let mut msg = String::from(
        "validate_emitted_exports: emitted JS contains duplicate `export` names — \
         ES module link error. Browsers and Node refuse modules with two \
         exports sharing one public name (`SyntaxError: Duplicate export of '<name>'`). \
         In Chromium this surfaces as a silent empty `pageerror` with no further \
         child-chunk loads — the page renders blank with no useful console output, \
         which is hard to debug downstream.\n",
    );
    for finding in &findings {
        msg.push_str(&format!(
            "\n  chunk {} file {}:\n",
            finding.chunk, finding.file,
        ));
        for dup in &finding.duplicates {
            msg.push_str(&format!(
                "    `{}` exported {}× at {}\n",
                dup.name,
                dup.sites.len(),
                render_sites(&dup.sites),
            ));
        }
    }
    bail!(msg);
}

#[derive(Debug, Clone)]
struct FileFinding {
    chunk: String,
    file: String,
    duplicates: Vec<DuplicateExport>,
}

#[derive(Debug, Clone)]
struct DuplicateExport {
    name: String,
    sites: Vec<ExportSite>,
}

#[derive(Debug, Clone)]
struct ExportSite {
    line: Option<usize>,
    /// Short tag describing which AST shape contributed the export
    /// (`decl`, `named`, `default`, `namespace`). Helps the reader
    /// distinguish e.g. an `export function av()` from a bare
    /// `export { av }`.
    shape: &'static str,
}

fn render_sites(sites: &[ExportSite]) -> String {
    sites
        .iter()
        .map(|s| match s.line {
            Some(line) => format!("L{line} ({})", s.shape),
            None => format!("L? ({})", s.shape),
        })
        .collect::<Vec<_>>()
        .join(", ")
}

fn duplicates_in_module(module: &Module, lines: &SourceLineIndex) -> Vec<DuplicateExport> {
    let mut sites: BTreeMap<String, Vec<ExportSite>> = BTreeMap::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(decl) = item else {
            continue;
        };
        record_decl(decl, lines, &mut sites);
    }
    sites
        .into_iter()
        .filter(|(_, s)| s.len() > 1)
        .map(|(name, sites)| DuplicateExport { name, sites })
        .collect()
}

fn record_decl(
    decl: &ModuleDecl,
    lines: &SourceLineIndex,
    sites: &mut BTreeMap<String, Vec<ExportSite>>,
) {
    match decl {
        ModuleDecl::ExportDefaultDecl(d) => {
            sites
                .entry("default".to_string())
                .or_default()
                .push(ExportSite {
                    line: lines.line_for_span(d.span()),
                    shape: "default",
                });
        }
        ModuleDecl::ExportDefaultExpr(d) => {
            sites
                .entry("default".to_string())
                .or_default()
                .push(ExportSite {
                    line: lines.line_for_span(d.span()),
                    shape: "default",
                });
        }
        ModuleDecl::ExportDecl(d) => {
            let line = lines.line_for_span(d.span());
            for name in declaration_name_strings(&d.decl) {
                sites.entry(name).or_default().push(ExportSite {
                    line,
                    shape: "decl",
                });
            }
        }
        ModuleDecl::ExportNamed(named) => {
            let line = lines.line_for_span(named.span());
            for spec in &named.specifiers {
                match spec {
                    ExportSpecifier::Named(n) => {
                        let public = n
                            .exported
                            .as_ref()
                            .map(module_export_name)
                            .unwrap_or_else(|| module_export_name(&n.orig));
                        sites.entry(public).or_default().push(ExportSite {
                            line,
                            shape: "named",
                        });
                    }
                    ExportSpecifier::Namespace(ns) => {
                        sites
                            .entry(module_export_name(&ns.name))
                            .or_default()
                            .push(ExportSite {
                                line,
                                shape: "namespace",
                            });
                    }
                    ExportSpecifier::Default(d) => {
                        sites
                            .entry(d.exported.sym.to_string())
                            .or_default()
                            .push(ExportSite {
                                line,
                                shape: "default",
                            });
                    }
                }
            }
        }
        // `export * from "..."` re-exports a star — the concrete names
        // come from the source module at link time. Spec rejection is
        // about literal-name collisions, so star re-exports can't
        // create a duplicate on their own and we skip them here.
        // Imports and TS-only declarations don't contribute exports.
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use js_ast::parse_js_module;

    use super::*;

    fn validate_sources(files: &[(&str, &str, Option<&str>)]) -> Result<()> {
        let parsed: Vec<_> = files
            .iter()
            .map(|(chunk, path, source)| {
                (
                    *chunk,
                    *path,
                    source.map(|source| parse_js_module(path, source).unwrap()),
                )
            })
            .collect();
        validate_modules(
            parsed
                .iter()
                .map(|(chunk, path, ast)| (*chunk, *path, ast.as_ref())),
        )
    }

    #[test]
    fn flags_named_alias_colliding_with_local_export() {
        js_ast::with_swc_globals(|| {
            // The Chromium-silent failure mode the the upstream smoke hit:
            // one `export {...}` block ships `BackgroundPattern as av`,
            // a separate block ships the local `av` directly.
            let source = "\
const BackgroundPattern = () => null;\n\
function av() {}\n\
export { BackgroundPattern as av };\n\
export { av };\n";
            let err = validate_sources(&[("chunk", "entry.js", Some(source))])
                .expect_err("duplicate av should be rejected");
            let msg = format!("{err}");
            assert!(msg.contains("`av` exported 2×"), "missing count: {msg}");
            assert!(msg.contains("entry.js"), "missing file: {msg}");
            assert!(msg.contains("chunk chunk"), "missing chunk: {msg}");
        });
    }

    #[test]
    fn flags_export_decl_vs_named_block_duplicate() {
        js_ast::with_swc_globals(|| {
            let source = "\
export const x = 1;\n\
const y = 2;\n\
export { y as x };\n";
            let err = validate_sources(&[("c", "f.js", Some(source))]).expect_err("duplicate x");
            let msg = format!("{err}");
            assert!(msg.contains("`x` exported 2×"), "{msg}");
            assert!(msg.contains("(decl)"), "decl shape missing: {msg}");
            assert!(msg.contains("(named)"), "named shape missing: {msg}");
        });
    }

    #[test]
    fn flags_two_default_exports() {
        js_ast::with_swc_globals(|| {
            let source = "\
export default 1;\n\
const fallback = 2;\n\
export { fallback as default };\n";
            let err =
                validate_sources(&[("c", "f.js", Some(source))]).expect_err("duplicate default");
            let msg = format!("{err}");
            assert!(msg.contains("`default` exported 2×"), "{msg}");
        });
    }

    #[test]
    fn star_reexport_does_not_count() {
        js_ast::with_swc_globals(|| {
            // `export *` re-exports whatever the source module exports;
            // the local `export { foo }` is the only literal-name export
            // in this file and link-time conflicts from `*` collisions
            // are diagnosed by the source module's check.
            let source = "\
const foo = 1;\n\
export { foo };\n\
export * from \"./sibling.js\";\n";
            validate_sources(&[("c", "f.js", Some(source))])
                .expect("star re-export does not duplicate");
        });
    }

    #[test]
    fn checks_every_file_in_chunk() {
        js_ast::with_swc_globals(|| {
            // Two files in the same chunk; only one has duplicates.
            let err = validate_sources(&[
                ("c", "good.js", Some("export const a = 1;\n")),
                (
                    "c",
                    "bad.js",
                    Some("export const z = 1;\nconst zz = 2;\nexport { zz as z };\n"),
                ),
            ])
            .expect_err("bad file flagged");
            let msg = format!("{err}");
            assert!(msg.contains("bad.js"), "{msg}");
            assert!(!msg.contains("good.js"), "good.js should not appear: {msg}");
        });
    }

    #[test]
    fn source_only_files_are_skipped() {
        js_ast::with_swc_globals(|| {
            // Files stored as raw source (no AST) are skipped — the pass
            // walks the in-memory AST and has nothing to inspect for raw
            // bodies. This is intentional: such files came from upstream
            // verbatim, not from a pipeline emit path.
            validate_sources(&[
                ("c", "ok.js", Some("export const a = 1;\n")),
                ("c", "raw.js", None),
            ])
            .expect("source-only file skipped");
        });
    }
}
