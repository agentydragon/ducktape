//! Private ownership of the complete post-materialization emission sequence.
//!
//! A draft can only be created by this crate's canonical materializer or
//! pass-through constructor. It owns the mutable emission while the fixed
//! rewrite stages run, then exposes a finalized, still-immutable product for
//! reconstruction's joint checker.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

use anyhow::Result;
use artifact::EmissionFiles;
use artifact::SourceProgram;
use artifact::reports::{
    CandidateSourceCorrespondence, ChunkDecompositionOutput, SelectedModuleLowering,
};
use gate::{
    ApplicationRelocationAssessmentReport, CandidateAssessmentContext,
    CandidateRelocationAssessmentReport, FinalGlobalResolutionSourceInputs,
};
use source_identity::ChunkId;
use vendor::{EmissionRewriteResult, VendorResolutionPlan, apply_emission_rewrites_in_place};

use super::materialize::EmissionChunkOutputs;
use super::naturalize_cross_chunk_imports;

/// A canonical materialization result before whole-program rewrites.
///
/// There is no constructor or mutable file accessor outside this module. The
/// only public operation consumes the draft and runs every final rewrite in
/// the required order.
pub struct CandidateEmissionDraft {
    output: EmissionChunkOutputs,
    source_inputs: FinalGlobalResolutionSourceInputs,
    assessment: ApplicationRelocationAssessmentReport,
    vendor_plan: Arc<VendorResolutionPlan>,
    selected_lowerings: Vec<SelectedModuleLowering>,
    required_chunks: BTreeSet<String>,
    vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    module_count: usize,
}

/// The consumed product after naturalization, vendor rewrites, and both prune
/// stages completed. Its fields remain private until reconstruction consumes
/// it, so no caller can rewrite files after source correspondence is checked.
pub struct FinalizedCandidateEmissionDraft {
    files: EmissionFiles,
    vendor_plan: Arc<VendorResolutionPlan>,
    decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
    source_inputs: FinalGlobalResolutionSourceInputs,
    assessment: ApplicationRelocationAssessmentReport,
    source_correspondence: BTreeMap<ChunkId, CandidateSourceCorrespondence>,
    global_use_elisions: BTreeMap<ChunkId, js_ast::GlobalUseElisionReceipt>,
    selected_lowerings: Vec<SelectedModuleLowering>,
    module_count: usize,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
}

impl CandidateEmissionDraft {
    pub(super) fn materialized(
        output: EmissionChunkOutputs,
        required_chunks: BTreeSet<String>,
        assessments: Vec<(String, CandidateRelocationAssessmentReport)>,
        assessment_context: CandidateAssessmentContext,
        source_inputs: FinalGlobalResolutionSourceInputs,
        vendor_plan: &VendorResolutionPlan,
        selected_lowerings: Vec<SelectedModuleLowering>,
        vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
        module_count: usize,
    ) -> Result<Self> {
        let assessment = ApplicationRelocationAssessmentReport::from_chunks(
            &required_chunks,
            assessments,
            Some(assessment_context),
        )?;
        Ok(Self {
            output,
            source_inputs,
            assessment,
            vendor_plan: Arc::new(vendor_plan.clone()),
            selected_lowerings,
            required_chunks,
            vendor_reference_rewrites,
            module_count,
        })
    }

    /// Build an exact-source pass-through draft for a run with no lowered
    /// chunks. Source files and indexes come from the prepared SourceProgram;
    /// the caller cannot provide output files or a correspondence map.
    pub fn pass_through(
        source_program: SourceProgram,
        source_inputs: FinalGlobalResolutionSourceInputs,
        assessment_context: CandidateAssessmentContext,
        vendor_plan: &VendorResolutionPlan,
    ) -> Result<Self> {
        let required_chunks = assessment_context.expected_materialized_chunks().clone();
        if !required_chunks.is_empty() {
            anyhow::bail!("pass-through candidate unexpectedly expects materialized chunks");
        }
        let artifact = source_program.artifact();
        let indexes = source_program.indexes().clone();
        if !assessment_context.matches_source_inputs(&source_inputs)
            || !source_inputs.matches_artifact(
                artifact,
                &indexes,
                &source_inputs.required_source_chunks,
            )
        {
            anyhow::bail!("pass-through source snapshot does not match the prepared program");
        }
        let chunk_table = artifact.chunk_table.clone();
        let mut source_correspondence_by_chunk = BTreeMap::new();
        for chunk in &artifact.chunks {
            source_correspondence_by_chunk.insert(
                chunk.chunk_id,
                crate::raw_source_correspondence(chunk, &chunk_table, &indexes, None),
            );
        }
        let assessment = ApplicationRelocationAssessmentReport::from_chunks(
            &required_chunks,
            Vec::new(),
            Some(assessment_context),
        )?;
        Ok(Self {
            output: EmissionChunkOutputs {
                files: EmissionFiles::from_prepared(source_program),
                decomposition_by_chunk: HashMap::new(),
                source_correspondence_by_chunk,
            },
            source_inputs,
            assessment,
            vendor_plan: Arc::new(vendor_plan.clone()),
            selected_lowerings: Vec::new(),
            required_chunks,
            vendor_reference_rewrites: BTreeMap::new(),
            module_count: 0,
        })
    }

