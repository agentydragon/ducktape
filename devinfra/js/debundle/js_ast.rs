use std::collections::BTreeMap;

use anyhow::{Result, bail};
use swc_atoms::Atom;
use swc_common::comments::{Comment, CommentKind, Comments, SingleThreadedComments};
use swc_common::sync::Lrc;
use swc_common::{BytePos, DUMMY_SP, FileName, GLOBALS, Globals, Mark, SourceMap, Spanned};
use swc_ecma_ast::{Decl, Expr, Module, ModuleDecl, ModuleItem, Pat, Stmt, Str};
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};
use swc_ecma_transforms_base::resolver;
use swc_ecma_visit::VisitMutWith;

/// Run `body` inside a fresh `swc_common::GLOBALS` arena. Production
/// entry point (`main.rs`) wraps the
/// whole program in `GLOBALS.set(...)`; tests that exercise the parse
/// pipeline directly (vs. through a subprocess) must wrap each test
/// body in this helper so `Mark::new()` inside `resolver` has an
/// arena to mint into.
///
/// Within a single thread, nested `GLOBALS.set` calls take the inner
/// scope's value for the duration of the inner closure — so this is
/// safe to call from a unit test even if the enclosing thread already
/// has its own GLOBALS set.
pub fn with_swc_globals<R>(body: impl FnOnce() -> R) -> R {
    let globals = Globals::default();
    GLOBALS.set(&globals, body)
}

/// Parsed module with SWC hygiene contexts assigned.
///
/// `unresolved_mark` / `top_level_mark` are the two `Mark`s the
/// `resolver` pass used when annotating every `Ident` in `module`
/// with its `SyntaxContext`. Downstream synthesis sites that mint new
/// bindings must derive fresh marks from `top_level_mark` (via
/// `Mark::fresh(top_level_mark)`) so the new bindings nest correctly
/// under the same chunk-root context.
///
/// See <devinfra/js/debundle/TODO.md> "Rename pipeline" for why
/// hygiene contexts matter to the debundler: they make
/// `ident.to_id() = (sym, ctxt)` the canonical binding identity,
/// distinguishing two same-named bindings declared in different
/// scopes.
#[derive(Clone)]
pub struct ParsedJsModule {
    pub cm: Lrc<SourceMap>,
    pub module: Module,
    pub unresolved_mark: Mark,
    pub top_level_mark: Mark,
}

impl ParsedJsModule {
    pub fn line_index(&self) -> SourceLineIndex {
        SourceLineIndex::for_source_map(&self.cm)
    }

    /// Clone the source text stored in the SourceMap. Returns the full
    /// source string that was passed to `parse_js_module` or
    /// `parse_js_module_consuming`.
    pub fn source_text(&self) -> String {
        self.cm.files()[0].src.to_string()
    }
}

#[derive(Clone, Debug)]
pub struct SourceLineIndex {
    files: Vec<FileLineIndex>,
}

#[derive(Clone, Debug)]
struct FileLineIndex {
    start_pos: BytePos,
    line_starts: Vec<BytePos>,
}

impl SourceLineIndex {
    pub fn for_source_map(cm: &SourceMap) -> Self {
        let files = cm
            .files()
            .iter()
            .map(|file| FileLineIndex {
                start_pos: file.start_pos,
                line_starts: file.analyze().lines.clone(),
            })
            .collect();
        Self { files }
    }

    pub fn line_for_span(&self, span: swc_common::Span) -> Option<usize> {
        if span.is_dummy() {
            return None;
        }
        self.line_for_pos(span.lo())
    }

    pub fn line_range_for_span(&self, span: swc_common::Span) -> Option<(usize, usize)> {
        if span.is_dummy() {
            return None;
        }
        Some((self.line_for_pos(span.lo())?, self.line_for_pos(span.hi())?))
    }

    fn line_for_pos(&self, pos: BytePos) -> Option<usize> {
        if pos.is_dummy() {
            return None;
        }
        let file = self.file_for_pos(pos)?;
        Some(file.line_for_pos(pos))
    }

    fn file_for_pos(&self, pos: BytePos) -> Option<&FileLineIndex> {
        let index = self.files.partition_point(|file| file.start_pos <= pos);
        if index == 0 {
            None
        } else {
            self.files.get(index - 1)
        }
    }
}

