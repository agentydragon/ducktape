//! Apply reviewed residual-owner proposals without transient name-pinned YAML.

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

use crate::binding::{apply_doc_changes, canonical_module_path, load_module_docs, matches_in_docs};
use crate::edit_gate::{Gate, post_edit_spec_from_docs};
use crate::outcome::{MutationOutcome, emit_gate_rejection_json, print_outcome_json};

#[derive(Debug, Args)]
pub struct ModulesApplyArgs {
    /// JSON array of reviewed proposal IDs, destinations and readable names; `-` reads stdin.
    #[arg(long)]
    batch: String,
    /// Root directory of per-module YAML files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    modules_root: PathBuf,
    /// Current owner graph, used to check proposal identity and the resulting partition.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    graph: PathBuf,
    /// Root for the chunk's source file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    source_root: PathBuf,
    /// Match the size cap used when generating the proposal IDs.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    size_cap_lines: usize,
    /// Validate and show the edit without writing YAML.
    #[arg(long)]
    dry_run: bool,
    /// Output format. Default text on tty, JSON on pipe.
    #[arg(long, value_enum)]
    format: Option<OutputFormat>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyRequest {
    proposal_id: String,
    /// Expected owner IDs from the reviewed proposal. Prevents an ID reused
    /// after a graph refresh from silently claiming a different owner set.
    owner_ids: Vec<String>,
    module: String,
    readable_names: BTreeMap<String, String>,
}

#[derive(Serialize)]
struct ApplyOutcome {
    #[serde(flatten)]
    outcome: MutationOutcome,
    proposals_applied: usize,
    bindings_claimed: usize,
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
    let graph = crate::load_owner_graph_report(&args.graph)?;
    let proposals = propose_from_graph(
        &graph,
        &ProposalOptions {
            owner_graph_path: args.graph.clone(),
            modules_root: args.modules_root.clone(),
            source_root: Some(args.source_root.clone()),
            size_cap_lines: args.size_cap_lines,
        },
    )?;
    let original_docs = load_module_docs(&args.modules_root)?;
    let mut docs = original_docs.clone();
    let mut selected_ids = BTreeSet::new();
    let mut selected_names = BTreeSet::new();
    let mut pending_by_source: BTreeMap<PathBuf, BTreeMap<String, BTreeMap<String, String>>> =
        BTreeMap::new();
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
        if proposal.owner_ids != request.owner_ids {
            bail!(
                "proposal {:?} owner IDs changed; rerun `debundle modules propose`",
                request.proposal_id
            );
        }
        if !proposal.landable_today
            || proposal.merge_into.is_some()
            || !proposal.anonymous_statement_owner_ids.is_empty()
        {
            bail!(
                "proposal {:?} is not a graph-ready binding-only claim; inspect `modules propose` diagnostics",
                request.proposal_id
            );
        }
        let destination = canonical_module_path(&request.module)?;
        if let Some(extends) = &proposal.extends_module_id
            && destination != *extends
        {
            bail!(
                "proposal {:?} extends module {:?}; use that destination",
                request.proposal_id,
                extends
            );
        }

        let mut residual_bindings = BTreeSet::new();
        for owner_id in &proposal.owner_ids {
            let node = graph
                .nodes
                .iter()
                .find(|node| node.id == *owner_id)
                .with_context(|| format!("proposal owner {owner_id:?} absent from graph"))?;
            if !graph.is_residual(&node.destination) {
                continue;
            }
            let location = node
                .source_location
                .as_ref()
                .with_context(|| format!("residual owner {owner_id:?} has no source location"))?;
            let source = args.source_root.join(&location.source_path);
            for binding in &node.declared_bindings {
                let name = binding.binding.to_string();
                if !residual_bindings.insert(name.clone()) {
                    bail!("binding {name:?} is declared by multiple proposal owners");
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
                pending_by_source
                    .entry(source.clone())
                    .or_default()
                    .entry(destination.clone())
                    .or_default()
                    .insert(name, readable.clone());
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
    }

    for (source, modules) in pending_by_source {
        let claims = selector_codemod::synthesize_residual_claims_for_modules(&source, &modules)
            .with_context(|| format!("synthesizing selectors for {}", source.display()))?;
        for (destination, claims) in claims {
            let (_, doc) = docs.entry(destination.clone()).or_insert_with(|| {
                (
                    args.modules_root.join(format!("{destination}.yaml")),
                    LogicalModule::default(),
                )
            });
            doc.source_matches.extend(claims);
        }
    }

    let deleted = BTreeSet::new();
    let gate = Gate::Run {
        graph: &args.graph,
        source_root: Some(&args.source_root),
    };
    if let Err(err) = gate.check(&args.modules_root, || {
        post_edit_spec_from_docs(&docs, &deleted)
    }) {
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
    };
    match OutputFormat::resolve(args.format) {
        OutputFormat::Text => println!(
            "{}: {} proposal(s), {} binding(s), {} file(s)",
            out.outcome.action,
            out.proposals_applied,
            out.bindings_claimed,
            out.outcome.files_written.len()
        ),
        format => print_outcome_json(&out, format)?,
    }
    Ok(())
}
