//! Runs the phases on one chunk and renders the measurement report.

use std::collections::BTreeMap;
use std::time::Instant;

use anyhow::{Context, Result};
use serde::Serialize;

use crate::effects::{Cell, Effects, summarize};
use crate::generate::{FunctionHeap, generate};
use crate::model::{ANY, Model, ObjKind, REASONS, Reason, SolveStats};
use crate::orders::{Modules, OrderCost, cost};
use crate::pairs::{ESCAPED, PairCheck, flag_names, positions};
use crate::tier0::{self, ImpureCause, Kind};

const EXAMPLE_LIMIT: usize = 3;

#[derive(Serialize, Default)]
pub struct Timings {
    pub parse_ms: f64,
    pub generate_ms: f64,
    pub solve_ms: f64,
    pub summarize_ms: f64,
    pub pair_check_ms: f64,
}

#[derive(Serialize)]
pub struct Sizes {
    pub source_bytes: usize,
    pub statements: usize,
    pub units: usize,
    pub nodes: usize,
    pub objects: usize,
    pub functions: usize,
    pub constraints_generated: usize,
    pub constraints_total: usize,
    pub variables: usize,
    pub cell_variables: usize,
    pub cells: usize,
    pub points_to_entries: usize,
    pub solve: SolveStats,
    pub effects: crate::effects::EffectStats,
}

#[derive(Serialize, Default)]
pub struct PairCounts {
    pub pairs: u64,
    pub conflicting: u64,
    pub proven_commuting: u64,
    /// Conflicting pairs by the exact set of conflict classes.
    pub by_classes: BTreeMap<String, u64>,
    /// Conflicting pairs whose only class is a call into Escaped.
    pub escaped_only: u64,
}

#[derive(Serialize)]
pub struct Degrees {
    pub min: usize,
    pub median: usize,
    pub p90: usize,
    pub max: usize,
    pub mean: f64,
}

#[derive(Serialize)]
pub struct PairExample {
    pub classes: String,
    pub first: usize,
    pub later: usize,
    pub first_source: String,
    pub later_source: String,
    pub witness: Option<String>,
    pub first_reasons: Vec<Reason>,
    pub later_reasons: Vec<Reason>,
}

#[derive(Serialize)]
pub struct WhatIf {
    /// The reasons assumed never to call into Escaped.
    pub resolved: Vec<Reason>,
    pub opaque_statements: usize,
    pub all_pairs_conflicting: u64,
    pub swapped_pairs_conflicting: Option<u64>,
}

#[derive(Serialize)]
pub struct ReasonCount {
    pub reason: Reason,
    pub sites: u64,
    /// Statements whose initialization reaches a site with this reason.
    pub statements: usize,
    /// Statements for which this is the only reason.
    pub sole_reason_statements: usize,
    pub examples: Vec<String>,
}

#[derive(Serialize)]
pub struct CurrentComparison {
    pub current_conflict_pairs: u64,
    pub current_modeled_pairs: u64,
    pub current_havoc_only_pairs: u64,
    pub both_conflict: u64,
    pub current_only: u64,
    pub prototype_only: u64,
    /// Current modeled-cell conflicts the prototype does not report.
    pub missed_modeled: Vec<MissedPair>,
}

#[derive(Serialize)]
pub struct MissedPair {
    pub first: usize,
    pub later: usize,
    pub first_source: String,
    pub later_source: String,
    pub current_cells: Vec<String>,
}

#[derive(Serialize)]
pub struct Report {
    pub chunk: String,
    pub function_heap: FunctionHeap,
    pub timings: Timings,
    pub peak_rss_kib: Option<u64>,
    pub sizes: Sizes,
    pub opaque_statements: usize,
    /// Non-opaque statements touching a cell the Escaped summary conflicts on.
    pub exposed_statements: usize,
    pub havoc_statements: usize,
    pub all_pairs: PairCounts,
    pub conflict_degree: Degrees,
    pub swapped_pairs: Option<PairCounts>,
    pub order_cost: Option<OrderCost>,
    pub reasons: Vec<ReasonCount>,
    pub builtin_sites: Vec<(String, u64)>,
    pub what_if: Vec<WhatIf>,
    pub examples: Vec<PairExample>,
    pub current: Option<CurrentComparison>,
}