    /// Run the immutable canonical rewrite sequence. Callers cannot supply
    /// another plan, source inventory, closure, or arbitrary body callback.
    pub fn finalize(self) -> Result<FinalizedCandidateEmissionDraft> {
        let mut source_correspondence = self.output.source_correspondence_by_chunk;
        let selected_lowerings = self.selected_lowerings;
        let required_chunks = self.required_chunks;
        let vendor_plan = self.vendor_plan;
        let processed_chunk_names = required_chunks;

        let (files, emission_rewrites): (EmissionFiles, EmissionRewriteResult) =
            self.output.files.rewrite_bodies(|artifact, indexes| {
                let naturalization_receipts = naturalize_cross_chunk_imports(
                    artifact,
                    indexes,
                    &selected_lowerings,
                    &processed_chunk_names,
                )?;
                for correspondence in source_correspondence.values_mut() {
                    correspondence.apply_import_naturalization_receipts(&naturalization_receipts);
                }
                let mut rewritten = apply_emission_rewrites_in_place(
                    artifact,
                    &vendor_plan,
                    indexes,
                    &source_correspondence,
                )?;
                for (chunk_id, receipts) in &mut rewritten.correspondence_receipts {
                    if let Some(correspondence) = source_correspondence.get_mut(chunk_id) {
                        correspondence.apply_emission_rewrite_receipts(receipts);
                    }
                }
                Ok(rewritten)
            })?;

        let (files, ()) = files.rewrite_bodies(|files, _| {
            prune_dead_imports::prune_dead_import_specifiers(files, &mut source_correspondence);
            prune_module_exports::prune_unimported_module_exports(
                files,
                &mut source_correspondence,
            );
            Ok(())
        })?;

        let mut references_by_symbol = self.vendor_reference_rewrites;
        merge_rewrite_counts(
            &mut references_by_symbol,
            emission_rewrites.references_by_symbol,
        );

        Ok(FinalizedCandidateEmissionDraft {
            files,
            vendor_plan,
            decomposition_by_chunk: self.output.decomposition_by_chunk,
            source_inputs: self.source_inputs,
            assessment: self.assessment,
            source_correspondence,
            global_use_elisions: emission_rewrites.global_use_elisions,
            selected_lowerings,
            module_count: self.module_count,
            references_by_symbol,
        })
    }
}

impl FinalizedCandidateEmissionDraft {
    pub fn files(&self) -> &EmissionFiles {
        &self.files
    }

    pub fn decomposition_by_chunk(&self) -> &HashMap<ChunkId, ChunkDecompositionOutput> {
        &self.decomposition_by_chunk
    }

    pub fn selected_lowerings(&self) -> &[SelectedModuleLowering] {
        &self.selected_lowerings
    }

    pub fn module_count(&self) -> usize {
        self.module_count
    }

    pub fn references_by_symbol(&self) -> &BTreeMap<(ChunkId, String), usize> {
        &self.references_by_symbol
    }

    /// Consume the opaque product for reconstruction's checker. Returning raw
    /// parts does not mint a writer capability; only reconstruction can turn
    /// the result into Accepted/Refused.
    pub fn into_final_check_parts(
        self,
    ) -> (
        EmissionFiles,
        ApplicationRelocationAssessmentReport,
        FinalGlobalResolutionSourceInputs,
        BTreeMap<ChunkId, CandidateSourceCorrespondence>,
        BTreeMap<ChunkId, js_ast::GlobalUseElisionReceipt>,
        Arc<VendorResolutionPlan>,
    ) {
        (
            self.files,
            self.assessment,
            self.source_inputs,
            self.source_correspondence,
            self.global_use_elisions,
            self.vendor_plan,
        )
    }
}

