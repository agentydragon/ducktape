//! Spec-file locations of author input premises, rendered into reports.
//!
//! Analysis output names each author input by its path inside the spec
//! (`chunk_analysis_options.<chunk>.value_inputs[<i>]`), so it does not depend
//! on where the spec or the parser inputs live and can be cached by content.
//! Report writers add the authored spec file, the parser input file and the
//! author's note here.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{Context, Result};
use graph_provenance::{CurrentRoots, GraphProvenance};
use spec::DependencyGraphOptions;

use crate::analysis_inputs::{NativeOperationInputEvidence, ValueInputEvidence};
use crate::reports::schema::{InferenceUse, NativeMutationWitness, SynchronousExecutionReport};

pub struct AuthorInputLocations<'a> {
    /// `None` when no chunk declares a value, native or argument input.
    spec_path: Option<PathBuf>,
    parser_input_files: BTreeMap<String, String>,
    options: &'a BTreeMap<String, DependencyGraphOptions>,
}

impl<'a> AuthorInputLocations<'a> {
    pub fn new(
        provenance: Option<&GraphProvenance>,
        options: &'a BTreeMap<String, DependencyGraphOptions>,
    ) -> Result<Self> {
        let with_inputs = options
            .iter()
            .filter(|(_, options)| {
                !options.value_inputs.is_empty()
                    || !options.native_operation_inputs.is_empty()
                    || !options.native_call_identity_inputs.is_empty()
                    || !options.argument_inputs.is_empty()
            })
            .map(|(chunk, _)| chunk)
            .collect::<Vec<_>>();
        if with_inputs.is_empty() {
            return Ok(Self {
                spec_path: None,
                parser_input_files: BTreeMap::new(),
                options,
            });
        }
        let provenance =
            provenance.context("author inputs require explicit graph parser-input provenance")?;
        let mut parser_input_files = BTreeMap::new();
        for chunk in with_inputs {
            // Analysis rejects an input whose chunk has no recorded source.
            let Some(source) = provenance
                .chunk_sources
                .iter()
                .find(|source| source.chunk_id == *chunk)
            else {
                continue;
            };
            let path = provenance.resolve(
                &source.input_file.root,
                &source.input_file.path,
                &CurrentRoots::default(),
            )?;
            parser_input_files.insert(chunk.clone(), path.to_string_lossy().into_owned());
        }
        Ok(Self {
            spec_path: Some(provenance.author_spec_path()?),
            parser_input_files,
            options,
        })
    }

    fn spec_location(&self, premise: &str) -> String {
        let spec_path = self
            .spec_path
            .as_ref()
            .expect("an author input premise implies a chunk with author inputs");
        format!("{}:{premise}", spec_path.display())
    }

    fn parser_input_file(&self, chunk: &str) -> String {
        self.parser_input_files
            .get(chunk)
            .unwrap_or_else(|| panic!("author input chunk {chunk:?} has no parser input file"))
            .clone()
    }

    fn chunk_options(&self, chunk: &str) -> &DependencyGraphOptions {
        self.options
            .get(chunk)
            .unwrap_or_else(|| panic!("author input chunk {chunk:?} has no analysis options"))
    }

    pub fn render_value_input(&self, evidence: &mut ValueInputEvidence) {
        let chunk = &evidence.reference.defining_chunk;
        evidence.note = self.chunk_options(chunk).value_inputs[evidence.reference.input_index]
            .note
            .clone();
        evidence.parser_input_file = Some(self.parser_input_file(chunk));
        evidence.spec_location = self.spec_location(&evidence.spec_location);
    }

    pub fn render_native_operation_input(&self, evidence: &mut NativeOperationInputEvidence) {
        let chunk = &evidence.reference.defining_chunk;
        let options = self.chunk_options(chunk);
        // The reference indexes operation inputs, then call-identity inputs.
        let index = evidence.reference.input_index;
        evidence.note = match index.checked_sub(options.native_operation_inputs.len()) {
            None => options.native_operation_inputs[index].note.clone(),
            Some(index) => options.native_call_identity_inputs[index].note.clone(),
        };
        evidence.parser_input_file = Some(self.parser_input_file(chunk));
        evidence.spec_location = self.spec_location(&evidence.spec_location);
    }

    pub fn render_inference_use(&self, inference_use: &mut InferenceUse) {
        inference_use.argument_input_spec_locations = inference_use
            .argument_input_refs
            .iter()
            .map(|reference| {
                self.spec_location(&format!(
                    "chunk_analysis_options.{}.argument_inputs[{}]",
                    reference.defining_chunk, reference.input_index
                ))
            })
            .collect();
    }

    pub fn render_synchronous_execution(&self, execution: &mut SynchronousExecutionReport) {
        for inference_use in &mut execution.inference_uses {
            self.render_inference_use(inference_use);
        }
        for input_use in &mut execution.native_operation_input_uses {
            self.render_native_operation_input(&mut input_use.evidence);
        }
    }

    pub fn render_native_mutation_witness(&self, witness: &mut NativeMutationWitness) {
        for evidence in &mut witness.value_input_evidence {
            self.render_value_input(evidence);
        }
    }
}
