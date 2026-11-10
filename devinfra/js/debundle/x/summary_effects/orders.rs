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
    /// With the entry residual (every unit no module lists) as one module.
    pub module_damage: Option<ModuleDamage>,
    /// Authored modules an unproven pair chains to the entry residual.
    pub modules_merging_into_entry: Option<usize>,
    /// Among authored modules only: unproven pairs with a statement in the
    /// entry residual are left out.
    pub authored_module_damage: Option<ModuleDamage>,
}

/// Modules joined by unproven pairs.
struct Groups {
    parent: Vec<usize>,
    involved: Vec<bool>,
}

impl Groups {
    fn new(count: usize) -> Self {
        Self {
            parent: (0..count).collect(),
            involved: vec![false; count],
        }
    }

    fn join(&mut self, a: usize, b: usize) {
        self.involved[a] = true;
        self.involved[b] = true;
        let (a, b) = (find(&mut self.parent, a), find(&mut self.parent, b));
        self.parent[a] = b;
    }

    /// The damage among the modules from `first` on.
    fn damage(&mut self, first: usize) -> ModuleDamage {
        let count = self.parent.len();
        let mut sizes: BTreeMap<usize, usize> = BTreeMap::new();
        for module in first..count {
            *sizes.entry(find(&mut self.parent, module)).or_default() += 1;
        }
        let grouped: Vec<usize> = sizes.values().copied().filter(|size| *size > 1).collect();
        ModuleDamage {
            modules: count - first,
            modules_in_unproven_pairs: self.involved[first..].iter().filter(|hit| **hit).count(),
            components: grouped.len(),
            largest_component: grouped.iter().copied().max().unwrap_or(0),
            modules_unaffected: count - first - grouped.iter().sum::<usize>(),
        }
    }
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
    let count = modules.map_or(0, |modules| modules.count);
    let (mut with_entry, mut authored) = (Groups::new(count), Groups::new(count));
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
                with_entry.join(a, b);
                if a != 0 && b != 0 {
                    authored.join(a, b);
                }
            }
        }
    }
    out.statements_in_unproven_pairs = involved.iter().filter(|involved| **involved).count();
    if modules.is_some() {
        let entry = find(&mut with_entry.parent, 0);
        out.modules_merging_into_entry = Some(
            (1..count)
                .filter(|module| find(&mut with_entry.parent, *module) == entry)
                .count(),
        );
        out.module_damage = Some(with_entry.damage(0));
        out.authored_module_damage = Some(authored.damage(1));
    }
    out
}
