//! Read-only inspection of the parsed top-level items in a JavaScript source file.
//!
//! Item indices are the source module's raw `Module.body` ordinals. They match
//! selector `body[N]` coordinates, but are not owner IDs or other graph IDs.

use std::path::{Path, PathBuf};
use std::str::FromStr;

use anyhow::{Context, Result, bail};
use serde::Serialize;
use swc_common::{DUMMY_SP, Spanned};
use swc_ecma_ast::{
    DefaultDecl, ExportDecl, ExportDefaultDecl, Module, ModuleDecl, ModuleItem, Stmt,
};

use binding_targets::declaration_name_strings;
use js_ast::{ParsedJsModule, emit_module_source, parse_js_module_consuming};

/// A parsed JavaScript source file together with its original text and path.
pub struct PreparedSource {
    path: PathBuf,
    source: String,
    line_starts: Vec<usize>,
    parsed: ParsedJsModule,
}

impl PreparedSource {
    /// Read and parse one source file using the same parser/resolver as
    /// `debundle spec match-selector`.
    ///
    /// Like the underlying `js_ast::parse_js_module_consuming`, this must be
    /// called inside an SWC `GLOBALS` scope. The debundle CLI provides that
    /// scope around the whole command.
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let source = std::fs::read_to_string(&path)
            .with_context(|| format!("reading source file {}", path.display()))?;
        let parsed = parse_js_module_consuming(&path.display().to_string(), source)
            .with_context(|| format!("parsing source file {}", path.display()))?;
        let source = parsed.source_text();
        let line_starts = std::iter::once(0)
            .chain(source.match_indices('\n').map(|(index, _)| index + 1))
            .collect();
        Ok(Self {
            path,
            source,
            line_starts,
            parsed,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Return the module produced by the shared SWC parser.
    pub fn module(&self) -> &Module {
        &self.parsed.module
    }

    pub fn statement_count(&self) -> usize {
        self.parsed.module.body.len()
    }

    /// Return statements in the requested inclusive range, or all statements
    /// when `range` is `None`.
    pub fn statement_rows(&self, range: Option<StatementRange>) -> Result<Vec<StatementRow>> {
        let count = self.statement_count();
        let (start, end) = match range {
            Some(range) => {
                range.validate(count)?;
                (range.start, range.end)
            }
            None if count > 0 => (0, count - 1),
            None => return Ok(Vec::new()),
        };

        self.parsed.module.body[start..=end]
            .iter()
            .enumerate()
            .map(|(offset, item)| self.statement_row(start + offset, item))
            .collect()
    }

    /// Find one top-level item declaring `name` and include up to
    /// `context_statements` items on either side.
    ///
    /// A repeated top-level declaration is rejected as ambiguous because this
    /// helper is intended to identify a source location, not choose an owner.
    pub fn statement_rows_around_binding(
        &self,
        name: &str,
        context_statements: usize,
    ) -> Result<Vec<StatementRow>> {
        let matches = self
            .parsed
            .module
            .body
            .iter()
            .enumerate()
            .filter_map(|(index, item)| {
                top_level_binding_names(item)
                    .iter()
                    .any(|binding| binding == name)
                    .then_some(index)
            })
            .collect::<Vec<_>>();
        let index = match matches.as_slice() {
            [] => bail!(
                "no top-level declaration of binding `{name}` in {}",
                self.path.display()
            ),
            [index] => *index,
            _ => bail!(
                "ambiguous top-level binding `{name}` in {}: declared by body[{}]",
                self.path.display(),
                matches
                    .iter()
                    .map(usize::to_string)
                    .collect::<Vec<_>>()
                    .join("], body[")
            ),
        };
        let start = index.saturating_sub(context_statements);
        let end = index
            .saturating_add(context_statements)
            .min(self.statement_count().saturating_sub(1));
        self.statement_rows(Some(StatementRange { start, end }))
    }

    fn statement_row(&self, index: usize, item: &ModuleItem) -> Result<StatementRow> {
        let span = item.span();
        if span.is_dummy() {
            bail!("parsed source item body[{index}] has no original source span");
        }
        let source_files = self.parsed.cm.files();
        let Some(source_file) = source_files.first() else {
            bail!(
                "parsed source file {} has no source map entry",
                self.path.display()
            );
        };
        let byte_start = span
            .lo()
            .0
            .checked_sub(source_file.start_pos.0)
            .map(|offset| offset as usize)
            .context("source span starts before the source file")?;
        let byte_end = span
            .hi()
            .0
            .checked_sub(source_file.start_pos.0)
            .map(|offset| offset as usize)
            .context("source span ends before the source file")?;
        if byte_start > byte_end || byte_end > self.source.len() {
            bail!(
                "invalid source span for body[{index}]: byte range {byte_start}..{byte_end} exceeds source length {}",
                self.source.len()
            );
        }
        let original_source = self
            .source
            .get(byte_start..byte_end)
            .with_context(|| format!("source span for body[{index}] is not on UTF-8 boundaries"))?
            .to_string();
        let (start_line, start_column) = line_and_byte_column(&self.line_starts, byte_start);
        let (end_line, end_column) = line_and_byte_column(&self.line_starts, byte_end);
        let pretty_source = pretty_print_item(item)?;

        Ok(StatementRow {
            index,
            byte_start,
            byte_end,
            start_line,
            start_column,
            end_line,
            end_column,
            bindings: top_level_binding_names(item),
            original_source,
            pretty_source,
        })
    }
}

/// Inclusive, zero-based range of raw parsed top-level items.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StatementRange {
    pub start: usize,
    pub end: usize,
}

impl StatementRange {
    pub fn validate(self, statement_count: usize) -> Result<()> {
        if self.start > self.end {
            bail!(
                "statement range starts after it ends: {}..{} (inclusive)",
                self.start,
                self.end
            );
        }
        if self.end >= statement_count {
            bail!(
                "statement range {}..{} is out of bounds for {statement_count} top-level statements",
                self.start,
                self.end
            );
        }
        Ok(())
    }
}

impl FromStr for StatementRange {
    type Err = anyhow::Error;

