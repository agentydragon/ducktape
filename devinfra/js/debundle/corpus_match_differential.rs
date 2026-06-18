//! Corpus-wide matcher differential: the gate the goal names
//! (`plans/selector_constraint_model.md`). For every `source_match` selector in
//! a spec's `modules/` tree, and every top-level statement of one or more real
//! chunks, compare the fact-based matcher (`selector_match::matches` over
//! `chunk_facts`) against the production matcher
//! (`source_match::needle_matches`). The two must agree on every verdict among
//! the selectors the fact matcher claims to support; the unsupported set
//! (fail-closed `Unsupported`) is reported and must shrink toward empty.
//!
//! Run locally against the gaffer `tana/re` corpus (ducktape CI cannot read the
//! private corpus, so this is a measurement binary, not a checked-in test):
//!
//! ```text
//! bazelisk run //devinfra/js/debundle:corpus_match_differential -- \
//!     <spec-modules-dir> <chunk.js> [<chunk2.js> ...]
//! ```

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};
use source_match::{
    AstWildcardResolver, DatalogResolver, SelectorResolver,
    binding_group_anonymous_statement_selector, binding_group_member_selectors,
};
use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};
use swc_common::DUMMY_SP;
use swc_ecma_ast::{Module, ModuleItem};

fn mode_of(selector: &AnonymousStatementSelector) -> selector_match::Mode {
    match selector.identifiers {
        SourceMatchIdentifierMode::Exact => selector_match::Mode::Exact,
        SourceMatchIdentifierMode::AlphaAll => selector_match::Mode::AlphaAll,
    }
}

/// Facts for a single top-level statement: wrap it in a one-item module so the
/// extractor's `top_level` join (and the matcher's root anchoring) see exactly
/// one root.
fn item_facts(item: &ModuleItem) -> Result<chunk_facts::ChunkFacts, chunk_facts::Unsupported> {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    chunk_facts::extract_facts(&module)
}

/// A binding group as the atomic-resolution pipeline (`resolve_member_group`)
/// sees it: the group's `source_match` selector (no `target_binding`) plus the
/// `target_binding -> export_name` map.
struct GroupCase {
    request_id: String,
    selector: AnonymousStatementSelector,
    exports: BTreeMap<String, String>,
}

/// Member and anonymous-statement `source_match` selectors under a spec
/// `modules/` root, plus the binding groups kept intact for the atomic
/// `resolve_member_group` differential.
#[derive(Default)]
struct Selectors {
    members: BTreeSet<AnonymousStatementSelector>,
    anonymous: BTreeSet<AnonymousStatementSelector>,
    groups: Vec<GroupCase>,
}

fn load_selectors(specs_root: &Path) -> Result<Selectors> {
    let mut selectors = Selectors::default();
    for path in spec_modules::collect_module_files(specs_root)? {
        let request_id = spec_modules::module_path_from_file(&path, specs_root);
        let claims = spec_modules::read_module_claims(&path)
            .with_context(|| format!("reading claims from {}", path.display()))?;
        selectors.members.extend(claims.member_selectors);
        selectors.anonymous.extend(claims.anonymous_selectors);
        // Binding groups are sugar for several member selectors (one per
        // exported target). Expand them exactly as the run pipeline does so they
        // are measured too; a target_statements group is a multi-statement
        // anonymous selector instead.
        for group in &claims.binding_groups {
            if let Some(selector) = binding_group_anonymous_statement_selector(group) {
                selectors.anonymous.insert(selector);
            } else if let Ok(members) = binding_group_member_selectors(&request_id, group) {
                // Reconstruct the atomic group inputs from the expanded members:
                // `target_binding -> export_name`, and the group selector (any
                // member's selector with `target_binding` cleared).
                let exports: BTreeMap<String, String> = members
                    .iter()
                    .filter_map(|member| {
                        member
                            .selector
                            .target_binding
                            .clone()
                            .map(|target| (target, member.export_name.clone()))
                    })
                    .collect();
                if let Some(first) = members.first() {
                    let mut selector = first.selector.clone();
                    selector.target_binding = None;
                    selectors.groups.push(GroupCase {
                        request_id: request_id.clone(),
                        selector,
                        exports,
                    });
                }
                selectors
                    .members
                    .extend(members.into_iter().map(|member| member.selector));
            }
        }
    }
    Ok(selectors)
}

