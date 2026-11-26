//! Complete polynomial inventory for one new owner appended to a fixed DFS.
//!
//! A moved atom must move with every residual C successor that would
//! otherwise remain in entry, and with every atom in its C/rebinding SCC.
//! Lazy imports into entry are legal gray-target imports; they do not
//! force the target to move.
//! The condensation DAG therefore gives the unique minimal group for each
//! seed SCC. Discovery/finish event windows compose by max-lower/min-upper;
//! feasibility of every minimal group is computed in one reverse-topological
//! pass. No subset or alternative-schedule search is involved.

use super::super::assignment::AssignmentProblem;
use super::super::search::SeedAtomReport;
use super::{Owner, Partition, Scope, Token};
use analysis::ModuleId;
use anyhow::{Result, bail, ensure};
use esm_schedule::EsmImportSchedule;
use gate::AssignmentConstraints;
use petgraph::{algo::kosaraju_scc, graphmap::DiGraphMap};
use source_identity::SourceUnitId;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug)]
struct Window {
    lower: usize,
    upper: usize,
    blocked: bool,
    potential_units: usize,
    blockers: BTreeSet<&'static str>,
}

impl Window {
    fn include(&mut self, other: &Self) {
        self.lower = self.lower.max(other.lower);
        self.upper = self.upper.min(other.upper);
        self.blocked |= other.blocked;
        self.potential_units = self.potential_units.saturating_add(other.potential_units);
        self.blockers.extend(other.blockers.iter().copied());
    }

    fn feasible(&self) -> bool {
        !self.blocked && self.lower < self.upper
    }

    fn blocker_list(&self) -> Vec<String> {
        let mut blockers: Vec<_> = self.blockers.iter().copied().map(str::to_owned).collect();
        if self.lower >= self.upper {
            blockers.push("no_common_discovery_finish_window".to_owned());
        }
        blockers
    }
}

struct SccWindowSummary {
    components: Vec<Vec<ModuleId>>,
    component_of: BTreeMap<ModuleId, usize>,
    dag: DiGraphMap<usize, ()>,
    aggregate: Vec<Window>,
}

/// Collapse mutually dependent movable atoms and summarize each minimal
/// successor closure with one reverse-topological pass over the condensation.
fn summarize_scc_windows(
    graph: &DiGraphMap<ModuleId, ()>,
    windows: &BTreeMap<ModuleId, Window>,
    entry_finish: usize,
) -> SccWindowSummary {
    let components = kosaraju_scc(graph);
    let component_of: BTreeMap<_, _> = components
        .iter()
        .enumerate()
        .flat_map(|(component, members)| members.iter().map(move |&id| (id, component)))
        .collect();
    let mut dag = DiGraphMap::<usize, ()>::new();
    let mut aggregate = Vec::with_capacity(components.len());
    for (component, members) in components.iter().enumerate() {
        dag.add_node(component);
        let mut window = Window {
            lower: 0,
            upper: entry_finish,
            blocked: false,
            potential_units: 0,
            blockers: BTreeSet::new(),
        };
        for member in members {
            window.include(&windows[member]);
        }
        aggregate.push(window);
    }
    for (from, to, _) in graph.all_edges() {
        let (from, to) = (component_of[&from], component_of[&to]);
        if from != to {
            dag.add_edge(from, to, ());
        }
    }
    // Kosaraju numbers SCCs in reverse topological order.
    for component in 0..aggregate.len() {
        let (dependencies, current) = aggregate.split_at_mut(component);
        for dependency in dag.neighbors(component) {
            current[0].include(&dependencies[dependency]);
        }
    }

    SccWindowSummary {
        components,
        component_of,
        dag,
        aggregate,
    }
}

fn combined_component_window(
    aggregate: &[Window],
    components: &BTreeSet<usize>,
    entry_finish: usize,
) -> Window {
    let mut combined = Window {
        lower: 0,
        upper: entry_finish,
        blocked: false,
        potential_units: 0,
        blockers: BTreeSet::new(),
    };
    for &component in components {
        combined.include(&aggregate[component]);
    }
    combined
}

