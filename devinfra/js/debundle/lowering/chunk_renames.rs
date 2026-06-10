//! Validate + collect chunk-level `chunk_renames` from the spec.

use super::*;

pub(super) fn collect_chunk_renames(
    chunk_renames: &ChunkRenames,
) -> Result<HashMap<String, String>> {
    let mut renames = HashMap::<String, String>::new();
    let id = chunk_renames.id.as_deref().unwrap_or("chunk_renames");
    for member in &chunk_renames.members {
        let Some(binding_selector) = &member.selector.binding else {
            bail!(
                "chunk_renames {id}: members[].selector.source_match is not supported here; use selector.binding.name"
            );
        };
        let binding = binding_selector.name.clone();
        let export_name = member.name.clone().unwrap_or_else(|| binding.clone());
        if let Some(existing) = renames.get(&binding) {
            if existing != &export_name {
                bail!(
                    "chunk_renames {id}: binding {binding} already renamed to \
                     {existing}; refusing to overwrite with {export_name}"
                );
            }
        } else {
            renames.insert(binding, export_name);
        }
    }
    Ok(renames)
}
