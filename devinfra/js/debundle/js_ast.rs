use anyhow::{Result, bail};
use swc_common::sync::Lrc;
use swc_common::{FileName, SourceMap};
use swc_ecma_ast::{Module, Str};
use swc_ecma_codegen::text_writer::JsWriter;
use swc_ecma_codegen::{Config, Emitter};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};

#[derive(Clone)]
pub struct ParsedJsModule {
    pub cm: Lrc<SourceMap>,
    pub module: Module,
}

pub fn parse_js_module(source_name: &str, source: &str) -> Result<ParsedJsModule> {
    let cm: Lrc<SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom(source_name.to_string()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        default_syntax(),
        Default::default(),
        StringInput::from(&*fm),
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
    Ok(ParsedJsModule { cm, module })
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
    if span.is_dummy() {
        return None;
    }
    Some(parsed.cm.lookup_char_pos(span.lo()).line)
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
