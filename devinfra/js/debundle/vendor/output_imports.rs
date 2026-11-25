//! Resolve vendor imports in emitted-output coordinates, including materialized chunk paths.
use artifact::ArtifactIndexes;
use artifact::paths::{
    join_module_path, module_path_dirname, normalize_module_path, relative_module_specifier,
};
use source_identity::{ChunkId, ChunkTable};
use std::collections::HashMap;
use std::path::Path;

/// Caller-relative module specifier for a generated bundled facade:
/// `facade_app_path` rebased against the caller file's output-tree
/// directory. Shared by the pass-through emission rewriter and lowering's
/// construction-time facade imports (where `caller_file_path` is the
/// materialized module's target file).
pub fn bundled_facade_import_source(
    chunk_table: &ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    facade_app_path: &str,
) -> String {
    let caller_output_file = Path::new(chunk_table.name(caller_chunk_id)).join(caller_file_path);
    let caller_output_dir = caller_output_file.parent().unwrap_or_else(|| Path::new(""));
    relative_module_specifier(caller_output_dir, Path::new(facade_app_path))
}

/// Resolve a directive source to its target chunk for swap
/// classification: artifact-index resolution first, then the
/// materialized-output longest-prefix fallback. Shared by the pass-through
/// emission rewriter, the consumer gate, and lowering's construction-time
/// vendor consultation (which resolves the source chunk's original
/// directives from the same coordinate system).
pub fn resolve_partial_swap_import_target(
    source: &str,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> Option<ChunkId> {
    references
        .resolve_runtime_import_reference(source, caller_chunk_id, caller_file_path, chunk_table)
        .map(|resolved| resolved.target_chunk_id)
        .or_else(|| {
            resolve_materialized_output_import_target(
                source,
                caller_chunk_id,
                caller_file_path,
                chunk_table,
                materialized_index,
            )
        })
}

fn resolve_materialized_output_import_target(
    source: &str,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> Option<ChunkId> {
    if source.is_empty() || !source.starts_with('.') {
        return None;
    }
    let caller_output_dir = join_module_path(&[
        chunk_table.name(caller_chunk_id),
        module_path_dirname(caller_file_path).as_str(),
    ]);
    let resolved_path =
        normalize_module_path(&join_module_path(&[caller_output_dir.as_str(), source])).ok()?;
    materialized_index.lookup(&resolved_path)
}

/// Precomputed longest-prefix-match index for resolving partial-swap
/// relative imports to their target chunk. Built once per pass, so lookups
/// need no per-import-decl O(N_chunks) scan over `ChunkTable`.
///
/// Index both the full name and the alias obtained by dropping the first
/// path component. A shared alias stays ambiguous instead of picking a chunk.
pub struct MaterializedOutputChunkIndex {
    aliases: HashMap<String, Option<ChunkId>>,
}

impl MaterializedOutputChunkIndex {
    pub fn build(chunk_table: &ChunkTable) -> Self {
        let mut aliases = HashMap::with_capacity(chunk_table.len() * 2);
        for index in 0..chunk_table.len() {
            let id = ChunkId(index);
            let name = chunk_table.name(id);
            let stripped = name.split_once('/').map(|(_, rest)| rest);
            for alias in std::iter::once(name).chain(stripped) {
                aliases
                    .entry(alias.to_owned())
                    .and_modify(|entry| {
                        if *entry != Some(id) {
                            *entry = None;
                        }
                    })
                    .or_insert(Some(id));
            }
        }
        Self { aliases }
    }

    fn lookup(&self, path: &str) -> Option<ChunkId> {
        // In "<name>.js", every slash is inside <name>, so an exact alias
        // is longer than any matching directory prefix. Even an ambiguous
        // exact alias takes precedence over a shorter unique prefix.
        if let Some(entry) = path
            .strip_suffix(".js")
            .and_then(|name| self.aliases.get(name))
        {
            return *entry;
        }
        let mut path = path;
        while let Some((parent, _)) = path.rsplit_once('/') {
            if let Some(entry) = self.aliases.get(parent) {
                return *entry;
            }
            path = parent;
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk_table_with(names: &[&str]) -> ChunkTable {
        let mut t = ChunkTable::default();
        for n in names {
            t.intern((*n).to_string());
        }
        t
    }

    #[test]
    fn materialized_index_resolves_simple_name() {
        let table = chunk_table_with(&["app", "vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(
            index.lookup("vendor.js"),
            Some(table.get("vendor").unwrap())
        );
        assert_eq!(
            index.lookup("vendor/entry.js"),
            Some(table.get("vendor").unwrap())
        );
        assert_eq!(
            index.lookup("app/entry.js"),
            Some(table.get("app").unwrap())
        );
        assert_eq!(index.lookup("missing.js"), None);
    }

    #[test]
    fn materialized_index_prefers_longer_prefix() {
        // Chunk "a/b" should win over "a" for path "a/b/x.js" because
        // match_len(3) > match_len(1).
        let table = chunk_table_with(&["a", "a/b"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(index.lookup("a/b/x.js"), Some(table.get("a/b").unwrap()));
        assert_eq!(index.lookup("a/x.js"), Some(table.get("a").unwrap()));
    }

    #[test]
    fn ambiguous_exact_alias_shadows_shorter_directory_match() {
        let table = chunk_table_with(&["a", "a/b", "static/a/b"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(index.lookup("a/b.js"), None);
        assert_eq!(index.lookup("a/b/file.js"), None);
        assert_eq!(index.lookup("a/c.js"), table.get("a"));
    }

    #[test]
    fn materialized_index_stripped_form_resolves() {
        // Chunk name "static/vendor" exposes stripped form "vendor"; a
        // path like "vendor.js" should resolve to that chunk.
        let table = chunk_table_with(&["static/vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        let target = table.get("static/vendor").unwrap();
        assert_eq!(index.lookup("static/vendor.js"), Some(target));
        assert_eq!(index.lookup("vendor.js"), Some(target));
        assert_eq!(index.lookup("vendor/foo.js"), Some(target));
    }

    #[test]
    fn materialized_index_ambiguous_returns_none() {
        // Both chunk "vendor" (exact name) and chunk "static/vendor"
        // (stripped form) match path "vendor.js" with match_len=6 →
        // ambiguous.
        let table = chunk_table_with(&["vendor", "static/vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(index.lookup("vendor.js"), None);
        assert_eq!(index.lookup("vendor/foo.js"), None);
    }
}