impl FileLineIndex {
    fn line_for_pos(&self, pos: BytePos) -> usize {
        match self.line_starts.binary_search(&pos) {
            Ok(line_index) => line_index + 1,
            Err(0) => 0,
            Err(insert_index) => insert_index,
        }
    }
}

pub fn parse_js_module(source_name: &str, source: &str) -> Result<ParsedJsModule> {
    let cm: Lrc<SourceMap> = Default::default();
    let fm = source_file(&cm, source_name, source);
    let (module, unresolved_mark, top_level_mark) = parse_and_resolve(source_name, &fm)?;
    Ok(ParsedJsModule {
        cm,
        module,
        unresolved_mark,
        top_level_mark,
    })
}

/// Like `parse_js_module` but takes ownership of the source string, avoiding
/// the clone inside `source_file`. The source text is retrievable via
/// `ParsedJsModule::source_text()`.
pub fn parse_js_module_consuming(source_name: &str, source: String) -> Result<ParsedJsModule> {
    let cm: Lrc<SourceMap> = Default::default();
    let fm = cm.new_source_file(FileName::Custom(source_name.to_string()).into(), source);
    let (module, unresolved_mark, top_level_mark) = parse_and_resolve(source_name, &fm)?;
    Ok(ParsedJsModule {
        cm,
        module,
        unresolved_mark,
        top_level_mark,
    })
}

/// Parse a module to a bare `Module` with hygiene contexts assigned.
/// The minted `Mark`s are discarded; callers that need to synthesize
/// fresh bindings should use `parse_js_module` (which preserves the
/// marks on `ParsedJsModule`) instead.
pub fn parse_js_module_ast(source_name: &str, source: &str) -> Result<Module> {
    let cm: Lrc<SourceMap> = Default::default();
    let fm = source_file(&cm, source_name, source);
    let (module, _, _) = parse_and_resolve(source_name, &fm)?;
    Ok(module)
}

/// Wrap a pre-existing `Module` in a `ParsedJsModule`. Callers must
/// pass the `Mark`s that were used when `resolver` was applied to
/// `module`; if the module hasn't been through `resolver` yet, mint
/// fresh marks with `Mark::new()` and call `resolver` first.
pub fn parsed_js_module_with_source_map(
    source_name: &str,
    source: &str,
    module: Module,
    unresolved_mark: Mark,
    top_level_mark: Mark,
) -> ParsedJsModule {
    let cm: Lrc<SourceMap> = Default::default();
    let _fm = source_file(&cm, source_name, source);
    ParsedJsModule {
        cm,
        module,
        unresolved_mark,
        top_level_mark,
    }
}

fn source_file(
    cm: &Lrc<SourceMap>,
    source_name: &str,
    source: &str,
) -> Lrc<swc_common::SourceFile> {
    cm.new_source_file(
        FileName::Custom(source_name.to_string()).into(),
        source.to_string(),
    )
}

/// Parse a source file and run SWC's `resolver` pass to assign
/// `SyntaxContext` to every `Ident`. Returns the parsed module
/// together with the two `Mark`s the resolver used. After this point
/// `ident.to_id()` is the canonical binding identity.
///
/// Callers MUST be inside a `GLOBALS.set(...)` scope. Production
/// entry point does this in `main.rs`;
/// tests that exercise this code directly do it via
/// `with_swc_globals` in their setup.
fn parse_and_resolve(
    source_name: &str,
    fm: &swc_common::SourceFile,
) -> Result<(Module, Mark, Mark)> {
    let mut module = parse_module_from_source_file(source_name, fm)?;
    let unresolved_mark = Mark::new();
    let top_level_mark = Mark::new();
    // `true` enables TypeScript-aware scoping (matches `default_syntax`).
    module.visit_mut_with(&mut resolver(unresolved_mark, top_level_mark, true));
    Ok((module, unresolved_mark, top_level_mark))
}

