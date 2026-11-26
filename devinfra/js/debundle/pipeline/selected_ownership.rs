use super::*;

/// Exact capture identity used to group selected corpus rows. Reuse is local
/// to one command and requires the complete provenance, relocated run context,
/// root mapping, and verified selected-view bytes to agree.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct SelectedOwnershipProgramKey(String);

/// One immutable source/spec preparation shared by selected-view edits from a
/// compatible capture. Semantic value-flow analysis comes from the saved graph;
/// source parsing and the full-base frontend are prepared once per group.
pub struct SelectedOwnershipProgram {
    spec: TransformSpec,
    prepared: PreparedTransform,
    key: SelectedOwnershipProgramKey,
    sources: BTreeMap<String, (PathBuf, String)>,
    baseline_placement_chunks: BTreeSet<String>,
}

impl SelectedOwnershipProgram {
    pub fn key(
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<SelectedOwnershipProgramKey> {
        let roots = graph_source.current_roots();
        let (path, bytes) = graph_source.ownership_subset_input().context(
            "selected-view editing requires the graph's captured ownership-subset input",
        )?;
        let encoded = serde_json::to_vec(&(
            provenance,
            graph_source.regeneration_command(),
            &roots.spec,
            &roots.source,
            &roots.ownership_subset,
            &roots.packages,
            path,
            graph_provenance::sha256(bytes),
        ))?;
        Ok(SelectedOwnershipProgramKey(graph_provenance::sha256(
            &encoded,
        )))
    }

    pub fn prepare(
        request: &TransformRequest,
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<Self> {
        let key = Self::key(graph_source, provenance)?;
        let (path, bytes) = graph_source.ownership_subset_input().context(
            "selected-view editing requires the graph's captured ownership-subset input",
        )?;
        let baseline: OwnershipSubset = serde_json::from_slice(bytes)
            .context("parsing the graph's verified ownership-subset input")?;
        let baseline_placement_chunks = baseline
            .placements
            .iter()
            .filter(|(_, modules)| !modules.is_empty())
            .map(|(chunk, _)| chunk.clone())
            .collect();
        let mut options = audit_options();
        options.ownership_subset = Some(baseline);
        options.ownership_subset_input = Some(OwnershipSubsetInput {
            path: path.to_path_buf(),
            bytes: bytes.to_vec(),
        });
        let (spec, prepared) = prepare_transform(request, &options, None, None, None)?;
        // Compare the bytes actually consumed, not a later reread that could
        // hide an intervening edit. The shared AST is this verified snapshot.
        let fingerprints = |provenance: &GraphProvenance, roots: &CurrentRoots| {
            provenance
                .files
                .iter()
                .map(|file| {
                    Ok((
                        graph_provenance::absolute(
                            &provenance.resolve(&file.root, &file.path, roots)?,
                        )?,
                        file.sha256.clone(),
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()
        };
        anyhow::ensure!(
            fingerprints(provenance, graph_source.current_roots())?
                == fingerprints(&prepared.provenance, &CurrentRoots::default())?,
            "selected-view preparation consumed inputs different from the verified graph; regenerate the graph"
        );
        let sources = prepared
            .provenance
            .chunk_sources
            .iter()
            .map(|source| {
                (
                    source.chunk_id.clone(),
                    (source.source_path.clone(), source.sha256.clone()),
                )
            })
            .collect();
        let program = Self {
            spec,
            prepared,
            key,
            sources,
            baseline_placement_chunks,
        };
        program.verify_context(graph_source, provenance)?;
        Ok(program)
    }

    /// Each row retains its own verified context. A common chunk name alone
    /// cannot authorize reuse of another capture's AST, premises or selectors.
    pub fn verify_context(
        &self,
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<()> {
        anyhow::ensure!(
            Self::key(graph_source, provenance)? == self.key,
            "selected-view graph inputs are incompatible with the prepared program"
        );
        let (path, sha256) = self
            .sources
            .get(graph_source.chunk_id())
            .context("selected-view transform context lacks the graph's chunk source")?;
        anyhow::ensure!(
            path == graph_source.source_path() && sha256 == graph_source.sha256(),
            "selected-view transform source differs from the verified graph"
        );
        Ok(())
    }

    /// Borrow one shared source/base frontend for a group. The scoped session
    /// owns resolved chunks and consumes each target once, without a cloned
    /// whole-program AST or another joint selector solve per candidate.
    pub fn with_preflight_session<R>(
        &self,
        mut placement_chunks: BTreeSet<String>,
        diagnostics: &analysis::diagnostics::Diagnostics<'_>,
        operation: impl FnOnce(&mut lowering::SelectedOwnershipPreflightSession<'_>) -> Result<R>,
    ) -> Result<R> {
        placement_chunks.extend(self.baseline_placement_chunks.iter().cloned());
        let chunks: Vec<_> = self.spec.materialize_chunk_ids().into_iter().collect();
        lowering::with_selected_ownership_preflight(
            lowering::SelectedOwnershipPreparationInput {
                artifact: self.prepared.source.artifact(),
                spec_inputs: MaterializeSpecInputs {
                    host_behavior: &self.spec.host_behavior,
                    program_premises: &self.spec.program_premises,
                    embedded_loaders: &self.prepared.embedded_loaders,
                    logical_modules: &self.spec.logical_modules,
                    chunk_renames: &self.spec.chunk_renames,
                    unassigned_mode: &self.spec.unassigned_mode,
                    import_order: &self.spec.import_order,
                    chunk_analysis_options: &self.spec.chunk_analysis_options,
                    chunk_export_purity: &self.spec.chunk_export_purity,
                },
                materialize_config: &self.spec.materialize_logical_modules,
                materialized_chunk_ids: &chunks,
                placement_chunks,
                diagnostics,
                graph_provenance: Some(&self.prepared.provenance),
            },
            operation,
        )
    }
}
