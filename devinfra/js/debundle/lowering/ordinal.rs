use super::*;

use js_ast::post_split_top_level_count;

/// Inverse of [`js_ast::statement_ordinal_for_body_index`]: given a
/// post-split statement ordinal, return the pre-split body index of the
/// body item that produced it. Returns `None` if the ordinal is past
/// the body.
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