fn parse_module_from_source_file(source_name: &str, fm: &swc_common::SourceFile) -> Result<Module> {
    let lexer = Lexer::new(
        default_syntax(),
        Default::default(),
        StringInput::from(fm),
        None,
    );
    let mut parser = Parser::new_from(lexer);
    let module = parser
        .parse_module()
        .map_err(|error| anyhow::anyhow!("failed to parse {source_name}: {:?}", error.kind()))?;
    let recovered = parser.take_errors();
    if !recovered.is_empty() {
        // Include each recovered error's message so the rejection is
        // actionable — e.g. a `with` statement in module (strict)
        // code surfaces as its strict-mode syntax error here
        // (docs/design.md A4) rather than as an opaque count.
        let details = recovered
            .iter()
            .map(|error| error.kind().msg())
            .collect::<Vec<_>>()
            .join("; ");
        bail!(
            "failed to parse {source_name}: {count} recoverable parser error(s): {details}",
            count = recovered.len(),
        );
    }
    Ok(module)
}

pub fn emit_js_module(parsed: &ParsedJsModule, header_lines: &[String]) -> Result<String> {
    emit_js_module_with_comments(parsed, header_lines, &BTreeMap::new(), &BTreeMap::new())
}

/// Serialize a standalone AST `Module` (e.g. a synthesized selector built by
/// pruning a parsed module) with the same codegen as the rest of the pipeline.
/// Spans are ignored, so `DUMMY_SP` nodes are fine.
pub fn emit_module_source(module: &Module) -> Result<String> {
    let cm: Lrc<SourceMap> = Lrc::default();
    let mut buf = Vec::new();
    {
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm: cm.clone(),
            comments: None,
            wr: JsWriter::new(cm, "\n", &mut buf, None),
        };
        emitter.emit_module(module)?;
    }
    Ok(String::from_utf8(buf)?.trim_end().to_string())
}

/// Strip every `(expr)` parenthesization, replacing it with its inner expression.
/// Parens are syntactically insignificant grouping, so removing them canonicalizes
/// an AST: two expressions that differ only in redundant parens emit identically
/// after this pass. Mirrors the source-match matcher, which sees through parens on
/// both sides — use it to compare selector shapes paren-insensitively.
pub fn strip_parens(module: &mut Module) {
    struct ParenStripper;
    impl swc_ecma_visit::VisitMut for ParenStripper {
        fn visit_mut_expr(&mut self, expr: &mut Expr) {
            expr.visit_mut_children_with(self);
            if let Expr::Paren(paren) = expr {
                *expr = (*paren.expr).clone();
            }
        }
    }
    module.visit_mut_with(&mut ParenStripper);
}

/// Number of post-comma-list-split positions a top-level body item
/// produces. Mirrors the owner-graph fact splitter: `var x = ..., y
/// = ...;` is one body item but two statement ordinals; other
/// top-level items count as one.
pub fn post_split_top_level_count(item: &ModuleItem) -> usize {
    fn decl_count(decl: &Decl) -> usize {
        match decl {
            Decl::Var(var) if var.decls.len() > 1 => var.decls.len(),
            _ => 1,
        }
    }
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_count(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            decl_count(&export_decl.decl)
        }
        _ => 1,
    }
}

/// Convert a pre-split body index to the first post-split
/// `StatementOrdinal` value for that body item.
pub fn statement_ordinal_for_body_index(body: &[ModuleItem], body_idx: usize) -> usize {
    body[..body_idx]
        .iter()
        .map(post_split_top_level_count)
        .sum()
}

