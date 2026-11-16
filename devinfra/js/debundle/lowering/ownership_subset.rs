//! Ownership selection is applied after the complete spec has resolved.

use anyhow::{Context, Result, ensure};
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Serialize};
use spec::{LogicalModule, ModulePath};
use spec_resolution::build_members;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
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
    /// Zero-based indices into the module's authored `anonymous_statements`
    /// selector list. Omission keeps every anonymous claim.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub anonymous_statements: Option<Vec<usize>>,
}

/// Per-chunk ownership choice recorded in ordinary reports.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct SelectedModule {
    pub module: ModulePath,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bindings: Option<BTreeSet<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub anonymous_statements: Option<BTreeSet<usize>>,
}

/// A normal run's retained ownership claims, independent of analysis inputs.
/// An empty list returns every logical-module claim to the chunk's unassigned
/// routing policy (which may synthesize residual modules).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OwnershipSubset {
    pub modules: Vec<RetainedModule>,
    /// Fresh selector-backed owners added to this selected view. These claims
    /// are resolved with the complete base spec's semantic premises.
    #[serde(
        default,
        deserialize_with = "deserialize_unique_placements",
        skip_serializing_if = "BTreeMap::is_empty"
    )]
    pub placements: BTreeMap<String, BTreeMap<String, LogicalModule>>,
}

struct UniqueStringMap<V>(BTreeMap<String, V>);

impl<'de, V: Deserialize<'de>> Deserialize<'de> for UniqueStringMap<V> {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct UniqueStringMapVisitor<V>(std::marker::PhantomData<V>);

        impl<'de, V: Deserialize<'de>> Visitor<'de> for UniqueStringMapVisitor<V> {
            type Value = UniqueStringMap<V>;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a map with unique string keys")
            }

            fn visit_map<M>(self, mut access: M) -> std::result::Result<Self::Value, M::Error>
            where
                M: MapAccess<'de>,
            {
                let mut entries = BTreeMap::new();
                while let Some((key, value)) = access.next_entry::<String, V>()? {
                    if entries.contains_key(&key) {
                        return Err(serde::de::Error::custom(format!(
                            "duplicate ownership subset placement key {key:?}"
                        )));
                    }
                    entries.insert(key, value);
                }
                Ok(UniqueStringMap(entries))
            }
        }

        deserializer.deserialize_map(UniqueStringMapVisitor(std::marker::PhantomData))
    }
}

fn deserialize_unique_placements<'de, D>(
    deserializer: D,
) -> std::result::Result<BTreeMap<String, BTreeMap<String, LogicalModule>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let UniqueStringMap(chunks) =
        UniqueStringMap::<UniqueStringMap<LogicalModule>>::deserialize(deserializer)?;
    Ok(chunks
        .into_iter()
        .map(|(chunk, UniqueStringMap(modules))| (chunk, modules))
        .collect())
}

impl OwnershipSubset {
    pub fn read(path: &Path) -> Result<Self> {
        Self::read_with_bytes(path).map(|(subset, _)| subset)
    }

    pub fn read_with_bytes(path: &Path) -> Result<(Self, Vec<u8>)> {
        let bytes = std::fs::read(path)
            .with_context(|| format!("read ownership subset {}", path.display()))?;
        let subset = serde_json::from_slice(&bytes)
            .with_context(|| format!("parse ownership subset {}", path.display()))?;
        Ok((subset, bytes))
    }

