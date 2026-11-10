//! What an emitted order costs under a conflict relation: the swapped pairs
//! left unproven, and the modules they would force together.

use std::collections::BTreeMap;

use serde::Serialize;

/// Statement to module, from the pipeline's `modules.json`.
pub struct Modules {
    /// Per source unit: its module's index; the entry is module 0.
    pub of_unit: Vec<usize>,
    pub count: usize,
}

impl Modules {
    pub fn parse(value: &serde_json::Value, units: usize) -> anyhow::Result<Self> {
        let mut of_unit = vec![0; units];
        let contents = value["final_module_contents"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("modules.json lacks `final_module_contents`"))?;
        for (index, module) in contents.iter().enumerate() {
            for unit in module["source_unit_ids"].as_array().into_iter().flatten() {
                let ordinal: usize = unit
                    .as_str()
                    .and_then(|unit| unit.strip_prefix("unit:"))
                    .and_then(|ordinal| ordinal.parse().ok())
                    .ok_or_else(|| anyhow::anyhow!("unexpected source unit id {unit}"))?;
                anyhow::ensure!(ordinal < units, "unit {ordinal} is outside the chunk");
                of_unit[ordinal] = index + 1;
            }
        }
        Ok(Self {
            of_unit,
            count: contents.len() + 1,
        })
    }
}

#[derive(Serialize, Default)]
pub struct ModuleDamage {
    pub modules: usize,
    /// Modules holding a statement of at least one unproven swapped pair.
    pub modules_in_unproven_pairs: usize,
    /// Groups of two or more modules the unproven pairs connect, i.e. that
    /// would have to be co-located.
    pub components: usize,
    pub largest_component: usize,
    /// Modules no unproven pair connects to another module.
    pub modules_unaffected: usize,
}

#[derive(Serialize, Default)]
pub struct OrderCost {
    pub swapped_pairs: u64,
    pub unproven_pairs: u64,
    /// Unproven swapped pairs by conflict class.
    pub by_class: BTreeMap<String, u64>,
    /// Statements in at least one unproven swapped pair.
    pub statements_in_unproven_pairs: usize,
    pub module_damage: Option<ModuleDamage>,
}

fn find(parent: &mut [usize], mut node: usize) -> usize {
    while parent[node] != node {
        parent[node] = parent[parent[node]];
        node = parent[node];
    }
    node
}

/// Costs `positions` (source unit to emitted position) under `conflict`,
/// which names the class of a conflicting pair. `visit` sees every unproven
/// swapped pair as `(source-earlier, source-later)`.
pub fn cost(
    positions: &[usize],
    modules: Option<&Modules>,
    conflict: impl Fn(usize, usize) -> Option<String>,
    mut visit: impl FnMut(usize, usize),
) -> OrderCost {
    let n = positions.len();
    let mut out = OrderCost::default();
    let mut involved = vec![false; n];
    let mut parent: Vec<usize> = (0..modules.map_or(0, |modules| modules.count)).collect();
    let mut module_involved = vec![false; parent.len()];
    for low in 0..n {
        for high in low + 1..n {
            if positions[low] < positions[high] {
                continue;
            }
            out.swapped_pairs += 1;
            let Some(class) = conflict(low, high) else {
                continue;
            };
            out.unproven_pairs += 1;
            *out.by_class.entry(class).or_default() += 1;
            involved[low] = true;
            involved[high] = true;
            visit(low, high);
            if let Some(modules) = modules {
                let (a, b) = (modules.of_unit[low], modules.of_unit[high]);
                module_involved[a] = true;
                module_involved[b] = true;
                let (a, b) = (find(&mut parent, a), find(&mut parent, b));
                parent[a] = b;
            }
        }
    }
    out.statements_in_unproven_pairs = involved.iter().filter(|involved| **involved).count();
    if let Some(modules) = modules {
        let mut sizes: BTreeMap<usize, usize> = BTreeMap::new();
        for module in 0..modules.count {
            *sizes.entry(find(&mut parent, module)).or_default() += 1;
        }
        let grouped: Vec<usize> = sizes.values().copied().filter(|size| *size > 1).collect();
        out.module_damage = Some(ModuleDamage {
            modules: modules.count,
            modules_in_unproven_pairs: module_involved.iter().filter(|hit| **hit).count(),
            components: grouped.len(),
            largest_component: grouped.iter().copied().max().unwrap_or(0),
            modules_unaffected: modules.count - grouped.iter().sum::<usize>(),
        });
    }
    out
}
