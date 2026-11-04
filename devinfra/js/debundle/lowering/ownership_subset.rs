//! Ownership selection is applied after the complete spec has resolved.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use spec::{LogicalModule, ModulePath, UnassignedMode};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// One exact output module, using the chunk-local path shown in module reports.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetainedModule {
    pub chunk: String,
    pub module: ModulePath,
}

/// A normal run's retained ownership claims, independent of analysis inputs.
/// An empty list returns every logical-module claim to the chunk's unassigned
/// routing policy (which may synthesize residual modules).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OwnershipSubset {
    pub modules: Vec<RetainedModule>,
}

impl OwnershipSubset {
    pub fn read(path: &Path) -> Result<Self> {
        let bytes = std::fs::read(path)
            .with_context(|| format!("read ownership subset {}", path.display()))?;
        serde_json::from_slice(&bytes)
            .with_context(|| format!("parse ownership subset {}", path.display()))
    }

    pub(super) fn validate(
        &self,
        logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
        unassigned_mode: &BTreeMap<String, UnassignedMode>,
        chunk_ids: &[String],
    ) -> Result<()> {
        let known = logical_modules
            .iter()
            .map(|(chunk, modules)| {
                Ok((
                    chunk.as_str(),
                    modules
                        .keys()
                        .map(|path| ModulePath::parse(path, ""))
                        .collect::<std::result::Result<BTreeSet<_>, _>>()?,
                ))
            })
            .collect::<Result<BTreeMap<_, _>>>()?;
        let mut selected = BTreeSet::new();
        for entry in &self.modules {
            ensure!(
                chunk_ids.contains(&entry.chunk),
                "ownership subset names unknown materialized chunk {:?}",
                entry.chunk
            );
            ensure!(
                known
                    .get(entry.chunk.as_str())
                    .is_some_and(|modules| modules.contains(&entry.module)),
                "ownership subset names unknown module {}::{}",
                entry.chunk,
                entry.module.as_str()
            );
            ensure!(
                selected.insert((&entry.chunk, &entry.module)),
                "ownership subset repeats module {}::{}",
                entry.chunk,
                entry.module
            );
        }
        for chunk in chunk_ids {
            ensure!(
                matches!(
                    unassigned_mode.get(chunk),
                    Some(
                        UnassignedMode::InlineInEntry
                            | UnassignedMode::PerAtomicGroup
                            | UnassignedMode::CoarseAtomicGroups
                    )
                ),
                "ownership subsets require inline_in_entry, per_atomic_group, or coarse_atomic_groups for chunk {chunk:?}"
            );
        }
        Ok(())
    }

    pub(super) fn module_paths(&self, chunk: &str) -> BTreeSet<ModulePath> {
        self.modules
            .iter()
            .filter(|entry| entry.chunk == chunk)
            .map(|entry| entry.module.clone())
            .collect()
    }
}
