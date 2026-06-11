use super::*;

/// Number of post-comma-list-split positions a top-level body
/// item produces. `var x = …, y = …;` is one body item but two
/// post-split owners (and therefore two `StatementOrdinal`s in
/// the owner graph). All other top-level items count as one.
/// Mirrors the splitting in `facts::top_level_item_views`.
pub(super) fn post_split_top_level_count(item: &ModuleItem) -> usize {
    fn decl_count(decl: &Decl) -> usize {
        match decl {
            Decl::Var(var) if var.decls.len() > 1 => var.decls.len(),
            _ => 1,
        }
    }
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_count(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            decl_count(&export_decl.decl)
        }
        _ => 1,
    }
}

/// Convert a pre-split body index to the first post-split
/// `StatementOrdinal` value for that body item. For anonymous
/// statements (which never split), this is the only ordinal in
/// the resulting range.
pub(super) fn statement_ordinal_for_body_index(body: &[ModuleItem], body_idx: usize) -> usize {
    body[..body_idx]
        .iter()
        .map(post_split_top_level_count)
        .sum()
}

/// Inverse of [`statement_ordinal_for_body_index`]: given a post-split
/// statement ordinal, return the pre-split body index of the body item
/// that produced it. Returns `None` if the ordinal is past the body.
pub(super) fn body_index_for_statement_ordinal(
    body: &[ModuleItem],
    stmt_ordinal: usize,
) -> Option<usize> {
    let mut running = 0usize;
    for (idx, item) in body.iter().enumerate() {
        let count = post_split_top_level_count(item);
        if stmt_ordinal < running + count {
            return Some(idx);
        }
        running += count;
    }
    None
}