/// Emit a JS module with optional leading line comments.
///
/// `binding_comments` maps a top-level binding name to a human-readable
/// comment block (verbatim spec text — newlines preserved). For each
/// top-level item in `parsed.module.body` whose declared binding names
/// intersect the map, the comment text is split on `\n` and emitted as
/// one `// <line>` per source line immediately before the statement,
/// using SWC's leading-comment machinery. Empty input lines emit as a
/// bare `//`. Trailing whitespace on each line is trimmed.
///
/// `binding_comments` keys that match no top-level statement are
/// silently ignored — the lowerer routes its own member set into each
/// module body, so a comment-but-no-statement state isn't reachable in
/// the normal pipeline, and a test fixture that exercises it shouldn't
/// fail emission.
///
/// `leading_item_comments` maps a top-level item's `span.lo()` to a
/// comment block. The lowerer uses this for comments attached to
/// resolved anonymous statements, which have no binding name to key
/// from.
pub fn emit_js_module_with_comments(
    parsed: &ParsedJsModule,
    header_lines: &[String],
    binding_comments: &BTreeMap<String, String>,
    leading_item_comments: &BTreeMap<BytePos, String>,
) -> Result<String> {
    let comments_storage = SingleThreadedComments::default();
    let comments_handle: Option<&dyn Comments> =
        if binding_comments.is_empty() && leading_item_comments.is_empty() {
            None
        } else {
            attach_binding_comments(&parsed.module, binding_comments, &comments_storage);
            attach_leading_item_comments(leading_item_comments, &comments_storage);
            Some(&comments_storage)
        };
    let mut buf = Vec::new();
    {
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm: parsed.cm.clone(),
            comments: comments_handle,
            wr: JsWriter::new(parsed.cm.clone(), "\n", &mut buf, None),
        };
        emitter.emit_module(&parsed.module)?;
    }
    let code = String::from_utf8(buf)?;
    let code = code.trim_end_matches(['\n', '\r']);
    let mut out = String::new();
    for line in header_lines {
        out.push_str(line);
        out.push('\n');
    }
    if !header_lines.is_empty() && !code.is_empty() {
        out.push('\n');
    }
    out.push_str(code);
    out.push('\n');
    Ok(out)
}

fn attach_leading_item_comments(
    leading_item_comments: &BTreeMap<BytePos, String>,
    storage: &SingleThreadedComments,
) {
    for (lo, comment_text) in leading_item_comments {
        if *lo == BytePos(0) || comment_text.is_empty() {
            continue;
        }
        for line in format_member_comment_line_texts(comment_text) {
            storage.add_leading(
                *lo,
                Comment {
                    kind: CommentKind::Line,
                    span: DUMMY_SP,
                    text: Atom::from(line),
                },
            );
        }
    }
}

/// Walk `module.body` and, for each top-level item whose declared
/// binding names overlap `binding_comments`, attach one `Line` comment
/// per source line of the comment text to the item's span lo position.
///
/// Each source-text line is trimmed of trailing whitespace and emitted
/// as `// <text>`; empty lines emit as `//` so paragraph structure
/// survives. Items with `DUMMY_SP` (synthesized statements with no
/// source-anchor lo) are skipped — the lowerer always carries the
/// declaration's original span through, so this is a defensive guard,
/// not an expected case.
fn attach_binding_comments(
    module: &Module,
    binding_comments: &BTreeMap<String, String>,
    storage: &SingleThreadedComments,
) {
    for item in &module.body {
        let names = item_declared_names(item);
        let comment_text = names
            .iter()
            .find_map(|name| binding_comments.get(name.as_str()));
        let Some(comment_text) = comment_text else {
            continue;
        };
        // Empty `comment:` text emits nothing — matches the spec's
        // "absent / empty string emit nothing" rule.
        if comment_text.is_empty() {
            continue;
        }
        let span = item.span();
        if span.lo() == BytePos(0) {
            // Synthesized item without a source-anchored lo (e.g. an
            // injected import). SWC keys comments by span lo, so we
            // cannot anchor here. Skip rather than corrupt the map.
            continue;
        }
        for line in format_member_comment_line_texts(comment_text) {
            storage.add_leading(
                span.lo(),
                Comment {
                    kind: CommentKind::Line,
                    span: DUMMY_SP,
                    text: Atom::from(line),
                },
            );
        }
    }
}

/// Format spec comment text into a sequence of `//`-prefixed lines.
///
/// Each input line is trimmed of trailing whitespace. Empty input
/// lines emit as `//` (no trailing space) so paragraph structure in
/// the source survives the round trip. Non-empty lines emit as
/// `// <text>`. The output is a `Vec<String>`, one entry per emitted
/// line, ready to be joined by `\n` callers.
pub fn format_comment_block_lines(text: &str) -> Vec<String> {
    text.split('\n')
        .map(|line| {
            let trimmed = line.trim_end();
            if trimmed.is_empty() {
                "//".to_string()
            } else {
                format!("// {trimmed}")
            }
        })
        .collect()
}

