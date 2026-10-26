//! Resolve vendor imports in emitted-output coordinates, including materialized chunk paths.
use artifact::paths::{
    join_module_path, module_path_dirname, normalize_module_path, relative_module_specifier,
};
use artifact::{ArtifactIndexes, ChunkId, ChunkTable};
use std::collections::HashMap;
use std::path::Path;

/// Caller-relative module specifier for a generated bundled facade:
/// `facade_app_path` rebased against the caller file's output-tree
/// directory. Shared by the bundled wave and lowering's
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
/// materialized-output longest-prefix fallback. Shared by the wave
/// dispatchers, the consumer gate, and lowering's construction-time
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
/// relative imports to their target chunk. Built once per
/// `apply_*partial_vendor_swaps` invocation; replaces the per-import-decl
/// O(N_chunks) scan over `ChunkTable`.
///
/// Per chunk we register candidate keys derived from the chunk name plus,
/// for slash-bearing names, the post-first-slash stripped form (mirroring
/// the original `materialized_output_chunk_match_len` two-shape match):
///   * `by_exact["<name>.js"]`         — exact match against
///     `resolved_path` (match_len = name.len()).
///   * `by_dir_prefix["<name>"]`       — `resolved_path` is
///     `"<name>/<rest>"` (match_len = name.len()).
///
/// Lookup checks the exact map plus walks `resolved_path`'s `/`-bounded
/// ancestors longest-to-shortest against `by_dir_prefix`, then combines
/// the two candidates with longest-match-wins / tie-breaks-as-`None`
/// (same semantics as the prior linear scan, including `ambiguous`).
pub struct MaterializedOutputChunkIndex {
    by_exact: HashMap<String, ChunkEntry>,
    by_dir_prefix: HashMap<String, ChunkEntry>,
}

#[derive(Clone, Copy)]
enum ChunkEntry {
    Unique(ChunkId, usize),
    Ambiguous(usize),
}

impl ChunkEntry {
    fn match_len(&self) -> usize {
        match self {
            ChunkEntry::Unique(_, len) | ChunkEntry::Ambiguous(len) => *len,
        }
    }

    fn merge(&mut self, chunk_id: ChunkId, match_len: usize) {
        match *self {
            ChunkEntry::Unique(existing, existing_len) => {
                debug_assert_eq!(existing_len, match_len);
                if existing != chunk_id {
                    *self = ChunkEntry::Ambiguous(match_len);
                }
            }
            ChunkEntry::Ambiguous(existing_len) => {
                debug_assert_eq!(existing_len, match_len);
            }
        }
    }
}

impl MaterializedOutputChunkIndex {
    pub fn build(chunk_table: &ChunkTable) -> Self {
        let len = chunk_table.len();
        let mut by_exact: HashMap<String, ChunkEntry> = HashMap::with_capacity(len * 2);
        let mut by_dir_prefix: HashMap<String, ChunkEntry> = HashMap::with_capacity(len * 2);
        for index in 0..len {
            let chunk_id = ChunkId(index);
            let chunk_name = chunk_table.name(chunk_id);
            insert_candidate(&mut by_exact, &mut by_dir_prefix, chunk_id, chunk_name);
            if let Some((_, stripped)) = chunk_name.split_once('/') {
                insert_candidate(&mut by_exact, &mut by_dir_prefix, chunk_id, stripped);
            }
        }
        Self {
            by_exact,
            by_dir_prefix,
        }
    }

    fn lookup(&self, resolved_path: &str) -> Option<ChunkId> {
        let exact = self.by_exact.get(resolved_path).copied();
        let prefix = self.longest_prefix_match(resolved_path);
        let candidate = match (exact, prefix) {
            (None, None) => return None,
            (Some(c), None) | (None, Some(c)) => c,
            (Some(a), Some(b)) => {
                if a.match_len() > b.match_len() {
                    a
                } else if b.match_len() > a.match_len() {
                    b
                } else {
                    // Equal lengths: ambiguous unless both resolve to the
                    // same Unique chunk (only possible when an exact
                    // `"<name>.js"` key happens to also be a registered
                    // dir-prefix for the same chunk, which the original
                    // semantics never produced — but we keep the check
                    // explicit).
                    match (a, b) {
                        (ChunkEntry::Unique(ax, _), ChunkEntry::Unique(bx, _)) if ax == bx => a,
                        _ => return None,
                    }
                }
            }
        };
        match candidate {
            ChunkEntry::Unique(chunk_id, _) => Some(chunk_id),
            ChunkEntry::Ambiguous(_) => None,
        }
    }

    fn longest_prefix_match(&self, resolved_path: &str) -> Option<ChunkEntry> {
        // Walk `/`-bounded ancestors of `resolved_path` longest-first.
        // A by_dir_prefix entry `K` matches iff `resolved_path == "<K>/<rest>"`.
        let mut end = resolved_path.rfind('/')?;
        loop {
            if let Some(entry) = self.by_dir_prefix.get(&resolved_path[..end]) {
                return Some(*entry);
            }
            end = resolved_path[..end].rfind('/')?;
        }
    }
}

fn insert_candidate(
    by_exact: &mut HashMap<String, ChunkEntry>,
    by_dir_prefix: &mut HashMap<String, ChunkEntry>,
    chunk_id: ChunkId,
    name: &str,
) {
    let match_len = name.len();
    let exact_key = format!("{name}.js");
    by_exact
        .entry(exact_key)
        .and_modify(|e| e.merge(chunk_id, match_len))
        .or_insert(ChunkEntry::Unique(chunk_id, match_len));
    by_dir_prefix
        .entry(name.to_string())
        .and_modify(|e| e.merge(chunk_id, match_len))
        .or_insert(ChunkEntry::Unique(chunk_id, match_len));
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
