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

/// Every distinct `source_match` selector under a spec `modules/` root: member
/// `source_match` selectors and anonymous-statement selectors. (Binding-group
/// sugar is not expanded here yet — noted in the summary.)
fn load_selectors(specs_root: &Path) -> Result<BTreeSet<AnonymousStatementSelector>> {
    let mut selectors = BTreeSet::new();
    for path in spec_modules::collect_module_files(specs_root)? {
        let claims = spec_modules::read_module_claims(&path)
            .with_context(|| format!("reading claims from {}", path.display()))?;
        selectors.extend(claims.member_selectors);
        selectors.extend(claims.anonymous_selectors);
    }
    Ok(selectors)
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

    let mut tally = Tally {
        selectors: selectors.len(),
        ..Default::default()
    };
    let mut disagreements: Vec<Disagreement> = Vec::new();
    // Why each fail-closed selector is unsupported, with a few example needles —
    // this is the worklist of remaining rungs.
    let mut unsupported_reasons: BTreeMap<&'static str, (usize, Vec<String>)> = BTreeMap::new();

    for selector in &selectors {
        if !selector.wildcard_string_literals.is_empty() {
            tally.skipped_string_wildcards += 1;
            continue;
        }
        let Ok(needle_module) = js_ast::parse_js_module_ast("<needle>", &selector.match_source)
        else {
            tally.skipped_needle_parse += 1;
            continue;
        };
        if needle_module.body.len() != 1 {
            tally.skipped_multi_statement += 1;
            continue;
        }
        let needle_item = &needle_module.body[0];
        let Ok(needle_facts) = item_facts(needle_item) else {
            // The needle itself does not project — count as needle-parse-ish.
            tally.skipped_needle_parse += 1;
            continue;
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
