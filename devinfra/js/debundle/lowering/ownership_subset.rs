//! Ownership selection is applied after the complete spec has resolved.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use spec::{LogicalModule, ModulePath};
use spec_resolution::build_members;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// One exact output module, using the chunk-local path shown in module reports.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetainedModule {
    pub chunk: String,
    pub module: ModulePath,
    /// Final readable member names. Omission keeps every member claim.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bindings: Option<Vec<String>>,
}

/// Per-chunk ownership choice recorded in ordinary reports.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct SelectedModule {
    pub module: ModulePath,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bindings: Option<BTreeSet<String>>,
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
            if let Some(bindings) = &entry.bindings {
                let module = &logical_modules[&entry.chunk][entry.module.as_str()];
                let request_id = format!("{}::{}", entry.chunk, entry.module);
                let known_names = build_members(
                    &module.members,
                    &module.source_matches,
                    &module.annotations,
                    &request_id,
                )?
                .into_iter()
                .map(|member| member.export_name)
                .collect::<BTreeSet<_>>();
                let mut selected_names = BTreeSet::new();
                for name in bindings {
                    ensure!(
                        selected_names.insert(name),
                        "ownership subset repeats binding {name:?} in {}::{}",
                        entry.chunk,
                        entry.module
                    );
                    ensure!(
                        known_names.contains(name),
                        "ownership subset names unknown binding {name:?} in {}::{}",
                        entry.chunk,
                        entry.module
                    );
                }
            }
        }
        Ok(())
    }

    pub(super) fn modules_for_chunk(&self, chunk: &str) -> BTreeSet<SelectedModule> {
        self.modules
            .iter()
            .filter(|entry| entry.chunk == chunk)
            .map(|entry| SelectedModule {
                module: entry.module.clone(),
                bindings: entry
                    .bindings
                    .as_ref()
                    .map(|names| names.iter().cloned().collect()),
            })
            .collect()
    }
}