/// Resolver-level outcome comparison (mirrors `DifferentialResolver`'s agreement
/// rule, but split so the fail-closed worklist is distinguished from genuine
/// disagreements).
#[derive(Default)]
struct ResolverTally {
    resolved_parity: usize,
    reject_parity: usize,
    fail_closed: usize,
    over_resolved: usize,
    value_disagreements: usize,
}

fn classify_resolver<T: PartialEq>(
    tally: &mut ResolverTally,
    datalog: &anyhow::Result<T>,
    production: &anyhow::Result<T>,
) {
    match (datalog, production) {
        (Ok(d), Ok(p)) if d == p => tally.resolved_parity += 1,
        (Ok(_), Ok(_)) => tally.value_disagreements += 1,
        (Err(_), Err(_)) => tally.reject_parity += 1,
        (Err(_), Ok(_)) => tally.fail_closed += 1,
        (Ok(_), Err(_)) => tally.over_resolved += 1,
    }
}

#[derive(Default)]
struct Tally {
    selectors: usize,
    compared: usize,
    skipped_string_wildcards: usize,
    skipped_multi_statement: usize,
    skipped_needle_parse: usize,
    unsupported_needle: usize,
    pairs: usize,
    disagreements: usize,
}

struct Disagreement {
    selector: String,
    production_true: usize,
    fact_true: usize,
    subjects: usize,
}