/// Distinct discovery/finish clocks preserve gray-target legality; postorder
/// alone cannot distinguish a gray fixed target from one still undiscovered.
fn event_clocks(
    schedule: &EsmImportSchedule,
    entry: ModuleId,
) -> BTreeMap<ModuleId, (usize, usize)> {
    let mut clocks = BTreeMap::from([(entry, (0, usize::MAX))]);
    let mut frames = vec![(entry, 0)];
    let mut clock = 1;
    while let Some((module, next)) = frames.last_mut() {
        if let Some(&target) = schedule.imports(*module).get(*next) {
            *next += 1;
            if let std::collections::btree_map::Entry::Vacant(slot) = clocks.entry(target) {
                slot.insert((clock, usize::MAX));
                clock += 1;
                frames.push((target, 0));
            }
        } else {
            let (module, _) = frames.pop().expect("active DFS frame");
            clocks.get_mut(&module).unwrap().1 = clock;
            clock += 1;
        }
    }
    clocks
}

pub(super) struct InsertionInventory {
    pub seed_atoms: Vec<SeedAtomReport>,
    pub selected_units: BTreeSet<usize>,
    pub blockers: Vec<String>,
    pub rows: u64,
    pub edges: u64,
    pub roots: u64,
}

pub(super) fn inventory(
    problem: &AssignmentProblem<'_>,
    scope: &Scope,
    initial: &Partition,
    module_ids: &BTreeMap<Token, ModuleId>,
    constraints: &AssignmentConstraints,
    target: &Owner,
) -> Result<InsertionInventory> {
    let Some((baseline, schedule)) = &problem.baseline_certificate else {
        bail!("append-last proposal requires an accepted explicit-order baseline");
    };
    ensure!(
        scope
            .movable_units
            .iter()
            .all(|&unit| problem.baseline[unit].is_none()),
        "append-last proposals only move true-entry residual units; existing owners remain fixed"
    );
    let entry = module_ids[&Token::Owner(Owner::Entry)];
    let baseline_to_arena: BTreeMap<_, _> = baseline
        .modules
        .iter()
        .enumerate()
        .map(|(index, path)| {
            (
                ModuleId(index),
                module_ids[&Token::Owner(Owner::Module(path.clone()))],
            )
        })
        .chain([(baseline.entry(), entry)])
        .collect();
    let clocks: BTreeMap<_, _> = event_clocks(schedule, baseline.entry())
        .into_iter()
        .map(|(id, event)| (baseline_to_arena[&id], event))
        .collect();
    let entry_finish = clocks[&entry].1;
    let blocks: BTreeMap<_, _> = initial
        .blocks
        .iter()
        .map(|block| (module_ids[&block.key()], block))
        .collect();
    let mut windows = BTreeMap::new();
    let mut graph = DiGraphMap::<ModuleId, ()>::new();
    let mut required_roots = BTreeSet::new();

    for (&id, block) in &blocks {
        if block.owner.is_some() {
            continue;
        }
        graph.add_node(id);
        let mut blockers = BTreeSet::new();
        let blocked = !block.allowed_owners.contains(target);
        if blocked {
            blockers.insert("destination_not_allowed_for_atom");
        }
        windows.insert(
            id,
            Window {
                lower: 0,
                upper: entry_finish,
                blocked,
                potential_units: block.units.len(),
                blockers,
            },
        );
        if !block.units.is_disjoint(&scope.required_units) {
            required_roots.insert(id);
        }
    }

    let mut rows = 0u64;
    let mut edges = 0u64;
    // Finest atom projection activates fallback-promoted edges for every
    // potentially moved caller. Unselected callers return to entry later.
    for (&from, targets) in &constraints.canonical.i_successors {
        rows += 1;
        for &to in targets {
            edges += 1;
            if windows.contains_key(&from) {
                if to != entry
                    && let Some(&(discovered, _)) = clocks.get(&to)
                {
                    windows.get_mut(&from).unwrap().lower = windows[&from].lower.max(discovered);
                }
            } else if from != entry
                && let Some(window) = windows.get_mut(&to)
            {
                window.upper = window.upper.min(clocks[&from].1);
            }
        }
    }
    for (from, to) in constraints.canonical.pairs() {
        edges += 1;
        if !windows.contains_key(&from) {
            continue;
        }
        if windows.contains_key(&to) {
            graph.add_edge(from, to, ());
        } else if to == entry {
            let window = windows.get_mut(&from).unwrap();
            window.blocked = true;
            window
                .blockers
                .insert("constraining_dependency_remains_in_entry");
        } else {
            let window = windows.get_mut(&from).unwrap();
            window.lower = window.lower.max(clocks[&to].1);
        }
    }
    for edge in &constraints.cross_rebinds {
        edges += 1;
        match (
            windows.contains_key(&edge.from),
            windows.contains_key(&edge.to),
        ) {
            (true, true) => {
                graph.add_edge(edge.from, edge.to, ());
                graph.add_edge(edge.to, edge.from, ());
            }
            (true, false) => {
                let window = windows.get_mut(&edge.from).unwrap();
                window.blocked = true;
                window.blockers.insert("cross_owner_rebinding");
            }
            (false, true) => {
                let window = windows.get_mut(&edge.to).unwrap();
                window.blocked = true;
                window.blockers.insert("cross_owner_rebinding");
            }
            (false, false) => {}
        }
    }

    // One reverse-topological DP computes every SCC's minimal successor
    // closure bounds. The sets themselves are materialized only once below.
    let SccWindowSummary {
        components,
        component_of,
        dag,
        aggregate,
    } = summarize_scc_windows(&graph, &windows, entry_finish);

    let mut seed_atoms = Vec::new();
    for (&id, block) in &blocks {
        if block.owner.is_some() {
            continue;
        }
        let component = component_of[&id];
        let colocated_units: BTreeSet<_> = components[component]
            .iter()
            .filter(|&&peer| peer != id)
            .flat_map(|peer| blocks[peer].units.iter().copied())
            .collect();
        seed_atoms.push(SeedAtomReport {
            source_units: block
                .units
                .iter()
                .map(|&unit| {
                    problem
                        .input
                        .source_unit_report(SourceUnitId(unit))
                        .id
                        .clone()
                })
                .collect(),
            co_located_with: colocated_units
                .iter()
                .map(|&unit| {
                    problem
                        .input
                        .source_unit_report(SourceUnitId(unit))
                        .id
                        .clone()
                })
                .collect(),
            feasible: aggregate[component].feasible(),
            blockers: aggregate[component].blocker_list(),
        });
    }

    let mut selected_roots = BTreeSet::new();
    let mut blockers = Vec::new();
    if required_roots.is_empty() {
        if let Some((component, _)) = aggregate
            .iter()
            .enumerate()
            .filter(|(_, window)| window.feasible())
            .max_by_key(|(component, window)| {
                (window.potential_units, std::cmp::Reverse(*component))
            })
        {
            selected_roots.insert(component);
        }
    } else {
        let required_components: BTreeSet<_> =
            required_roots.iter().map(|id| component_of[id]).collect();
        let combined = combined_component_window(&aggregate, &required_components, entry_finish);
        if combined.feasible() {
            selected_roots = required_components;
        } else {
            blockers = combined.blocker_list();
        }
    }

    let mut selected_components = BTreeSet::new();
    let mut todo: Vec<_> = selected_roots.into_iter().collect();
    while let Some(component) = todo.pop() {
        if selected_components.insert(component) {
            todo.extend(dag.neighbors(component));
        }
    }
    let selected_units: BTreeSet<_> = selected_components
        .iter()
        .flat_map(|&component| components[component].iter())
        .flat_map(|id| blocks[id].units.iter().copied())
        .collect();
    Ok(InsertionInventory {
        seed_atoms,
        selected_units,
        blockers,
        rows,
        edges,
        roots: aggregate.len() as u64,
    })
}

