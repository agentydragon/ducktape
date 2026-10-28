//! Fresh, conditional ablation of one authored chunk-export contract at a time.

use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use clap::{Args, ValueEnum};
use graph_provenance::{CurrentRoots, GraphProvenance};
use peel::OutputFormat;
use pipeline::{TransformArgs, load_transform_spec_source, run_transform_probe};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use spec::{ChunkExportPurity, RetainedCallbackSignature, TransformSpec};
use tempfile::NamedTempFile;

use crate::{emit_report, print_section};

#[derive(Debug, Args)]
pub(super) struct AuditContractsArgs {
    #[command(flatten)]
    transform: TransformArgs,
    /// Exact key in chunk_export_purity. A source-marker key is a policy
    /// label, not a defining chunk ID.
    #[arg(long)]
    policy_key: String,
    /// Select one or more contract families. Omit to inspect all families.
    #[arg(long, value_enum)]
    kind: Vec<ContractKind>,
    /// Select one export within the policy.
    #[arg(long)]
    export: Option<String>,
    /// Select one member within a member contract family.
    #[arg(long)]
    member: Option<String>,
    /// Select one typed callback signature as JSON, for example
    /// {"argument_count":1,"callback_arguments":[0]}.
    #[arg(long, value_parser = parse_signature)]
    signature: Option<RetainedCallbackSignature>,
    /// Maximum number of independent removal probes; must be positive.
    #[arg(long)]
    max_probes: usize,
    /// Create a new exact-input checkpoint; refuse an existing file.
    #[arg(long, conflicts_with = "resume_from")]
    checkpoint: Option<PathBuf>,
    /// Resume and update an existing checkpoint after fresh baseline validation.
    #[arg(long, conflicts_with = "checkpoint")]
    resume_from: Option<PathBuf>,
    #[arg(long, value_enum)]
    format: Option<OutputFormat>,
}

