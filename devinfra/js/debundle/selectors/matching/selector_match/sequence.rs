//! Multi-statement sequence, window and exact-range matching.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

use crate::bindings::{Bindings, DIAGNOSTIC_DEPTH_LIMIT, DIAGNOSTIC_LIMIT_REASON, FailureTrace};
use crate::holes::is_module_stmt_list_hole;
use crate::homo::homo;
use crate::index::Index;
use crate::list_align::{segment_partition, var_decl_node};
use crate::validate::unsupported_needle_construct;
use crate::{ExactRangeMatch, Matched, Mode, Unsupported};

/// Match the caller's entire selected range, including any statements absorbed
/// by top-level `STMT_LIST` holes. This is a diagnostic-only entry point: it
/// calls the same `homo` relation and carries the same alpha state across every
/// statement. Unlike the resolver's candidate enumeration, neither end may
/// silently float outside the supplied range.
pub fn explain_exact_range_indexed(
    needle: &[Index],
    subject: &[Index],
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<ExactRangeMatch, Unsupported> {
    for item in needle.iter().filter(|item| !is_module_stmt_list_hole(item)) {
        if let Some(reason) = unsupported_needle_construct(item) {
            return Err(Unsupported { reason });
        }
    }
    let trace = Rc::new(RefCell::new(FailureTrace::default()));
    let mut bindings = Bindings::with_trace(free, Rc::clone(&trace));
    let mut alignment = vec![None; needle.len()];
    // An all-hole template has no pin, as in match_top_level_sequence_indexed.
    let mut range = ExactRangeContext {
        needle,
        subject,
        mode,
        alignment: &mut alignment,
    };
    let matched = if needle.iter().any(|item| !is_module_stmt_list_hole(item)) {
        match range.place(0, 0, &mut bindings) {
            Ok(matched) => matched,
            Err(err) => {
                if trace.borrow().limited {
                    false
                } else {
                    return Err(err);
                }
            }
        }
    } else {
        false
    };
    let trace = trace.borrow();
    Ok(ExactRangeMatch {
        matched,
        alignment: if matched { alignment } else { Vec::new() },
        free_bindings: if matched {
            bindings.free_bindings()
        } else {
            BTreeMap::new()
        },
        failure: if matched { None } else { trace.best.clone() },
        diagnostic_limited: !matched && trace.limited,
    })
}

struct ExactRangeContext<'a> {
    needle: &'a [Index],
    subject: &'a [Index],
    mode: Mode,
    alignment: &'a mut [Option<usize>],
}

impl ExactRangeContext<'_> {
    fn place(
        &mut self,
        needle_pos: usize,
        subject_pos: usize,
        bindings: &mut Bindings<'_>,
    ) -> Result<bool, Unsupported> {
        bindings.step()?;
        if needle_pos == self.needle.len() {
            return Ok(subject_pos == self.subject.len());
        }
        if needle_pos >= DIAGNOSTIC_DEPTH_LIMIT {
            if let Some(trace) = &bindings.trace {
                trace.borrow_mut().limited = true;
            }
            return Err(Unsupported {
                reason: DIAGNOSTIC_LIMIT_REASON,
            });
        }
        if is_module_stmt_list_hole(&self.needle[needle_pos]) {
            for next_subject in subject_pos..=self.subject.len() {
                let snapshot = bindings.clone();
                if self.place(needle_pos + 1, next_subject, bindings)? {
                    return Ok(true);
                }
                *bindings = snapshot;
            }
            return Ok(false);
        }
        let (Some(item), Some(candidate)) =
            (self.needle.get(needle_pos), self.subject.get(subject_pos))
        else {
            return Ok(false);
        };
        let (Some(&needle_root), Some(&subject_root)) =
            (item.roots.first(), candidate.roots.first())
        else {
            return Ok(false);
        };
        bindings.statement_index = needle_pos;
        bindings.subject_statement_index = subject_pos;
        if !homo(
            item,
            needle_root,
            candidate,
            subject_root,
            self.mode,
            bindings,
        )? {
            return Ok(false);
        }
        self.alignment[needle_pos] = Some(subject_pos);
        self.place(needle_pos + 1, subject_pos + 1, bindings)
    }
}