fn merge_rewrite_counts(
    into: &mut BTreeMap<(ChunkId, String), usize>,
    from: BTreeMap<(ChunkId, String), usize>,
) {
    for (key, count) in from {
        *into.entry(key).or_insert(0) += count;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    use artifact::{EmissionFiles, load_js_chunks};
    use graph_provenance::{InputCapture, ProvenanceRoots};
    use serde_json::json;
    use spec::{VendorLevel, VendorMark, VendorRole};
    use swc_common::{GLOBALS, Globals};
    use swc_ecma_ast::{
        Decl, ExportSpecifier, ModuleDecl, ModuleExportName, ModuleItem, Pat, Stmt,
    };

    #[test]
    fn final_checker_refuses_boundary_export_rebound_to_another_source_cell() -> Result<()> {
        let globals = Globals::new();
        GLOBALS.set(&globals, || {
            let root = tempfile::tempdir()?;
            let source_root = root.path().join("source");
            fs::create_dir_all(source_root.join("static"))?;
            fs::write(
                source_root.join("static/vendor.js"),
                "const a = 1;\nconst z = 2;\nexport { a as alpha };\n",
            )?;
            fs::write(
                source_root.join("static/app.js"),
                "import { a } from '../vendor/entry.js';\n\
                 function read() { return a; }\n\
                 console.log(read());\n\
                 export { read };\n",
            )?;
            let js_list = source_root.join("js-files.txt");
            fs::write(&js_list, "static/vendor.js\nstatic/app.js\n")?;

            let capture = InputCapture::start(
                ProvenanceRoots {
                    spec: root.path().to_path_buf(),
                    source: source_root.clone(),
                    packages: BTreeMap::new(),
                },
                None,
                vec!["candidate-emission-test".to_owned()],
            )?;
            capture.set_source_root(&source_root)?;

            let vendor_marks = BTreeMap::from([(
                "static/vendor.js".to_owned(),
                VendorMark {
                    identity: "boundary route test".to_owned(),
                    role: VendorRole::Module,
                    level: VendorLevel::BoundaryRename,
                },
            )]);
            let loaded = load_js_chunks(&source_root, &js_list)?;
            let prepared = prepare_chunks::prepare_js_chunks(
                &BTreeSet::from(["static/app".to_owned()]),
                &BTreeSet::new(),
                &vendor_marks,
                loaded,
            )?;
            let source_program = prepared.program;
            let mut package_roots = HashMap::new();
            let packages_root = None;
            let vendor_plan = vendor::build_vendor_resolution_plan(
                source_program.artifact(),
                source_program.indexes(),
                &vendor_marks,
                &vendor::VendorPlanOptions {
                    package_roots: &package_roots,
                    packages_root: &packages_root,
                    output_manifest_path: None,
                    output_wrapper_dir: None,
                },
            )?;
            let provenance = capture.finish()?;
            let source_inputs = FinalGlobalResolutionSourceInputs::capture(
                source_program.artifact(),
                source_program.indexes(),
                &BTreeSet::new(),
                None,
                &provenance,
            );
            let expected_chunks = BTreeSet::from(["static/app".to_owned()]);
            let context = CandidateAssessmentContext::capture(
                "boundary-route-final-check-test".to_owned(),
                &source_inputs,
                expected_chunks,
            )
            .ok_or_else(|| anyhow::anyhow!("canonical source context was not captured"))?;

            let logical_modules: BTreeMap<String, BTreeMap<String, spec::LogicalModule>> =
                serde_json::from_value(json!({
                    "static/app": {
                        "readers": {
                            "members": [{
                                "name": "read",
                                "selector": { "binding": { "name": "read" } },
                            }],
                        },
                    },
                }))?;
            let unassigned_mode = BTreeMap::from([(
                "static/app".to_owned(),
                spec::UnassignedMode::InlineInEntry,
            )]);
            let host_behavior = spec::HostBehavior::default();
            let program_premises = spec::ProgramPremises::default();
            let embedded_loaders = BTreeMap::new();
            let chunk_renames = BTreeMap::new();
            let chunk_analysis_options = BTreeMap::new();
            let chunk_export_purity = BTreeMap::new();
            let materialized = super::super::materialize_logical_modules(
                source_program.into_artifact(),
                super::super::MaterializeSpecInputs {
                    host_behavior: &host_behavior,
                    program_premises: &program_premises,
                    embedded_loaders: &embedded_loaders,
                    logical_modules: &logical_modules,
                    chunk_renames: &chunk_renames,
                    unassigned_mode: &unassigned_mode,
                    chunk_analysis_options: &chunk_analysis_options,
                    chunk_export_purity: &chunk_export_purity,
                },
                &vendor_plan,
                super::super::MaterializeLogicalModulesOptions {
                    ownership_subset: None,
                    graph_provenance: Some(provenance),
                    candidate_assessment_context: context,
                    candidate_source_inputs: source_inputs.clone(),
                    config: spec::MaterializeLogicalModulesConfig {
                        file: None,
                        prune_other_chunks: false,
                        report_out_dir: None,
                        target_dir: "modules".to_owned(),
                    },
                    chunk_ids: vec!["static/app".to_owned()],
                    keep_going: false,
                    list_template_identifiers: false,
                    report_emission: super::super::ReportEmission::None,
                    graph_reports: super::super::GraphReportSelection::None,
                    effect_conflicts_jsonl: false,
                    effect_conflicts_chunks: BTreeSet::new(),
                    full_reports: BTreeSet::new(),
                    full_report_chunks: BTreeSet::new(),
                    gate_rejection: super::super::GateRejectionPolicy::Refuse,
                    assessment_only: false,
                    analysis: super::super::AnalysisMode::Compute,
                },
            )?;
            let super::super::Materialized::Lowered(result) = materialized else {
                anyhow::bail!("expected the real materializer to lower the fixture");
            };
            let finalized = result.output.finalize()?;
            let (files, assessment, source_inputs, correspondence, elisions) =
                finalized.into_final_check_parts();
            let preserved = gate::assess_final_candidate_emission(
                assessment.clone(),
                &source_inputs,
                &correspondence,
                files.files(),
                &elisions,
            );
            let preserved_global = preserved
                .global_resolution
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("final checker did not attach its result"))?;
            assert_eq!(
                preserved_global.source_correspondence_status,
                Some(gate::GlobalResolutionStatus::Preserved),
                "real canonical materialization should preserve the checked boundary route: {preserved_global:#?}"
            );

            let consumer_chunk = source_inputs
                .source_chunk_names
                .iter()
                .find_map(|(chunk, name)| (name == "static/app").then_some(*chunk))
                .ok_or_else(|| anyhow::anyhow!("consumer source chunk missing"))?;
            let provider_chunk = source_inputs
                .source_chunk_names
                .iter()
                .find_map(|(chunk, name)| (name == "static/vendor").then_some(*chunk))
                .ok_or_else(|| anyhow::anyhow!("provider source chunk missing"))?;
            let provider_row = correspondence
                .get(&provider_chunk)
                .ok_or_else(|| anyhow::anyhow!("provider correspondence missing"))?;
            let provider_export = provider_row
                .exports
                .iter()
                .find(|export| export.source.public_name == "alpha")
                .ok_or_else(|| anyhow::anyhow!("canonical provider export missing"))?;
            let provider_local = provider_export
                .source
                .local
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("canonical provider local missing"))?;
            let provider_cell = provider_row
                .source_module_cells
                .as_ref()
                .and_then(|cells| cells.get(provider_local))
                .copied()
                .ok_or_else(|| anyhow::anyhow!("canonical provider cell missing"))?;
            let consumer_row = correspondence
                .get(&consumer_chunk)
                .ok_or_else(|| anyhow::anyhow!("consumer correspondence missing"))?;
            let source_import = consumer_row
                .imports
                .iter()
                .find(|import| import.source.imported_name == "a")
                .ok_or_else(|| anyhow::anyhow!("source boundary import missing"))?;
            let consumer_source_cells = consumer_row
                .source_module_cells
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("consumer source-cell inventory missing"))?;
            assert!(
                !consumer_source_cells.contains_key(&source_import.source.local),
                "the vendor-local import spelling does not identify the provider cell in source analysis"
            );
            assert!(
                consumer_row
                    .source_initialized_module_cells
                    .as_ref()
                    .is_some_and(|cells| cells.contains(&provider_cell)),
                "the canonical provider cell must be independently initialized through the source import closure"
            );

            let mut wrong_consumer_cell = correspondence.clone();
            let wrong_cell = ModuleCellId {
                chunk: consumer_chunk,
                declaration_origin: provider_cell.declaration_origin,
            };
            wrong_consumer_cell
                .get_mut(&consumer_chunk)
                .and_then(|row| row.source_module_cells.as_mut())
                .ok_or_else(|| anyhow::anyhow!("consumer source-cell inventory missing"))?
                .insert(source_import.source.local.clone(), wrong_cell);
            let wrong_consumer_cell_report = gate::assess_final_candidate_emission(
                assessment.clone(),
                &source_inputs,
                &wrong_consumer_cell,
                files.files(),
                &elisions,
            );
            assert_ne!(
                wrong_consumer_cell_report
                    .global_resolution
                    .as_ref()
                    .map(|global| global.status),
                Some(gate::GlobalResolutionStatus::Preserved),
                "a present consumer-cell claim that conflicts with the canonical provider cell must not be ignored"
            );

            let mut missing_use = correspondence.clone();
            let consumer = missing_use
                .get_mut(&consumer_chunk)
                .ok_or_else(|| anyhow::anyhow!("consumer correspondence missing"))?;
            let import = consumer
                .imports
                .iter_mut()
                .find(|import| import.source.imported_name == "a")
                .ok_or_else(|| anyhow::anyhow!("boundary import row missing"))?;
            let use_route = import
                .uses
                .first_mut()
                .ok_or_else(|| anyhow::anyhow!("boundary use route missing"))?;
            use_route.routes.clear();
            let missing_use_report = gate::assess_final_candidate_emission(
                assessment.clone(),
                &source_inputs,
                &missing_use,
                files.files(),
                &elisions,
            );
            assert_ne!(
                missing_use_report.global_resolution.as_ref().map(|global| global.status),
                Some(gate::GlobalResolutionStatus::Preserved),
                "missing one source-use destination must not be discharged by the other moved/residual route"
            );

            let mut missing_initialization = correspondence.clone();
            missing_initialization
                .get_mut(&consumer_chunk)
                .ok_or_else(|| anyhow::anyhow!("consumer correspondence missing"))?
                .source_initialized_module_cells = Some(BTreeSet::new());
            let missing_initialization_report = gate::assess_final_candidate_emission(
                assessment.clone(),
                &source_inputs,
                &missing_initialization,
                files.files(),
                &elisions,
            );
            assert_ne!(
                missing_initialization_report
                    .global_resolution
                    .as_ref()
                    .map(|global| global.status),
                Some(gate::GlobalResolutionStatus::Preserved),
                "a final route without source initialization authority must remain incomplete"
            );
            assert!(missing_initialization_report
                .global_resolution
                .as_ref()
                .is_some_and(|global| global
                    .chunks
                    .values()
                    .flat_map(|chunk| &chunk.findings)
                    .any(|finding| {
                        finding.reason == gate::GlobalResolutionReason::InitializationOrderUnproved
                    })),
                "missing initialization evidence must be attributed to the final provider-before-consumer obligation");

            // Keep an added static request in the census even when its WTF-8
            // specifier cannot be represented by the route ABI. This begins
            // with the real materialized artifact and changes only its final
            // candidate AST, leaving source proofs and correspondence intact.
            let mut unsupported_request =
                js_ast::parse_js_module("unrepresentable-request.js", r#"import "\ud800";"#)?;
            let Some(ModuleItem::ModuleDecl(ModuleDecl::Import(import))) =
                unsupported_request.module.body.first()
            else {
                anyhow::bail!("unrepresentable-request fixture did not parse as an import");
            };
            assert!(
                import.src.value.as_str().is_none(),
                "fixture must exercise a module specifier outside UTF-8"
            );
            let extra_request = unsupported_request
                .module
                .body
                .pop()
                .ok_or_else(|| anyhow::anyhow!("unrepresentable request item missing"))?;
            if !unsupported_request.module.body.is_empty() {
                anyhow::bail!("unrepresentable-request fixture had extra module items");
            }
            let output_copy = EmissionFiles::new(files.files().clone())?;
            let (unrepresentable_files, ()) = output_copy.rewrite_bodies(move |bundle, _| {
                let candidate_chunk = bundle
                    .chunks
                    .iter_mut()
                    .find(|chunk| chunk.chunk_id == consumer_chunk)
                    .ok_or_else(|| anyhow::anyhow!("candidate consumer chunk missing"))?;
                let entry_path = candidate_chunk.js.entry_file.clone();
                let entry = candidate_chunk
                    .js
                    .files
                    .iter_mut()
                    .find(|file| file.path == entry_path)
                    .ok_or_else(|| anyhow::anyhow!("candidate consumer entry missing"))?;
                let parsed = entry
                    .ast_mut()
                    .ok_or_else(|| anyhow::anyhow!("candidate consumer entry has no AST"))?;
                parsed.module.body.push(extra_request);
                Ok(())
            })?;
            let unrepresentable_report = gate::assess_final_candidate_emission(
                assessment.clone(),
                &source_inputs,
                &correspondence,
                unrepresentable_files.files(),
                &elisions,
            );
            let unrepresentable_global = unrepresentable_report
                .global_resolution
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("final checker did not attach its result"))?;
            assert_ne!(
                unrepresentable_global.source_correspondence_status,
                Some(gate::GlobalResolutionStatus::Preserved),
                "an added static request with an unrepresentable specifier must remain visible to the final checker: {unrepresentable_global:#?}"
            );
            assert!(
                unrepresentable_global
                    .source_correspondence_findings
                    .iter()
                    .any(|finding| finding.reason == gate::GlobalResolutionReason::UnplannedSourceRequest),
                "the unrepresentable static request must be reported as an unplanned final request"
            );

            // Change only the final provider export's local binding after the
            // canonical materializer and all rewrite receipts have completed.
            // The same public export name now denotes a different source cell.
            let provider = files
                .files()
                .chunk_table
                .get("static/vendor")
                .ok_or_else(|| anyhow::anyhow!("provider chunk missing"))?;
            let (wrong_files, changed) = files.rewrite_bodies(|bundle, _| {
                let provider_chunk = bundle.js_chunk_mut(provider)?;
                let file = provider_chunk
                    .files
                    .iter_mut()
                    .find(|file| file.path == "entry.js")
                    .ok_or_else(|| anyhow::anyhow!("provider entry missing"))?;
                let parsed = file
                    .ast_mut()
                    .ok_or_else(|| anyhow::anyhow!("provider entry did not retain its final AST"))?;
                let z = parsed
                    .module
                    .body
                    .iter()
                    .find_map(|item| match item {
                        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => declaration
                            .decls
                            .iter()
                            .find_map(|declarator| match &declarator.name {
                                Pat::Ident(binding) if binding.id.sym == "z" => {
                                    Some(binding.id.clone())
                                }
                                _ => None,
                            }),
                        _ => None,
                    })
                    .ok_or_else(|| anyhow::anyhow!("provider z binding missing"))?;
                let mut changed = 0;
                for item in &mut parsed.module.body {
                    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = item else {
                        continue;
                    };
                    for specifier in &mut export.specifiers {
                        let ExportSpecifier::Named(named) = specifier else {
                            continue;
                        };
                        let is_alpha = named.exported.as_ref().is_some_and(|name| {
                            matches!(name, ModuleExportName::Ident(ident) if ident.sym == "alpha")
                        });
                        if is_alpha {
                            named.orig = ModuleExportName::Ident(z.clone());
                            changed += 1;
                        }
                    }
                }
                Ok(changed)
            })?;
            assert_eq!(changed, 1, "test must change exactly the final export cell");
            let refused = gate::assess_final_candidate_emission(
                assessment,
                &source_inputs,
                &correspondence,
                wrong_files.files(),
                &elisions,
            );
            let refused_global = refused
                .global_resolution
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("final checker did not attach its result"))?;
            assert_ne!(
                refused_global.source_correspondence_status,
                Some(gate::GlobalResolutionStatus::Preserved),
                "the final checker must not accept alpha after its binding changed from the recorded source cell: {refused_global:#?}"
            );
            assert!(
                !refused_global.source_correspondence_findings.is_empty(),
                "same-cell disagreement must be visible in final correspondence findings: {refused_global:#?}"
            );
            Ok(())
        })
    }
}