#[cfg(test)]
mod tests {
    use super::{ModuleId, Window, combined_component_window, summarize_scc_windows};
    use petgraph::graphmap::DiGraphMap;
    use proptest::prelude::*;
    use std::collections::{BTreeMap, BTreeSet};

    const ENTRY_FINISH: usize = 11;

    #[derive(Clone, Copy)]
    struct OracleWindow {
        lower: usize,
        upper: usize,
        blocked: bool,
    }

    fn compare_with_raw_subset_oracle(
        raw_windows: &[OracleWindow],
        arcs: &[(usize, usize)],
    ) -> Vec<(String, bool, bool)> {
        let mut graph = DiGraphMap::new();
        let mut windows = BTreeMap::new();
        for (index, raw) in raw_windows.iter().enumerate() {
            let id = ModuleId(index);
            graph.add_node(id);
            windows.insert(
                id,
                Window {
                    lower: raw.lower,
                    upper: raw.upper,
                    blocked: raw.blocked,
                    potential_units: 1,
                    blockers: if raw.blocked {
                        BTreeSet::from(["test_blocked"])
                    } else {
                        BTreeSet::new()
                    },
                },
            );
        }
        for &(from, to) in arcs {
            graph.add_edge(ModuleId(from), ModuleId(to), ());
        }

        let summary = summarize_scc_windows(&graph, &windows, ENTRY_FINISH);
        let mut comparisons = Vec::new();
        for seed in 0..raw_windows.len() {
            let component = summary.component_of[&ModuleId(seed)];
            let actual = summary.aggregate[component].feasible();
            let expected = raw_subset_oracle(raw_windows, arcs, 1 << seed);
            comparisons.push((format!("seed {seed}"), actual, expected));
        }
        for required in 1..(1 << raw_windows.len()) {
            let components = (0..raw_windows.len())
                .filter(|&root| required & (1 << root) != 0)
                .map(|root| summary.component_of[&ModuleId(root)])
                .collect();
            let actual =
                combined_component_window(&summary.aggregate, &components, ENTRY_FINISH).feasible();
            let expected = raw_subset_oracle(raw_windows, arcs, required);
            comparisons.push((format!("required roots {required:#b}"), actual, expected));
        }
        comparisons
    }

