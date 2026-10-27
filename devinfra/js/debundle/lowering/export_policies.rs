//! Resolve analysis-policy labels without altering provider source or layout.
use std::borrow::Cow;
use std::collections::BTreeMap;

use anyhow::{Result, bail};
use artifact::ChunkBundle;
use spec::ChunkExportPurity;

pub(super) fn resolve_source_export_policies<'a>(
    artifact: &ChunkBundle,
    configured: &'a BTreeMap<String, ChunkExportPurity>,
) -> Result<Cow<'a, BTreeMap<String, ChunkExportPurity>>> {
    let source_policies: Vec<_> = configured
        .iter()
        .filter_map(|(label, policy)| {
            policy
                .source_markers
                .as_ref()
                .map(|markers| (label, policy, markers))
        })
        .collect();
    if source_policies.is_empty() {
        return Ok(Cow::Borrowed(configured));
    }
    for (label, _, markers) in &source_policies {
        if markers.is_empty() || markers.iter().any(|marker| marker.trim().is_empty()) {
            bail!(
                "chunk_export_purity.{label}.source_markers must contain at least one nonempty marker, and no empty markers"
            );
        }
    }
    let mut matches = vec![Vec::new(); source_policies.len()];
    for chunk in &artifact.chunks {
        let Some(entry) = chunk.js.get_file(&chunk.analysis.entry_file) else {
            continue;
        };
        // Raw entries retain their exact input text. AST entries use the current
        // staged body, not stale original text retained in the parser SourceMap.
        let source = match entry.source() {
            Some(source) => Cow::Borrowed(source),
            None => Cow::Owned(entry.render_source()?),
        };
        for (index, (_, _, markers)) in source_policies.iter().enumerate() {
            if markers
                .iter()
                .all(|marker| source.contains(marker.as_str()))
            {
                matches[index].push(artifact.chunk_table.name(chunk.chunk_id).to_string());
            }
        }
    }
    let mut resolved: BTreeMap<_, _> = configured
        .iter()
        .filter(|(_, policy)| policy.source_markers.is_none())
        .map(|(chunk, policy)| (chunk.clone(), policy.clone()))
        .collect();
    for ((label, policy, _), providers) in source_policies.into_iter().zip(matches) {
        let [provider] = providers.as_slice() else {
            bail!(
                "chunk_export_purity.{label}.source_markers must match exactly one loaded provider; matched {}: {}",
                providers.len(),
                providers.join(", ")
            );
        };
        if resolved.contains_key(provider) {
            bail!(
                "chunk_export_purity.{label}.source_markers collides with another policy for provider {provider}"
            );
        }
        let mut policy = policy.clone();
        policy.source_markers = None;
        resolved.insert(provider.clone(), policy);
    }
    Ok(Cow::Owned(resolved))
}