/// Match a multi-statement needle (each element one statement's index, in source
/// order) against a chunk body (each element one top-level statement's index, in
/// body order) as an ordered subsequence with module-level `STMT_LIST` holes,
/// **enumerating every alignment** (needle-index → body-index, `None` for a hole
/// position). Never anchored at module level, and an all-holes needle pins
/// nothing (no alignment). Fail-closed on an unsupported non-hole needle
/// statement.
pub fn match_top_level_sequence_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<Vec<Matched<Vec<Option<usize>>>>, Unsupported> {
    let (segments, _, _) = segment_partition(
        needle_idx.len(),
        |i| is_module_stmt_list_hole(&needle_idx[i]),
        |_| false,
    );
    // A fixed (non-hole) needle statement must be faithfully supported.
    for &(start, len) in &segments {
        for item in &needle_idx[start..start + len] {
            if let Some(reason) = unsupported_needle_construct(item) {
                return Err(Unsupported { reason });
            }
        }
    }
    // An all-holes needle pins nothing — no alignment (matches the matcher).
    if segments.is_empty() {
        return Ok(Vec::new());
    }
    let mut alignment = vec![None; needle_idx.len()];
    let mut matches = Vec::new();
    let mut bindings = Bindings::new(free);
    place_top_level(
        needle_idx,
        subject_idx,
        &segments,
        0,
        0,
        mode,
        &mut bindings,
        &mut alignment,
        &mut matches,
    )?;
    Ok(matches)
}

/// Align a multi-statement needle against the subject body as a **fixed
/// contiguous window** (the member binding-group path): slide
/// `subject.windows(needle.len())` and match each window positionally 1:1 with one
/// [`Bindings`] threaded through the window. Returns the body start index of every
/// full-window match.
///
/// Deviation from [`match_top_level_sequence_indexed`]: there are **no**
/// module-level `STMT_LIST` holes here. A run-hole keyword is not faithfully
/// encodable in a fixed-window position, so a needle bearing one fails closed via
/// the per-item unsupported scan (`STMT_LIST` is a statement-list hole, not a
/// single-statement wildcard, so a fixed window finds zero either way).
pub fn match_fixed_window_sequence_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<Vec<Matched<usize>>, Unsupported> {
    for needle in needle_idx {
        if let Some(reason) = unsupported_needle_construct(needle) {
            return Err(Unsupported { reason });
        }
    }
    let n = needle_idx.len();
    let mut starts = Vec::new();
    if n == 0 || n > subject_idx.len() {
        return Ok(starts);
    }
    for start in 0..=(subject_idx.len() - n) {
        let mut bindings = Bindings::new(free);
        let mut matched = true;
        for offset in 0..n {
            let needle = &needle_idx[offset];
            let subject = &subject_idx[start + offset];
            let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first())
            else {
                matched = false;
                break;
            };
            if !homo(needle, n_root, subject, s_root, mode, &mut bindings)? {
                matched = false;
                break;
            }
        }
        if matched {
            starts.push(Matched {
                site: start,
                free_bindings: bindings.free_bindings(),
            });
        }
    }
    Ok(starts)
}

