//! Final-candidate binding correspondence for source-level unresolved uses.
//!
//! The source inventory comes from the one prepared parse. This component
//! checks each exact source `Id`/span against final emitted syntax after all
//! artifact rewrites and pruning. It proves only binding correspondence; it
//! does not promote unresolved host reads into value or effect facts.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use artifact::reports::ParsedSourceImports;
use artifact::{ArtifactIndexes, ChunkBundle, FileRole, JsFileBody};
use graph_provenance::{ChunkSource, GraphProvenance, InputPath, sha256};
use js_ast::{GlobalUseElisionReceipt, ParsedGlobalUses};
use serde::Serialize;
use source_identity::{ChunkId, ChunkTable};
use swc_common::{GLOBALS, Globals, Mark, SyntaxContext};
use swc_ecma_ast::{
    BreakStmt, ContinueStmt, ExportAll, ExportSpecifier, Id, Ident, ImportDecl, LabeledStmt,
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

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalResolutionReason {
    CandidateNotMaterialized,
    CandidatePreparationUnproven,
    MissingManifest,
    UnmatchedMaterializedChunk,
    DynamicScope,
    MissingFinalUse,
    DuplicateFinalUse,
    DuplicateSourceUse,
    BindingChanged,
    RawSourceUnproven,
    SourceIdentityMismatch,
    MissingFinalEntry,
    DuplicateFinalEntry,
    UnknownElisionChunk,
    ElisionWithoutManifest,
    ElisionSourceMismatch,
    UnknownElisionUse,
    DuplicateElisionUse,
    ConflictingElisionAndCandidate,
    CandidateTraversalChanged,
    SourceCorrespondenceMissing,
    SourceCorrespondenceIncomplete,
    SourceRequestUnmatched,
    SourceRequestChanged,
    SourceRequestTargetUnproved,
    UnplannedSourceRequest,
    ModuleCellRouteUnproved,
    InitializationOrderUnproved,
    ProviderRouteUnproved,
    HostRouteUnproved,
}

#[derive(Debug, Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct GlobalResolutionSpan {
    pub lo: u32,
    pub hi: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct GlobalResolutionFinding {
    /// Present for a source-site finding; absent for chunk/app-level evidence gaps.
    pub source_file: Option<String>,
    pub symbol: Option<String>,
    pub span: Option<GlobalResolutionSpan>,
    pub reason: GlobalResolutionReason,
    pub candidate_files: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GlobalResolutionElision {
    pub source_file: String,
    pub symbol: String,
    pub span: GlobalResolutionSpan,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkGlobalResolutionAssessment {
    pub status: GlobalResolutionStatus,
    /// `None` means that the complete required-use inventory was unavailable.
    pub required_use_count: Option<usize>,
    /// Source sites found unresolved in final AST or proven in exact raw bytes.
    pub preserved_use_count: usize,
    /// Exact source sites discharged by successful checked replacement receipts.
    /// These were intentionally removed and are not counted as final AST uses.
    pub elided_use_count: usize,
    pub elided_uses: Vec<GlobalResolutionElision>,
    pub findings: Vec<GlobalResolutionFinding>,
}

impl ChunkGlobalResolutionAssessment {
    pub fn incomplete(reason: GlobalResolutionReason) -> Self {
        Self {
            status: GlobalResolutionStatus::Incomplete,
            required_use_count: None,
            preserved_use_count: 0,
            elided_use_count: 0,
            elided_uses: Vec::new(),
            findings: vec![finding(reason, None, None, None, Vec::new())],
        }
    }

    fn incomplete_with_count(
        required_use_count: Option<usize>,
        reason: GlobalResolutionReason,
    ) -> Self {
        let mut assessment = Self::incomplete(reason);
        assessment.required_use_count = required_use_count;
        assessment
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalGlobalResolutionAssessment {
    pub status: GlobalResolutionStatus,
    /// The exact source-chunk inventory the caller required this result to cover.
    pub checked_chunks: BTreeSet<String>,
    /// A scoped result for every checked chunk, including incomplete rows.
    pub chunks: BTreeMap<String, ChunkGlobalResolutionAssessment>,
    /// App-scoped evidence gaps that cannot be assigned to a checked chunk.
    pub findings: Vec<GlobalResolutionFinding>,
    /// Final source-request/binding routes checked against the exact emitted
    /// AST and resolver. `None` denotes the pre-route-check assessment stage.
    pub source_correspondence_status: Option<GlobalResolutionStatus>,
    pub source_correspondence_findings: Vec<GlobalResolutionFinding>,
}

impl FinalGlobalResolutionAssessment {
    pub fn from_chunks(
        required_chunks: &BTreeSet<String>,
        chunks: BTreeMap<String, ChunkGlobalResolutionAssessment>,
    ) -> Self {
        Self::from_chunks_and_findings(required_chunks, chunks, Vec::new())
    }

    fn from_chunks_and_findings(
        required_chunks: &BTreeSet<String>,
        mut chunks: BTreeMap<String, ChunkGlobalResolutionAssessment>,
        findings: Vec<GlobalResolutionFinding>,
    ) -> Self {
        chunks.retain(|chunk, _| required_chunks.contains(chunk));
        for chunk in required_chunks {
            chunks.entry(chunk.clone()).or_insert_with(|| {
                ChunkGlobalResolutionAssessment::incomplete(GlobalResolutionReason::MissingManifest)
            });
        }
        let mut status = aggregate_status(chunks.values().map(|assessment| assessment.status));
        if status == GlobalResolutionStatus::Preserved && !findings.is_empty() {
            status = GlobalResolutionStatus::Incomplete;
        }
        Self {
            status,
            checked_chunks: required_chunks.clone(),
            chunks,
            findings,
            source_correspondence_status: None,
            source_correspondence_findings: Vec::new(),
        }
    }

    pub(crate) fn attach_source_correspondence(
        &mut self,
        result: &super::final_source_correspondence::FinalSourceCorrespondenceResult,
        source_chunk_names: &BTreeMap<ChunkId, String>,
    ) {
        self.source_correspondence_status = Some(result.status);
        for (chunk_id, status) in &result.per_chunk_status {
            let Some(name) = source_chunk_names.get(chunk_id) else {
                continue;
            };
            if let Some(chunk) = self.chunks.get_mut(name) {
                chunk.status = combine_global_resolution_status(chunk.status, *status);
                if let Some(findings) = result.findings.get(chunk_id) {
                    chunk.findings.extend(findings.iter().cloned());
                }
            } else if let Some(findings) = result.findings.get(chunk_id) {
                self.source_correspondence_findings
                    .extend(findings.iter().cloned());
            }
        }
        self.source_correspondence_findings.extend(
            result
                .findings
                .iter()
                .filter(|(chunk, _)| !source_chunk_names.contains_key(chunk))
                .flat_map(|(_, findings)| findings.iter().cloned()),
        );
        self.status = combine_global_resolution_status(self.status, result.status);
    }

    pub fn candidate_not_materialized(source_inputs: &FinalGlobalResolutionSourceInputs) -> Self {
        let required_names = source_inputs
            .required_source_chunks
            .iter()
            .filter_map(|chunk_id| source_inputs.source_chunk_names.get(chunk_id).cloned())
            .collect::<BTreeSet<_>>();
        let chunks = source_inputs
            .required_source_chunks
            .iter()
            .filter_map(|chunk_id| {
                let chunk = source_inputs.source_chunk_names.get(chunk_id)?.clone();
                let required_use_count = source_inputs
                    .source_uses
                    .get(chunk_id)
                    .map(|manifest| manifest.uses.values().map(Vec::len).sum());
                Some((
                    chunk.clone(),
                    ChunkGlobalResolutionAssessment::incomplete_with_count(
                        required_use_count,
                        if required_use_count.is_some() {
                            GlobalResolutionReason::CandidateNotMaterialized
                        } else {
                            GlobalResolutionReason::MissingManifest
                        },
                    ),
                ))
            })
            .collect();
        Self::from_chunks(&required_names, chunks)
    }

    pub fn candidate_preparation_unproven(
        source_inputs: &FinalGlobalResolutionSourceInputs,
    ) -> Self {
        let required_names = source_inputs
            .required_source_chunks
            .iter()
            .filter_map(|chunk_id| source_inputs.source_chunk_names.get(chunk_id).cloned())
            .collect::<BTreeSet<_>>();
        let chunks = required_names
            .iter()
            .cloned()
            .map(|name| {
                (
                    name,
                    ChunkGlobalResolutionAssessment::incomplete(
                        GlobalResolutionReason::CandidatePreparationUnproven,
                    ),
                )
            })
            .collect();
        Self::from_chunks(&required_names, chunks)
    }
}

/// Exact original-input identity required to certify a final raw-source body.
/// The canonical pipeline constructs this only from captured graph provenance.
#[derive(Debug, Clone)]
pub struct RawSourceIdentityProof {
    chunk_id: ChunkId,
    source_file: String,
    source_path: std::path::PathBuf,
    input_file: InputPath,
    sha256: String,
}

impl RawSourceIdentityProof {
    /// Preserve the graph's exact parser-input identity alongside the source
    /// map name from its canonical parsed-use manifest.
    pub fn from_chunk_source(
        chunk_id: ChunkId,
        source: &ChunkSource,
        source_file: impl Into<String>,
    ) -> Self {
        Self {
            chunk_id,
            source_file: source_file.into(),
            source_path: source.source_path.clone(),
            input_file: source.input_file.clone(),
            sha256: source.sha256.clone(),
        }
    }

    pub(crate) fn chunk_id(&self) -> ChunkId {
        self.chunk_id
    }

    pub(crate) fn source_path(&self) -> &std::path::Path {
        &self.source_path
    }

    pub(crate) fn matches_bytes(&self, bytes: &[u8]) -> bool {
        !self.input_file.path.as_os_str().is_empty() && self.sha256 == sha256(bytes)
    }
}

/// Snapshot the canonical per-source manifests and their captured raw-input
/// identities before the prepared artifact and provenance are consumed.
/// `emission_scope` is the explicit selected set when nonempty materialization
/// prunes other chunks; `None` means every source chunk may be emitted.
#[derive(Debug, Clone, Default)]
pub struct FinalGlobalResolutionSourceInputs {
    pub required_source_chunks: BTreeSet<ChunkId>,
    pub source_uses: BTreeMap<ChunkId, Arc<ParsedGlobalUses>>,
    /// Canonical import, re-export, and local-export identities retained from
    /// the same source parse as the candidate preparation. `Some(empty)` is a
    /// complete file with no such requests; `None` is unavailable evidence.
    pub source_imports: BTreeMap<ChunkId, Option<Arc<ParsedSourceImports>>>,
    pub raw_source_proofs: BTreeMap<ChunkId, RawSourceIdentityProof>,
    /// Stable human-readable projections used only when building the wire
    /// report. Assessment joins retain `ChunkId` throughout.
    pub source_chunk_names: BTreeMap<ChunkId, String>,
    /// Exact source entry-file path used as the importer context for original
    /// static requests. This is captured from the typed artifact, not rebuilt
    /// from report strings.
    pub source_entry_files: BTreeMap<ChunkId, String>,
    /// Canonical input resolver and chunk table captured before lowering.
    /// The joint final-output checker uses these to resolve each original
    /// static request without rebuilding identity from its wire spelling.
    pub source_indexes: Option<ArtifactIndexes>,
    pub source_chunk_table: Option<ChunkTable>,
    /// Non-serialized commitment to the captured source inventory. Public
    /// report fields remain inspectable, but cannot be edited and then reused
    /// as the source context for a different candidate assessment.
    context_identity: Option<String>,
}

impl FinalGlobalResolutionSourceInputs {
    pub fn capture(
        source_artifact: &ChunkBundle,
        source_indexes: &ArtifactIndexes,
        excluded_chunks: &BTreeSet<ChunkId>,
        emission_scope: Option<&BTreeSet<ChunkId>>,
        provenance: &GraphProvenance,
    ) -> Self {
        let mut snapshot = Self::default();
        snapshot.source_indexes = Some(source_indexes.clone());
        snapshot.source_chunk_table = Some(source_artifact.chunk_table.clone());
        for chunk in &source_artifact.chunks {
            if excluded_chunks.contains(&chunk.chunk_id)
                || emission_scope.is_some_and(|scope| !scope.contains(&chunk.chunk_id))
            {
                continue;
            }
            let chunk_name = source_artifact.chunk_table.name(chunk.chunk_id).to_string();
            snapshot.required_source_chunks.insert(chunk.chunk_id);
            snapshot
                .source_chunk_names
                .insert(chunk.chunk_id, chunk_name.clone());
            snapshot
                .source_entry_files
                .insert(chunk.chunk_id, chunk.js.entry_file.clone());
            snapshot.source_imports.insert(
                chunk.chunk_id,
                chunk
                    .analysis
                    .runtime_facts
                    .parsed_source_imports
                    .clone()
                    .map(Arc::new),
            );
            let Some(manifest) = chunk.analysis.runtime_facts.global_uses.clone() else {
                continue;
            };
            snapshot
                .source_uses
                .insert(chunk.chunk_id, manifest.clone());
            let mut chunk_sources = provenance
                .chunk_sources
                .iter()
                .filter(|source| source.chunk_id == chunk_name);
            if let (Some(source), None) = (chunk_sources.next(), chunk_sources.next()) {
                snapshot.raw_source_proofs.insert(
                    chunk.chunk_id,
                    RawSourceIdentityProof::from_chunk_source(
                        chunk.chunk_id,
                        source,
                        manifest.source_file.clone(),
                    ),
                );
            }
        }
        snapshot.context_identity = Some(snapshot.compute_context_identity());
        snapshot
    }

    pub(crate) fn context_identity(&self) -> Option<&str> {
        self.context_identity.as_deref()
    }

    pub(crate) fn has_unchanged_context(&self) -> bool {
        self.context_identity
            .as_deref()
            .is_some_and(|identity| identity == self.compute_context_identity())
    }

    /// Compare two complete, internally committed source snapshots without
    /// exposing the identity commitment itself to downstream crates.
    pub fn same_context_as(&self, other: &Self) -> bool {
        self.has_unchanged_context()
            && other.has_unchanged_context()
            && self.context_identity == other.context_identity
    }

    /// Verify that the public source snapshot names the same canonical
    /// artifact entries and parse manifests supplied to materialization.
    /// Provenance-backed captures are additionally compared with a fresh
    /// `capture` at the caller boundary.
    pub fn matches_artifact(
        &self,
        artifact: &ChunkBundle,
        indexes: &ArtifactIndexes,
        required_chunks: &BTreeSet<ChunkId>,
    ) -> bool {
        if !self.has_unchanged_context()
            || &self.required_source_chunks != required_chunks
            || self
                .source_indexes
                .as_ref()
                .map(|value| format!("{value:?}"))
                != Some(format!("{indexes:?}"))
            || self
                .source_chunk_table
                .as_ref()
                .map(|value| format!("{value:?}"))
                != Some(format!("{:?}", artifact.chunk_table))
        {
            return false;
        }
        for chunk in &artifact.chunks {
            if !required_chunks.contains(&chunk.chunk_id) {
                continue;
            }
            let name = artifact.chunk_table.name(chunk.chunk_id);
            if self
                .source_chunk_names
                .get(&chunk.chunk_id)
                .map(String::as_str)
                != Some(name)
                || self.source_entry_files.get(&chunk.chunk_id) != Some(&chunk.js.entry_file)
                || self.source_uses.get(&chunk.chunk_id)
                    != chunk.analysis.runtime_facts.global_uses.as_ref()
                || self
                    .source_imports
                    .get(&chunk.chunk_id)
                    .and_then(Option::as_deref)
                    != chunk.analysis.runtime_facts.parsed_source_imports.as_ref()
            {
                return false;
            }
        }
        true
    }

    fn compute_context_identity(&self) -> String {
        let mut fields = Vec::new();
        for chunk in &self.required_source_chunks {
            fields.push(format!("chunk:{}", chunk.0));
            fields.push(format!(
                "name:{}",
                self.source_chunk_names
                    .get(chunk)
                    .map(String::as_str)
                    .unwrap_or("<missing>")
            ));
            fields.push(format!(
                "entry:{}",
                self.source_entry_files
                    .get(chunk)
                    .map(String::as_str)
                    .unwrap_or("<missing>")
            ));
            if let Some(manifest) = self.source_uses.get(chunk) {
                fields.push(format!("source-file:{}", manifest.source_file));
                fields.push(format!("dynamic:{}", manifest.dynamic_scope));
                for ((symbol, context), spans) in &manifest.uses {
                    fields.push(format!("use:{symbol}:{context:?}"));
                    for span in spans {
                        fields.push(format!("span:{}:{}", span.lo.0, span.hi.0));
                    }
                }
            } else {
                fields.push("source-uses:missing".to_owned());
            }
            fields.push(format!(
                "source-imports:{:?}",
                self.source_imports.get(chunk)
            ));
            if let Some(proof) = self.raw_source_proofs.get(chunk) {
                fields.push(format!("raw-source:{}", proof.source_path.display()));
                fields.push(format!("raw-file:{}", proof.input_file.path.display()));
                fields.push(format!("raw-digest:{}", proof.sha256));
                fields.push(format!("raw-source-file:{}", proof.source_file));
            } else {
                fields.push("raw-proof:missing".to_owned());
            }
        }
        // These private index structures determine source-request resolution
        // and emitted-module lookup. Their debug representation is only used
        // as a same-process commitment (this snapshot is runtime-only), so it
        // need not be a stable persisted wire format.
        fields.push(format!("source-indexes:{:?}", self.source_indexes));
        fields.push(format!("source-chunk-table:{:?}", self.source_chunk_table));
        sha256(fields.join("\0").as_bytes())
    }
}

#[derive(Debug, Clone, Eq, Ord, PartialEq, PartialOrd)]
struct SiteKey {
    source_file: String,
    id: Id,
    span: GlobalResolutionSpan,
}

#[derive(Debug, Clone)]
struct CandidateUse {
    source_file: String,
    symbol: String,
    unresolved: bool,
    output_file: String,
}

/// Check every source manifest in `required_source_chunks` against the final
/// artifact. `source_uses` is the canonical prepared-parse inventory; source
/// identity is carried by the manifest's file name and exact SWC `Id`/span.
/// `global_use_elisions` contains only successful exact-site strip receipts,
/// keyed by typed source chunk identity. A valid receipt discharges a removed
/// use; it cannot hide a use that remains in the candidate.
pub fn assess_final_global_resolution(
    source_inputs: &FinalGlobalResolutionSourceInputs,
    final_files: &ChunkBundle,
    global_use_elisions: &BTreeMap<ChunkId, GlobalUseElisionReceipt>,
) -> FinalGlobalResolutionAssessment {
    let required_source_chunks = &source_inputs.required_source_chunks;
    let source_uses = &source_inputs.source_uses;
    let raw_source_proofs = &source_inputs.raw_source_proofs;
    let globals = Globals::default();
    let (chunk_reports, app_findings) = GLOBALS.set(&globals, || {
        let mut candidates = BTreeMap::<SiteKey, Vec<CandidateUse>>::new();
        let mut unaddressable_candidates = BTreeMap::<SiteKey, Vec<String>>::new();
        let mut unproven_raw_files = BTreeMap::<ChunkId, Vec<String>>::new();
        let mut mismatched_raw_files = BTreeMap::<ChunkId, Vec<String>>::new();
        let mut exact_raw_files = BTreeMap::<ChunkId, Vec<String>>::new();
        let mut final_entry_files = BTreeMap::<ChunkId, Vec<String>>::new();
        let mut app_findings = Vec::new();

        for chunk in &final_files.chunks {
            let output_chunk = final_files.chunk_table.name(chunk.chunk_id);
            for file in &chunk.js.files {
                let output_file = format!("{output_chunk}/{}", file.path);
                let source_chunk = file.metadata.chunk_id.as_str();
                let source_chunk_id = final_files.chunk_table.get(source_chunk);
                let is_required =
                    source_chunk_id.is_some_and(|id| required_source_chunks.contains(&id));
                let is_entry = file.metadata.role == FileRole::Entry;
                if is_required && is_entry {
                    final_entry_files
                        .entry(source_chunk_id.expect("required source chunk has an id"))
                        .or_default()
                        .push(output_file.clone());
                }
                match &file.body {
                    JsFileBody::Source(source) => {
                        if !is_required || !is_entry {
                            continue;
                        }
                        let source_chunk_id =
                            source_chunk_id.expect("required source chunk has an id");
                        let Some(proof) = raw_source_proofs.get(&source_chunk_id) else {
                            unproven_raw_files
                                .entry(source_chunk_id)
                                .or_default()
                                .push(output_file);
                            continue;
                        };
                        let identity_matches = proof.chunk_id == source_chunk_id
                            && proof.source_path.to_string_lossy().as_ref()
                                == file.metadata.source_path
                            && source_uses
                                .get(&source_chunk_id)
                                .is_some_and(|manifest| manifest.source_file == proof.source_file);
                        let bytes_match = !proof.input_file.path.as_os_str().is_empty()
                            && proof.sha256 == sha256(source.as_bytes());
                        if identity_matches && bytes_match {
                            exact_raw_files
                                .entry(source_chunk_id)
                                .or_default()
                                .push(output_file);
                        } else if !identity_matches {
                            mismatched_raw_files
                                .entry(source_chunk_id)
                                .or_default()
                                .push(output_file);
                        } else {
                            unproven_raw_files
                                .entry(source_chunk_id)
                                .or_default()
                                .push(output_file);
                        }
                    }
                    JsFileBody::SourceWithAst(_) | JsFileBody::Ast(_) => {
                        let Some(parsed) = file.ast() else {
                            if is_required && is_entry {
                                unproven_raw_files
                                    .entry(
                                        source_chunk_id.expect("required source chunk has an id"),
                                    )
                                    .or_default()
                                    .push(output_file);
                            }
                            continue;
                        };
                        let source_file = parsed.source_file_name();
                        let mut module = parsed.module.clone();
                        let mut source_idents = Vec::new();
                        module.visit_with(&mut CandidateUseCollector {
                            idents: &mut source_idents,
                        });
                        module.visit_mut_with(&mut ClearSyntaxContexts);
                        let unresolved_mark = Mark::new();
                        let top_level_mark = Mark::new();
                        module.visit_mut_with(&mut resolver(unresolved_mark, top_level_mark, true));
                        let unresolved_context = SyntaxContext::empty().apply_mark(unresolved_mark);
                        let mut resolved_idents = Vec::new();
                        module.visit_with(&mut CandidateUseCollector {
                            idents: &mut resolved_idents,
                        });

                        // Resolver changes contexts but not syntax shape. If a
                        // future pass violates that property, all affected
                        // source sites become unaddressable and remain incomplete.
                        if source_idents.len() == resolved_idents.len() {
                            for (source, resolved) in source_idents.into_iter().zip(resolved_idents)
                            {
                                if source.span != resolved.span {
                                    unaddressable_candidates
                                        .entry(SiteKey {
                                            source_file: source_file.clone(),
                                            id: source.id,
                                            span: source.span,
                                        })
                                        .or_default()
                                        .push(output_file.clone());
                                    app_findings.push(finding(
                                        GlobalResolutionReason::CandidateTraversalChanged,
                                        None,
                                        None,
                                        Some(source.span),
                                        vec![output_file.clone()],
                                    ));
                                    continue;
                                }
                                candidates
                                    .entry(SiteKey {
                                        source_file: source_file.clone(),
                                        id: source.id,
                                        span: source.span,
                                    })
                                    .or_default()
                                    .push(CandidateUse {
                                        source_file: source_file.clone(),
                                        symbol: resolved.symbol,
                                        unresolved: resolved.ctxt == unresolved_context,
                                        output_file: output_file.clone(),
                                    });
                            }
                        } else {
                            app_findings.push(finding(
                                GlobalResolutionReason::CandidateTraversalChanged,
                                None,
                                None,
                                None,
                                vec![output_file.clone()],
                            ));
                            for source in source_idents {
                                unaddressable_candidates
                                    .entry(SiteKey {
                                        source_file: source_file.clone(),
                                        id: source.id,
                                        span: source.span,
                                    })
                                    .or_default()
                                    .push(output_file.clone());
                            }
                        }
                    }
                }
            }
        }

        for (chunk_id, receipt) in global_use_elisions {
            if !required_source_chunks.contains(chunk_id) {
                app_findings.push(finding(
                    GlobalResolutionReason::UnknownElisionChunk,
                    Some(receipt.source_file.clone()),
                    None,
                    None,
                    vec![
                        source_inputs
                            .source_chunk_names
                            .get(chunk_id)
                            .cloned()
                            .unwrap_or_else(|| format!("chunk#{}", chunk_id.0)),
                    ],
                ));
            }
        }

        let mut reports = BTreeMap::new();
        for chunk_id in required_source_chunks {
            let report_chunk_name = source_inputs
                .source_chunk_names
                .get(chunk_id)
                .cloned()
                .unwrap_or_else(|| format!("chunk#{}", chunk_id.0));
            let receipt = global_use_elisions.get(chunk_id);
            let Some(manifest) = source_uses.get(chunk_id) else {
                let mut report = ChunkGlobalResolutionAssessment::incomplete(
                    GlobalResolutionReason::MissingManifest,
                );
                if let Some(receipt) = receipt {
                    report.findings.push(finding(
                        GlobalResolutionReason::ElisionWithoutManifest,
                        Some(receipt.source_file.clone()),
                        None,
                        None,
                        vec![report_chunk_name.clone()],
                    ));
                }
                reports.insert(report_chunk_name, report);
                continue;
            };

            let required_use_count = manifest.uses.values().map(Vec::len).sum();
            let mut report = ChunkGlobalResolutionAssessment {
                status: GlobalResolutionStatus::Preserved,
                required_use_count: Some(required_use_count),
                preserved_use_count: 0,
                elided_use_count: 0,
                elided_uses: Vec::new(),
                findings: Vec::new(),
            };
            let mut changed = false;
            let mut incomplete = false;
            match final_entry_files.get(chunk_id).map(Vec::len) {
                None | Some(0) => {
                    incomplete = true;
                    report.findings.push(finding(
                        GlobalResolutionReason::MissingFinalEntry,
                        Some(manifest.source_file.clone()),
                        None,
                        None,
                        Vec::new(),
                    ));
                }
                Some(1) => {}
                Some(_) => {
                    incomplete = true;
                    report.findings.push(finding(
                        GlobalResolutionReason::DuplicateFinalEntry,
                        Some(manifest.source_file.clone()),
                        None,
                        None,
                        final_entry_files.get(chunk_id).cloned().unwrap_or_default(),
                    ));
                }
            }
            // Static Id/span correspondence cannot account for names that
            // eval or with resolve through the surrounding lexical scope.
            // A unique raw entry with the captured source identity and exact
            // input digest preserves that scope unchanged; transformed ASTs
            // need a separate proof and remain incomplete here.
            let unchanged_raw_entry = final_entry_files
                .get(chunk_id)
                .is_some_and(|entries| entries.len() == 1)
                && exact_raw_files
                    .get(chunk_id)
                    .is_some_and(|files| files.len() == 1);
            if manifest.dynamic_scope && !unchanged_raw_entry {
                incomplete = true;
                report.findings.push(finding(
                    GlobalResolutionReason::DynamicScope,
                    Some(manifest.source_file.clone()),
                    None,
                    None,
                    Vec::new(),
                ));
            }

            let mut source_sites = BTreeMap::<SiteKey, usize>::new();
            for (id, spans) in &manifest.uses {
                for source_span in spans {
                    *source_sites
                        .entry(SiteKey {
                            source_file: manifest.source_file.clone(),
                            id: id.clone(),
                            span: span_key(*source_span),
                        })
                        .or_default() += 1;
                }
            }
            for (site, count) in &source_sites {
                if *count > 1 {
                    incomplete = true;
                    report.findings.push(finding(
                        GlobalResolutionReason::DuplicateSourceUse,
                        Some(manifest.source_file.clone()),
                        Some(site.id.0.to_string()),
                        Some(site.span),
                        Vec::new(),
                    ));
                }
            }

            let mut valid_elisions = BTreeSet::new();
            if let Some(receipt) = receipt {
                if receipt.source_file != manifest.source_file {
                    incomplete = true;
                    report.findings.push(finding(
                        GlobalResolutionReason::ElisionSourceMismatch,
                        Some(receipt.source_file.clone()),
                        None,
                        None,
                        vec![report_chunk_name.clone()],
                    ));
                } else {
                    let mut seen_elisions = BTreeSet::new();
                    for (id, spans) in &receipt.uses {
                        for receipt_span in spans {
                            let site = SiteKey {
                                source_file: manifest.source_file.clone(),
                                id: id.clone(),
                                span: span_key(*receipt_span),
                            };
                            if !seen_elisions.insert(site.clone()) {
                                incomplete = true;
                                report.findings.push(finding(
                                    GlobalResolutionReason::DuplicateElisionUse,
                                    Some(manifest.source_file.clone()),
                                    Some(id.0.to_string()),
                                    Some(site.span),
                                    Vec::new(),
                                ));
                                continue;
                            }
                            match source_sites.get(&site) {
                                Some(1) => {
                                    valid_elisions.insert(site);
                                }
                                Some(_) => {}
                                None => {
                                    incomplete = true;
                                    report.findings.push(finding(
                                        GlobalResolutionReason::UnknownElisionUse,
                                        Some(manifest.source_file.clone()),
                                        Some(id.0.to_string()),
                                        Some(site.span),
                                        Vec::new(),
                                    ));
                                }
                            }
                        }
                    }
                }
            }

            for (site, count) in source_sites {
                if count != 1 {
                    continue;
                }
                let symbol = site.id.0.to_string();
                let source_use_span = site.span;
                let mut found = candidates.get(&site).cloned().unwrap_or_default();
                if let Some(raw_files) = exact_raw_files.get(chunk_id) {
                    found.extend(raw_files.iter().map(|output_file| CandidateUse {
                        source_file: manifest.source_file.clone(),
                        symbol: symbol.clone(),
                        unresolved: true,
                        output_file: output_file.clone(),
                    }));
                }

                if let Some(files) = unaddressable_candidates.get(&site)
                    && !files.is_empty()
                {
                    incomplete = true;
                    report.findings.push(finding(
                        GlobalResolutionReason::CandidateTraversalChanged,
                        Some(manifest.source_file.clone()),
                        Some(symbol),
                        Some(source_use_span),
                        files.clone(),
                    ));
                    continue;
                }

                if valid_elisions.contains(&site) {
                    if found.is_empty() {
                        report.elided_use_count += 1;
                        report.elided_uses.push(GlobalResolutionElision {
                            source_file: manifest.source_file.clone(),
                            symbol,
                            span: source_use_span,
                        });
                    } else {
                        incomplete = true;
                        report.findings.push(finding(
                            GlobalResolutionReason::ConflictingElisionAndCandidate,
                            Some(manifest.source_file.clone()),
                            Some(symbol),
                            Some(source_use_span),
                            found
                                .iter()
                                .map(|candidate| candidate.output_file.clone())
                                .collect(),
                        ));
                    }
                    continue;
                }

                match found.as_slice() {
                    [candidate]
                        if candidate.source_file == manifest.source_file
                            && candidate.symbol == symbol
                            && candidate.unresolved =>
                    {
                        report.preserved_use_count += 1;
                    }
                    [candidate] => {
                        changed = true;
                        report.findings.push(finding(
                            GlobalResolutionReason::BindingChanged,
                            Some(manifest.source_file.clone()),
                            Some(symbol),
                            Some(source_use_span),
                            vec![candidate.output_file.clone()],
                        ));
                    }
                    [] => {
                        incomplete = true;
                        let (reason, files) = unaddressable_candidates
                            .get(&site)
                            .map(|files| {
                                (
                                    GlobalResolutionReason::CandidateTraversalChanged,
                                    files.clone(),
                                )
                            })
                            .unwrap_or((GlobalResolutionReason::MissingFinalUse, Vec::new()));
                        report.findings.push(finding(
                            reason,
                            Some(manifest.source_file.clone()),
                            Some(symbol),
                            Some(source_use_span),
                            files,
                        ));
                    }
                    many => {
                        incomplete = true;
                        report.findings.push(finding(
                            GlobalResolutionReason::DuplicateFinalUse,
                            Some(manifest.source_file.clone()),
                            Some(symbol),
                            Some(source_use_span),
                            many.iter()
                                .map(|candidate| candidate.output_file.clone())
                                .collect(),
                        ));
                    }
                }
            }

            if let Some(files) = unproven_raw_files.get(chunk_id)
                && !files.is_empty()
            {
                incomplete = true;
                report.findings.push(finding(
                    GlobalResolutionReason::RawSourceUnproven,
                    Some(manifest.source_file.clone()),
                    None,
                    None,
                    files.clone(),
                ));
            }
            if let Some(files) = mismatched_raw_files.get(chunk_id)
                && !files.is_empty()
            {
                incomplete = true;
                report.findings.push(finding(
                    GlobalResolutionReason::SourceIdentityMismatch,
                    Some(manifest.source_file.clone()),
                    None,
                    None,
                    files.clone(),
                ));
            }
            report.status = if changed {
                GlobalResolutionStatus::Changed
            } else if incomplete {
                GlobalResolutionStatus::Incomplete
            } else {
                GlobalResolutionStatus::Preserved
            };
            reports.insert(report_chunk_name, report);
        }
        (reports, app_findings)
    });

    let required_chunk_names = source_inputs
        .required_source_chunks
        .iter()
        .filter_map(|chunk_id| source_inputs.source_chunk_names.get(chunk_id).cloned())
        .collect::<BTreeSet<_>>();
    FinalGlobalResolutionAssessment::from_chunks_and_findings(
        &required_chunk_names,
        chunk_reports,
        app_findings,
    )
}

fn span_key(span: swc_common::Span) -> GlobalResolutionSpan {
    GlobalResolutionSpan {
        lo: span.lo.0,
        hi: span.hi.0,
    }
}

fn finding(
    reason: GlobalResolutionReason,
    source_file: Option<String>,
    symbol: Option<String>,
    span: Option<GlobalResolutionSpan>,
    candidate_files: Vec<String>,
) -> GlobalResolutionFinding {
    GlobalResolutionFinding {
        source_file,
        symbol,
        span,
        reason,
        candidate_files,
    }
}

fn aggregate_status(
    statuses: impl Iterator<Item = GlobalResolutionStatus>,
) -> GlobalResolutionStatus {
    let mut saw_changed = false;
    let mut saw_incomplete = false;
    for status in statuses {
        match status {
            GlobalResolutionStatus::Preserved => {}
            GlobalResolutionStatus::Changed => saw_changed = true,
            GlobalResolutionStatus::Incomplete => saw_incomplete = true,
        }
    }
    if saw_changed {
        GlobalResolutionStatus::Changed
    } else if saw_incomplete {
        GlobalResolutionStatus::Incomplete
    } else {
        GlobalResolutionStatus::Preserved
    }
}

fn combine_global_resolution_status(
    left: GlobalResolutionStatus,
    right: GlobalResolutionStatus,
) -> GlobalResolutionStatus {
    match (left, right) {
        (GlobalResolutionStatus::Changed, _) | (_, GlobalResolutionStatus::Changed) => {
            GlobalResolutionStatus::Changed
        }
        (GlobalResolutionStatus::Incomplete, _) | (_, GlobalResolutionStatus::Incomplete) => {
            GlobalResolutionStatus::Incomplete
        }
        _ => GlobalResolutionStatus::Preserved,
    }
}

struct ClearSyntaxContexts;

impl VisitMut for ClearSyntaxContexts {
    fn visit_mut_syntax_context(&mut self, context: &mut SyntaxContext) {
        *context = SyntaxContext::empty();
    }
}

#[derive(Clone)]
struct ObservedIdent {
    id: Id,
    span: GlobalResolutionSpan,
    symbol: String,
    ctxt: SyntaxContext,
}

struct CandidateUseCollector<'a> {
    idents: &'a mut Vec<ObservedIdent>,
}

impl Visit for CandidateUseCollector<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        if ident.span.is_dummy() {
            return;
        }
        self.idents.push(ObservedIdent {
            id: ident.to_id(),
            span: span_key(ident.span),
            symbol: ident.sym.to_string(),
            ctxt: ident.ctxt,
        });
    }

    fn visit_import_decl(&mut self, _import: &ImportDecl) {
        // Imported names and local import bindings are module-interface
        // syntax, not lexical reads in the importing chunk.
    }

    fn visit_named_export(&mut self, export: &NamedExport) {
        // Only `orig` in a local runtime export refers to a local binding.
        // Re-export names and `exported` aliases are public interface syntax.
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

#[cfg(test)]
mod tests {
    use super::*;
    use source_identity::ChunkTable;
    use swc_common::GLOBALS;

    #[test]
    fn candidate_uses_exclude_module_interface_names_and_statement_labels() {
        GLOBALS.set(&Default::default(), || {
            let parsed = js_ast::parse_js_module(
                "candidate/module-interfaces.js",
                r#"
import { remoteImport as localImport } from './dep.js';
const local = actualRead;
const shorthand = { shorthandRead };
const imported = localImport;
export { local as publicLocal };
export { plainMissing };
export { missingLocal as publicUnresolved };
export { remoteName as publicReexport } from './reexport.js';
export * as publicNamespace from './namespace.js';
export default defaultRead;
scopeLabel: for (;;) {
    if (condition) continue scopeLabel;
    if (shouldBreak) break scopeLabel;
}
"#,
            )
            .expect("parse");
            let mut module = parsed.module.clone();
            module.visit_mut_with(&mut ClearSyntaxContexts);
            let unresolved_mark = Mark::new();
            let top_level_mark = Mark::new();
            module.visit_mut_with(&mut resolver(unresolved_mark, top_level_mark, true));
            let unresolved_context = SyntaxContext::empty().apply_mark(unresolved_mark);
            let mut idents = Vec::new();
            module.visit_with(&mut CandidateUseCollector {
                idents: &mut idents,
            });
            let names = idents
                .iter()
                .filter(|ident| ident.ctxt == unresolved_context)
                .map(|ident| ident.symbol.clone())
                .collect::<BTreeSet<_>>();

            assert_eq!(
                names,
                BTreeSet::from([
                    "actualRead".to_string(),
                    "condition".to_string(),
                    "defaultRead".to_string(),
                    "missingLocal".to_string(),
                    "plainMissing".to_string(),
                    "shouldBreak".to_string(),
                    "shorthandRead".to_string(),
                ])
            );
            assert!(idents.iter().all(|ident| {
                !matches!(
                    ident.symbol.as_str(),
                    "remoteImport"
                        | "publicLocal"
                        | "publicUnresolved"
                        | "remoteName"
                        | "publicReexport"
                        | "publicNamespace"
                        | "scopeLabel"
                )
            }));
        });
    }

    #[test]
    fn incomplete_inventory_cannot_become_preserved_by_absence() {
        let required = BTreeSet::from(["app/main".to_string()]);
        let assessment = FinalGlobalResolutionAssessment::from_chunks(&required, BTreeMap::new());
        assert_eq!(assessment.checked_chunks, required);
        assert_eq!(assessment.status, GlobalResolutionStatus::Incomplete);
        assert_eq!(
            assessment.chunks["app/main"].findings[0].reason,
            GlobalResolutionReason::MissingManifest
        );
    }

    #[test]
    fn explicit_empty_manifest_without_a_final_entry_is_incomplete() {
        let chunk_id = ChunkId(0);
        let required = BTreeSet::from([chunk_id]);
        let files = ChunkBundle {
            chunks: Vec::new(),
            chunk_table: ChunkTable::default(),
        };
        let source_inputs = FinalGlobalResolutionSourceInputs {
            required_source_chunks: required.clone(),
            source_uses: BTreeMap::from([(
                chunk_id,
                Arc::new(ParsedGlobalUses {
                    source_file: "app/main/entry.js".to_string(),
                    uses: BTreeMap::new(),
                    dynamic_scope: false,
                }),
            )]),
            source_chunk_names: BTreeMap::from([(chunk_id, "app/main".to_string())]),
            ..FinalGlobalResolutionSourceInputs::default()
        };
        let assessment = assess_final_global_resolution(&source_inputs, &files, &BTreeMap::new());
        assert_eq!(
            assessment.checked_chunks,
            BTreeSet::from(["app/main".to_string()])
        );
        assert_eq!(assessment.status, GlobalResolutionStatus::Incomplete);
        assert_eq!(assessment.chunks["app/main"].required_use_count, Some(0));
        assert_eq!(
            assessment.chunks["app/main"].findings[0].reason,
            GlobalResolutionReason::MissingFinalEntry
        );
    }
}