pub struct Inputs<'a> {
    pub chunk: &'a str,
    pub source: String,
    /// The emitted order, as source-unit ordinals.
    pub order: Option<Vec<usize>>,
    /// Rows of the current analysis's `--effect-conflicts-jsonl` export.
    pub current_conflicts: Option<Vec<serde_json::Value>>,
    pub function_heap: FunctionHeap,
    /// The pipeline's `modules.json` for the same assignment.
    pub modules: Option<serde_json::Value>,
    /// Include source text, identifiers and property names in the report.
    pub examples: bool,
    /// Where to write the unproven swapped pairs, one
    /// `earlier<TAB>later<TAB>class` line each, in source ordinals.
    pub pairs_out: Option<std::path::PathBuf>,
}

pub struct Analysis {
    pub model: Model,
    pub effects: Effects,
    pub check: PairCheck,
    pub statement_spans: Vec<(u32, u32)>,
    pub timings: Timings,
    pub constraints_generated: usize,
}

fn elapsed_ms(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

pub fn analyze(chunk: &str, source: &str, function_heap: FunctionHeap) -> Result<Analysis> {
    let mut timings = Timings::default();
    let start = Instant::now();
    let parsed = js_ast::parse_js_module(chunk, source)?;
    timings.parse_ms = elapsed_ms(start);

    let start = Instant::now();
    let generated = generate(&parsed, function_heap);
    timings.generate_ms = elapsed_ms(start);
    let mut model = generated.model;
    let constraints_generated = model.cxs.len();

    let start = Instant::now();
    model.solve();
    timings.solve_ms = elapsed_ms(start);

    let statement_count = generated.statement_spans.len();
    let start = Instant::now();
    let mut effects = summarize(&model, statement_count, generated.escaped_unit);
    timings.summarize_ms = elapsed_ms(start);

    let start = Instant::now();
    let check = PairCheck::new(&model, &mut effects, statement_count);
    timings.pair_check_ms = elapsed_ms(start);
    Ok(Analysis {
        model,
        effects,
        check,
        statement_spans: generated.statement_spans,
        timings,
        constraints_generated,
    })
}

fn snippet(source: &str, lo: u32, limit: usize) -> String {
    // SWC byte positions start at 1.
    let start = (lo as usize).saturating_sub(1).min(source.len());
    let mut end = (start + limit).min(source.len());
    while !source.is_char_boundary(end) {
        end -= 1;
    }
    let mut start = start;
    while !source.is_char_boundary(start) {
        start -= 1;
    }
    source[start..end].replace('\n', " ")
}

fn reasons_of(mask: u16) -> Vec<Reason> {
    REASONS
        .into_iter()
        .filter(|reason| mask & reason.bit() != 0)
        .collect()
}

fn describe_cell(model: &Model, source: &str, cell: Cell) -> String {
    let object = |obj: u32| {
        let info = &model.objs[obj as usize];
        let kind = match info.kind {
            ObjKind::Function(_) => "function",
            ObjKind::Array => "array",
            ObjKind::Plain => "object",
            ObjKind::Regex => "regex",
            ObjKind::NativeResult => "native result",
            _ => "unknown",
        };
        format!("{kind} `{}`", snippet(source, info.span_lo, 40))
    };
    match cell {
        Cell::Var(var) => format!("binding `{}`", model.vars[var as usize].name),
        Cell::Field(obj, prop) => {
            format!("property `{}` of {}", model.props.name(prop), object(obj))
        }
        Cell::Star(obj) | Cell::AnyOf(obj) => format!("any property of {}", object(obj)),
        Cell::Elems(obj) => format!("elements of {}", object(obj)),
        Cell::UnknownHeap => "unmodeled heap".to_string(),
        Cell::Intrinsic(prop) => format!("intrinsic property `{}`", model.props.name(prop)),
        Cell::IntrinsicStar | Cell::AnyIntrinsic => "any intrinsic property".to_string(),
    }
}

fn count_pairs(
    check: &PairCheck,
    resolved: u16,
    swapped_only: Option<&[usize]>,
    mut visit: impl FnMut(usize, usize, u8),
) -> PairCounts {
    let mut counts = PairCounts::default();
    let mut by_flags: BTreeMap<u8, u64> = BTreeMap::new();
    for low in 0..check.n {
        for high in low + 1..check.n {
            if let Some(positions) = swapped_only
                && positions[low] < positions[high]
            {
                continue;
            }
            counts.pairs += 1;
            let flags = check.flags(low, high, resolved);
            if flags == 0 {
                counts.proven_commuting += 1;
                continue;
            }
            counts.conflicting += 1;
            if flags == ESCAPED {
                counts.escaped_only += 1;
            }
            *by_flags.entry(flags).or_default() += 1;
            visit(low, high, flags);
        }
    }
    counts.by_classes = by_flags
        .into_iter()
        .map(|(flags, count)| (flag_names(flags), count))
        .collect();
    counts
}

fn peak_rss_kib() -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let line = status.lines().find(|line| line.starts_with("VmHWM:"))?;
    line.split_whitespace().nth(1)?.parse().ok()
}