    /// Enumerate every raw vertex subset, checking edge closure and the window
    /// intersection directly. This oracle does not compute SCCs or graph closure.
    fn raw_subset_oracle(
        windows: &[OracleWindow],
        arcs: &[(usize, usize)],
        required: usize,
    ) -> bool {
        let limit = 1usize << windows.len();
        for selected in 0..limit {
            if selected & required != required {
                continue;
            }
            if arcs
                .iter()
                .any(|&(from, to)| selected & (1 << from) != 0 && selected & (1 << to) == 0)
            {
                continue;
            }

            let mut lower = 0;
            let mut upper = ENTRY_FINISH;
            let mut blocked = false;
            for (index, window) in windows.iter().enumerate() {
                if selected & (1 << index) != 0 {
                    lower = lower.max(window.lower);
                    upper = upper.min(window.upper);
                    blocked |= window.blocked;
                }
            }
            if !blocked && lower < upper {
                return true;
            }
        }
        false
    }

    proptest! {
        #![proptest_config(ProptestConfig {
            cases: 512,
            ..ProptestConfig::default()
        })]

        #[test]
        fn scc_window_dp_matches_raw_closed_subset_oracle(
            node_count in 3usize..=4,
            arc_mask in any::<u16>(),
            lower_values in any::<[u8; 4]>(),
            upper_values in any::<[u8; 4]>(),
            blocked_mask in any::<u8>(),
        ) {
            let raw_windows: Vec<_> = (0..node_count)
                .map(|node| OracleWindow {
                    lower: usize::from(lower_values[node] % ENTRY_FINISH as u8),
                    upper: usize::from(upper_values[node] % ENTRY_FINISH as u8),
                    blocked: blocked_mask & (1 << node) != 0,
                })
                .collect();
            let arcs: Vec<_> = (0..node_count)
                .flat_map(|from| {
                    (0..node_count).filter_map(move |to| {
                        let bit = from * 4 + to;
                        (arc_mask & (1 << bit) != 0).then_some((from, to))
                    })
                })
                .collect();

            let comparisons = compare_with_raw_subset_oracle(&raw_windows, &arcs);
            prop_assert_eq!(
                comparisons.len(),
                node_count + (1usize << node_count) - 1,
                "each input compares every seed and every nonempty required-root set",
            );
            for (case, actual, expected) in comparisons {
                prop_assert_eq!(actual, expected, "{}; arcs={:?}; windows={:?}", case, arcs,
                    raw_windows.iter().map(|w| (w.lower, w.upper, w.blocked)).collect::<Vec<_>>());
            }
        }
    }

    #[test]
    fn scc_window_dp_covers_shared_descendants_cycles_equal_bounds_and_blocked_successors() {
        let cases = [
            (
                "shared descendant and cycle",
                vec![
                    OracleWindow {
                        lower: 0,
                        upper: 10,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 3,
                        upper: 7,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 2,
                        upper: 6,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 5,
                        upper: 9,
                        blocked: false,
                    },
                ],
                vec![(0, 1), (0, 2), (1, 3), (2, 3), (3, 1)],
            ),
            (
                "equal lower and upper bounds",
                vec![
                    OracleWindow {
                        lower: 0,
                        upper: 10,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 4,
                        upper: 7,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 0,
                        upper: 4,
                        blocked: false,
                    },
                ],
                vec![(0, 1), (1, 2)],
            ),
            (
                "blocked successor",
                vec![
                    OracleWindow {
                        lower: 0,
                        upper: 10,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 1,
                        upper: 9,
                        blocked: false,
                    },
                    OracleWindow {
                        lower: 2,
                        upper: 8,
                        blocked: true,
                    },
                ],
                vec![(0, 1), (1, 2)],
            ),
        ];
        for (name, windows, arcs) in cases {
            for (case, actual, expected) in compare_with_raw_subset_oracle(&windows, &arcs) {
                assert_eq!(actual, expected, "{name}: {case}; arcs={arcs:?}");
            }
        }
    }
}