/// Format spec comment text into the per-line texts SWC's `Line`-kind
/// Comment expects (the prefix `//` is added by the emitter, so each
/// entry is the *body* of one line comment, including any leading
/// space).
///
/// Mirrors [`format_comment_block_lines`] but returns the bare body
/// for each line (`" foo"` vs `"// foo"`), since SWC emits the `//`
/// itself.
fn format_member_comment_line_texts(text: &str) -> Vec<String> {
    text.split('\n')
        .map(|line| {
            let trimmed = line.trim_end();
            if trimmed.is_empty() {
                String::new()
            } else {
                format!(" {trimmed}")
            }
        })
        .collect()
}

/// Collect every top-level binding name a `ModuleItem` declares.
///
/// Covers the top-level declaration shapes the debundler lowerer
/// emits: `function`, `class`, `var`/`let`/`const` (named patterns
/// only — destructured names are also returned so a `comment:` on
/// any one binding of a destructure anchors above the whole
/// statement), and the matching `export` variants. Returns an empty
/// vec for statements that bind no top-level name (expression
/// statements, side-effect calls, etc.).
fn item_declared_names(item: &ModuleItem) -> Vec<String> {
    let decl = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => &export.decl,
        _ => return Vec::new(),
    };
    decl_declared_names(decl)
}

fn decl_declared_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(f) => vec![f.ident.sym.to_string()],
        Decl::Class(c) => vec![c.ident.sym.to_string()],
        Decl::Var(var) => {
            let mut names = Vec::new();
            for declarator in &var.decls {
                pat_names_into(&declarator.name, &mut names);
            }
            names
        }
        _ => Vec::new(),
    }
}

