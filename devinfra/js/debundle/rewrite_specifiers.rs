use anyhow::{Context, Result};
use rayon::prelude::*;
use std::path::Path;
use swc_common::{DUMMY_SP, GLOBALS, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, ChunkTable, ImportReferenceKind, JsFile, JsFileAstParts,
    get_chunk_entry_path, join_module_path, module_path_dirname, relative_module_path,
};
use js_ast::{ParsedJsModule, set_str_value, str_value};

#[derive(Debug, Clone)]
pub struct RewriteChunkEntrySpecifiersManifest {
    pub counts: RewriteCounts,
}

pub struct RewriteChunkEntrySpecifiersResult {
    pub artifact: ChunkBundle,
    pub manifest: RewriteChunkEntrySpecifiersManifest,
}

#[derive(Debug, Clone)]
pub struct RewriteCounts {
    pub traversed_files: usize,
    pub files: usize,
    pub rewrites: usize,
}

pub fn rewrite_chunk_entry_specifiers(
    mut artifact: ChunkBundle,
    references: &ArtifactIndexes,
) -> Result<RewriteChunkEntrySpecifiersResult> {
    let mut jobs = Vec::new();

    let chunk_table = artifact.chunk_table.clone();
    for (chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
        let chunk_id = chunk_artifact.chunk_id;
        let chunk_name = chunk_table.name(chunk_id).to_string();
        let file_paths = chunk_artifact
            .js
            .file_paths()
            .map(|s| s.to_string())
            .collect::<Vec<_>>();
        for file_path in file_paths {
            let Some(file) = chunk_artifact.js.get_file(&file_path) else {
                continue;
            };
            if !file.is_ast() {
                continue;
            }
            let file = chunk_artifact
                .js
                .remove_file(&file_path)
                .with_context(|| format!("missing artifact file {chunk_name}/{file_path}"))?;
            let (parts, ast) = file
                .into_ast_parts()
                .with_context(|| format!("artifact file has no AST: {chunk_name}/{file_path}"))?;
            jobs.push(RewriteFileJob {
                chunk_index,
                chunk_id,
                file_path,
                parts,
                ast,
            });
        }
    }
    let traversed_files = jobs.len();
    // Rayon workers don't inherit `GLOBALS`; re-set per worker so any
    // `Mark::new()` / `Id` use stays in the caller's arena.
    let results = GLOBALS.with(|globals| {
        jobs.into_par_iter()
            .map(|job| GLOBALS.set(globals, || rewrite_file(job, references, &chunk_table)))
            .collect::<Vec<_>>()
    });

    let mut rewritten_files = 0usize;
    let mut rewritten_specifiers = 0usize;
    for result in results {
        let chunk = &mut artifact.chunks[result.chunk_index].js;
        chunk.insert_file(JsFile::from_ast_parts(result.parts, result.ast));
        if result.rewrites > 0 {
            rewritten_files += 1;
            rewritten_specifiers += result.rewrites;
        }
    }

    Ok(RewriteChunkEntrySpecifiersResult {
        artifact,
        manifest: RewriteChunkEntrySpecifiersManifest {
            counts: RewriteCounts {
                traversed_files,
                files: rewritten_files,
                rewrites: rewritten_specifiers,
            },
        },
    })
}

