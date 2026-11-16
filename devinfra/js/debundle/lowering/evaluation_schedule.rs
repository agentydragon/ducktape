//! Final emission schedule, derived from the immutable emitted syntax. File
//! identity includes the chunk and physical file; logical module IDs alone
//! cannot describe ESM evaluation after cross-chunk import naturalization.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis::program_execution::ProgramExecutables;
use analysis::program_execution::schedule::{
    EvaluationCommutations, EvaluationEvent, EvaluationScheduleRefusal,
};
use anyhow::{Context, Result, ensure};
use artifact::paths::{join_module_path, module_path_dirname};
use artifact::{ChunkBundle, EmissionFiles};
use source_identity::ChunkId;
use swc_ecma_ast::{ImportPhase, ModuleDecl, ModuleItem};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct EmittedFile {
    pub(super) chunk: ChunkId,
    pub(super) path: String,
}

pub(super) struct EvaluationPlan {
    program: Arc<ProgramExecutables>,
    commutations: EvaluationCommutations,
    /// Source root -> concrete emitted root, supplied by materialization.
    entries: BTreeMap<String, EmittedFile>,
    opaque_modules: BTreeSet<String>,
    excluded_modules: BTreeSet<ChunkId>,
    replacements: BTreeMap<vendor::VendorEvaluationTarget, String>,
}

impl EvaluationPlan {
    pub(super) fn new(
        source: &ChunkBundle,
        program: Arc<ProgramExecutables>,
        selected: &[String],
        entry_files: BTreeMap<ChunkId, String>,
        vendor_plan: &vendor::VendorResolutionPlan,
        commutations: EvaluationCommutations,
    ) -> Result<Self> {
        let excluded_modules = vendor_plan.full_swap_chunk_ids();
        let opaque_modules: BTreeSet<_> = vendor_plan
            .replacement_packages()
            .into_keys()
            .map(|chunk| source.chunk_table.name(chunk).to_owned())
            .collect();
        let mut replacements = BTreeMap::new();
        for (chunk, target) in vendor_plan.evaluation_targets() {
            let module = source.chunk_table.name(chunk).to_owned();
            ensure!(
                replacements
                    .insert(target, module.clone())
                    .is_none_or(|prior| prior == module),
                "evaluation_schedule_unsupported: a replacement target represents multiple source vendor modules"
            );
        }
        let mut entries = BTreeMap::new();
        // Only the selected roots' source closure is prepared. Passthrough
        // rows are recovered from final AST origins without guessing a body file.
        for entry in selected {
            let chunk = source
                .chunk_table
                .get(entry)
                .context("evaluation_schedule_missing: selected source root")?;
            let original = source
                .find_chunk(chunk)
                .context("selected source chunk exists")?;
            entries.insert(
                entry.clone(),
                EmittedFile {
                    chunk,
                    path: entry_files
                        .get(&chunk)
                        .cloned()
                        .unwrap_or_else(|| original.js.entry_file.clone()),
                },
            );
        }
        Ok(Self {
            program,
            commutations,
            opaque_modules,
            excluded_modules,
            replacements,
            entries,
        })
    }

    pub(super) fn validate(&self, files: &EmissionFiles) -> Result<()> {
        self.validate_files(files)
            .map_err(|error| EvaluationScheduleRefusal::from_error(None, error).into())
    }

