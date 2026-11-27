//! Run-hole list placement and var-decl declarator alignment.

use std::collections::BTreeSet;

use chunk_facts::{NodeId, NodeKind};

use crate::bindings::Bindings;
use crate::holes::{is_floating_declarator, is_run_hole_carrier};
use crate::homo::homo;
use crate::index::Index;
use crate::validate::unsupported_needle_construct;
use crate::{Matched, Mode, Unsupported};

/// Partition `0..len` into maximal runs of non-hole positions — the fixed
/// segments placed as an ordered subsequence with run-hole gaps — plus the
/// anchoring flags (`anchored_left`/`anchored_right` = the first/last position is
/// a fixed anchor). Position `i` is a hole iff `is_hole(i)`; a floating position
/// (`is_floating(i)`) is a singleton segment with gaps allowed on both sides. An
/// all-holes list yields no segments. Shared by the list / declarator /
/// top-level placement paths.
pub(crate) fn segment_partition(
    len: usize,
    is_hole: impl Fn(usize) -> bool,
    is_floating: impl Fn(usize) -> bool,
) -> (Vec<(usize, usize)>, bool, bool) {
    let mut segments = Vec::new();
    let mut idx = 0;
    while idx < len {
        if is_hole(idx) {
            idx += 1;
            continue;
        }
        let start = idx;
        if is_floating(idx) {
            idx += 1;
        } else {
            while idx < len && !is_hole(idx) && !is_floating(idx) {
                idx += 1;
            }
        }
        segments.push((start, idx - start));
    }
    let anchored = |i: usize| !is_hole(i) && !is_floating(i);
    let anchored_left = len > 0 && anchored(0);
    let anchored_right = len > 0 && anchored(len - 1);
    (segments, anchored_left, anchored_right)
}

/// Ordered-subsequence-with-gaps match of a run-hole-bearing needle list against
/// a candidate list. The carriers partition the needle into maximal fixed
/// segments; an all-holes list pins nothing (matches any candidate run).
pub(crate) fn match_list_with_holes(
    needle: &Index,
    nlist: &[NodeId],
    subject: &Index,
    slist: &[NodeId],
    parent_kind: NodeKind,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let (segments, anchored_left, anchored_right) = segment_partition(
        nlist.len(),
        |i| is_run_hole_carrier(needle, parent_kind, nlist[i]),
        |i| is_floating_declarator(needle, parent_kind, nlist[i]),
    );
    if segments.is_empty() {
        return Ok(true);
    }
    place_segments(
        needle,
        nlist,
        subject,
        slist,
        &segments,
        anchored_left,
        anchored_right,
        0,
        0,
        mode,
        bindings,
        None,
    )
}

/// The var-decl node of a single var-decl-statement's facts, paired with the
/// root wrapper kind so the caller can enforce wrapper symmetry (a plain `const`
/// statement must not match an `export const`). `None` if the statement is not a
/// (possibly exported) variable declaration.
pub(crate) fn var_decl_node(index: &Index) -> Option<(NodeKind, NodeId)> {
    let &root = index.roots.first()?;
    match index.kind_of(root) {
        NodeKind::VarDecl => Some((NodeKind::VarDecl, root)),
        NodeKind::ExportDecl => {
            let &kid = index.children_of(root).first()?;
            (index.kind_of(kid) == NodeKind::VarDecl).then_some((NodeKind::ExportDecl, kid))
        }
        _ => None,
    }
}