fn pat_names_into(pat: &Pat, out: &mut Vec<String>) {
    match pat {
        Pat::Ident(ident) => out.push(ident.id.sym.to_string()),
        Pat::Array(arr) => {
            for elem in arr.elems.iter().flatten() {
                pat_names_into(elem, out);
            }
        }
        Pat::Object(obj) => {
            for prop in &obj.props {
                match prop {
                    swc_ecma_ast::ObjectPatProp::KeyValue(kv) => pat_names_into(&kv.value, out),
                    swc_ecma_ast::ObjectPatProp::Assign(assign) => {
                        out.push(assign.key.id.sym.to_string());
                    }
                    swc_ecma_ast::ObjectPatProp::Rest(rest) => pat_names_into(&rest.arg, out),
                }
            }
        }
        Pat::Rest(rest) => pat_names_into(&rest.arg, out),
        Pat::Assign(assign) => pat_names_into(&assign.left, out),
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

pub fn line_for_span(parsed: &ParsedJsModule, span: swc_common::Span) -> Option<usize> {
    parsed.line_index().line_for_span(span)
}

pub fn line_range_for_span(
    parsed: &ParsedJsModule,
    span: swc_common::Span,
) -> Option<(usize, usize)> {
    parsed.line_index().line_range_for_span(span)
}

pub fn str_value(value: &Str) -> String {
    value.value.to_string_lossy().to_string()
}

pub fn set_str_value(value: &mut Str, next: String) {
    value.value = next.into();
    value.raw = None;
}

fn default_syntax() -> Syntax {
    Syntax::Typescript(TsSyntax {
        tsx: true,
        decorators: true,
        no_early_errors: true,
        ..Default::default()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{DUMMY_SP, Spanned};

    #[test]
    fn source_line_index_matches_source_map_line_numbers() {
        with_swc_globals(|| {
            let parsed = parse_js_module(
                "test.js",
                "import a from 'a';\n\nconst b =\n  a;\nexport { b };\n",
            )
            .unwrap();
            let line_index = parsed.line_index();

            for item in &parsed.module.body {
                let span = item.span();
                assert_eq!(
                    line_index.line_for_span(span),
                    Some(parsed.cm.lookup_char_pos(span.lo()).line)
                );
                assert_eq!(
                    line_index.line_range_for_span(span),
                    Some((
                        parsed.cm.lookup_char_pos(span.lo()).line,
                        parsed.cm.lookup_char_pos(span.hi()).line,
                    ))
                );
            }
        });
    }

    #[test]
    fn source_line_index_ignores_dummy_spans() {
        with_swc_globals(|| {
            let parsed = parse_js_module("test.js", "const a = 1;\n").unwrap();
            let line_index = parsed.line_index();

            assert_eq!(line_for_span(&parsed, DUMMY_SP), None);
            assert_eq!(line_range_for_span(&parsed, DUMMY_SP), None);
            assert_eq!(line_index.line_for_span(DUMMY_SP), None);
            assert_eq!(line_index.line_range_for_span(DUMMY_SP), None);
        });
    }

    #[test]
    fn format_comment_block_lines_handles_multi_line_paragraph_structure() {
        // Each input line trims trailing whitespace; empty lines emit
        // as a bare `//` so paragraph breaks survive the round trip.
        assert_eq!(
            format_comment_block_lines("Line one.\n\nLine three.   \nLine four."),
            vec![
                "// Line one.".to_string(),
                "//".to_string(),
                "// Line three.".to_string(),
                "// Line four.".to_string(),
            ],
        );
    }

    #[test]
    fn format_comment_block_lines_single_line_is_a_single_entry() {
        assert_eq!(
            format_comment_block_lines("just one line"),
            vec!["// just one line".to_string()],
        );
    }

    #[test]
    fn format_comment_block_lines_empty_input_is_a_single_empty_marker() {
        // An empty `comment:` block ("") still produces one
        // line — the caller decides whether to emit it at all (the
        // lowerer skips on `Option::is_none` / empty string).
        assert_eq!(format_comment_block_lines(""), vec!["//".to_string()]);
    }

    #[test]
    fn emit_js_module_with_comments_attaches_leading_lines_above_decl() {
        with_swc_globals(|| {
            let parsed = parse_js_module(
                "test.js",
                "const a = 1;\nfunction b() { return 2; }\nexport { a, b };\n",
            )
            .unwrap();
            let mut binding_comments = BTreeMap::new();
            binding_comments.insert("a".to_string(), "doc for a.\nsecond line.".to_string());
            binding_comments.insert("b".to_string(), "doc for b.".to_string());
            let source =
                emit_js_module_with_comments(&parsed, &[], &binding_comments, &BTreeMap::new())
                    .unwrap();
            // Each binding's comment lands above its declaration; SWC
            // emits one `// <text>` per `Line` comment in the map.
            let a_pos = source
                .find("const a = 1")
                .expect("must contain const a = 1");
            let comment_a_pos = source
                .find("// doc for a.")
                .expect("must contain doc for a");
            let comment_a2_pos = source
                .find("// second line.")
                .expect("must contain second line");
            assert!(
                comment_a_pos < comment_a2_pos && comment_a2_pos < a_pos,
                "both lines of a's comment must precede `const a = 1`:\n{source}",
            );
            let b_pos = source.find("function b").expect("must contain function b");
            let comment_b_pos = source
                .find("// doc for b.")
                .expect("must contain doc for b");
            assert!(
                comment_b_pos < b_pos && comment_b_pos > a_pos,
                "b's comment must precede `function b` and follow a:\n{source}",
            );
        });
    }

    #[test]
    fn emit_js_module_with_comments_drops_unmatched_binding_keys() {
        with_swc_globals(|| {
            let parsed = parse_js_module("test.js", "const a = 1;\nexport { a };\n").unwrap();
            let mut binding_comments = BTreeMap::new();
            binding_comments.insert("not_here".to_string(), "noise".to_string());
            let source =
                emit_js_module_with_comments(&parsed, &[], &binding_comments, &BTreeMap::new())
                    .unwrap();
            assert!(
                !source.contains("// noise"),
                "unmatched binding keys must emit no comment:\n{source}",
            );
        });
    }

    #[test]
    fn emit_js_module_uses_one_terminal_newline() {
        with_swc_globals(|| {
            let parsed =
                parse_js_module("test.js", "const value = 1;\nexport { value };\n").unwrap();
            let source = emit_js_module(&parsed, &["// generated".to_string()]).unwrap();

            assert_eq!(
                source
                    .as_bytes()
                    .iter()
                    .rev()
                    .take_while(|&&byte| byte == b'\n')
                    .count(),
                1,
                "emitted module must end with exactly one newline:\n{source:?}",
            );
            assert!(
                source.starts_with("// generated\n\n"),
                "header must stay separated from code by one blank line:\n{source}",
            );
        });
    }
}