    pub(super) fn validate(
        &self,
        logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
        chunk_ids: &[String],
    ) -> Result<()> {
        let mut known = BTreeMap::new();
        for (chunk, modules) in logical_modules {
            let mut by_path = BTreeMap::new();
            for (raw_path, module) in modules {
                let path = ModulePath::parse(raw_path, "")?;
                if let Some((previous, _)) = by_path.insert(path.clone(), (raw_path, module)) {
                    anyhow::bail!(
                        "logical modules {}::{} and {}::{} normalize to the same path {}",
                        chunk,
                        previous,
                        chunk,
                        raw_path,
                        path
                    );
                }
            }
            known.insert(chunk.as_str(), by_path);
        }
        let mut selected = BTreeSet::new();
        for entry in &self.modules {
            ensure!(
                chunk_ids.contains(&entry.chunk),
                "ownership subset names unknown materialized chunk {:?}",
                entry.chunk
            );
            let module = known
                .get(entry.chunk.as_str())
                .and_then(|modules| modules.get(&entry.module))
                .map(|(_, module)| *module)
                .with_context(|| {
                    format!(
                        "ownership subset names unknown module {}::{}",
                        entry.chunk, entry.module
                    )
                })?;
            ensure!(
                selected.insert((&entry.chunk, &entry.module)),
                "ownership subset repeats module {}::{}",
                entry.chunk,
                entry.module
            );
            if let Some(anonymous_statements) = &entry.anonymous_statements {
                let mut selected_indices = BTreeSet::new();
                for &index in anonymous_statements {
                    ensure!(
                        selected_indices.insert(index),
                        "ownership subset repeats anonymous statement selector index {index} in {}::{}",
                        entry.chunk,
                        entry.module
                    );
                    ensure!(
                        index < module.anonymous_statements.len(),
                        "ownership subset names unknown anonymous statement selector index {index} in {}::{} (module has {} selectors)",
                        entry.chunk,
                        entry.module,
                        module.anonymous_statements.len()
                    );
                }
            }
            if let Some(bindings) = &entry.bindings {
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
        for chunk in self.placements.keys() {
            ensure!(
                chunk_ids.contains(chunk),
                "ownership subset placements name unknown materialized chunk {chunk:?}"
            );
            let base_modules = known.get(chunk.as_str());
            for (path, module) in self.placements_for_chunk(chunk)? {
                ensure!(
                    !base_modules.is_some_and(|modules| modules.contains_key(&path)),
                    "ownership subset placement {chunk}::{path} collides with a base module path"
                );
                ensure!(
                    module.annotations.is_empty(),
                    "ownership subset placement {chunk}::{path} cannot add semantic annotations"
                );
                ensure!(
                    module
                        .anonymous_statements
                        .iter()
                        .all(|statement| statement.no_reentry_call.is_none()),
                    "ownership subset placement {chunk}::{path} cannot add anonymous no_reentry_call premises"
                );
                ensure!(
                    !module.members.is_empty()
                        || !module.source_matches.is_empty()
                        || !module.anonymous_statements.is_empty(),
                    "ownership subset placement {chunk}::{path} must claim at least one selector"
                );
            }
        }
        Ok(())
    }

    pub(super) fn placements_for_chunk(
        &self,
        chunk: &str,
    ) -> Result<BTreeMap<ModulePath, LogicalModule>> {
        let mut placements = BTreeMap::new();
        for (raw_path, module) in self.placements.get(chunk).into_iter().flatten() {
            let path = ModulePath::parse(raw_path, "").with_context(|| {
                format!("invalid ownership subset placement path {chunk}::{raw_path}")
            })?;
            ensure!(
                placements.insert(path.clone(), module.clone()).is_none(),
                "ownership subset placement paths in {chunk} normalize to the same module path {path}"
            );
        }
        Ok(placements)
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
                anonymous_statements: entry
                    .anonymous_statements
                    .as_ref()
                    .map(|indices| indices.iter().copied().collect()),
            })
            .collect()
    }

