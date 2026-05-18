use anyhow::{Result, bail};
use swc_common::sync::Lrc;
use swc_common::{BytePos, FileName, Mark, SourceMap};
use swc_ecma_ast::{Module, Str};
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};
use swc_ecma_transforms_base::resolver;
use swc_ecma_visit::VisitMutWith;

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
        bail!(
            "failed to parse {source_name}: {} recoverable parser error(s)",
            recovered.len()
        );
    }
    Ok(module)
}

pub fn emit_js_module(parsed: &ParsedJsModule, header_lines: &[String]) -> Result<String> {
    let mut buf = Vec::new();
    {
        let mut emitter = Emitter {
            cfg: Config::default(),
            cm: parsed.cm.clone(),
            comments: None,
            wr: JsWriter::new(parsed.cm.clone(), "\n", &mut buf, None),
        };
        emitter.emit_module(&parsed.module)?;
    }
    let code = String::from_utf8(buf)?;
    let mut out = String::new();
    for line in header_lines {
        out.push_str(line);
        out.push('\n');
    }
    out.push('\n');
    out.push_str(&code);
    out.push('\n');
    Ok(out)
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
    use swc_common::{DUMMY_SP, GLOBALS, Globals, Spanned};

    /// Wrap `body` in a fresh SWC `GLOBALS` so `Mark::new()` inside the
    /// resolver pass can mint marks. Production code does the same in
    /// `main.rs` and `debundle_agent_cli::run_agent`.
    fn with_globals<R>(body: impl FnOnce() -> R) -> R {
        let globals = Globals::default();
        GLOBALS.set(&globals, body)
    }

    #[test]
    fn source_line_index_matches_source_map_line_numbers() {
        with_globals(|| {
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
        with_globals(|| {
            let parsed = parse_js_module("test.js", "const a = 1;\n").unwrap();
            let line_index = parsed.line_index();

            assert_eq!(line_for_span(&parsed, DUMMY_SP), None);
            assert_eq!(line_range_for_span(&parsed, DUMMY_SP), None);
            assert_eq!(line_index.line_for_span(DUMMY_SP), None);
            assert_eq!(line_index.line_range_for_span(DUMMY_SP), None);
        });
    }
}