/// Resolve a **contiguous** multi-statement needle whose item at `target_idx` is
/// a single-declarator var-decl, over **prebuilt** subject indices (the caller
/// reuses cached chunk body indices instead of rebuilding them per selector). For
/// each body window of length `needle_idx.len()`, thread one `Bindings` through
/// the items in source order: every non-target item matches as a whole statement,
/// and at `target_idx` the match branches over the window item's declarators (each
/// matched via the single needle declarator, so a target inside a multi-declarator
/// source_unit is found). Returns `(target_body_idx, subject_declarator_idx)` for every
/// full-window match — the caller reads the binding from that declarator and
/// applies source-unit-level categoricity. This path is fixed-window (no module-level
/// `STMT_LIST` holes); a hole-bearing needle item fails closed via the per-item
/// unsupported scan.
pub fn match_single_declarator_target_windows_indexed(
    needle_idx: &[Index],
    subject_idx: &[Index],
    target_idx: usize,
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<Vec<Matched<(usize, usize)>>, Unsupported> {
    for needle in needle_idx {
        if let Some(reason) = unsupported_needle_construct(needle) {
            return Err(Unsupported { reason });
        }
    }
    let n = needle_idx.len();
    let mut matches = Vec::new();
    if n == 0 || n > subject_idx.len() {
        return Ok(matches);
    }
    for window_start in 0..=(subject_idx.len() - n) {
        let mut bindings = Bindings::new(free);
        let mut found = Vec::new();
        match_declarator_target_window(
            needle_idx,
            subject_idx,
            window_start,
            target_idx,
            0,
            mode,
            &mut bindings,
            None,
            &mut found,
        )?;
        matches.extend(found.into_iter().map(|found| Matched {
            site: (window_start + target_idx, found.site),
            free_bindings: found.free_bindings,
        }));
    }
    Ok(matches)
}

/// One window's recursive item-by-item match for
/// [`match_single_declarator_target_windows_indexed`], threading `Bindings` in
/// source order and branching at `target_idx` over the window item's declarators. The
/// chosen declarator index is carried to the window end (when it is recorded),
/// so a binding is reported only for a full-window match.
#[allow(clippy::too_many_arguments)]
fn match_declarator_target_window(
    needles: &[Index],
    subjects: &[Index],
    window_start: usize,
    target_idx: usize,
    item_idx: usize,
    mode: Mode,
    bindings: &mut Bindings,
    chosen_decl: Option<usize>,
    found: &mut Vec<Matched<usize>>,
) -> Result<(), Unsupported> {
    if item_idx == needles.len() {
        if let Some(decl) = chosen_decl {
            found.push(Matched {
                site: decl,
                free_bindings: bindings.free_bindings(),
            });
        }
        return Ok(());
    }
    let needle = &needles[item_idx];
    let subject = &subjects[window_start + item_idx];
    let snapshot = bindings.clone();
    if item_idx == target_idx {
        let (Some((nwrap, nvd)), Some((swrap, svd))) =
            (var_decl_node(needle), var_decl_node(subject))
        else {
            return Ok(());
        };
        if nwrap != swrap || needle.operator_of(nvd) != subject.operator_of(svd) {
            return Ok(());
        }
        let [needle_decl] = needle.children_of(nvd) else {
            return Ok(());
        };
        for (decl_idx, &subject_decl) in subject.children_of(svd).iter().enumerate() {
            *bindings = snapshot.clone();
            if homo(needle, *needle_decl, subject, subject_decl, mode, bindings)? {
                match_declarator_target_window(
                    needles,
                    subjects,
                    window_start,
                    target_idx,
                    item_idx + 1,
                    mode,
                    bindings,
                    Some(decl_idx),
                    found,
                )?;
            }
        }
        *bindings = snapshot;
    } else {
        let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first()) else {
            return Ok(());
        };
        if homo(needle, n_root, subject, s_root, mode, bindings)? {
            match_declarator_target_window(
                needles,
                subjects,
                window_start,
                target_idx,
                item_idx + 1,
                mode,
                bindings,
                chosen_decl,
                found,
            )?;
        }
        *bindings = snapshot;
    }
    Ok(())
}

/// Recursive ordered-subsequence placement of the needle's fixed segments into
/// the chunk body, enumerating every alignment with `Bindings` + alignment
/// snapshot/restore.
#[allow(clippy::too_many_arguments)]
fn place_top_level(
    needle_idx: &[Index],
    subject_idx: &[Index],
    segments: &[(usize, usize)],
    seg_idx: usize,
    cand_min: usize,
    mode: Mode,
    bindings: &mut Bindings,
    alignment: &mut [Option<usize>],
    matches: &mut Vec<Matched<Vec<Option<usize>>>>,
) -> Result<(), Unsupported> {
    let Some(&(needle_start, seg_len)) = segments.get(seg_idx) else {
        matches.push(Matched {
            site: alignment.to_vec(),
            free_bindings: bindings.free_bindings(),
        });
        return Ok(());
    };
    let remaining: usize = segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = subject_idx.len().checked_sub(remaining) else {
        return Ok(());
    };
    for start in cand_min..=latest_start {
        let bindings_snapshot = bindings.clone();
        let alignment_snapshot = alignment.to_vec();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            let needle = &needle_idx[needle_start + offset];
            let subject = &subject_idx[start + offset];
            let (Some(&n_root), Some(&s_root)) = (needle.roots.first(), subject.roots.first())
            else {
                segment_ok = false;
                break;
            };
            if !homo(needle, n_root, subject, s_root, mode, bindings)? {
                segment_ok = false;
                break;
            }
            alignment[needle_start + offset] = Some(start + offset);
        }
        if segment_ok {
            place_top_level(
                needle_idx,
                subject_idx,
                segments,
                seg_idx + 1,
                start + seg_len,
                mode,
                bindings,
                alignment,
                matches,
            )?;
        }
        *bindings = bindings_snapshot;
        alignment.copy_from_slice(&alignment_snapshot);
    }
    Ok(())
}