    fn validate_files(&self, files: &EmissionFiles) -> Result<()> {
        let artifact = files.files();
        let paths: BTreeMap<_, _> = artifact
            .chunks
            .iter()
            .flat_map(|chunk| {
                chunk.js.files.iter().map(move |file| {
                    (
                        join_module_path(&[artifact.chunk_table.name(chunk.chunk_id), &file.path]),
                        EmittedFile {
                            chunk: chunk.chunk_id,
                            path: file.path.clone(),
                        },
                    )
                })
            })
            .collect();
        enum Task {
            Enter(EmittedFile),
            Finish(EmittedFile),
            Opaque(EvaluationEvent),
            Replacement(vendor::VendorEvaluationTarget, String),
        }
        for (source_entry, emitted_entry) in &self.entries {
            // Missing source dependencies are source refusals even when the
            // finalized candidate would bypass that loader failure entirely.
            self.program
                .source_schedule(source_entry, &self.opaque_modules)?;
            let mut seen = BTreeSet::new();
            let mut opaque = BTreeSet::new();
            let mut events = Vec::new();
            let mut replacements_seen = BTreeSet::new();
            let mut tasks = vec![Task::Enter(emitted_entry.clone())];
            while let Some(task) = tasks.pop() {
                match task {
                    Task::Opaque(identity) => {
                        if opaque.insert(identity.clone()) {
                            events.push(identity);
                        }
                    }
                    Task::Replacement(target, module) => {
                        if replacements_seen.insert(target) {
                            events.push(EvaluationEvent::OpaqueModule(module));
                        }
                    }
                    Task::Finish(identity) => {
                        let module = artifact.chunk_table.name(identity.chunk);
                        if self.opaque_modules.contains(module) {
                            events.push(EvaluationEvent::OpaqueModule(module.to_owned()));
                            continue;
                        }
                        let chunk = artifact.find_chunk(identity.chunk).expect("entered chunk");
                        let ast = chunk
                            .js
                            .get_file(&identity.path)
                            .expect("entered file")
                            .ast()
                            .expect("entered AST");
                        let ordered = self.program.emitted_owner_order(
                            artifact.chunk_table.name(identity.chunk),
                            &ast.module.body,
                        )?;
                        events.extend(ordered.into_iter().map(EvaluationEvent::Owner));
                    }
                    Task::Enter(identity) => {
                        if !seen.insert(identity.clone()) {
                            continue;
                        }
                        let module = artifact.chunk_table.name(identity.chunk);
                        if self.excluded_modules.contains(&identity.chunk) {
                            let event = EvaluationEvent::OpaqueModule(module.to_owned());
                            if opaque.insert(event.clone()) {
                                events.push(event);
                            }
                            continue;
                        }
                        let chunk = artifact
                            .find_chunk(identity.chunk)
                            .context("evaluation_schedule_missing: emitted chunk")?;
                        let file = chunk
                            .js
                            .get_file(&identity.path)
                            .context("evaluation_schedule_missing: emitted file")?;
                        let ast = file.ast().with_context(|| format!("evaluation_schedule_unsupported: final AST unavailable for {identity:?}"))?;
                        let mut requests = Vec::new();
                        for item in &ast.module.body {
                            let (specifier, supported) = match item {
                                ModuleItem::ModuleDecl(ModuleDecl::Import(import))
                                    if !import.type_only =>
                                {
                                    (
                                        &import.src,
                                        import.with.is_none()
                                            && import.phase == ImportPhase::Evaluation,
                                    )
                                }
                                ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export))
                                    if !export.type_only && export.src.is_some() =>
                                {
                                    (
                                        export.src.as_ref().expect("source checked"),
                                        export.with.is_none(),
                                    )
                                }
                                ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export))
                                    if !export.type_only =>
                                {
                                    (&export.src, export.with.is_none())
                                }
                                _ => continue,
                            };
                            ensure!(
                                supported,
                                "evaluation_schedule_unsupported: attributed or non-evaluation request in {identity:?}"
                            );
                            let specifier = specifier.value.to_string_lossy().into_owned();
                            if specifier.starts_with('.') {
                                let path = join_module_path(&[
                                    artifact.chunk_table.name(identity.chunk),
                                    &module_path_dirname(&identity.path),
                                    &specifier,
                                ]);
                                let replacement =
                                    vendor::VendorEvaluationTarget::File(path.clone());
                                if let Some(module) = self.replacements.get(&replacement) {
                                    requests.push(Task::Replacement(replacement, module.clone()));
                                } else {
                                    let target = paths.get(&path).with_context(|| format!("evaluation_schedule_missing: emitted {identity:?} requests {specifier}"))?;
                                    requests.push(Task::Enter(target.clone()));
                                }
                            } else {
                                let replacement =
                                    vendor::VendorEvaluationTarget::Package(specifier.clone());
                                if let Some(module) = self.replacements.get(&replacement) {
                                    requests.push(Task::Replacement(replacement, module.clone()));
                                } else {
                                    requests.push(Task::Opaque(EvaluationEvent::opaque_request(
                                        artifact.chunk_table.name(identity.chunk).to_owned(),
                                        specifier,
                                        join_module_path(&[
                                            artifact.chunk_table.name(identity.chunk),
                                            &module_path_dirname(&identity.path),
                                        ]),
                                    )?));
                                }
                            }
                        }
                        tasks.push(Task::Finish(identity));
                        tasks.extend(requests.into_iter().rev());
                    }
                }
            }
            // A replacement and its retained residual may be several loader
            // files. Only an uninterrupted evaluation block represents the
            // original opaque module boundary; never merge across app code.
            events.dedup_by(|left, right| matches!((left, right),
                (EvaluationEvent::OpaqueModule(left), EvaluationEvent::OpaqueModule(right)) if left == right));
            let mut vendor_blocks = BTreeSet::new();
            for event in &events {
                if let EvaluationEvent::OpaqueModule(module) = event {
                    ensure!(
                        vendor_blocks.insert(module),
                        "evaluation_schedule_opaque_reordered: replacement for {module} is interleaved with another module"
                    );
                }
            }
            self.program.validate_schedule(
                source_entry,
                &events,
                &self.opaque_modules,
                &self.commutations,
            )?;
        }
        Ok(())
    }
}