    /// Parse one zero-based index (`4`) or an inclusive range (`2..4`).
    fn from_str(input: &str) -> Result<Self> {
        let input = input.trim();
        let (start, end) = match input.split_once("..") {
            Some((start, end)) if !end.contains("..") => (
                start
                    .trim()
                    .parse::<usize>()
                    .with_context(|| format!("invalid statement range start `{start}`"))?,
                end.trim()
                    .parse::<usize>()
                    .with_context(|| format!("invalid statement range end `{end}`"))?,
            ),
            Some(_) => bail!("invalid statement range `{input}`; expected INDEX or START..END"),
            None => {
                let index = input
                    .parse::<usize>()
                    .with_context(|| format!("invalid statement index `{input}`"))?;
                (index, index)
            }
        };
        Ok(Self { start, end })
    }
}

/// One raw top-level module item and its original-source coordinates.
#[derive(Debug, Clone, Serialize)]
pub struct StatementRow {
    /// Zero-based index in the parsed source module's `Module.body` array.
    pub index: usize,
    /// Half-open UTF-8 byte span in the original source file.
    pub byte_start: usize,
    pub byte_end: usize,
    /// One-based byte position. The end position is exclusive.
    pub start_line: usize,
    pub start_column: usize,
    pub end_line: usize,
    pub end_column: usize,
    /// Names declared at module top level by this item, including imports.
    pub bindings: Vec<String>,
    /// Original source slice covered by the AST span.
    pub original_source: String,
    /// SWC codegen's readable rendering of this single AST item.
    pub pretty_source: String,
}

fn top_level_binding_names(item: &ModuleItem) -> Vec<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_name_strings(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl { decl, .. })) => {
            declaration_name_strings(decl)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(ExportDefaultDecl {
            decl, ..
        })) => match decl {
            DefaultDecl::Class(class) => class
                .ident
                .as_ref()
                .map(|ident| vec![ident.sym.to_string()])
                .unwrap_or_default(),
            DefaultDecl::Fn(function) => function
                .ident
                .as_ref()
                .map(|ident| vec![ident.sym.to_string()])
                .unwrap_or_default(),
            DefaultDecl::TsInterfaceDecl(_) => Vec::new(),
        },
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                swc_ecma_ast::ImportSpecifier::Default(default) => default.local.sym.to_string(),
                swc_ecma_ast::ImportSpecifier::Namespace(namespace) => {
                    namespace.local.sym.to_string()
                }
                swc_ecma_ast::ImportSpecifier::Named(named) => named.local.sym.to_string(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn pretty_print_item(item: &ModuleItem) -> Result<String> {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    emit_module_source(&module)
}

fn line_and_byte_column(line_starts: &[usize], offset: usize) -> (usize, usize) {
    let line_index = line_starts.partition_point(|start| *start <= offset) - 1;
    let line_start = line_starts[line_index];
    (line_index + 1, offset - line_start + 1)
}