fn run(specs_root: &Path, chunk_paths: &[String]) -> Result<()> {
    let selectors = load_selectors(specs_root)?;

    // Fast residual-path classification (no subjects needed): the resolver's
    // shape dispatch bails with "not yet handled" before touching the body, so
    // running it against an empty module surfaces exactly the member selectors
    // that still fail closed by shape. `CLASSIFY_ONLY=1` returns after this.
    if std::env::var("CLASSIFY_ONLY").is_ok() {
        let empty = Module {
            span: DUMMY_SP,
            body: vec![],
            shebang: None,
        };
        let mut residual: Vec<(&str, String)> = Vec::new();
        for selector in &selectors.members {
            if let Err(error) =
                DatalogResolver.resolve_member(&empty, "classify", "export", selector)
                && error.to_string().contains("not yet handled")
            {
                residual.push((selector.match_source.as_str(), error.to_string()));
            }
        }
        println!(
            "residual-path member selectors (still fail closed by shape): {}",
            residual.len()
        );
        for (source, reason) in &residual {
            println!("  --- {reason}");
            for line in source.lines() {
                println!("      {line}");
            }
        }
        return Ok(());
    }

    // Parse every chunk once; collect (item, facts) for each top-level statement
    // that the extractor can project (an unsupported chunk construct is skipped
    // as a subject and counted).
    let mut subjects: Vec<(ModuleItem, chunk_facts::ChunkFacts)> = Vec::new();
    let mut subject_unsupported = 0usize;
    for chunk_path in chunk_paths {
        let source = fs::read_to_string(chunk_path)
            .with_context(|| format!("reading chunk {chunk_path}"))?;
        let module = js_ast::parse_js_module_ast(chunk_path, &source)
            .with_context(|| format!("parsing chunk {chunk_path}"))?;
        for item in &module.body {
            match item_facts(item) {
                Ok(facts) => subjects.push((item.clone(), facts)),
                Err(_) => subject_unsupported += 1,
            }
        }
    }
    if subjects.is_empty() {
        bail!(
            "no extractable subject statements across {} chunk(s)",
            chunk_paths.len()
        );
    }
    // The production matcher re-parses each needle per call, so the full
    // selectors x statements product is large; cap subjects for a tractable
    // measurement (the support breakdown and disagreement signal need diverse
    // subjects, not exhaustive ones).
    if let Some(cap) = std::env::var("CORPUS_DIFF_MAX_SUBJECTS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        && subjects.len() > cap
    {
        subjects.truncate(cap);
    }

    let all: BTreeSet<AnonymousStatementSelector> = selectors
        .members
        .iter()
        .chain(&selectors.anonymous)
        .cloned()
        .collect();
    let mut tally = Tally {
        selectors: all.len(),
        ..Default::default()
    };
    let mut disagreements: Vec<Disagreement> = Vec::new();
    // Why each fail-closed selector is unsupported, with a few example needles —
    // this is the worklist of remaining rungs.
    let mut unsupported_reasons: BTreeMap<&'static str, (usize, Vec<String>)> = BTreeMap::new();
    let mut needle_parse_examples: Vec<String> = Vec::new();
    let mut needle_facts_unsupported: BTreeMap<&'static str, (usize, Vec<String>)> =
        BTreeMap::new();

    for selector in &all {
        if !selector.wildcard_string_literals.is_empty() {
            tally.skipped_string_wildcards += 1;
            continue;
        }
        let Ok(needle_module) = js_ast::parse_js_module_ast("<needle>", &selector.match_source)
        else {
            tally.skipped_needle_parse += 1;
            if needle_parse_examples.len() < 8 {
                needle_parse_examples.push(
                    selector
                        .match_source
                        .lines()
                        .next()
                        .unwrap_or("")
                        .to_string(),
                );
            }
            continue;
        };
        if needle_module.body.len() != 1 {
            tally.skipped_multi_statement += 1;
            continue;
        }
        let needle_item = &needle_module.body[0];
        let needle_facts = match item_facts(needle_item) {
            Ok(facts) => facts,
            Err(unsupported) => {
                // The needle parses but the extractor cannot project it (a
                // construct chunk_facts has not modeled).
                tally.skipped_needle_parse += 1;
                let entry = needle_facts_unsupported
                    .entry(unsupported.context)
                    .or_default();
                entry.0 += 1;
                if entry.1.len() < 4 {
                    entry.1.push(
                        selector
                            .match_source
                            .lines()
                            .next()
                            .unwrap_or("")
                            .to_string(),
                    );
                }
                continue;
            }
        };
        let mode = mode_of(selector);
        // Support probe: a needle with an unhandled construct (regex predicate,
        // misplaced run hole) errors uniformly — fail-closed, delegated.
        if let Err(unsupported) = selector_match::matches(&needle_facts, &needle_facts, mode) {
            tally.unsupported_needle += 1;
            let entry = unsupported_reasons.entry(unsupported.reason).or_default();
            entry.0 += 1;
            if entry.1.len() < 4 {
                entry.1.push(
                    selector
                        .match_source
                        .lines()
                        .next()
                        .unwrap_or("")
                        .to_string(),
                );
            }
            continue;
        }

        tally.compared += 1;
        let mut production_true = 0usize;
        let mut fact_true = 0usize;
        let mut disagreeing = 0usize;
        // `RESOLVER_ONLY=1` skips the per-subject matcher pass (the matcher core is
        // unchanged and already proven 0-disagreement) to re-validate just the
        // resolver pass quickly, uncapped.
        if std::env::var("RESOLVER_ONLY").is_err() {
            for (subject_item, subject_facts) in &subjects {
                tally.pairs += 1;
                let production = source_match::needle_matches(selector, subject_item);
                let fact = selector_match::matches(&needle_facts, subject_facts, mode)
                    .expect("supported needle never errors per-subject");
                production_true += production as usize;
                fact_true += fact as usize;
                if production != fact {
                    tally.disagreements += 1;
                    disagreeing += 1;
                }
            }
        }
        if disagreeing > 0 {
            disagreements.push(Disagreement {
                selector: selector
                    .match_source
                    .lines()
                    .next()
                    .unwrap_or("")
                    .to_string(),
                production_true,
                fact_true,
                subjects: disagreeing,
            });
        }
    }

    println!("corpus matcher differential");
    println!("  spec root:        {}", specs_root.display());
    println!("  chunks:           {}", chunk_paths.len());
    println!(
        "  subjects:         {} statements ({subject_unsupported} unsupported-by-extractor, skipped)",
        subjects.len(),
    );
    println!("  selectors:        {}", tally.selectors);
    println!("    compared:       {}", tally.compared);
    println!(
        "    unsupported (fail-closed, delegated): {}",
        tally.unsupported_needle
    );
    for (reason, (count, examples)) in &unsupported_reasons {
        println!("      {count:>4}  {reason}");
        for example in examples {
            println!("              e.g. {example}");
        }
    }
    println!(
        "    skipped multi-statement:   {}",
        tally.skipped_multi_statement
    );
    println!(
        "    skipped string-wildcards:  {}",
        tally.skipped_string_wildcards
    );
    println!(
        "    skipped needle-parse:      {}",
        tally.skipped_needle_parse
    );
    for example in &needle_parse_examples {
        println!("              e.g. (parse) {example}");
    }
    for (context, (count, examples)) in &needle_facts_unsupported {
        println!("      {count:>4}  needle facts unsupported: {context}");
        for example in examples {
            println!("              e.g. {example}");
        }
    }
    println!("  pairs compared:   {}", tally.pairs);
    println!("  DISAGREEMENTS:    {}", tally.disagreements);
    if disagreements.is_empty() {
        println!("\nGATE: zero disagreements across the compared selectors. ✅");
    } else {
        println!(
            "\nGATE: {} selector(s) disagree (production vs fact verdict differs on some subject):",
            disagreements.len(),
        );
        for d in disagreements.iter().take(40) {
            println!(
                "  [{} subj differ; prod_true={} fact_true={}]  {}",
                d.subjects, d.production_true, d.fact_true, d.selector,
            );
        }
        if disagreements.len() > 40 {
            println!("  … and {} more", disagreements.len() - 40);
        }
    }

    // Resolver-level differential: run the full SelectorResolver path both ways
    // (DatalogResolver vs AstWildcardResolver) over the same capped chunk body,
    // and classify the outcomes. This is end-to-end (claimed bindings), not just
    // per-statement verdicts.
    let resolver_module = Module {
        span: DUMMY_SP,
        body: subjects.iter().map(|(item, _)| item.clone()).collect(),
        shebang: None,
    };
    // `NEW_PATHS_ONLY=1` restricts the member pass to the shapes this change
    // affects — declarator-hole needles (carry `DECLARATORS`) and multi-statement
    // needles — so their real owners can be exercised uncapped quickly. The other
    // member shapes are unchanged and already proven parity-clean.
    let new_paths_only = std::env::var("NEW_PATHS_ONLY").is_ok();
    // `GROUPS_ONLY=1` runs only the atomic binding-group pass (skips the member
    // and anonymous passes) for a fast `resolve_member_group` validation.
    let groups_only = std::env::var("GROUPS_ONLY").is_ok();
    let affects_new_path = |selector: &AnonymousStatementSelector| {
        selector.match_source.contains("DECLARATORS")
            || js_ast::parse_js_module_ast("<n>", &selector.match_source)
                .is_ok_and(|module| module.body.len() > 1)
    };
    let mut members = ResolverTally::default();
    // Fail-closed (datalog Err, production Ok) member selectors — the resolver
    // worklist. Print the source + the datalog reason so the residual rungs are
    // identifiable, not just counted.
    let mut fail_closed_members: Vec<(String, String)> = Vec::new();
    // Genuine member disagreements (value-disagreement or over-resolved): the
    // claims diverge. These must be zero; print them so any are identifiable.
    let mut member_disagreements: Vec<(String, String, String)> = Vec::new();
    for selector in &selectors.members {
        if groups_only || (new_paths_only && !affects_new_path(selector)) {
            continue;
        }
        let datalog =
            DatalogResolver.resolve_member(&resolver_module, "corpus", "export", selector);
        let production =
            AstWildcardResolver.resolve_member(&resolver_module, "corpus", "export", selector);
        match (&datalog, &production) {
            (Err(reason), Ok(_)) => {
                fail_closed_members.push((selector.match_source.clone(), reason.to_string()))
            }
            (Ok(d), Ok(p)) if d != p => member_disagreements.push((
                selector.match_source.clone(),
                format!("{d:?}"),
                format!("{p:?}"),
            )),
            (Ok(d), Err(p)) => member_disagreements.push((
                selector.match_source.clone(),
                format!("{d:?}"),
                format!("Err({p})"),
            )),
            _ => {}
        }
        classify_resolver(&mut members, &datalog, &production);
    }
    let mut anonymous = ResolverTally::default();
    // The anonymous-statement path is unchanged by this work and already proven
    // parity-clean, so `NEW_PATHS_ONLY` / `GROUPS_ONLY` skip it.
    if !new_paths_only && !groups_only {
        for selector in &selectors.anonymous {
            classify_resolver(
                &mut anonymous,
                &DatalogResolver.resolve_anonymous_groups(&resolver_module, "corpus", selector),
                &AstWildcardResolver.resolve_anonymous_groups(&resolver_module, "corpus", selector),
            );
        }
    }
    // Atomic binding-group resolution (`resolve_member_group`) — the path the
    // materialize step uses (distinct from the expanded-member measurement).
    let mut groups = ResolverTally::default();
    let mut group_disagreements: Vec<(String, String, String)> = Vec::new();
    for case in &selectors.groups {
        let datalog = DatalogResolver.resolve_member_group(
            &resolver_module,
            &case.request_id,
            &case.selector,
            &case.exports,
        );
        let production = AstWildcardResolver.resolve_member_group(
            &resolver_module,
            &case.request_id,
            &case.selector,
            &case.exports,
        );
        match (&datalog, &production) {
            (Ok(d), Ok(p)) if d != p => group_disagreements.push((
                case.selector.match_source.clone(),
                format!("{d:?}"),
                format!("{p:?}"),
            )),
            (Ok(d), Err(p)) => group_disagreements.push((
                case.selector.match_source.clone(),
                format!("{d:?}"),
                format!("Err({p})"),
            )),
            _ => {}
        }
        classify_resolver(&mut groups, &datalog, &production);
    }
    for (label, count, tally) in [
        ("member", selectors.members.len(), &members),
        ("anonymous", selectors.anonymous.len(), &anonymous),
        ("binding-group", selectors.groups.len(), &groups),
    ] {
        println!("\nresolver differential ({label}, {count} selectors)");
        println!(
            "  resolved-parity (both claim the same owner): {}",
            tally.resolved_parity
        );
        println!(
            "  reject-parity   (both reject):                {}",
            tally.reject_parity
        );
        println!(
            "  fail-closed     (datalog Err, production Ok): {}",
            tally.fail_closed
        );
        println!(
            "  over-resolved   (datalog Ok, production Err): {}",
            tally.over_resolved
        );
        println!(
            "  VALUE DISAGREEMENTS (both Ok, differ):        {}",
            tally.value_disagreements
        );
    }
    if !fail_closed_members.is_empty() {
        println!("\nfail-closed member selectors (datalog Err, production Ok):");
        for (source, reason) in &fail_closed_members {
            println!("  --- reason: {reason}");
            for line in source.lines() {
                println!("      {line}");
            }
        }
    }
    for (label, disagreements) in [
        ("member", &member_disagreements),
        ("binding-group", &group_disagreements),
    ] {
        if !disagreements.is_empty() {
            println!("\nGENUINE {label} disagreements (datalog vs production claim):");
            for (source, datalog, production) in disagreements {
                println!("  --- datalog={datalog} production={production}");
                for line in source.lines() {
                    println!("      {line}");
                }
            }
        }
    }
    let genuine = members.value_disagreements
        + members.over_resolved
        + anonymous.value_disagreements
        + anonymous.over_resolved
        + groups.value_disagreements
        + groups.over_resolved;
    if genuine == 0 {
        println!(
            "\nRESOLVER GATE: 0 genuine disagreements; the rest is the fail-closed worklist. ✅"
        );
    } else {
        println!("\nRESOLVER GATE: {genuine} genuine disagreement(s) — investigate.");
    }
    Ok(())
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [specs_root, chunk_paths @ ..] = args.as_slice() else {
        bail!("usage: corpus_match_differential <spec-modules-dir> <chunk.js> [<chunk2.js> ...]");
    };
    if chunk_paths.is_empty() {
        bail!("usage: corpus_match_differential <spec-modules-dir> <chunk.js> [<chunk2.js> ...]");
    }
    js_ast::with_swc_globals(|| run(Path::new(specs_root), chunk_paths))
}
