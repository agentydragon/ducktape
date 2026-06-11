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
//! This stage doesn't *fix* the underlying generation bug — the
//! emit-side paths that produced the duplicate still need patching
//! (the immediate one is `auto_grown_residual_exports` in
//! `lowering/exports.rs`, which compares local binding names to
//! `pre_existing_entry_exports` but never checks whether the public
//! name it's about to grow would collide with an existing alias's
//! public name). It just makes the failure mode unmissable.

use std::collections::BTreeMap;

use anyhow::{Result, bail};
use swc_common::Spanned;
use swc_ecma_ast::*;

use artifact::ChunkBundle;
use js_ast::SourceLineIndex;

pub fn validate_emitted_exports(artifact: &ChunkBundle) -> Result<()> {
    let mut findings: Vec<FileFinding> = Vec::new();
    for chunk in &artifact.chunks {
        let chunk_name = artifact.chunk_table.name(chunk.chunk_id).to_string();
        for file in &chunk.js.files {
            let Some(ast) = file.ast() else {
                continue;
            };
            let lines = ast.line_index();
            let duplicates = duplicates_in_module(&ast.module, &lines);
            if !duplicates.is_empty() {
                findings.push(FileFinding {
                    chunk: chunk_name.clone(),
                    file: file.path.clone(),
                    duplicates,
                });
            }
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
            for name in exported_decl_names(&d.decl) {
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
                            .map(module_export_atom)
                            .unwrap_or_else(|| module_export_atom(&n.orig));
                        sites.entry(public).or_default().push(ExportSite {
                            line,
                            shape: "named",
                        });
                    }
                    ExportSpecifier::Namespace(ns) => {
                        sites
                            .entry(module_export_atom(&ns.name))
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

fn exported_decl_names(decl: &Decl) -> Vec<String> {
    let mut out = Vec::new();
    match decl {
        Decl::Fn(f) => out.push(f.ident.sym.to_string()),
        Decl::Class(c) => out.push(c.ident.sym.to_string()),
        Decl::Var(v) => {
            for d in &v.decls {
                collect_pat_names(&d.name, &mut out);
            }
        }
        _ => {}
    }
    out
}

fn collect_pat_names(pat: &Pat, out: &mut Vec<String>) {
    match pat {
        Pat::Ident(b) => out.push(b.id.sym.to_string()),
        Pat::Array(arr) => {
            for elem in arr.elems.iter().flatten() {
                collect_pat_names(elem, out);
            }
        }
        Pat::Object(obj) => {
            for prop in &obj.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => collect_pat_names(&kv.value, out),
                    ObjectPatProp::Assign(a) => out.push(a.key.sym.to_string()),
                    ObjectPatProp::Rest(r) => collect_pat_names(&r.arg, out),
                }
            }
        }
        Pat::Rest(r) => collect_pat_names(&r.arg, out),
        Pat::Assign(a) => collect_pat_names(&a.left, out),
        _ => {}
    }
}

fn module_export_atom(name: &ModuleExportName) -> String {
    name.atom().to_string()
}

#[cfg(test)]
mod tests {
    use artifact::{
        ChunkAnalysis, ChunkArtifact, ChunkBundle, ChunkMetadata, ChunkTable, FileMetadata,
        FileRole, JsChunk, JsFile, JsFileBody,
    };
    use js_ast::parse_js_module;

    use super::*;

    fn bundle_with_file(chunk_name: &str, file_name: &str, source: &str) -> ChunkBundle {
        let mut bundle = ChunkBundle {
            chunks: Vec::new(),
            chunk_table: ChunkTable::default(),
        };
        insert_file(&mut bundle, chunk_name, file_name, source, FileRole::Entry);
        bundle
    }

    fn insert_file(
        bundle: &mut ChunkBundle,
        chunk_name: &str,
        file_name: &str,
        source: &str,
        role: FileRole,
    ) {
        let parsed = parse_js_module(&format!("{chunk_name}/{file_name}"), source).expect("parse");
        let chunk_id = bundle.chunk_table.intern(chunk_name.to_string());
        let file = JsFile {
            path: file_name.to_string(),
            body: JsFileBody::Ast(parsed),
            header_lines: Vec::new(),
            metadata: FileMetadata {
                chunk_id: chunk_name.to_string(),
                chunk_file: file_name.to_string(),
                role,
                source_path: format!("{chunk_name}.js"),
                generated_by_selected_module_lowering: false,
            },
        };
        if let Some(existing) = bundle.chunks.iter_mut().find(|c| c.chunk_id == chunk_id) {
            existing.js.files.push(file);
            return;
        }
        bundle.chunks.push(ChunkArtifact {
            chunk_id,
            js: JsChunk {
                entry_file: file_name.to_string(),
                files: vec![file],
                metadata: ChunkMetadata {
                    source_path: Some(format!("{chunk_name}.js")),
                },
            },
            analysis: ChunkAnalysis {
                chunk_id: chunk_name.to_string(),
                source_path: format!("{chunk_name}.js"),
                parser: Default::default(),
                entry_file: file_name.to_string(),
                counts: Default::default(),
                files: Vec::new(),
                imports: Vec::new(),
                export_aliases: Vec::new(),
                unresolved_exports: Vec::new(),
                kept_top_level_declarations: Vec::new(),
            },
        });
    }

    #[test]
    fn passes_on_clean_module() {
        js_ast::with_swc_globals(|| {
            let bundle = bundle_with_file(
                "ok",
                "entry.js",
                "const a = 1;\nconst b = 2;\nexport { a, b };\n",
            );
            validate_emitted_exports(&bundle).expect("clean module passes");
        });
    }

    #[test]
    fn flags_named_alias_colliding_with_local_export() {
        js_ast::with_swc_globals(|| {
            // The Chromium-silent failure mode the tana smoke hit:
            // one `export {...}` block ships `BackgroundPattern as av`,
            // a separate block ships the local `av` directly.
            let source = "\
const BackgroundPattern = () => null;\n\
function av() {}\n\
export { BackgroundPattern as av };\n\
export { av };\n";
            let bundle = bundle_with_file("chunk", "entry.js", source);
            let err =
                validate_emitted_exports(&bundle).expect_err("duplicate av should be rejected");
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
            let bundle = bundle_with_file("c", "f.js", source);
            let err = validate_emitted_exports(&bundle).expect_err("duplicate x");
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
            let bundle = bundle_with_file("c", "f.js", source);
            let err = validate_emitted_exports(&bundle).expect_err("duplicate default");
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
            let bundle = bundle_with_file("c", "f.js", source);
            validate_emitted_exports(&bundle).expect("star re-export does not duplicate");
        });
    }

    #[test]
    fn checks_every_file_in_chunk() {
        js_ast::with_swc_globals(|| {
            // Two files in the same chunk; only one has duplicates.
            let mut bundle = bundle_with_file("c", "good.js", "export const a = 1;\n");
            insert_file(
                &mut bundle,
                "c",
                "bad.js",
                "export const z = 1;\nconst zz = 2;\nexport { zz as z };\n",
                FileRole::Module,
            );
            let err = validate_emitted_exports(&bundle).expect_err("bad file flagged");
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
            let mut bundle = bundle_with_file("c", "ok.js", "export const a = 1;\n");
            bundle.chunks[0].js.files.push(JsFile {
                path: "raw.js".to_string(),
                body: JsFileBody::Source(
                    "export const x = 1;\nconst y = 2;\nexport { y as x };\n".to_string(),
                ),
                header_lines: Vec::new(),
                metadata: FileMetadata {
                    chunk_id: "c".to_string(),
                    chunk_file: "raw.js".to_string(),
                    role: FileRole::Module,
                    source_path: "c.js".to_string(),
                    generated_by_selected_module_lowering: false,
                },
            });
            validate_emitted_exports(&bundle).expect("source-only file skipped");
        });
    }
}