struct RewriteFileJob {
    chunk_index: usize,
    chunk_id: ChunkId,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

struct RewriteFileResult {
    chunk_index: usize,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    rewrites: usize,
}

fn rewrite_file(
    mut job: RewriteFileJob,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
) -> RewriteFileResult {
    let mut rewriter = RuntimeSourceRewriter {
        references,
        chunk_table,
        caller_chunk_id: job.chunk_id,
        caller_file: job.file_path.clone(),
        rewrites: 0,
    };
    job.ast.module.visit_mut_with(&mut rewriter);
    let rewrites = rewriter.rewrites;
    RewriteFileResult {
        chunk_index: job.chunk_index,
        parts: job.parts,
        ast: job.ast,
        rewrites,
    }
}

pub fn runtime_js_href(
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
        .join(chunk_name.split('/').collect::<std::path::PathBuf>())
        .join(entry_file.split('/').collect::<std::path::PathBuf>());
    Ok(artifact::relative_module_specifier(out_dir, &entry_path))
}

/// Returns true when the parsed module contains any specifier that
/// `rewrite_chunk_entry_specifiers` could rewrite: any `import`,
/// `export … from`, `import(...)`, or `new Worker(...)` /
/// `new SharedWorker(...)` whose source is a relative path (starts
/// with `.` or `/`).
///
/// `prepare_js_chunks` consults this to decide whether to drop the
/// AST in favor of the original source: dropping is safe iff this
/// returns false. Both this function and `RuntimeSourceRewriter` use
/// the same `dynamic_import_str` / `worker_new_str` guards, so they
/// stay in sync automatically.
pub fn ast_has_rewritable_specifier(parsed: &ParsedJsModule) -> bool {
    let mut detector = RewritableSpecifierDetector { found: false };
    parsed.module.visit_with(&mut detector);
    detector.found
}

fn is_relative_specifier(source: &str) -> bool {
    source.starts_with('.') || source.starts_with('/')
}

fn dynamic_import_str(node: &CallExpr) -> Option<&Str> {
    if matches!(node.callee, Callee::Import(_))
        && let Some(first) = node.args.first()
        && first.spread.is_none()
        && let Expr::Lit(Lit::Str(s)) = &*first.expr
    {
        Some(s)
    } else {
        None
    }
}

fn dynamic_import_str_mut(node: &mut CallExpr) -> Option<&mut Str> {
    if matches!(node.callee, Callee::Import(_))
        && let Some(first) = node.args.first_mut()
        && first.spread.is_none()
        && let Expr::Lit(Lit::Str(s)) = &mut *first.expr
    {
        Some(s)
    } else {
        None
    }
}

fn worker_new_str(node: &NewExpr) -> Option<&Str> {
    if is_runtime_worker_constructor(&node.callee)
        && let Some(args) = &node.args
        && let Some(first) = args.first()
        && first.spread.is_none()
        && let Expr::Lit(Lit::Str(s)) = &*first.expr
    {
        Some(s)
    } else {
        None
    }
}

struct RewritableSpecifierDetector {
    found: bool,
}

impl Visit for RewritableSpecifierDetector {
    fn visit_import_decl(&mut self, node: &ImportDecl) {
        if !self.found && is_relative_specifier(&str_value(&node.src)) {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_named_export(&mut self, node: &NamedExport) {
        if !self.found
            && let Some(src) = &node.src
            && is_relative_specifier(&str_value(src))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_export_all(&mut self, node: &ExportAll) {
        if !self.found && is_relative_specifier(&str_value(&node.src)) {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if !self.found
            && dynamic_import_str(node).is_some_and(|s| is_relative_specifier(&str_value(s)))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if !self.found && worker_new_str(node).is_some_and(|s| is_relative_specifier(&str_value(s)))
        {
            self.found = true;
        }
        node.visit_children_with(self);
    }
}

struct RuntimeSourceRewriter<'a> {
    references: &'a ArtifactIndexes,
    chunk_table: &'a ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file: String,
    rewrites: usize,
}

impl RuntimeSourceRewriter<'_> {
    fn rewrite_source(&mut self, source: &str) -> Result<String> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return Ok(source.to_string());
        }
        let Some(resolved) = self.references.resolve_runtime_import_reference(
            source,
            self.caller_chunk_id,
            &self.caller_file,
            self.chunk_table,
        ) else {
            return Ok(source.to_string());
        };
        if resolved.kind == ImportReferenceKind::ArtifactPath {
            return Ok(source.to_string());
        }
        let caller_chunk_name = self.chunk_table.name(self.caller_chunk_id);
        let caller_dir = join_module_path(&[
            caller_chunk_name,
            module_path_dirname(&self.caller_file).as_str(),
        ]);
        let mut rewritten = relative_module_path(&caller_dir, &resolved.target_path);
        if !rewritten.starts_with('.') {
            rewritten = format!("./{rewritten}");
        }
        Ok(rewritten)
    }