    pub fn normalized_by_chunk(
        &self,
        chunks: &[String],
    ) -> BTreeMap<String, Vec<graph_provenance::SelectedOwnershipModule>> {
        chunks
            .iter()
            .map(|chunk| {
                let modules = self
                    .modules_for_chunk(chunk)
                    .into_iter()
                    .map(|module| graph_provenance::SelectedOwnershipModule {
                        module: module.module.to_string(),
                        bindings: module.bindings,
                        anonymous_statements: module.anonymous_statements,
                    })
                    .collect();
                (chunk.clone(), modules)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn subset_with_paths(paths: Value) -> OwnershipSubset {
        serde_json::from_value(json!({
            "modules": [],
            "placements": {"static/app": paths},
        }))
        .unwrap()
    }

    fn module_with_member() -> Value {
        json!({
            "members": [{"name": "Readable", "selector": {"binding": {"name": "raw"}}}],
        })
    }

    fn validate(
        subset: &OwnershipSubset,
        logical_modules: BTreeMap<String, BTreeMap<String, LogicalModule>>,
    ) -> Result<()> {
        subset.validate(&logical_modules, &["static/app".to_owned()])
    }

    #[test]
    fn placements_round_trip_normal_grouped_and_anonymous_selectors() {
        let json = json!({
            "modules": [],
            "placements": {
                "static/app": {
                    "features/Fresh/": {
                        "members": [{
                            "name": "ReadableMember",
                            "selector": {"binding": {"name": "rawMember"}}
                        }],
                        "source_matches": [{
                            "match": "const first = 1, second = 2;",
                            "bindings": [
                                {"local": "first", "name": "First"},
                                {"local": "second", "name": "Second"}
                            ],
                            "note": "grouped selector"
                        }],
                        "anonymous_statements": [{
                            "match": "console.log(first, second);",
                            "comment": "Keep the log with these values",
                            "note": "anonymous source claim"
                        }],
                        "comment": "Fresh module",
                        "note": "Authored placement"
                    }
                }
            }
        });
        let subset: OwnershipSubset = serde_json::from_value(json.clone()).unwrap();
        assert_eq!(serde_json::to_value(&subset).unwrap(), json);

        let base = BTreeMap::from([(
            "static/app".to_owned(),
            BTreeMap::from([("features/base".to_owned(), LogicalModule::default())]),
        )]);
        subset.validate(&base, &["static/app".to_owned()]).unwrap();
        let modules = subset.placements_for_chunk("static/app").unwrap();
        let fresh = modules
            .get(&ModulePath::parse("features/fresh", "").unwrap())
            .unwrap();
        assert_eq!(fresh.members.len(), 1);
        assert_eq!(fresh.source_matches.len(), 1);
        assert_eq!(fresh.source_matches[0].bindings.len(), 2);
        assert_eq!(fresh.anonymous_statements.len(), 1);
        assert_eq!(fresh.comment.as_deref(), Some("Fresh module"));
        assert_eq!(fresh.note.as_deref(), Some("Authored placement"));
    }

    #[test]
    fn placements_reject_unknown_chunks_invalid_paths_and_normalized_collisions() {
        let unknown_chunk: OwnershipSubset = serde_json::from_value(json!({
            "modules": [],
            "placements": {"static/other": {"fresh": module_with_member()}},
        }))
        .unwrap();
        assert!(validate(&unknown_chunk, BTreeMap::new()).is_err());

        let invalid_path = subset_with_paths(json!({"../outside": module_with_member()}));
        assert!(validate(&invalid_path, BTreeMap::new()).is_err());

        let duplicate_paths = subset_with_paths(json!({
            "features/Reader": module_with_member(),
            "features/reader/": module_with_member(),
        }));
        assert!(validate(&duplicate_paths, BTreeMap::new()).is_err());
    }

    #[test]
    fn placement_collisions_include_base_modules_omitted_from_selection() {
        let subset = subset_with_paths(json!({
            "features/existing/": module_with_member(),
        }));
        let base = BTreeMap::from([(
            "static/app".to_owned(),
            BTreeMap::from([("features/existing".to_owned(), LogicalModule::default())]),
        )]);
        assert!(validate(&subset, base).is_err());
    }

    #[test]
    fn placements_cannot_add_semantic_annotations_or_no_reentry_premises() {
        let annotated = subset_with_paths(json!({
            "fresh": {
                "members": [{"name": "Readable", "selector": {"binding": {"name": "raw"}}}],
                "annotations": {"Readable": {"purity": "pure"}}
            }
        }));
        assert!(validate(&annotated, BTreeMap::new()).is_err());

        let asserted_anonymous = subset_with_paths(json!({
            "fresh": {
                "anonymous_statements": [{
                    "match": "invoke(callback);",
                    "no_reentry_call": {"callback_arguments": [0]}
                }]
            }
        }));
        assert!(validate(&asserted_anonymous, BTreeMap::new()).is_err());
    }

    #[test]
    fn ownership_subset_json_rejects_duplicate_raw_placement_keys() {
        let error = serde_json::from_str::<OwnershipSubset>(
            r#"{"modules":[],"placements":{"static/app":{"fresh":{},"fresh":{}}}}"#,
        )
        .unwrap_err();
        assert!(
            error
                .to_string()
                .contains("duplicate ownership subset placement key")
        );
    }
}
