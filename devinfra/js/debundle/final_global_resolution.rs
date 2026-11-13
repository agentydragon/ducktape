//! Check whether emission captured a source global.
//!
//! The source inventory is the prepared parse's `ParsedGlobalUses`: every
//! identifier site the resolver left unbound. The output side resolves each
//! final emitted AST once, from cleared syntax contexts, and matches source
//! sites by span within the same source chunk. A site is `preserved` when it
//! appears exactly once in the chunk's final files, with the same spelling,
//! still unbound. Capture by an emitted import or local, or a rename, is
//! `changed`; anything the two inventories cannot settle is `incomplete`.
//! Final validation refuses a proven capture; other findings remain diagnostic.
//!
//! This proves binding correspondence only. It does not give an unbound read
//! value or effect authority, and it does not cover identifiers that emission
//! introduced without a source site.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use artifact::{ChunkBundle, FileRole, JsFileBody};
use graph_provenance::sha256;
use js_ast::{GlobalUseElisions, ParsedGlobalUses};
use serde::Serialize;
use source_identity::{ChunkId, ChunkTable};
use swc_common::{BytePos, GLOBALS, Globals, Mark, Span, SyntaxContext};
use swc_ecma_ast::{
    BreakStmt, ContinueStmt, ExportAll, ExportSpecifier, Ident, ImportDecl, LabeledStmt,
    NamedExport,
};
use swc_ecma_transforms_base::resolver;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalResolutionStatus {
    Preserved,
    Changed,
    Incomplete,
}