fn row_unit(row: &serde_json::Value, side: &str) -> Result<usize> {
    row[side]["unit"]
        .as_u64()
        .map(|unit| unit as usize)
        .with_context(|| format!("conflict row lacks `{side}.unit`"))
}

pub fn report(inputs: Inputs<'_>) -> Result<Report> {
    let Inputs {
        chunk,
        source,
        order,
        current_conflicts,
        function_heap,
        modules,
        examples: with_examples,
        pairs_out,
    } = inputs;
    let Analysis {
        model,
        effects,
        check,
        statement_spans,
        timings,
        constraints_generated,
    } = analyze(chunk, &source, function_heap)?;
    let n = check.n;
    let statement_source = |statement: usize| snippet(&source, statement_spans[statement].0, 100);
    let positions = order.as_deref().map(|order| positions(order, n));

    let mut degree = vec![0usize; n];
    let mut examples: Vec<PairExample> = Vec::new();
    let mut example_counts: BTreeMap<u8, usize> = BTreeMap::new();
    let mut example = |low: usize, high: usize, flags: u8| {
        let seen = example_counts.entry(flags).or_default();
        if *seen >= EXAMPLE_LIMIT {
            return;
        }
        *seen += 1;
        examples.push(PairExample {
            classes: flag_names(flags),
            first: low,
            later: high,
            first_source: statement_source(low),
            later_source: statement_source(high),
            witness: check
                .witness(low, high)
                .map(|cell| describe_cell(&model, &source, cell)),
            first_reasons: reasons_of(check.reasons[low]),
            later_reasons: reasons_of(check.reasons[high]),
        });
    };
    let swapped_pairs = positions
        .as_deref()
        .map(|positions| count_pairs(&check, 0, Some(positions), &mut example));
    let all_pairs = count_pairs(&check, 0, None, |low, high, flags| {
        degree[low] += 1;
        degree[high] += 1;
        if positions.is_none() {
            example(low, high, flags);
        }
    });
    let modules = modules
        .as_ref()
        .map(|modules| Modules::parse(modules, n))
        .transpose()?;
    let mut unproven = String::new();
    let order_cost = positions.as_deref().map(|positions| {
        cost(
            positions,
            modules.as_ref(),
            |low, high| {
                Some(check.flags(low, high, 0))
                    .filter(|flags| *flags != 0)
                    .map(flag_names)
            },
            |low, high| {
                if pairs_out.is_some() {
                    let class = flag_names(check.flags(low, high, 0));
                    unproven.push_str(&format!("{low}\t{high}\t{class}\n"));
                }
            },
        )
    });
    if let Some(path) = &pairs_out {
        std::fs::write(path, &unproven).with_context(|| format!("writing {}", path.display()))?;
    }
    let mut sorted_degree = degree.clone();
    sorted_degree.sort_unstable();
    let conflict_degree = Degrees {
        min: sorted_degree.first().copied().unwrap_or(0),
        median: sorted_degree.get(n / 2).copied().unwrap_or(0),
        p90: sorted_degree.get(n * 9 / 10).copied().unwrap_or(0),
        max: sorted_degree.last().copied().unwrap_or(0),
        mean: degree.iter().sum::<usize>() as f64 / n.max(1) as f64,
    };

    let reasons = REASONS
        .into_iter()
        .map(|reason| ReasonCount {
            reason,
            sites: effects.opaque_sites.get(&reason).copied().unwrap_or(0),
            statements: check
                .reasons
                .iter()
                .filter(|mask| *mask & reason.bit() != 0)
                .count(),
            sole_reason_statements: check
                .reasons
                .iter()
                .filter(|mask| **mask == reason.bit())
                .count(),
            examples: effects
                .reason_examples
                .get(&reason)
                .into_iter()
                .flatten()
                .filter(|span_lo| with_examples && **span_lo != 0)
                .map(|span_lo| snippet(&source, *span_lo, 80))
                .collect(),
        })
        .collect();
    let mut builtin_sites: Vec<(String, u64)> = effects
        .builtin_sites
        .iter()
        .map(|(name, count)| {
            let name = if *name == ANY {
                "<computed>"
            } else {
                model.props.name(*name)
            };
            (name.to_string(), *count)
        })
        .collect();
    builtin_sites.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    builtin_sites.truncate(40);

    let library = [Reason::Builtin, Reason::GlobalCall, Reason::GlobalMember];
    let imports = [Reason::ImportCall, Reason::ImportMember];
    let mut scenarios: Vec<Vec<Reason>> = REASONS.into_iter().map(|reason| vec![reason]).collect();
    scenarios.push(library.to_vec());
    scenarios.push(imports.to_vec());
    scenarios.push([library.as_slice(), imports.as_slice()].concat());
    scenarios.push(
        [
            library.as_slice(),
            imports.as_slice(),
            &[Reason::EscapedObject],
        ]
        .concat(),
    );
    scenarios.push(REASONS.to_vec());
    let what_if = scenarios
        .into_iter()
        .map(|resolved| {
            let mask = resolved.iter().fold(0, |mask, reason| mask | reason.bit());
            WhatIf {
                opaque_statements: (0..n).filter(|unit| check.opaque(*unit, mask)).count(),
                all_pairs_conflicting: count_pairs(&check, mask, None, |_, _, _| {}).conflicting,
                swapped_pairs_conflicting: positions.as_deref().map(|positions| {
                    count_pairs(&check, mask, Some(positions), |_, _, _| {}).conflicting
                }),
                resolved,
            }
        })
        .collect();

    let current = match current_conflicts {
        Some(rows) => {
            let positions = positions
                .as_deref()
                .context("comparing with the current analysis needs the emitted order")?;
            let mut comparison = CurrentComparison {
                current_conflict_pairs: rows.len() as u64,
                current_modeled_pairs: 0,
                current_havoc_only_pairs: 0,
                both_conflict: 0,
                current_only: 0,
                prototype_only: 0,
                missed_modeled: Vec::new(),
            };
            for row in &rows {
                let (first, later) = (row_unit(row, "runs_first")?, row_unit(row, "runs_later")?);
                anyhow::ensure!(
                    positions[first] < positions[later] && first > later,
                    "conflict row {first}->{later} is not an inversion of the emitted order",
                );
                let cells = row["cells"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or_default();
                let conflict = check.flags(first, later, 0) != 0;
                if conflict {
                    comparison.both_conflict += 1;
                } else {
                    comparison.current_only += 1;
                }
                if cells.is_empty() {
                    comparison.current_havoc_only_pairs += 1;
                } else {
                    comparison.current_modeled_pairs += 1;
                    if !conflict {
                        comparison.missed_modeled.push(MissedPair {
                            first,
                            later,
                            first_source: if with_examples {
                                statement_source(first)
                            } else {
                                String::new()
                            },
                            later_source: if with_examples {
                                statement_source(later)
                            } else {
                                String::new()
                            },
                            current_cells: cells
                                .iter()
                                .filter(|_| with_examples)
                                .map(|cell| cell["cell"].to_string())
                                .collect(),
                        });
                    }
                }
            }
            let swapped_conflicting = swapped_pairs
                .as_ref()
                .map_or(0, |counts| counts.conflicting);
            comparison.prototype_only = swapped_conflicting - comparison.both_conflict;
            Some(comparison)
        }
        None => None,
    };

    Ok(Report {
        chunk: file_name(chunk),
        function_heap,
        timings,
        peak_rss_kib: peak_rss_kib(),
        sizes: Sizes {
            source_bytes: source.len(),
            statements: n,
            units: model.units.len(),
            nodes: model.pts.len(),
            objects: model.objs.len(),
            functions: model.fns.len(),
            constraints_generated,
            constraints_total: model.cxs.len(),
            variables: model.vars.len(),
            cell_variables: effects.cell_vars.len(),
            cells: check.cells.len(),
            points_to_entries: model.pts.iter().map(|set| set.len()).sum(),
            solve: model.stats.clone(),
            effects: effects.stats.clone(),
        },
        opaque_statements: (0..n).filter(|unit| check.opaque(*unit, 0)).count(),
        exposed_statements: (0..n)
            .filter(|unit| !check.opaque(*unit, 0) && check.exposed[*unit] != 0)
            .count(),
        havoc_statements: check.havoc.iter().filter(|havoc| **havoc).count(),
        all_pairs,
        conflict_degree,
        swapped_pairs,
        order_cost,
        reasons,
        builtin_sites: if with_examples {
            builtin_sites
        } else {
            Vec::new()
        },
        what_if,
        examples: if with_examples { examples } else { Vec::new() },
        current,
    })
}

fn file_name(path: &str) -> String {
    std::path::Path::new(path).file_name().map_or_else(
        || path.to_string(),
        |name| name.to_string_lossy().into_owned(),
    )
}

#[derive(Serialize, Default)]
pub struct KindCounts {
    pub inert: usize,
    pub hoisted_function: usize,
    pub pure_definition: usize,
    pub impure: usize,
}

#[derive(Serialize)]
pub struct Tier0Pair {
    pub class: tier0::Conflict,
    pub first: usize,
    pub later: usize,
    pub first_source: String,
    pub later_source: String,
}

#[derive(Serialize)]
pub struct Tier0Comparison {
    pub current_conflict_pairs: u64,
    pub both_conflict: u64,
    pub current_only: u64,
    pub tier0_only: u64,
}

#[derive(Serialize)]
pub struct Precondition {
    pub name: &'static str,
    /// `None` when the tool had no way to check.
    pub holds: Option<bool>,
    pub violating_sites: usize,
    /// What the analysis did instead where it is violated or unchecked.
    pub fallback: &'static str,
}

#[derive(Serialize)]
pub struct Tier0Report {
    pub chunk: String,
    pub exports_reachable: bool,
    /// How many program chunks were read for cycle and census facts.
    pub program_chunks: Option<usize>,
    pub upgrades: Vec<tier0::Upgrade>,
    /// Non-empty marks the report an unsound estimate.
    pub what_if_pure: Vec<ImpureCause>,
    pub parse_ms: f64,
    pub analysis_ms: f64,
    pub pair_check_ms: f64,
    pub peak_rss_kib: Option<u64>,
    pub statements: usize,
    pub top_level_bindings: usize,
    /// What the verdicts rest on, and whether this input satisfies it.
    pub preconditions: Vec<Precondition>,
    pub kinds: KindCounts,
    pub impure_causes: BTreeMap<ImpureCause, usize>,
    /// Pure definitions that depend on an intrinsic family being unmodified.
    pub intrinsic_families_used: BTreeMap<&'static str, usize>,
    /// Globals the chunk itself writes: how many, and how many of them in a
    /// way the census cannot name (replaced, escaping, computed key).
    pub globals_written: usize,
    pub globals_written_unnamed: usize,
    /// With `--examples`: per written global, the property names, or `*`.
    pub globals_written_detail: BTreeMap<String, Vec<String>>,
    pub all_pairs: u64,
    pub all_pairs_conflicting: u64,
    pub all_pairs_by_class: BTreeMap<String, u64>,
    pub conflict_degree: Degrees,
    pub order_cost: Option<OrderCost>,
    pub current: Option<Tier0Comparison>,
    pub examples: Vec<Tier0Pair>,
    pub impure_examples: BTreeMap<ImpureCause, Vec<String>>,
    /// With `--examples`: every impure statement's ordinal, cause and start.
    pub impure_statements: Vec<(usize, ImpureCause, String)>,
}

fn class_name(class: tier0::Conflict) -> String {
    serde_json::to_value(class)
        .ok()
        .and_then(|value| value.as_str().map(str::to_string))
        .unwrap_or_default()
}

pub fn tier0_report(inputs: Inputs<'_>, options: &tier0::Options) -> Result<Tier0Report> {
    let Inputs {
        chunk,
        source,
        order,
        current_conflicts,
        modules,
        examples: with_examples,
        pairs_out,
        ..
    } = inputs;
    let start = Instant::now();
    let parsed = js_ast::parse_js_module(chunk, &source)?;
    let parse_ms = elapsed_ms(start);
    let start = Instant::now();
    let unresolved = swc_common::SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let analysis = tier0::analyze(&parsed.module, unresolved, options);
    let analysis_ms = elapsed_ms(start);
    let n = analysis.units.len();

    let mut spans = Vec::with_capacity(n);
    for item in &parsed.module.body {
        match js_ast::split_var_declarators(item) {
            Some(declarators) => {
                spans.extend(declarators.iter().map(|declarator| declarator.span.lo.0))
            }
            None => spans.push(swc_common::Spanned::span(item).lo.0),
        }
    }
    let mut kinds = KindCounts::default();
    let mut impure_causes: BTreeMap<ImpureCause, usize> = BTreeMap::new();
    let mut impure_examples: BTreeMap<ImpureCause, Vec<String>> = BTreeMap::new();
    let mut impure_statements = Vec::new();
    for (ordinal, unit) in analysis.units.iter().enumerate() {
        match unit.kind {
            Kind::Inert => kinds.inert += 1,
            Kind::Hoisted => kinds.hoisted_function += 1,
            Kind::Pure => kinds.pure_definition += 1,
            Kind::Impure(cause) => {
                kinds.impure += 1;
                *impure_causes.entry(cause).or_default() += 1;
                let examples = impure_examples.entry(cause).or_default();
                if with_examples && examples.len() < EXAMPLE_LIMIT {
                    examples.push(snippet(&source, spans[ordinal], 100));
                }
                if with_examples {
                    impure_statements.push((ordinal, cause, snippet(&source, spans[ordinal], 80)));
                }
            }
        }
    }

    let start = Instant::now();
    let mut degree = vec![0usize; n];
    let mut all_pairs_conflicting = 0;
    let mut all_pairs_by_class: BTreeMap<String, u64> = BTreeMap::new();
    for low in 0..n {
        for high in low + 1..n {
            if let Some(class) = analysis.conflict(low, high) {
                all_pairs_conflicting += 1;
                *all_pairs_by_class.entry(class_name(class)).or_default() += 1;
                degree[low] += 1;
                degree[high] += 1;
            }
        }
    }
    let positions = order.as_deref().map(|order| positions(order, n));
    let modules = modules
        .as_ref()
        .map(|modules| Modules::parse(modules, n))
        .transpose()?;
    let mut examples = Vec::new();
    let mut example_counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut unproven = String::new();
    let order_cost = positions.as_deref().map(|positions| {
        cost(
            positions,
            modules.as_ref(),
            |low, high| analysis.conflict(low, high).map(class_name),
            |low, high| {
                let Some(class) = analysis.conflict(low, high) else {
                    return;
                };
                if pairs_out.is_some() {
                    unproven.push_str(&format!("{low}\t{high}\t{}\n", class_name(class)));
                }
                let seen = example_counts.entry(class_name(class)).or_default();
                if with_examples && *seen < EXAMPLE_LIMIT {
                    *seen += 1;
                    examples.push(Tier0Pair {
                        class,
                        first: low,
                        later: high,
                        first_source: snippet(&source, spans[low], 100),
                        later_source: snippet(&source, spans[high], 100),
                    });
                }
            },
        )
    });
    let pair_check_ms = elapsed_ms(start);
    if let Some(path) = &pairs_out {
        std::fs::write(path, &unproven).with_context(|| format!("writing {}", path.display()))?;
    }

    let current = match (current_conflicts, positions.as_deref(), &order_cost) {
        (Some(rows), Some(_), Some(order_cost)) => {
            let mut both = 0;
            for row in &rows {
                let (first, later) = (row_unit(row, "runs_first")?, row_unit(row, "runs_later")?);
                if analysis.conflict(first, later).is_some() {
                    both += 1;
                }
            }
            Some(Tier0Comparison {
                current_conflict_pairs: rows.len() as u64,
                both_conflict: both,
                current_only: rows.len() as u64 - both,
                tier0_only: order_cost.unproven_pairs - both,
            })
        }
        _ => None,
    };
    let cycle = options
        .program
        .as_ref()
        .map(|program| program.on_import_cycle);
    let preconditions = vec![
        Precondition {
            name: "no_eval_or_with",
            holds: Some(analysis.refused == 0),
            violating_sites: analysis.refused,
            fallback: "every statement is impure",
        },
        Precondition {
            name: "no_top_level_await",
            holds: Some(analysis.top_level_awaits == 0),
            violating_sites: analysis.top_level_awaits,
            fallback: "the awaiting statement is impure and exports count as reachable",
        },
        Precondition {
            name: "not_on_import_cycle",
            holds: cycle.map(|cycle| !cycle),
            violating_sites: usize::from(cycle == Some(true)),
            fallback: if cycle.is_none() && !options.exports_reachable {
                "unchecked: assumed to hold; pass --program-chunks or --exports-reachable"
            } else {
                "exports count as reachable"
            },
        },
        Precondition {
            name: "intrinsics_unmodified_by_program_code",
            holds: Some(analysis.intrinsics_denied == 0),
            violating_sites: analysis.intrinsics_denied,
            fallback: if options.program.is_some() {
                "the intrinsic use is impure; census over every program chunk"
            } else {
                "the intrinsic use is impure; census over this chunk only"
            },
        },
        Precondition {
            name: "intrinsics_unmodified_by_unknown_code",
            holds: None,
            violating_sites: 0,
            fallback: "assumed for the globals in intrinsic_families_used",
        },
    ];
    let mut sorted_degree = degree.clone();
    sorted_degree.sort_unstable();
    Ok(Tier0Report {
        chunk: file_name(chunk),
        exports_reachable: analysis.exports_reachable,
        program_chunks: options.program.as_ref().map(|program| program.chunks),
        upgrades: options.upgrades.iter().copied().collect(),
        what_if_pure: options.what_if_pure.iter().copied().collect(),
        parse_ms,
        analysis_ms,
        pair_check_ms,
        peak_rss_kib: peak_rss_kib(),
        statements: n,
        top_level_bindings: analysis.bindings,
        preconditions,
        kinds,
        impure_causes,
        intrinsic_families_used: analysis.families_used.clone(),
        globals_written: analysis.census.written.len(),
        globals_written_unnamed: analysis
            .census
            .written
            .values()
            .filter(|writes| writes.any)
            .count(),
        globals_written_detail: analysis
            .census
            .written
            .iter()
            .filter(|_| with_examples)
            .map(|(global, writes)| {
                let mut props: Vec<String> = writes.props.iter().cloned().collect();
                props.sort();
                if writes.any {
                    props.insert(0, "*".to_string());
                }
                (global.clone(), props)
            })
            .collect(),
        all_pairs: (n * n.saturating_sub(1) / 2) as u64,
        all_pairs_conflicting,
        all_pairs_by_class,
        conflict_degree: Degrees {
            min: sorted_degree.first().copied().unwrap_or(0),
            median: sorted_degree.get(n / 2).copied().unwrap_or(0),
            p90: sorted_degree.get(n * 9 / 10).copied().unwrap_or(0),
            max: sorted_degree.last().copied().unwrap_or(0),
            mean: degree.iter().sum::<usize>() as f64 / n.max(1) as f64,
        },
        order_cost,
        current,
        examples,
        impure_examples,
        impure_statements,
    })
}
