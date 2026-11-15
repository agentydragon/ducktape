//! Complete fixed-owner discovery over the prepared graph's finite assignments.
//!
//! See docs/module_search.md for the finite representation and coverage proof.

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use analysis::StatementKind;
use anyhow::{Context, Result, ensure};
use clap::ValueEnum;
use serde::Serialize;
use source_identity::SourceUnitId;
use spec::{ModulePath, is_residual_namespace_path};

use super::assignment::{AssignmentProblem, SourceUnitAssignment, accepted};

#[derive(Debug, Clone, Copy, ValueEnum, Serialize, Default)]
#[value(rename_all = "snake_case")]
#[serde(rename_all = "snake_case")]
pub enum Destinations {
    New,
    Existing,
    #[default]
    Both,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SearchStatus {
    Found,
    ExhaustedNoPeel,
    Incomplete,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    MaxChecks,
    TimeBudget,
}

pub struct SearchOptions {
    pub destinations: Destinations,
    pub into: Vec<String>,
    pub required: Vec<String>,
    pub only_units: Option<Vec<String>>,
    pub max_new_modules: Option<usize>,
    pub max_checks: Option<u64>,
    pub time_budget: Option<Duration>,
}

#[derive(Serialize)]
pub struct SearchReport {
    pub status: SearchStatus,
    pub stop_reason: Option<StopReason>,
    pub checks: u64,
    pub movable_atoms: usize,
    pub residual_units_extracted: usize,
    pub existing_units_moved: usize,
    pub scope: SearchScope,
    #[serde(skip)]
    pub changes: Vec<SourceUnitAssignment>,
}

#[derive(Serialize)]
pub struct SearchScope {
    pub destinations: Destinations,
    pub into: Vec<String>,
    pub required_units: Vec<String>,
    pub only_units: Option<Vec<String>>,
    pub max_new_modules: Option<usize>,
}

pub fn search(problem: &AssignmentProblem<'_>, options: &SearchOptions) -> Result<SearchReport> {
    let started = Instant::now();
    let resolve = |ids: &[String]| -> Result<BTreeSet<SourceUnitId>> {
        ids.iter()
            .map(|id| {
                problem
                    .input
                    .source_unit_id(id)
                    .with_context(|| format!("unknown source unit {id:?}"))
            })
            .collect()
    };
    let required = resolve(&options.required)?;
    let only = options.only_units.as_deref().map(resolve).transpose()?;
    let mut atoms = Vec::new();
    for atom in &problem.atoms {
        let permitted = atom.members.iter().all(|unit| {
            problem.residual_origins[unit.0]
                && !matches!(
                    problem.input.source_unit_report(*unit).statement_kind,
                    StatementKind::Import | StatementKind::Export
                )
        });
        if let Some(only) = &only {
            let selected = atom.members.intersection(only).count();
            ensure!(
                selected == 0 || selected == atom.members.len(),
                "--only-units cuts an atomic group; include its complete companion set: {:?}",
                atom.members
                    .iter()
                    .map(|unit| &problem.input.source_unit_report(*unit).id)
                    .collect::<Vec<_>>()
            );
            if selected == 0 {
                continue;
            }
            ensure!(
                permitted,
                "--only-units contains authored units or import/export scaffolding"
            );
        }
        if permitted {
            atoms.push(atom);
        }
    }
    let movable: BTreeSet<_> = atoms
        .iter()
        .flat_map(|atom| atom.members.iter().copied())
        .collect();
    ensure!(
        required.is_subset(&movable),
        "--require must name movable residual body units inside the permitted scope"
    );
    let existing: BTreeSet<_> = problem
        .existing_modules
        .difference(&problem.residual_modules)
        .cloned()
        .collect();
    let new_limit = options.max_new_modules.unwrap_or(atoms.len());
    let mut targets = BTreeSet::new();
    if options.into.is_empty() {
        if !matches!(options.destinations, Destinations::New) {
            targets.extend(existing.iter().cloned());
        }
        if !matches!(options.destinations, Destinations::Existing) && new_limit > 0 {
            targets.extend(fresh_representatives(
                &existing,
                &problem.residual_modules,
                atoms.len().min(new_limit),
            )?);
        }
    } else {
        for raw in &options.into {
            ensure!(
                !std::path::Path::new(raw).is_absolute() && !raw.contains('\0'),
                "destination must be a relative module path"
            );
            let path = ModulePath::parse(raw, "")?;
            ensure!(
                !path.as_str().ends_with(".yaml") && !path.as_str().ends_with(".yml"),
                "destination must be extensionless"
            );
            let is_existing = existing.contains(&path);
            ensure!(
                is_existing || new_limit > 0,
                "--into names a fresh owner but --max-new-modules is zero"
            );
            ensure!(
                !problem.residual_modules.contains(&path)
                    && (!is_residual_namespace_path(path.as_str()) || is_existing),
                "destination is reserved for generated residual output"
            );
            ensure!(
                match options.destinations {
                    Destinations::New => !is_existing,
                    Destinations::Existing => is_existing,
                    Destinations::Both => true,
                },
                "--into conflicts with --destinations"
            );
            targets.insert(path);
        }
    }
    let targets: Vec<_> = targets.into_iter().collect();
    let mut report = SearchReport {
        status: SearchStatus::ExhaustedNoPeel,
        stop_reason: None,
        checks: 0,
        movable_atoms: atoms.len(),
        residual_units_extracted: 0,
        existing_units_moved: 0,
        scope: SearchScope {
            destinations: options.destinations,
            into: options.into.clone(),
            required_units: options.required.clone(),
            only_units: options.only_units.clone(),
            max_new_modules: options.max_new_modules,
        },
        changes: Vec::new(),
    };
    if atoms.is_empty() || targets.is_empty() {
        return Ok(report);
    }
    let mut visit = |digits: &[usize]| -> Result<bool> {
        // Scope filters can skip exponentially many combinations. Honor both
        // budgets even when this combination will not invoke the gate.
        if options
            .time_budget
            .is_some_and(|budget| started.elapsed() >= budget)
        {
            report.status = SearchStatus::Incomplete;
            report.stop_reason = Some(StopReason::TimeBudget);
            return Ok(true);
        }
        if options
            .max_checks
            .is_some_and(|limit| report.checks >= limit)
        {
            report.status = SearchStatus::Incomplete;
            report.stop_reason = Some(StopReason::MaxChecks);
            return Ok(true);
        }
        let moved: BTreeSet<_> = atoms
            .iter()
            .zip(digits)
            .filter(|(_, digit)| **digit != 0)
            .flat_map(|(atom, _)| atom.members.iter().copied())
            .collect();
        if moved.is_empty() || !required.is_subset(&moved) {
            return Ok(false);
        }
        let new_modules: BTreeSet<_> = digits
            .iter()
            .filter(|digit| **digit != 0)
            .map(|digit| &targets[digit - 1])
            .filter(|path| !existing.contains(*path))
            .collect();
        if new_modules.len() > new_limit {
            return Ok(false);
        }
        let mut candidate = problem.baseline.clone();
        for (atom, digit) in atoms.iter().zip(digits) {
            if *digit != 0 {
                for unit in &atom.members {
                    candidate[unit.0] = Some(targets[digit - 1].clone());
                }
            }
        }
        report.checks = report
            .checks
            .checked_add(1)
            .context("assignment check count overflow")?;
        let validation = problem.validate(&candidate)?;
        if !accepted(&validation) {
            return Ok(false);
        }
        report.status = SearchStatus::Found;
        report.residual_units_extracted = problem.residual_gain(&candidate);
        report.changes = candidate
            .iter()
            .zip(&problem.baseline)
            .enumerate()
            .filter(|(_, (after, before))| after != before)
            .map(|(index, (after, _))| SourceUnitAssignment {
                unit_id: problem
                    .input
                    .source_unit_report(SourceUnitId(index))
                    .id
                    .clone(),
                module: after
                    .as_ref()
                    .expect("search only extracts")
                    .as_str()
                    .to_string(),
            })
            .collect();
        Ok(true)
    };
    // Cheap useful witnesses first. Failure here never prunes the exhaustive
    // product below, which includes rejected intermediate moves and partitions.
    for target in 1..=targets.len() {
        if visit(&vec![target; atoms.len()])? {
            return Ok(report);
        }
    }
    let mut digits = vec![0; atoms.len()];
    loop {
        if visit(&digits)? {
            return Ok(report);
        }
        let mut position = 0;
        while position < digits.len() && digits[position] == targets.len() {
            digits[position] = 0;
            position += 1;
        }
        if position == digits.len() {
            break;
        }
        digits[position] += 1;
    }
    Ok(report)
}

/// At most n nonempty fresh owners are needed for n atoms. Represent each
/// possible relative order by n fresh paths in every gap between old owners.
/// Any real assignment maps order-preservingly to these representatives, so
/// its numeric ModuleAssignment and full gate verdict are unchanged.
fn fresh_representatives(
    existing: &BTreeSet<ModulePath>,
    forbidden: &BTreeSet<ModulePath>,
    count: usize,
) -> Result<BTreeSet<ModulePath>> {
    let forbidden: BTreeSet<_> = forbidden.iter().map(ModulePath::as_str).collect();
    let mut result = BTreeSet::new();
    let boundaries: Vec<_> = std::iter::once(None)
        .chain(existing.iter().map(Some))
        .chain(std::iter::once(None))
        .collect();
    for gap in boundaries.windows(2) {
        let lower = gap[0].map_or("", ModulePath::as_str);
        let upper = gap[1].map(ModulePath::as_str);
        let in_gap = |path: &str| {
            path > lower
                && upper.is_none_or(|upper| path < upper)
                && !is_residual_namespace_path(path)
                && !forbidden.contains(path)
        };
        let mut representatives = BTreeSet::new();
        for index in 0..count {
            for path in [
                format!("extracted/{index:04}"),
                format!("{lower}/extracted/{index:04}"),
            ] {
                if !path.starts_with('/') && in_gap(&path) {
                    representatives.insert(ModulePath::parse(&path, "")?);
                }
                if representatives.len() == count {
                    break;
                }
            }
            if representatives.len() == count {
                break;
            }
        }
        // U+0001 is the least usable filename character (NUL is forbidden).
        // Appending it is the immediate strict lexical successor. This also
        // handles finite gaps where upper is lower followed by U+0001s.
        let mut path = format!("{lower}\u{1}");
        while representatives.len() < count && upper.is_none_or(|upper| path.as_str() < upper) {
            if path.starts_with("residual/") {
                path = "residual0".to_string();
                continue;
            }
            if in_gap(&path) {
                representatives.insert(ModulePath::parse(&path, "")?);
            }
            path.push('\u{1}');
        }
        result.extend(representatives);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn representatives_cover_finite_dense_and_reserved_gaps() {
        let existing = ["\u{1}", "a", "a\u{1}\u{1}", "residual/a", "residual/z", "z"]
            .map(|path| ModulePath::parse(path, "").unwrap())
            .into_iter()
            .collect();
        let representatives = fresh_representatives(&existing, &BTreeSet::new(), 3).unwrap();
        assert!(representatives.contains(&ModulePath::parse("a\u{1}", "").unwrap()));
        assert!(
            representatives
                .iter()
                .all(|path| !existing.contains(path) && !is_residual_namespace_path(path.as_str()))
        );
        assert!(!representatives.iter().any(|path| path.as_str() < "\u{1}"));
        assert!(
            !representatives
                .iter()
                .any(|path| path.as_str() > "residual/a" && path.as_str() < "residual/z")
        );
        assert_eq!(
            representatives
                .iter()
                .filter(|path| path.as_str() > "z")
                .count(),
            3
        );
    }

    proptest! {
        #[test]
        fn every_sampled_fresh_partition_has_an_order_preserving_representative(
            old in proptest::collection::btree_set("[a-c]{1,3}", 0..5),
            fresh in proptest::collection::btree_set("[a-d]{1,4}", 0..5),
        ) {
            let existing: BTreeSet<_> = old.iter().map(|path| ModulePath::parse(path, "").unwrap()).collect();
            let fresh: Vec<_> = fresh.into_iter().filter(|path| !old.contains(path)).collect();
            let representatives = fresh_representatives(&existing, &BTreeSet::new(), fresh.len()).unwrap();
            let mut chosen = BTreeSet::new();
            for path in fresh {
                let lower = old.iter().rfind(|old| *old < &path);
                let upper = old.iter().find(|old| *old > &path);
                let representative = representatives.iter().find(|candidate| {
                    lower.is_none_or(|lower| candidate.as_str() > lower.as_str())
                        && upper.is_none_or(|upper| candidate.as_str() < upper.as_str())
                        && !chosen.contains(*candidate)
                });
                prop_assert!(representative.is_some(), "missing ordering representative for {} between {:?} and {:?}", path, lower, upper);
                chosen.insert(representative.unwrap().clone());
            }
        }
    }
}