fn parse_signature(value: &str) -> Result<RetainedCallbackSignature, String> {
    serde_json::from_str(value).map_err(|error| error.to_string())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, ValueEnum, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[value(rename_all = "snake_case")]
enum ContractKind {
    PureExports,
    PureNewExports,
    PureMembers,
    FluentExports,
    CallbackFactoryExports,
    InlineCallbackFactoryExports,
    CallbackFactoryMembers,
    RetainedCallbackFactoryExports,
    RetainedCallbackFactoryMembers,
    RetainedCallbackConstructorExports,
    NoReentryExports,
    NoReentryCallbackExports,
    NoReentryMembers,
    NoReentryRecordMembers,
    NamespaceIndependentMembers,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
struct ContractTarget {
    policy_key: String,
    kind: ContractKind,
    export: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    member: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    signature: Option<RetainedCallbackSignature>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ProbeVerdict {
    RemovableUnderRemainingAssumptions,
    FailedFullAcceptance,
    Inconclusive,
    BudgetSkipped,
}

impl ProbeVerdict {
    fn label(&self) -> &'static str {
        match self {
            Self::RemovableUnderRemainingAssumptions => "removable_under_remaining_assumptions",
            Self::FailedFullAcceptance => "failed_full_acceptance",
            Self::Inconclusive => "inconclusive",
            Self::BudgetSkipped => "budget_skipped",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProbeResult {
    target: ContractTarget,
    verdict: ProbeVerdict,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct AuditReport {
    policy_key: String,
    input_provenance_sha256: String,
    /// These identify the provider selected by this policy. They are retained
    /// in every probe and are not independently audited here.
    source_markers: Option<BTreeSet<String>>,
    baseline: &'static str,
    scope: &'static str,
    independently_probed: usize,
    budget_skipped: usize,
    results: Vec<ProbeResult>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuditSelection {
    policy_key: String,
    kinds: BTreeSet<ContractKind>,
    export: Option<String>,
    member: Option<String>,
    signature: Option<RetainedCallbackSignature>,
}

impl AuditSelection {
    fn from_args(args: &AuditContractsArgs) -> Self {
        Self {
            policy_key: args.policy_key.clone(),
            kinds: if args.kind.is_empty() {
                ContractKind::value_variants().iter().copied().collect()
            } else {
                args.kind.iter().copied().collect()
            },
            export: args.export.clone(),
            member: args.member.clone(),
            signature: args.signature.clone(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct AuditCheckpoint {
    provenance: GraphProvenance,
    selection: AuditSelection,
    results: Vec<ProbeResult>,
}

struct CheckpointSession {
    path: PathBuf,
    // Keep this stable sidecar inode locked while replacing the checkpoint itself.
    _lock: File,
    record: Option<AuditCheckpoint>,
}

impl CheckpointSession {
    fn open(args: &AuditContractsArgs) -> Result<Option<Self>> {
        let Some(path) = args.checkpoint.as_ref().or(args.resume_from.as_ref()) else {
            return Ok(None);
        };
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let parent = fs::canonicalize(parent)
            .with_context(|| format!("checkpoint directory {}", parent.display()))?;
        let name = path
            .file_name()
            .context("checkpoint requires a file name")?;
        let path = parent.join(name);
        let mut lock_name = name.to_os_string();
        lock_name.push(".lock");
        let lock_path = parent.join(lock_name);
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&lock_path)
            .with_context(|| format!("opening checkpoint lock {}", lock_path.display()))?;
        lock.try_lock()
            .with_context(|| format!("checkpoint is locked: {}", path.display()))?;
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| format!("checkpoint {}", path.display()));
            }
        };
        let record = if args.resume_from.is_some() {
            ensure!(
                metadata.is_some_and(|m| m.is_file()),
                "resume checkpoint must be a regular file: {}",
                path.display()
            );
            Some(
                serde_json::from_reader(File::open(&path)?)
                    .with_context(|| format!("reading checkpoint {}", path.display()))?,
            )
        } else {
            ensure!(
                metadata.is_none(),
                "checkpoint already exists: {}",
                path.display()
            );
            None
        };
        Ok(Some(Self {
            path,
            _lock: lock,
            record,
        }))
    }

    fn initialize(
        &mut self,
        provenance: &GraphProvenance,
        selection: AuditSelection,
        targets: &[ContractTarget],
    ) -> Result<()> {
        if let Some(record) = &self.record {
            ensure!(
                &record.provenance == provenance,
                "checkpoint input identity differs from fresh baseline"
            );
            ensure!(
                record.selection == selection,
                "checkpoint audit selection differs"
            );
            let mut seen = BTreeSet::new();
            for result in &record.results {
                ensure!(
                    targets.contains(&result.target),
                    "checkpoint contains an unselected target"
                );
                ensure!(
                    seen.insert(&result.target),
                    "checkpoint contains duplicate targets"
                );
                ensure!(
                    !matches!(result.verdict, ProbeVerdict::BudgetSkipped),
                    "checkpoint must not store budget skips"
                );
            }
        } else {
            self.record = Some(AuditCheckpoint {
                provenance: provenance.clone(),
                selection,
                results: Vec::new(),
            });
            self.write(true)?;
        }
        Ok(())
    }

    fn reusable(&self, target: &ContractTarget) -> Option<&ProbeResult> {
        self.record.as_ref()?.results.iter().find(|result| {
            &result.target == target
                && matches!(
                    result.verdict,
                    ProbeVerdict::RemovableUnderRemainingAssumptions
                        | ProbeVerdict::FailedFullAcceptance
                )
        })
    }

    fn save(&mut self, result: &ProbeResult) -> Result<()> {
        if matches!(
            result.verdict,
            ProbeVerdict::Inconclusive | ProbeVerdict::BudgetSkipped
        ) {
            return Ok(());
        }
        let record = self
            .record
            .as_mut()
            .expect("checkpoint initialized after baseline");
        record.results.retain(|saved| saved.target != result.target);
        record.results.push(result.clone());
        record
            .results
            .sort_by(|left, right| left.target.cmp(&right.target));
        self.write(false)
    }

    fn write(&self, create: bool) -> Result<()> {
        let mut temp =
            NamedTempFile::new_in(self.path.parent().expect("canonical checkpoint parent"))?;
        serde_json::to_writer(
            temp.as_file_mut(),
            self.record.as_ref().expect("checkpoint initialized"),
        )?;
        if create {
            temp.persist_noclobber(&self.path)
        } else {
            temp.persist(&self.path)
        }
        .with_context(|| format!("replacing checkpoint {}", self.path.display()))?;
        Ok(())
    }
}

pub(super) fn run(args: AuditContractsArgs) -> Result<()> {
    ensure!(args.max_probes > 0, "--max-probes must be positive");
    ensure!(
        !args.transform.dry_run && !args.transform.fail_fast,
        "audit-contracts always runs a dry-run full acceptance probe; omit --dry-run and --fail-fast"
    );
    let mut checkpoint = CheckpointSession::open(&args)?;
    let cli = args.transform.clone().resolve()?;
    let spec = load_transform_spec_source(&cli.spec_source)?;
    let policy = spec
        .chunk_export_purity
        .get(&args.policy_key)
        .with_context(|| {
            format!(
                "unknown chunk_export_purity policy key {:?}",
                args.policy_key
            )
        })?;
    let targets = select_targets(&args, policy)?;
    ensure!(
        !targets.is_empty(),
        "contract selection matched no contracts"
    );

    let eligible = targets.len().min(args.max_probes);
    eprintln!("audit-progress baseline start eligible={eligible} completed=0");
    let mut baseline_provenance = None;
    if let Err(error) = run_transform_probe(&cli, &spec, |_| Ok(()), &mut baseline_provenance) {
        eprintln!(
            "audit-progress baseline complete eligible={eligible} completed=0 verdict=failed"
        );
        return Err(error).context("baseline full acceptance failed; no removal probes were run");
    }
    eprintln!("audit-progress baseline complete eligible={eligible} completed=0 verdict=passed");
    let baseline_provenance =
        baseline_provenance.context("baseline recorded no input provenance")?;

    if let Some(checkpoint) = &mut checkpoint {
        checkpoint.initialize(
            &baseline_provenance,
            AuditSelection::from_args(&args),
            &targets,
        )?;
    }

    let mut results = Vec::with_capacity(targets.len());
    for (index, target) in targets.into_iter().enumerate() {
        if index >= args.max_probes {
            results.push(ProbeResult {
                target,
                verdict: ProbeVerdict::BudgetSkipped,
                error: None,
            });
            continue;
        }
        let ordinal = index + 1;
        baseline_provenance
            .verify(&CurrentRoots::default())
            .context("audit inputs changed after baseline")?;
        if let Some(saved) = checkpoint
            .as_ref()
            .and_then(|checkpoint| checkpoint.reusable(&target))
        {
            eprintln!(
                "audit-progress probe reused ordinal={}/{} target={target:?}",
                index + 1,
                eligible
            );
            results.push(saved.clone());
            continue;
        }
        eprintln!(
            "audit-progress probe start ordinal={ordinal}/{eligible} completed={index} target={target:?}"
        );
        let mut probe_provenance = None;
        let outcome = run_transform_probe(
            &cli,
            &spec,
            |candidate| remove_one(candidate, &target),
            &mut probe_provenance,
        );
        if let Some(provenance) = &probe_provenance
            && !same_inputs(&baseline_provenance, provenance)
        {
            eprintln!(
                "audit-progress probe complete ordinal={ordinal}/{eligible} completed={} verdict=aborted target={target:?}",
                ordinal
            );
            bail!("audit inputs changed between baseline and probe");
        }
        let (verdict, error) = match outcome {
            Ok(()) => (ProbeVerdict::RemovableUnderRemainingAssumptions, None),
            Err(error)
                if probe_provenance.is_some()
                    && !error.chain().any(|cause| cause.is::<std::io::Error>()) =>
            {
                (
                    ProbeVerdict::FailedFullAcceptance,
                    Some(format!("{error:#}")),
                )
            }
            Err(error) => (ProbeVerdict::Inconclusive, Some(format!("{error:#}"))),
        };
        eprintln!(
            "audit-progress probe complete ordinal={ordinal}/{eligible} completed={} verdict={} target={target:?}",
            ordinal,
            verdict.label()
        );
        let result = ProbeResult {
            target,
            verdict,
            error,
        };
        if let Some(checkpoint) = &mut checkpoint {
            baseline_provenance
                .verify(&CurrentRoots::default())
                .context("audit inputs changed before checkpoint update")?;
            checkpoint.save(&result)?;
        }
        results.push(result);
    }
    baseline_provenance
        .verify(&CurrentRoots::default())
        .context("audit inputs changed before report completion")?;
    let independently_probed = results
        .iter()
        .filter(|result| !matches!(result.verdict, ProbeVerdict::BudgetSkipped))
        .count();
    let budget_skipped = results.len() - independently_probed;
    let fingerprint = Sha256::digest(serde_json::to_vec(&baseline_provenance)?);
    let report = AuditReport {
        policy_key: args.policy_key,
        input_provenance_sha256: fingerprint
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        source_markers: policy.source_markers.clone(),
        baseline: "passed",
        scope: "each target removed separately for this exact source, spec, and assignment; all other author contracts and runtime assumptions remain; passing targets are not jointly removable or independently inferred",
        independently_probed,
        budget_skipped,
        results,
    };
    if OutputFormat::resolve(args.format) == OutputFormat::Ndjson {
        print_section("audit", &report)
    } else {
        emit_report(args.format, &report, render_text, "writing contract audit")
    }
}

fn same_inputs(left: &GraphProvenance, right: &GraphProvenance) -> bool {
    left == right
}

fn render_text(report: &AuditReport, out: &mut String) {
    out.push_str("baseline: passed\n");
    out.push_str(report.scope);
    out.push('\n');
    for result in &report.results {
        out.push_str(&format!("{:?}: {:?}\n", result.target, result.verdict));
        if let Some(error) = &result.error {
            out.push_str(&format!("  {error}\n"));
        }
    }
}

fn select_targets(
    args: &AuditContractsArgs,
    policy: &ChunkExportPurity,
) -> Result<Vec<ContractTarget>> {
    let mut all = BTreeSet::new();
    let key = &args.policy_key;
    macro_rules! add_exports {
        ($kind:ident, $field:ident) => {
            for export in &policy.$field {
                all.insert(ContractTarget {
                    policy_key: key.clone(),
                    kind: ContractKind::$kind,
                    export: export.clone(),
                    member: None,
                    signature: None,
                });
            }
        };
    }
    macro_rules! add_members {
        ($kind:ident, $field:ident) => {
            for (export, members) in &policy.$field {
                for member in members {
                    all.insert(ContractTarget {
                        policy_key: key.clone(),
                        kind: ContractKind::$kind,
                        export: export.clone(),
                        member: Some(member.clone()),
                        signature: None,
                    });
                }
            }
        };
    }
    macro_rules! add_signatures {
        ($kind:ident, $field:ident) => {
            for (export, signatures) in &policy.$field {
                for signature in signatures {
                    all.insert(ContractTarget {
                        policy_key: key.clone(),
                        kind: ContractKind::$kind,
                        export: export.clone(),
                        member: None,
                        signature: Some(signature.clone()),
                    });
                }
            }
        };
    }
    add_exports!(PureExports, pure_exports);
    add_exports!(PureNewExports, pure_new_exports);
    add_exports!(FluentExports, fluent_exports);
    add_exports!(CallbackFactoryExports, callback_factory_exports);
    add_exports!(
        InlineCallbackFactoryExports,
        inline_callback_factory_exports
    );
    add_exports!(NoReentryExports, no_reentry_exports);
    add_members!(PureMembers, pure_members);
    add_members!(CallbackFactoryMembers, callback_factory_members);
    add_members!(NoReentryMembers, no_reentry_members);
    add_members!(NoReentryRecordMembers, no_reentry_record_members);
    add_members!(NamespaceIndependentMembers, namespace_independent_members);
    add_signatures!(
        RetainedCallbackFactoryExports,
        retained_callback_factory_exports
    );
    add_signatures!(
        RetainedCallbackConstructorExports,
        retained_callback_constructor_exports
    );
    add_signatures!(NoReentryCallbackExports, no_reentry_callback_exports);
    for (export, members) in &policy.retained_callback_factory_members {
        for (member, signatures) in members {
            for signature in signatures {
                all.insert(ContractTarget {
                    policy_key: key.clone(),
                    kind: ContractKind::RetainedCallbackFactoryMembers,
                    export: export.clone(),
                    member: Some(member.clone()),
                    signature: Some(signature.clone()),
                });
            }
        }
    }
    Ok(all
        .into_iter()
        .filter(|target| {
            (args.kind.is_empty() || args.kind.contains(&target.kind))
                && args
                    .export
                    .as_ref()
                    .is_none_or(|export| export == &target.export)
                && args
                    .member
                    .as_ref()
                    .is_none_or(|member| target.member.as_ref() == Some(member))
                && args
                    .signature
                    .as_ref()
                    .is_none_or(|signature| target.signature.as_ref() == Some(signature))
        })
        .collect())
}

fn remove_one(spec: &mut TransformSpec, target: &ContractTarget) -> Result<()> {
    let policy = spec
        .chunk_export_purity
        .get_mut(&target.policy_key)
        .context("selected policy disappeared")?;
    let removed = match target.kind {
        ContractKind::PureExports => policy.pure_exports.remove(&target.export),
        ContractKind::PureNewExports => policy.pure_new_exports.remove(&target.export),
        ContractKind::FluentExports => policy.fluent_exports.remove(&target.export),
        ContractKind::CallbackFactoryExports => {
            policy.callback_factory_exports.remove(&target.export)
        }
        ContractKind::InlineCallbackFactoryExports => policy
            .inline_callback_factory_exports
            .remove(&target.export),
        ContractKind::NoReentryExports => policy.no_reentry_exports.remove(&target.export),
        ContractKind::PureMembers => remove_member(&mut policy.pure_members, target),
        ContractKind::CallbackFactoryMembers => {
            remove_member(&mut policy.callback_factory_members, target)
        }
        ContractKind::NoReentryMembers => remove_member(&mut policy.no_reentry_members, target),
        ContractKind::NoReentryRecordMembers => {
            remove_member(&mut policy.no_reentry_record_members, target)
        }
        ContractKind::NamespaceIndependentMembers => {
            remove_member(&mut policy.namespace_independent_members, target)
        }
        ContractKind::RetainedCallbackFactoryExports => {
            remove_signature(&mut policy.retained_callback_factory_exports, target)
        }
        ContractKind::RetainedCallbackConstructorExports => {
            remove_signature(&mut policy.retained_callback_constructor_exports, target)
        }
        ContractKind::NoReentryCallbackExports => {
            remove_signature(&mut policy.no_reentry_callback_exports, target)
        }
        ContractKind::RetainedCallbackFactoryMembers => {
            remove_member_signature(&mut policy.retained_callback_factory_members, target)
        }
    };
    if !removed {
        bail!("selected contract disappeared before probe: {target:?}");
    }
    Ok(())
}

fn remove_member(
    map: &mut std::collections::BTreeMap<String, BTreeSet<String>>,
    target: &ContractTarget,
) -> bool {
    let removed = map.get_mut(&target.export).is_some_and(|members| {
        target
            .member
            .as_ref()
            .is_some_and(|member| members.remove(member))
    });
    if map.get(&target.export).is_some_and(BTreeSet::is_empty) {
        map.remove(&target.export);
    }
    removed
}

fn remove_signature(
    map: &mut std::collections::BTreeMap<String, Vec<RetainedCallbackSignature>>,
    target: &ContractTarget,
) -> bool {
    let removed = map
        .get_mut(&target.export)
        .is_some_and(|signatures| remove_exact_signature(signatures, target.signature.as_ref()));
    if map.get(&target.export).is_some_and(Vec::is_empty) {
        map.remove(&target.export);
    }
    removed
}

fn remove_member_signature(
    map: &mut std::collections::BTreeMap<
        String,
        std::collections::BTreeMap<String, Vec<RetainedCallbackSignature>>,
    >,
    target: &ContractTarget,
) -> bool {
    let Some(member) = target.member.as_ref() else {
        return false;
    };
    let Some(members) = map.get_mut(&target.export) else {
        return false;
    };
    let removed = members
        .get_mut(member)
        .is_some_and(|signatures| remove_exact_signature(signatures, target.signature.as_ref()));
    if members.get(member).is_some_and(Vec::is_empty) {
        members.remove(member);
    }
    if members.is_empty() {
        map.remove(&target.export);
    }
    removed
}

fn remove_exact_signature(
    signatures: &mut Vec<RetainedCallbackSignature>,
    signature: Option<&RetainedCallbackSignature>,
) -> bool {
    let Some(signature) = signature else {
        return false;
    };
    let before = signatures.len();
    // Identical duplicate entries grant the same semantic contract. Remove
    // all copies of this exact signature and leave every other shape intact.
    signatures.retain(|candidate| candidate != signature);
    signatures.len() != before
}