    fn rewrite_str(&mut self, string: &mut Str) {
        let source = str_value(string);
        let Ok(rewritten) = self.rewrite_source(&source) else {
            return;
        };
        if rewritten != source {
            set_str_value(string, rewritten);
            self.rewrites += 1;
        }
    }
}

impl VisitMut for RuntimeSourceRewriter<'_> {
    fn visit_mut_import_decl(&mut self, node: &mut ImportDecl) {
        self.rewrite_str(&mut node.src);
        node.visit_mut_children_with(self);
    }

    fn visit_mut_named_export(&mut self, node: &mut NamedExport) {
        if let Some(src) = &mut node.src {
            self.rewrite_str(src);
        }
        node.visit_mut_children_with(self);
    }

    fn visit_mut_export_all(&mut self, node: &mut ExportAll) {
        self.rewrite_str(&mut node.src);
        node.visit_mut_children_with(self);
    }

    fn visit_mut_call_expr(&mut self, node: &mut CallExpr) {
        if let Some(string) = dynamic_import_str_mut(node) {
            self.rewrite_str(string);
        }
        node.visit_mut_children_with(self);
    }

    fn visit_mut_new_expr(&mut self, node: &mut NewExpr) {
        if let Some(source) = worker_new_str(node).map(str_value) {
            if let Ok(rewritten) = self.rewrite_source(&source)
                && rewritten != source
                && let Some(args) = &mut node.args
                && let Some(first) = args.first_mut()
            {
                first.expr = Box::new(new_url_expr(rewritten));
                self.rewrites += 1;
            }
        }
        node.visit_mut_children_with(self);
    }
}

fn is_runtime_worker_constructor(expr: &Expr) -> bool {
    matches!(expr, Expr::Ident(ident) if ident.sym == *"Worker" || ident.sym == *"SharedWorker")
}

fn new_url_expr(source: String) -> Expr {
    Expr::New(NewExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Box::new(Expr::Ident(Ident::new_no_ctxt("URL".into(), DUMMY_SP))),
        args: Some(vec![
            ExprOrSpread {
                spread: None,
                expr: Box::new(Expr::Lit(Lit::Str(Str {
                    span: DUMMY_SP,
                    value: source.into(),
                    raw: None,
                }))),
            },
            ExprOrSpread {
                spread: None,
                expr: Box::new(import_meta_url_expr()),
            },
        ]),
        type_args: None,
    })
}

fn import_meta_url_expr() -> Expr {
    Expr::Member(MemberExpr {
        span: DUMMY_SP,
        obj: Box::new(Expr::MetaProp(MetaPropExpr {
            span: DUMMY_SP,
            kind: MetaPropKind::ImportMeta,
        })),
        prop: MemberProp::Ident(IdentName::new("url".into(), DUMMY_SP)),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_url_rewrite_uses_import_meta_url_as_base() {
        let Expr::New(new_expr) = new_url_expr("../worker.js".to_string()) else {
            panic!("expected new URL expression");
        };
        let args = new_expr.args.expect("new URL args");
        assert_eq!(args.len(), 2);
        let Expr::Member(member) = &*args[1].expr else {
            panic!("expected import.meta.url member expression");
        };
        assert!(matches!(
            &*member.obj,
            Expr::MetaProp(MetaPropExpr {
                kind: MetaPropKind::ImportMeta,
                ..
            })
        ));
        let MemberProp::Ident(prop) = &member.prop else {
            panic!("expected ident member property");
        };
        assert_eq!(prop.sym.as_ref(), "url");
    }
}
