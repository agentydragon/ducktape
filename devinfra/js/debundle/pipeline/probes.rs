use super::*;

/// One accepted baseline and its immutable input preparation. Only the contract
/// map may change between probes: other spec fields affect preparation.
pub struct ContractAudit {
    spec: TransformSpec,
    prepared: PreparedTransform,
}

#[derive(Debug)]
pub enum ContractProbeOutcome {
    Accepted,
    Rejected(anyhow::Error),
    Inconclusive(anyhow::Error),
}

impl ContractAudit {
    pub fn prepare(
        cli: &TransformRequest,
        expected_spec: &TransformSpec,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<Self> {
        let (spec, prepared) =
            prepare_transform(cli, &audit_options(), Some(expected_spec), None, None)?;
        run_prepared_transform(
            &spec,
            prepared.clone(),
            audit_options(),
            true,
            AnalysisMode::Compute,
            false,
            diagnostics,
        )?;
        prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("audit inputs changed during baseline")?;
        Ok(Self { spec, prepared })
    }

    pub fn provenance(&self) -> &GraphProvenance {
        &self.prepared.provenance
    }

    /// Recompute all contract-dependent analysis and full reconstruction
    /// acceptance. Input-verification failures abort the audit, including when
    /// analysis rejects; they must never become a cached contract verdict.
    /// No graph or output artifacts may escape an edited probe.
    pub fn probe(
        &self,
        contracts: BTreeMap<String, ChunkExportPurity>,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<ContractProbeOutcome> {
        self.provenance()
            .verify(&CurrentRoots::default())
            .context("audit inputs changed after baseline")?;
        let mut spec = self.spec.clone();
        spec.chunk_export_purity = contracts;
        if let Err(error) = validate_transform_spec(&spec) {
            return Ok(ContractProbeOutcome::Inconclusive(error));
        }
        let outcome = run_prepared_transform(
            &spec,
            self.prepared.clone(),
            audit_options(),
            true,
            AnalysisMode::Compute,
            false,
            diagnostics,
        );
        self.provenance()
            .verify(&CurrentRoots::default())
            .context("audit inputs changed between baseline and probe")?;
        Ok(match outcome {
            Ok(_) => ContractProbeOutcome::Accepted,
            Err(error) if error.chain().any(|cause| cause.is::<std::io::Error>()) => {
                ContractProbeOutcome::Inconclusive(error)
            }
            Err(error) => ContractProbeOutcome::Rejected(error),
        })
    }
}

/// One prepared input for `spec lint --probe-analysis`: each probe withholds
/// one authored annotation from a copy of the spec and recomputes analysis,
/// graph, gate and reconstruction on the same prepared input, writing its
/// reports and full graphs under its own root for comparison.
pub struct AnnotationProbe {
    spec: TransformSpec,
    prepared: PreparedTransform,
}

/// How one probe run ended.
#[derive(Debug)]
pub enum AnnotationProbeRun {
    /// Final acceptance passed; reports are complete under the run's root.
    Accepted,
    /// The run failed (a gate rejection, an analysis or reconstruction error).
    Rejected(anyhow::Error),
    /// The probe itself could not run (an invalid edited spec, an I/O error).
    Inconclusive(anyhow::Error),
}

impl AnnotationProbe {
    pub fn prepare(cli: &TransformRequest, expected_spec: &TransformSpec) -> Result<Self> {
        let (spec, prepared) =
            prepare_transform(cli, &audit_options(), Some(expected_spec), None, None)?;
        Ok(Self { spec, prepared })
    }

    /// The spec the input was prepared for; probes edit copies of it.
    pub fn spec(&self) -> &TransformSpec {
        &self.spec
    }

    /// Run `spec` on the prepared input with reports and full graphs under
    /// `out_root`. Input changes since preparation abort the probe.
    pub fn run(
        &self,
        spec: &TransformSpec,
        out_root: &Path,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<AnnotationProbeRun> {
        self.prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("probe inputs changed after preparation")?;
        if let Err(error) = validate_transform_spec(spec) {
            return Ok(AnnotationProbeRun::Inconclusive(error));
        }
        let outcome = run_prepared_transform(
            spec,
            self.prepared.clone(),
            TransformRunOptions {
                output: RunOutput::Accepted,
                artifacts: RunArtifacts::ReportsOnly {
                    out_root: out_root.to_path_buf(),
                },
                graph_reports: GraphReportSelection::Full,
                ..audit_options()
            },
            false,
            AnalysisMode::Compute,
            false,
            diagnostics,
        );
        self.prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("probe inputs changed during a probe")?;
        Ok(match outcome {
            Ok(_) => AnnotationProbeRun::Accepted,
            Err(error) if error.chain().any(|cause| cause.is::<std::io::Error>()) => {
                AnnotationProbeRun::Inconclusive(error)
            }
            Err(error) => AnnotationProbeRun::Rejected(error),
        })
    }
}

pub(super) fn audit_options() -> TransformRunOptions {
    TransformRunOptions {
        ownership_subset: None,
        ownership_subset_input: None,
        output: RunOutput::DryRun,
        artifacts: RunArtifacts::Program,
        graph_reports: GraphReportSelection::None,
        keep_going: false,
        report_dir_override: None,
        list_template_identifiers: false,
        effect_conflicts_jsonl: false,
        effect_conflicts_chunks: BTreeSet::new(),
        full_reports: BTreeSet::new(),
        full_report_chunks: BTreeSet::new(),
    }
}
