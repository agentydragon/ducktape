//! Collect chunk-level `chunk_renames` from the spec into the rename
//! ledger (scope: `Chunk`, origin: `Explicit`). Duplicate members that
//! disagree on one binding's target surface as a seal-time conflict
//! naming both entries.

use analysis::top_level_id;
use anyhow::Result;
use rename_ledger::{RenameIntent, RenameLedger, RenameOrigin, RenameScope};
use spec::ChunkRenames;

pub(super) const CHUNK_RENAMES_CONTRIBUTOR: &str = "spec chunk_renames member";

pub(super) fn collect_chunk_renames(
    chunk_renames: &ChunkRenames,
    chunk_top_level_mark: swc_common::Mark,
    ledger: &mut RenameLedger,
) -> Result<()> {
    for member in &chunk_renames.members {
        let binding = &member.selector.binding.name;
        let export_name = member.name.clone().unwrap_or_else(|| binding.clone());
        ledger.submit(RenameIntent {
            scope: RenameScope::Chunk,
            from: top_level_id(binding, chunk_top_level_mark),
            to: export_name.into(),
            origin: RenameOrigin::Explicit {
                contributor: CHUNK_RENAMES_CONTRIBUTOR,
            },
        });
    }
    Ok(())
}