impl GlobalResolutionStatus {
    fn combine(self, other: Self) -> Self {
        match (self, other) {
            (Self::Changed, _) | (_, Self::Changed) => Self::Changed,
            (Self::Incomplete, _) | (_, Self::Incomplete) => Self::Incomplete,
            _ => Self::Preserved,
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalResolutionReason {
    /// The prepared parse produced no inventory for this chunk.
    MissingManifest,
    MissingFinalEntry,
    DuplicateFinalEntry,
    /// A raw-source entry whose bytes differ from the prepared input.
    RawSourceUnproven,
    /// `with` or nontrivial direct `eval` in a transformed body: static
    /// resolution cannot account for its scope.
    DynamicScope,
    MissingFinalUse,
    DuplicateFinalUse,
    /// The final site resolves to an emitted binding.
    CapturedByBinding,
    /// The final site at the source span is spelled differently.
    Renamed,
    /// A vendor strip recorded the site as removed, yet it is still emitted.
    ElisionConflict,
}

#[derive(Debug, Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct GlobalResolutionSpan {
    pub lo: u32,
    pub hi: u32,
}

impl From<Span> for GlobalResolutionSpan {
    fn from(span: Span) -> Self {
        Self {
            lo: span.lo.0,
            hi: span.hi.0,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct GlobalResolutionFinding {
    pub reason: GlobalResolutionReason,
    /// Source spelling; absent for chunk-level findings.
    pub symbol: Option<String>,
    /// Source span; absent for chunk-level findings.
    pub span: Option<GlobalResolutionSpan>,
    /// Emitted files (`<chunk>/<file>`) holding the evidence.
    pub output_files: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkGlobalResolution {
    pub status: GlobalResolutionStatus,
    /// `None` when the chunk has no prepared inventory.
    pub required_use_count: Option<usize>,
    pub preserved_use_count: usize,
    /// Sites a vendor residual strip removed together with their statement.
    pub elided_use_count: usize,
    pub findings: Vec<GlobalResolutionFinding>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GlobalResolutionReport {
    pub status: GlobalResolutionStatus,
    /// Every emitted source chunk, by name.
    pub chunks: BTreeMap<String, ChunkGlobalResolution>,
}

struct SourceChunkGlobals {
    uses: Option<Arc<ParsedGlobalUses>>,
    /// Digest of the prepared entry's original bytes.
    entry_sha256: Option<String>,
}

/// Prepared-parse inventories captured before materialization consumes the
/// source artifact.
pub struct GlobalResolutionSources {
    chunk_table: ChunkTable,
    chunks: BTreeMap<ChunkId, SourceChunkGlobals>,
}

impl GlobalResolutionSources {
    pub fn capture(source: &ChunkBundle) -> Self {
        let chunks = source
            .chunks
            .iter()
            .map(|chunk| {
                let entry_sha256 = chunk.js.get_file(&chunk.js.entry_file).and_then(|file| {
                    file.source()
                        .map(|text| sha256(text.as_bytes()))
                        .or_else(|| file.ast().map(|ast| sha256(ast.source_text().as_bytes())))
                });
                (
                    chunk.chunk_id,
                    SourceChunkGlobals {
                        uses: chunk.analysis.runtime_facts.global_uses.clone(),
                        entry_sha256,
                    },
                )
            })
            .collect();
        Self {
            chunk_table: source.chunk_table.clone(),
            chunks,
        }
    }
}

struct FinalUse {
    symbol: String,
    unresolved: bool,
    output_file: String,
}

#[derive(Default)]
struct FinalChunkUses {
    entries: Vec<String>,
    /// Entries emitted as raw bytes identical to the prepared input.
    unchanged_raw_entries: usize,
    changed_raw_entries: Vec<String>,
    /// AST-bodied entries; dynamic scope is unprovable through them.
    transformed_entries: usize,
    uses: BTreeMap<(BytePos, BytePos), Vec<FinalUse>>,
}

/// Check every emitted source chunk's prepared global-use inventory against
/// the final emitted files. `elisions` are the sites vendor residual strips
/// recorded as removed with their statements.
pub fn assess_final_global_resolution(
    sources: &GlobalResolutionSources,
    final_files: &ChunkBundle,
    excluded_chunks: &BTreeSet<ChunkId>,
    elisions: &BTreeMap<ChunkId, GlobalUseElisions>,
) -> GlobalResolutionReport {
    let finals = GLOBALS.set(&Globals::new(), || {
        collect_final_uses(sources, final_files, excluded_chunks)
    });
    let mut chunks = BTreeMap::new();
    for (chunk_id, final_uses) in &finals {
        let assessment = assess_chunk(
            sources.chunks.get(chunk_id),
            final_uses,
            elisions.get(chunk_id),
        );
        chunks.insert(sources.chunk_table.name(*chunk_id).to_string(), assessment);
    }
    let status = chunks
        .values()
        .fold(GlobalResolutionStatus::Preserved, |status, chunk| {
            status.combine(chunk.status)
        });
    GlobalResolutionReport { status, chunks }
}

fn collect_final_uses(
    sources: &GlobalResolutionSources,
    final_files: &ChunkBundle,
    excluded_chunks: &BTreeSet<ChunkId>,
) -> BTreeMap<ChunkId, FinalChunkUses> {
    let mut finals = BTreeMap::<ChunkId, FinalChunkUses>::new();
    for chunk in &final_files.chunks {
        if excluded_chunks.contains(&chunk.chunk_id) {
            continue;
        }
        let output_chunk = final_files.chunk_table.name(chunk.chunk_id);
        for file in &chunk.js.files {
            // Files carry their source chunk; spans are only comparable
            // within one source parse.
            let Some(source_chunk) = sources.chunk_table.get(&file.metadata.chunk_id) else {
                continue;
            };
            let output_file = format!("{output_chunk}/{}", file.path);
            let entry = file.metadata.role == FileRole::Entry;
            let target = finals.entry(source_chunk).or_default();
            if entry {
                target.entries.push(output_file.clone());
            }
            match &file.body {
                JsFileBody::Source(text) => {
                    if !entry {
                        continue;
                    }
                    let unchanged = sources
                        .chunks
                        .get(&source_chunk)
                        .and_then(|source| source.entry_sha256.as_deref())
                        .is_some_and(|digest| digest == sha256(text.as_bytes()));
                    if unchanged {
                        target.unchanged_raw_entries += 1;
                    } else {
                        target.changed_raw_entries.push(output_file);
                    }
                }
                JsFileBody::SourceWithAst(_) | JsFileBody::Ast(_) => {
                    let Some(parsed) = file.ast() else {
                        continue;
                    };
                    if entry {
                        target.transformed_entries += 1;
                    }
                    for observed in resolve_final_idents(&parsed.module) {
                        target
                            .uses
                            .entry((observed.span.lo, observed.span.hi))
                            .or_default()
                            .push(FinalUse {
                                symbol: observed.symbol,
                                unresolved: observed.unresolved,
                                output_file: output_file.clone(),
                            });
                    }
                }
            }
        }
    }
    finals
}

fn assess_chunk(
    source: Option<&SourceChunkGlobals>,
    finals: &FinalChunkUses,
    elisions: Option<&GlobalUseElisions>,
) -> ChunkGlobalResolution {
    let Some(manifest) = source.and_then(|source| source.uses.as_deref()) else {
        return ChunkGlobalResolution {
            status: GlobalResolutionStatus::Incomplete,
            required_use_count: None,
            preserved_use_count: 0,
            elided_use_count: 0,
            findings: vec![chunk_finding(
                GlobalResolutionReason::MissingManifest,
                Vec::new(),
            )],
        };
    };
    let mut report = ChunkGlobalResolution {
        status: GlobalResolutionStatus::Preserved,
        required_use_count: Some(manifest.uses.values().map(Vec::len).sum()),
        preserved_use_count: 0,
        elided_use_count: 0,
        findings: Vec::new(),
    };
    let mut changed = false;
    let mut incomplete = false;
    match finals.entries.len() {
        0 => {
            incomplete = true;
            report.findings.push(chunk_finding(
                GlobalResolutionReason::MissingFinalEntry,
                Vec::new(),
            ));
        }
        1 => {}
        _ => {
            incomplete = true;
            report.findings.push(chunk_finding(
                GlobalResolutionReason::DuplicateFinalEntry,
                finals.entries.clone(),
            ));
        }
    }
    if !finals.changed_raw_entries.is_empty() {
        incomplete = true;
        report.findings.push(chunk_finding(
            GlobalResolutionReason::RawSourceUnproven,
            finals.changed_raw_entries.clone(),
        ));
    }
    // An unchanged raw entry is the prepared input itself: every site keeps
    // its original scope, including any `with`/`eval` scope.
    let unchanged_raw = finals.entries.len() == 1 && finals.unchanged_raw_entries == 1;
    if manifest.dynamic_scope && finals.transformed_entries > 0 {
        incomplete = true;
        report.findings.push(chunk_finding(
            GlobalResolutionReason::DynamicScope,
            Vec::new(),
        ));
    }
    let elided = elisions
        .into_iter()
        .flat_map(|elisions| elisions.uses.values().flatten())
        .map(|span| (span.lo, span.hi))
        .collect::<BTreeSet<_>>();

    for ((symbol, _), spans) in &manifest.uses {
        for span in spans {
            let found = finals
                .uses
                .get(&(span.lo, span.hi))
                .map(Vec::as_slice)
                .unwrap_or_default();
            let site_finding = |reason, files: Vec<String>| GlobalResolutionFinding {
                reason,
                symbol: Some(symbol.to_string()),
                span: Some((*span).into()),
                output_files: files,
            };
            let files = || found.iter().map(|use_| use_.output_file.clone()).collect();
            if elided.contains(&(span.lo, span.hi)) {
                if found.is_empty() {
                    report.elided_use_count += 1;
                } else {
                    incomplete = true;
                    report.findings.push(site_finding(
                        GlobalResolutionReason::ElisionConflict,
                        files(),
                    ));
                }
                continue;
            }
            match found {
                [] if unchanged_raw => report.preserved_use_count += 1,
                [] => {
                    incomplete = true;
                    report.findings.push(site_finding(
                        GlobalResolutionReason::MissingFinalUse,
                        Vec::new(),
                    ));
                }
                [final_use] if final_use.symbol != symbol.as_ref() => {
                    changed = true;
                    report
                        .findings
                        .push(site_finding(GlobalResolutionReason::Renamed, files()));
                }
                [final_use] if !final_use.unresolved => {
                    changed = true;
                    report.findings.push(site_finding(
                        GlobalResolutionReason::CapturedByBinding,
                        files(),
                    ));
                }
                [_] => report.preserved_use_count += 1,
                _ => {
                    incomplete = true;
                    report.findings.push(site_finding(
                        GlobalResolutionReason::DuplicateFinalUse,
                        files(),
                    ));
                }
            }
        }
    }
    report.status = if changed {
        GlobalResolutionStatus::Changed
    } else if incomplete {
        GlobalResolutionStatus::Incomplete
    } else {
        GlobalResolutionStatus::Preserved
    };
    report
}

fn chunk_finding(
    reason: GlobalResolutionReason,
    output_files: Vec<String>,
) -> GlobalResolutionFinding {
    GlobalResolutionFinding {
        reason,
        symbol: None,
        span: None,
        output_files,
    }
}

struct ObservedIdent {
    span: Span,
    symbol: String,
    unresolved: bool,
}

/// Resolve one emitted module from scratch: clear every syntax context so no
/// binding decision survives from lowering, run the resolver, and report each
/// lexical identifier reference with a real span. Must run inside `GLOBALS`.
fn resolve_final_idents(module: &swc_ecma_ast::Module) -> Vec<ObservedIdent> {
    let mut module = module.clone();
    module.visit_mut_with(&mut ClearSyntaxContexts);
    let unresolved_mark = Mark::new();
    let top_level_mark = Mark::new();
    module.visit_mut_with(&mut resolver(unresolved_mark, top_level_mark, true));
    let mut collector = LexicalIdentCollector {
        unresolved: SyntaxContext::empty().apply_mark(unresolved_mark),
        idents: Vec::new(),
    };
    module.visit_with(&mut collector);
    collector.idents
}

struct ClearSyntaxContexts;

impl VisitMut for ClearSyntaxContexts {
    fn visit_mut_syntax_context(&mut self, context: &mut SyntaxContext) {
        *context = SyntaxContext::empty();
    }
}

/// Lexical identifier references, excluding module-interface names and
/// statement labels (the same exclusions as the prepared inventory).
struct LexicalIdentCollector {
    unresolved: SyntaxContext,
    idents: Vec<ObservedIdent>,
}

impl Visit for LexicalIdentCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        if ident.span.is_dummy() {
            return;
        }
        self.idents.push(ObservedIdent {
            span: ident.span,
            symbol: ident.sym.to_string(),
            unresolved: ident.ctxt == self.unresolved,
        });
    }

    fn visit_import_decl(&mut self, _import: &ImportDecl) {}

    fn visit_named_export(&mut self, export: &NamedExport) {
        if export.src.is_none() && !export.type_only {
            for specifier in &export.specifiers {
                if let ExportSpecifier::Named(named) = specifier
                    && !named.is_type_only
                {
                    named.orig.visit_with(self);
                }
            }
        }
    }

    fn visit_export_all(&mut self, _export: &ExportAll) {}

    fn visit_labeled_stmt(&mut self, labeled: &LabeledStmt) {
        labeled.body.visit_with(self);
    }

    fn visit_break_stmt(&mut self, _break_stmt: &BreakStmt) {}

    fn visit_continue_stmt(&mut self, _continue_stmt: &ContinueStmt) {}
}
