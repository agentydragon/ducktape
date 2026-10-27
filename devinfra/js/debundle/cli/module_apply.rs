//! Apply reviewed residual-source unit proposals without transient name-pinned YAML.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use clap::Args;
use peel::OutputFormat;
use peel::propose::{DEFAULT_SIZE_CAP_LINES, ProposalOptions, propose_from_graph};
use serde::{Deserialize, Serialize};
use spec::LogicalModule;
use spec_tree::ModuleTreeScope;

use crate::binding::{apply_doc_changes, canonical_module_path, load_module_docs, matches_in_docs};
use crate::edit_gate::{ExpectedResolvedMember, Gate, post_edit_spec_from_docs};
use crate::outcome::{MutationOutcome, emit_gate_rejection_json, print_outcome_json};

#[derive(Debug, Args)]
pub struct ModulesApplyArgs {
    /// JSON array of reviewed proposal IDs, destinations and readable names; `-` reads stdin.
    #[arg(long)]
    batch: String,
    /// Root directory of per-module YAML files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub(super) modules_root: PathBuf,
    /// Tree config that scopes this graph-backed edit to its chunk's module roots.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub(super) tree_config: Option<PathBuf>,
    /// Current dependency graph, used to check proposal identity and the resulting assignment.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub(super) graph: PathBuf,
    /// Root for the chunk's source file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub(super) source_root: PathBuf,
    /// Match the size cap used when generating the proposal IDs.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    size_cap_lines: usize,
    /// Validate and show the edit without writing YAML.
    #[arg(long)]
    pub(super) dry_run: bool,
    /// Output format. Default text on tty, JSON on pipe.
    #[arg(long, value_enum)]
    pub(super) format: Option<OutputFormat>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyRequest {
    proposal_id: String,
    /// Expected source unit IDs from the reviewed proposal. Prevents an ID reused
    /// after a graph refresh from silently claiming a different source unit set.
    source_unit_ids: Vec<String>,
    module: String,
    readable_names: BTreeMap<String, String>,
    /// Optional per-binding author choice. Omitted bindings use synthesis.
    #[serde(default)]
    selectors: BTreeMap<String, ApplySelectorOverride>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", content = "selector", rename_all = "snake_case")]
enum ApplySelectorOverride {
    SourceMatch(spec::SourceMatchClaim),
    Relational(Box<spec::MemberSelector>),
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
enum SelectorOrigin {
    Synthesized,
    Provided,
}

#[derive(Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum SelectorReview {
    SourceMatch {
        module: String,
        source_file: PathBuf,
        binding_ids: Vec<String>,
        origin: SelectorOrigin,
        claim: spec::SourceMatchClaim,
        review_warnings: Vec<&'static str>,
    },
    Relational {
        module: String,
        source_file: PathBuf,
        binding_id: String,
        readable_name: String,
        selector: Box<spec::MemberSelector>,
        review_warnings: Vec<&'static str>,
    },
}

#[derive(Serialize)]
struct ApplyOutcome {
    #[serde(flatten)]
    outcome: MutationOutcome,
    proposals_applied: usize,
    bindings_claimed: usize,
    selector_reviews: Vec<SelectorReview>,
}

pub fn run_modules_apply(args: ModulesApplyArgs) -> Result<()> {
    let json = if args.batch == "-" {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text)?;
        text
    } else {
        fs::read_to_string(&args.batch).with_context(|| format!("reading {}", args.batch))?
    };
    let requests: Vec<ApplyRequest> = serde_json::from_str(&json).context("parsing apply batch")?;
    if requests.is_empty() {
        bail!("apply batch is empty");
    }
    let graph = crate::load_dependency_graph_report(&args.graph)?;
    let scope = args
        .tree_config
        .as_deref()
        .map(|config| ModuleTreeScope::from_config(config, &graph.chunk_id))
        .transpose()?;
    let proposals = propose_from_graph(
        &graph,
        &ProposalOptions {
            dependency_graph_path: args.graph.clone(),
            modules_root: args.modules_root.clone(),
            tree_config: args.tree_config.clone(),
            source_root: Some(args.source_root.clone()),
            size_cap_lines: args.size_cap_lines,
        },
    )?;
    let mut original_docs = load_module_docs(&args.modules_root)?;
    if let Some(scope) = &scope {
        original_docs.retain(|path, _| scope.contains(std::path::Path::new(path)));
    }
    let mut docs = original_docs.clone();
    let mut selected_ids = BTreeSet::new();
    let mut selected_names = BTreeSet::new();
    let mut pending_by_source: BTreeMap<PathBuf, BTreeMap<String, BTreeMap<String, String>>> =
        BTreeMap::new();
    let mut provided = Vec::new();
    let mut expected = Vec::new();
    let mut selector_reviews = Vec::new();
    let mut bindings_claimed = 0;

    for request in &requests {
        if !selected_ids.insert(request.proposal_id.clone()) {
            bail!(
                "duplicate proposal ID {:?} in apply batch",
                request.proposal_id
            );
        }
        let proposal = proposals
            .proposals
            .iter()
            .find(|proposal| proposal.proposed_module_id == request.proposal_id)
            .with_context(|| {
                format!(
                    "proposal {:?} is absent from current graph; rerun `debundle modules propose`",
                    request.proposal_id
                )
            })?;
        if proposal.source_unit_ids != request.source_unit_ids {
            bail!(
                "proposal {:?} source unit IDs changed; rerun `debundle modules propose`",
                request.proposal_id
            );
        }
        if !proposal.landable_today
            || proposal.merge_into.is_some()
            || !proposal.anonymous_statement_source_unit_ids.is_empty()
        {
            bail!(
                "proposal {:?} is not a graph-ready binding-only claim; inspect `modules propose` diagnostics",
                request.proposal_id
            );
        }
        let destination = if let Some(scope) = &scope {
            scope.canonical_destination(&request.module)?
        } else {
            canonical_module_path(&request.module)?
        };
        let logical_destination = if let Some(scope) = &scope {
            scope.logical_destination(&destination)?
        } else {
            &destination
        };
        if let Some(extends) = &proposal.extends_module_id
            && logical_destination != extends
        {
            bail!(
                "proposal {:?} extends module {:?}; use that destination",
                request.proposal_id,
                extends
            );
        }

        let mut residual_bindings = BTreeSet::new();
        for source_unit_id in &proposal.source_unit_ids {
            let node = graph
                .nodes
                .iter()
                .find(|node| node.id == *source_unit_id)
                .with_context(|| {
                    format!("proposal source unit {source_unit_id:?} absent from graph")
                })?;
            if !graph.is_unassigned_or_entry(&node.destination) {
                continue;
            }
            let location = node.source_location.as_ref().with_context(|| {
                format!("residual source unit {source_unit_id:?} has no source location")
            })?;
            let source = args.source_root.join(&location.source_path);
            for binding in &node.declared_bindings {
                let name = binding.binding.to_string();
                if !residual_bindings.insert(name.clone()) {
                    bail!("binding {name:?} is declared by multiple proposal source units");
                }
                let readable = request.readable_names.get(&name).with_context(|| {
                    format!(
                        "proposal {:?} needs a readable name for {name:?}",
                        request.proposal_id
                    )
                })?;
                if readable.is_empty() {
                    bail!("readable name for {name:?} is empty");
                }
                if !selected_names.insert(name.clone())
                    || (readable != &name && !selected_names.insert(readable.clone()))
                {
                    bail!("binding or readable name {name:?}/{readable:?} repeats in apply batch");
                }
                for spelling in [&name, readable] {
                    if !matches_in_docs(&original_docs, spelling).is_empty() {
                        bail!("binding name {spelling:?} is already claimed in YAML");
                    }
                }
                expected.push(ExpectedResolvedMember {
                    module_path: args.modules_root.join(format!("{destination}.yaml")),
                    export_name: readable.clone(),
                    binding_name: name.clone(),
                });
                if let Some(selector) = request.selectors.get(&name) {
                    provided.push((
                        source.clone(),
                        destination.clone(),
                        name,
                        readable.clone(),
                        selector.clone(),
                    ));
                } else {
                    pending_by_source
                        .entry(source.clone())
                        .or_default()
                        .entry(destination.clone())
                        .or_default()
                        .insert(name, readable.clone());
                }
                bindings_claimed += 1;
            }
        }
        let supplied = request
            .readable_names
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        if residual_bindings.is_empty() || supplied != residual_bindings {
            bail!(
                "proposal {:?} readable_names must cover exactly its residual bindings: expected {:?}, got {:?}",
                request.proposal_id,
                residual_bindings,
                supplied
            );
        }
        for binding in request.selectors.keys() {
            if !residual_bindings.contains(binding) {
                bail!("selector override for {binding:?} is not in the selected proposal");
            }
        }
    }

    for (source, modules) in &pending_by_source {
        let claims = selector_codemod::synthesize_residual_claims_for_modules(source, modules)
            .with_context(|| format!("synthesizing selectors for {}", source.display()))?;
        for (destination, claims) in claims {
            let (_, doc) = docs.entry(destination.clone()).or_insert_with(|| {
                (
                    args.modules_root.join(format!("{destination}.yaml")),
                    LogicalModule::default(),
                )
            });
            for claim in claims {
                let binding_ids = claim
                    .bindings
                    .iter()
                    .map(|binding| {
                        modules[&destination]
                            .iter()
                            .find(|(_, readable)| readable.as_str() == binding.name())
                            .map(|(runtime, _)| runtime.clone())
                            .with_context(|| {
                                format!(
                                    "synthesized selector has unexpected export {}",
                                    binding.name()
                                )
                            })
                    })
                    .collect::<Result<Vec<_>>>()?;
                selector_reviews.push(SelectorReview::SourceMatch {
                    module: destination.clone(),
                    source_file: source.clone(),
                    binding_ids,
                    origin: SelectorOrigin::Synthesized,
                    claim: claim.clone(),
                    review_warnings: vec![
                        "Uniqueness is proven for this source snapshot; review retained literals and property names as rebuild anchors.",
                    ],
                });
                doc.source_matches.push(claim);
            }
        }
    }

    for (source, destination, runtime, readable, choice) in provided {
        let (_, doc) = docs.entry(destination.clone()).or_insert_with(|| {
            (
                args.modules_root.join(format!("{destination}.yaml")),
                LogicalModule::default(),
            )
        });
        match choice {
            ApplySelectorOverride::SourceMatch(claim) => {
                let [binding] = claim.bindings.as_slice() else {
                    bail!("source_match override for {runtime:?} must claim exactly one binding");
                };
                if binding.name() != readable {
                    bail!(
                        "source_match override for {runtime:?} exports {:?}, expected {readable:?}",
                        binding.name()
                    );
                }
                if binding.local() == runtime && readable != runtime {
                    bail!(
                        "source_match override for {runtime:?} retains its minified local name; use a neutral selector-local name"
                    );
                }
                selector_reviews.push(SelectorReview::SourceMatch {
                    module: destination,
                    source_file: source,
                    binding_ids: vec![runtime],
                    origin: SelectorOrigin::Provided,
                    claim: claim.clone(),
                    review_warnings: vec![
                        "Uniqueness is proven for this source snapshot; review retained literals and property names as rebuild anchors.",
                    ],
                });
                doc.source_matches.push(claim);
            }
            ApplySelectorOverride::Relational(selector) => {
                if matches!(selector.selected()?, spec::MemberSelectorSpec::Binding(_)) {
                    bail!("relational override for {runtime:?} cannot use a binding name pin");
                }
                selector_reviews.push(SelectorReview::Relational {
                    module: destination,
                    source_file: source,
                    binding_id: runtime,
                    readable_name: readable.clone(),
                    selector: selector.clone(),
                    review_warnings: vec![
                        "Review the referenced member and exact key names as rebuild anchors.",
                    ],
                });
                doc.members.push(spec::Member {
                    name: Some(readable),
                    selector: *selector,
                });
            }
        }
    }

    let deleted = BTreeSet::new();
    let gate = Gate::Run {
        graph: &args.graph,
        source_root: Some(&args.source_root),
    };
    if let Err(err) = gate.check_expected(
        &args.modules_root,
        || post_edit_spec_from_docs(&docs, &deleted),
        &expected,
    ) {
        emit_gate_rejection_json("apply", args.format, &err);
        return Err(err);
    }
    let (files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &deleted, args.dry_run)?;
    let out = ApplyOutcome {
        outcome: MutationOutcome {
            verb: "apply",
            action: if args.dry_run { "dry-run" } else { "applied" },
            gate: gate.outcome(),
            files_written,
            files_deleted,
        },
        proposals_applied: requests.len(),
        bindings_claimed,
        selector_reviews,
    };
    match OutputFormat::resolve(args.format) {
        OutputFormat::Text => {
            println!(
                "{}: {} proposal(s), {} binding(s), {} file(s)",
                out.outcome.action,
                out.proposals_applied,
                out.bindings_claimed,
                out.outcome.files_written.len()
            );
            for review in &out.selector_reviews {
                println!("{}", serde_yaml::to_string(review)?.trim_end());
            }
        }
        format => print_outcome_json(&out, format)?,
    }
    Ok(())
}