/// Greedy-leftmost alignment of a needle var-decl's declarators (possibly
/// carrying `DECLARATORS` run holes) onto a subject var-decl's declarators,
/// recording for each needle declarator the subject declarator it placed onto. A
/// hole-free needle aligns 1:1 (lengths must match); otherwise the non-hole
/// declarators partition into maximal fixed segments placed as an ordered
/// subsequence with gaps. `None` when no placement matches.
fn align_var_declarators(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<Option<Vec<Option<usize>>>, Unsupported> {
    let mut alignment = vec![None; ndecls.len()];
    let is_hole = |d: NodeId| is_run_hole_carrier(needle, NodeKind::VarDecl, d);
    let is_floating = |d: NodeId| is_floating_declarator(needle, NodeKind::VarDecl, d);
    if !ndecls.iter().any(|&d| is_hole(d) || is_floating(d)) {
        if ndecls.len() != sdecls.len() {
            return Ok(None);
        }
        for (idx, (&nd, &sd)) in ndecls.iter().zip(sdecls).enumerate() {
            if !homo(needle, nd, subject, sd, mode, bindings)? {
                return Ok(None);
            }
            alignment[idx] = Some(idx);
        }
        return Ok(Some(alignment));
    }
    let (segments, anchored_left, anchored_right) = segment_partition(
        ndecls.len(),
        |i| is_hole(ndecls[i]),
        |i| is_floating(ndecls[i]),
    );
    // An all-holes declarator list pins nothing (no positions to align).
    if segments.is_empty() {
        return Ok(Some(alignment));
    }
    let placed = place_segments(
        needle,
        ndecls,
        subject,
        sdecls,
        &segments,
        anchored_left,
        anchored_right,
        0,
        0,
        mode,
        bindings,
        Some(&mut alignment),
    )?;
    Ok(placed.then_some(alignment))
}

/// Shared run-hole placement for ordinary lists and declarator diagnostics.
/// Record alignment only when requested; boolean matches allocate no throwaway
/// alignment buffer or alignment snapshots. Binding rollback is unconditional.
#[allow(clippy::too_many_arguments)]
fn place_segments(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    segments: &[(usize, usize)],
    anchored_left: bool,
    anchored_right: bool,
    seg_idx: usize,
    cand_min: usize,
    mode: Mode,
    bindings: &mut Bindings,
    mut alignment: Option<&mut [Option<usize>]>,
) -> Result<bool, Unsupported> {
    let Some(&(needle_start, seg_len)) = segments.get(seg_idx) else {
        return Ok(true);
    };
    let remaining: usize = segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = sdecls.len().checked_sub(remaining) else {
        return Ok(false);
    };
    let mut lo = cand_min;
    let mut hi = latest_start;
    if seg_idx == 0 && anchored_left {
        hi = 0;
    }
    if seg_idx == segments.len() - 1 && anchored_right {
        lo = lo.max(latest_start);
    }
    for start in lo..=hi {
        let bindings_snapshot = bindings.clone();
        let alignment_snapshot = alignment.as_deref().map(<[_]>::to_vec);
        let mut segment_ok = true;
        for offset in 0..seg_len {
            if !homo(
                needle,
                ndecls[needle_start + offset],
                subject,
                sdecls[start + offset],
                mode,
                bindings,
            )? {
                segment_ok = false;
                break;
            }
            if let Some(alignment) = alignment.as_deref_mut() {
                alignment[needle_start + offset] = Some(start + offset);
            }
        }
        if segment_ok
            && place_segments(
                needle,
                ndecls,
                subject,
                sdecls,
                segments,
                anchored_left,
                anchored_right,
                seg_idx + 1,
                start + seg_len,
                mode,
                bindings,
                alignment.as_deref_mut(),
            )?
        {
            return Ok(true);
        }
        *bindings = bindings_snapshot;
        if let (Some(alignment), Some(snapshot)) = (alignment.as_deref_mut(), alignment_snapshot) {
            alignment.copy_from_slice(&snapshot);
        }
    }
    Ok(false)
}

/// Match two var-decl statements over prebuilt indices and, on success, return the greedy-leftmost
/// declarator alignment (needle declarator index → subject declarator index,
/// `None` for a `DECLARATORS`-hole-absorbed needle position); `None` if they do
/// not match. `prebind` pins needle identifiers to subject identifiers in the
/// root frame before matching, so the caller can force the target binding's
/// identity (in either mode). This composes the whole-statement
/// var-decl match (wrapper symmetry, `var`/`let`/`const` kind, declarator
/// alignment): a `Some` result *is* a faithful match. Fail-closed on an
/// unsupported needle construct.
pub fn var_declarator_alignment_indexed(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    prebind: &[(&str, &str)],
    free: &BTreeSet<String>,
) -> Result<Option<Matched<Vec<Option<usize>>>>, Unsupported> {
    if let Some(reason) = unsupported_needle_construct(needle) {
        return Err(Unsupported { reason });
    }
    Ok(var_declarator_alignment_prepared(
        needle, subject, mode, prebind, free,
    ))
}

/// Like [`var_declarator_alignment_indexed`], but **without** re-running the
/// needle-only [`unsupported_needle_construct`] faithful-subset check — the caller
/// must have already probed this exact needle (once, before the candidate loop) so
/// the only `Unsupported` source is provably absent. This is the per-candidate hot
/// path: `unsupported_needle_construct` is three full passes over the needle index
/// plus `collect_subtree` recursion, all a function of the needle alone, so re-running
/// it for every subject is `O(selectors × candidates)` wasted work the up-front probe
/// already did. The return is therefore infallible (`Option`, not `Result`).
pub fn var_declarator_alignment_prepared(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    prebind: &[(&str, &str)],
    free: &BTreeSet<String>,
) -> Option<Matched<Vec<Option<usize>>>> {
    let (Some((nwrap, nvd)), Some((swrap, svd))) = (var_decl_node(needle), var_decl_node(subject))
    else {
        return None;
    };
    // Wrapper symmetry (plain vs exported) and `var`/`let`/`const` kind: the
    // node-level structure the declarator alignment does not itself compare.
    if nwrap != swrap || needle.operator_of(nvd) != subject.operator_of(svd) {
        return None;
    }
    let mut bindings = Bindings::new(free);
    if !bindings.prebind(prebind) {
        return None;
    }
    // The needle is probed-supported, so no sub-node descent can error (see
    // [`nodes_match`]); unwrap the infallible alignment.
    let alignment = align_var_declarators(
        needle,
        needle.children_of(nvd),
        subject,
        subject.children_of(svd),
        mode,
        &mut bindings,
    )
    .expect("needle construct already probed as supported")?;
    Some(Matched {
        site: alignment,
        free_bindings: bindings.free_bindings(),
    })
}

/// Greedy-leftmost in-order match of a needle var-decl's **pinned** (non-hole)
/// declarators onto a subject var-decl's declarators, returning the matched
/// `(needle_decl_idx, subject_decl_idx)` pairs in order — the diagnostic greedy
/// scan used by `fact_near_miss`'s declarator-alignment reasons (*not* the segment
/// placement of [`align_var_declarators`]). One [`Bindings`] is threaded across the
/// whole scan with snapshot/restore, so an alpha binding committed by an earlier
/// pin constrains a later one. `ndecls`/`sdecls` are the two `VarDecl` nodes'
/// declarator children (`index.children_of(var_decl_node)`).
pub fn pinned_declarator_matches_in_order(
    needle: &Index,
    ndecls: &[NodeId],
    subject: &Index,
    sdecls: &[NodeId],
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<Vec<(usize, usize)>, Unsupported> {
    let mut bindings = Bindings::new(free);
    let mut subject_start = 0;
    let mut matches = Vec::new();
    for (needle_idx, &ndecl) in ndecls.iter().enumerate() {
        if is_run_hole_carrier(needle, NodeKind::VarDecl, ndecl) {
            continue;
        }
        let mut found = None;
        for (subject_idx, &sdecl) in sdecls.iter().enumerate().skip(subject_start) {
            let snapshot = bindings.clone();
            if homo(needle, ndecl, subject, sdecl, mode, &mut bindings)? {
                found = Some(subject_idx);
                break;
            }
            bindings = snapshot;
        }
        let Some(subject_idx) = found else {
            break;
        };
        matches.push((needle_idx, subject_idx));
        subject_start = subject_idx + 1;
    }
    Ok(matches)
}

/// The init node of a single-declarator var-decl statement: `var_decl_node` →
/// first `VarDeclarator` → its `init` child (declarator child ordinal 1).
pub(crate) fn init_node_of(index: &Index) -> Option<NodeId> {
    let (_, var_decl) = var_decl_node(index)?;
    let &declarator = index.children_of(var_decl).first()?;
    index.children_of(declarator).get(1).copied()
}
