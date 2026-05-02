use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};
use serde::Serialize;
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use artifact::{
    JsFile, JsPipelineArtifact, get_chunk_entry_path, posix_join, posix_relative,
    resolve_artifact_import_reference, resolve_artifact_source_import_reference,
};
use js_ast::{set_str_value, str_value};

#[derive(Debug, Clone, Serialize)]
pub struct RewriteChunkEntrySpecifiersManifest {
    pub kind: &'static str,
    pub counts: RewriteCounts,
}

#[derive(Debug, Clone, Serialize)]
pub struct RewriteCounts {
    #[serde(rename = "traversedFiles")]
    pub traversed_files: usize,
    pub files: usize,
    pub rewrites: usize,
}

pub fn rewrite_chunk_entry_specifiers(
    artifact: &mut JsPipelineArtifact,
) -> Result<RewriteChunkEntrySpecifiersManifest> {
    let mut rewritten_files = 0usize;
    let mut rewritten_specifiers = 0usize;
    let mut traversed_files = 0usize;
    let chunk_ids = artifact.list_chunk_ids();

    for chunk_id in chunk_ids {
        let file_paths = artifact
            .chunks
            .get(&chunk_id)
            .map(|chunk| chunk.files.keys().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        for file_path in file_paths {
            if !should_rewrite_file(
                artifact
                    .chunks
                    .get(&chunk_id)
                    .and_then(|chunk| chunk.files.get(&file_path))
                    .context("missing artifact file while checking rewrite eligibility")?,
            ) {
                continue;
            }
            traversed_files += 1;
            let started = Instant::now();
            let mut ast = {
                let chunk = artifact
                    .chunks
                    .get_mut(&chunk_id)
                    .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
                let file = chunk
                    .files
                    .get_mut(&file_path)
                    .with_context(|| format!("missing artifact file {chunk_id}/{file_path}"))?;
                file.ast
                    .take()
                    .with_context(|| format!("artifact file has no AST: {chunk_id}/{file_path}"))?
            };
            let mut rewriter = RuntimeSourceRewriter {
                artifact,
                caller_chunk_id: chunk_id.clone(),
                caller_file: file_path.clone(),
                rewrites: 0,
            };
            ast.module.visit_mut_with(&mut rewriter);
            let file_rewrites = rewriter.rewrites;
            drop(rewriter);
            {
                let chunk = artifact
                    .chunks
                    .get_mut(&chunk_id)
                    .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
                let file = chunk
                    .files
                    .get_mut(&file_path)
                    .with_context(|| format!("missing artifact file {chunk_id}/{file_path}"))?;
                file.ast = Some(ast);
            }
            let _duration_ms = started.elapsed().as_secs_f64() * 1000.0;
            if file_rewrites > 0 {
                rewritten_files += 1;
                rewritten_specifiers += file_rewrites;
            }
        }
    }

    Ok(RewriteChunkEntrySpecifiersManifest {
        kind: "js.rewrite_chunk_entry_specifiers_manifest",
        counts: RewriteCounts {
            traversed_files,
            files: rewritten_files,
            rewrites: rewritten_specifiers,
        },
    })
}

pub fn runtime_js_href(
    artifact: &JsPipelineArtifact,
    js_path: &str,
    out_dir: &Path,
    runtime_root: &Path,
) -> Result<String> {
    let chunk_id = js_path
        .strip_suffix(".js")
        .with_context(|| format!("Expected a .js path: {js_path}"))?;
    let entry_file = get_chunk_entry_path(artifact, chunk_id)
        .with_context(|| format!("Missing chunk entry file for {chunk_id}"))?;
    let entry_path = runtime_root
        .join(chunk_id.split('/').collect::<std::path::PathBuf>())
        .join(entry_file.split('/').collect::<std::path::PathBuf>());
    Ok(artifact::relative_module_specifier(out_dir, &entry_path))
}

fn should_rewrite_file(file: &JsFile) -> bool {
    if file.ast.is_none() {
        return false;
    }
    if file.metadata.role.as_deref() == Some("module")
        && file.metadata.generated_stage.as_deref() == Some("selected_module_lowering")
    {
        return false;
    }
    true
}

struct RuntimeSourceRewriter<'a> {
    artifact: &'a JsPipelineArtifact,
    caller_chunk_id: String,
    caller_file: String,
    rewrites: usize,
}

impl RuntimeSourceRewriter<'_> {
    fn rewrite_source(&mut self, source: &str) -> Result<String> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return Ok(source.to_string());
        }
        if resolve_artifact_import_reference(
            self.artifact,
            source,
            &self.caller_chunk_id,
            &self.caller_file,
        )
        .is_some()
        {
            return Ok(source.to_string());
        }
        let Some((target_chunk_id, target_file, _path)) = resolve_artifact_source_import_reference(
            self.artifact,
            source,
            &self.caller_chunk_id,
            &self.caller_file,
        )?
        else {
            return Ok(source.to_string());
        };
        let caller_dir = posix_join(&[
            self.caller_chunk_id.as_str(),
            posix_dirname(&self.caller_file).as_str(),
        ]);
        let target_path = posix_join(&[target_chunk_id.as_str(), target_file.as_str()]);
        let mut rewritten = posix_relative(&caller_dir, &target_path);
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
        if matches!(node.callee, Callee::Import(_))
            && let Some(first) = node.args.first_mut()
            && first.spread.is_none()
            && let Expr::Lit(Lit::Str(string)) = &mut *first.expr
        {
            self.rewrite_str(string);
        }
        node.visit_mut_children_with(self);
    }

    fn visit_mut_new_expr(&mut self, node: &mut NewExpr) {
        if is_runtime_worker_constructor(&node.callee)
            && let Some(args) = &mut node.args
            && let Some(first) = args.first_mut()
            && first.spread.is_none()
            && let Expr::Lit(Lit::Str(string)) = &mut *first.expr
        {
            let source = str_value(string);
            if let Ok(rewritten) = self.rewrite_source(&source)
                && rewritten != source
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
                expr: Box::new(Expr::MetaProp(MetaPropExpr {
                    span: DUMMY_SP,
                    kind: MetaPropKind::ImportMeta,
                })),
            },
        ]),
        type_args: None,
    })
}

fn posix_dirname(path: &str) -> String {
    std::path::Path::new(path)
        .parent()
        .and_then(|parent| parent.to_str())
        .unwrap_or("")
        .replace('\\', "/")
}
