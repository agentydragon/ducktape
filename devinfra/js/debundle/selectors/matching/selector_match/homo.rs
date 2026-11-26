//! The core structural homomorphism `homo` and its per-node helpers.

use std::collections::BTreeSet;

use chunk_facts::{NodeId, NodeKind};
use source_match_holes::is_hole_keyword;

use crate::bindings::{Bindings, FrameKind, alpha_frame};
use crate::holes::{
    is_floating_declarator, is_run_hole_carrier, is_single_node_hole, list_prefix_len,
};
use crate::index::Index;
use crate::list_align::match_list_with_holes;
use crate::predicates::{
    descendant_string_pattern, regex_predicate_pattern, subtree_contains_string,
};
use crate::{Mode, TraceCategory, Unsupported};

/// Match two arbitrary sub-nodes (`nid` in `needle`, `sid` in `subject`) with a
/// fresh [`Bindings`], the per-node match oracle the near-miss descent uses to
/// decide whether one aligned pair (a class member, a declarator) matches. This
/// is the *same* [`homo`] relation the whole-statement match runs, just rooted at
/// a sub-node instead of the top-level statement. Sound to call on a sub-node of a
/// needle that already passed [`matches_indexed`]/[`var_declarator_alignment_indexed`]:
/// the only [`Unsupported`] sources (a misplaced run-hole keyword, a malformed
/// predicate) are whole-needle properties already rejected up front, so a
/// sub-node descent of a probed needle never newly errors.
pub fn nodes_match(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<bool, Unsupported> {
    let mut bindings = Bindings::new(free);
    homo(needle, nid, subject, sid, mode, &mut bindings)
}

/// The normalized view of an object/destructure property with an identifier
/// value. It pins one stable key name plus one (possibly alpha-renamable) value
/// identifier. Shorthand and explicit forms can then match across node kinds.
struct ShorthandProperty<'a> {
    /// The property key — a stable source name, compared **exactly** even in alpha
    /// mode (a destructure shorthand key is a real property name, not a binding).
    key: &'a str,
    /// The value identifier: a reference for an object-literal property, the
    /// introduced binding for a destructure pattern property.
    value_ident: &'a str,
    /// True for a destructure pattern property (the value identifier is a
    /// **binding** → `match_binding`); false for an object-literal property (the
    /// value is a **reference** → `match_ref`).
    is_binding: bool,
    /// Default value of a destructured binding, if present. It must match
    /// structurally even when the local binding is alpha-renamed.
    default: Option<NodeId>,
}

/// View `node` as an identifier-valued property, for the shorthand⟷explicit
/// equivalence in [`homo`]. Covers the four fact node kinds the two surface
/// forms project to — object literal `Shorthand(k)` and
/// `KeyValue(PropName(k), Ident(k))`, destructure pattern `PatAssign(k)` and
/// `PatKeyValue(PropName(k), BindingIdent(k))`. Defaulted destructure bindings
/// also normalize across `{ k = value }` and `{ k: renamed = value }`.
///
/// A `KeyValue` whose value is not a bare identifier (e.g. `{ k: f() }`), or a
/// `PatKeyValue` whose value is not a bare binding or defaulted bare binding,
/// returns `None` and falls through to structural matching. Hole carriers (`ANYTHING`
/// shorthands) are excluded — they are consumed by list placement and must keep
/// their run-hole identity.
fn shorthand_property_view(index: &Index, node: NodeId) -> Option<ShorthandProperty<'_>> {
    let same_name_key_value = |is_binding: bool| {
        let [key, value] = index.children_of(node) else {
            return None;
        };
        let key = index.prop_name_of(*key)?;
        let (value, default) = if is_binding && index.kind_of(*value) == NodeKind::AssignPat {
            let [binding, default] = index.children_of(*value) else {
                return None;
            };
            if index.kind_of(*binding) != NodeKind::BindingIdent {
                return None;
            }
            (*binding, Some(*default))
        } else {
            (*value, None)
        };
        // A hole-keyword value (`{ k: ANYTHING }`) is not a same-name property: it
        // must keep its single-node-hole identity and fall through to structural
        // matching (where `homo` treats it as match-any), not be alpha-bound as a
        // real identifier. Mirrors the `Shorthand`/`PatAssign` hole exclusion.
        let value_ident = index
            .ident_of(value)
            .filter(|name| !is_hole_keyword(name))?;
        Some(ShorthandProperty {
            key,
            value_ident,
            is_binding,
            default,
        })
    };
    match index.kind_of(node) {
        NodeKind::Shorthand => {
            let name = index.ident_of(node).filter(|name| !is_hole_keyword(name))?;
            Some(ShorthandProperty {
                key: name,
                value_ident: name,
                is_binding: false,
                default: None,
            })
        }
        NodeKind::PatAssign if index.children_of(node).len() <= 1 => {
            let name = index.ident_of(node).filter(|name| !is_hole_keyword(name))?;
            Some(ShorthandProperty {
                key: name,
                value_ident: name,
                is_binding: true,
                default: index.children_of(node).first().copied(),
            })
        }
        NodeKind::KeyValue => same_name_key_value(false),
        NodeKind::PatKeyValue => same_name_key_value(true),
        _ => None,
    }
}

pub(crate) fn homo(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    bindings.step()?;
    let nkind = needle.kind_of(nid);

    // Single-node hole (expression / pattern / statement position): matches any
    // one subtree, before the kind check so a `STMT;`/`ANYTHING` needle can match
    // a different-kind subject. (Unhandled constructs — the regex predicate,
    // misplaced run-hole keywords — are rejected up front in [`matches`], before
    // structural matching can mask them, so they never reach here.)
    if is_single_node_hole(needle, nid) {
        return Ok(true);
    }

    // `STR_LITERAL_MATCHING_RE("re")` predicate: matches a string-literal subject
    // whose value matches `re` (an invalid pattern matches nothing), not by
    // structure. Checked before the kind comparison (the needle is a `Call`, the
    // subject a `StrLit`).
    if regex_predicate_pattern(needle, nid).is_some() {
        let matched = subject.kind_of(sid) == NodeKind::StrLit
            && match (needle.predicate_regex.get(&nid), subject.str_lit_of(sid)) {
                (Some(re), Some(value)) => re.is_match(value),
                _ => false,
            };
        if !matched {
            bindings.fail(nid, sid, TraceCategory::Literal, || {
                "string does not satisfy the selector regex".into()
            });
        }
        return Ok(matched);
    }

    if let Some(expected) = descendant_string_pattern(needle, nid) {
        let matched = subtree_contains_string(subject, sid, expected);
        if !matched {
            bindings.fail(nid, sid, TraceCategory::Literal, || {
                format!("expression does not contain string literal {expected:?}")
            });
        }
        return Ok(matched);
    }

    // Shorthand ⟷ explicit property equivalence (object literals and
    // destructuring patterns): `{ k }` matches `{ k: k }`, and a destructured
    // binding can alpha-rename through `{ k: renamed }`. The forms project to
    // *different* fact node kinds
    // (`Shorthand`/`KeyValue`, `PatAssign`/`PatKeyValue`), so this is matched
    // before the structural kind comparison would reject the cross-pair. The key
    // name is invariant (exact); the value identifier alpha-binds — as a reference
    // for object literals, as a binding for patterns.
    if let (Some(n_prop), Some(s_prop)) = (
        shorthand_property_view(needle, nid),
        shorthand_property_view(subject, sid),
    ) {
        if n_prop.key != s_prop.key {
            bindings.fail(nid, sid, TraceCategory::Literal, || {
                format!(
                    "property key {:?} differs from {:?}",
                    n_prop.key, s_prop.key
                )
            });
            return Ok(false);
        }
        if n_prop.default.is_some() != s_prop.default.is_some() {
            bindings.fail(nid, sid, TraceCategory::Shape, || {
                "destructure defaults differ".into()
            });
            return Ok(false);
        }
        let matched = if n_prop.is_binding {
            bindings.match_binding(n_prop.value_ident, s_prop.value_ident, mode)
        } else {
            bindings.match_ref(n_prop.value_ident, s_prop.value_ident, mode)
        };
        if !matched {
            bindings.fail(nid, sid, TraceCategory::Binding, || {
                format!(
                    "identifier {:?} cannot bind to {:?}",
                    n_prop.value_ident, s_prop.value_ident
                )
            });
        }
        if !matched {
            return Ok(false);
        }
        return match (n_prop.default, s_prop.default) {
            (Some(n_default), Some(s_default)) => {
                homo(needle, n_default, subject, s_default, mode, bindings)
            }
            _ => Ok(true),
        };
    }

    // Structural equality: kind, then non-identifier labels (always exact),
    // then the identifier label (exact or alpha-bound), then children.
    if nkind != subject.kind_of(sid) {
        bindings.fail(nid, sid, TraceCategory::Shape, || {
            format!("expected {nkind:?}, found {:?}", subject.kind_of(sid))
        });
        return Ok(false);
    }
    if needle.str_lit_of(nid) != subject.str_lit_of(sid)
        || needle.num_lit_of(nid) != subject.num_lit_of(sid)
        || needle.bool_lit_of(nid) != subject.bool_lit_of(sid)
        || needle.prop_name_of(nid) != subject.prop_name_of(sid)
        || needle.operator_of(nid) != subject.operator_of(sid)
        || needle.regex_of(nid) != subject.regex_of(sid)
    {
        bindings.fail(nid, sid, TraceCategory::Literal, || {
            if needle.str_lit_of(nid) != subject.str_lit_of(sid) {
                return format!(
                    "string literal {} differs from {}",
                    brief_label(needle.str_lit_of(nid)),
                    brief_label(subject.str_lit_of(sid))
                );
            }
            if needle.num_lit_of(nid) != subject.num_lit_of(sid) {
                return format!(
                    "number literal {} differs from {}",
                    brief_label(needle.num_lit_of(nid)),
                    brief_label(subject.num_lit_of(sid))
                );
            }
            if needle.prop_name_of(nid) != subject.prop_name_of(sid) {
                return format!(
                    "property {} differs from {}",
                    brief_label(needle.prop_name_of(nid)),
                    brief_label(subject.prop_name_of(sid))
                );
            }
            if needle.operator_of(nid) != subject.operator_of(sid) {
                return format!(
                    "operator {} differs from {}",
                    brief_label(needle.operator_of(nid)),
                    brief_label(subject.operator_of(sid))
                );
            }
            "an invariant literal differs".into()
        });
        return Ok(false);
    }
    match (needle.ident_of(nid), subject.ident_of(sid)) {
        (Some(n), Some(s)) => {
            // A binding identifier (declaration) shadows within its frame; a
            // reference resolves against the visible scope stack. Both honor a
            // prebound `target_binding` mapping first (in either mode), then fall
            // back to alpha-binding or exact spelling per `mode`.
            let consistent = if nkind == NodeKind::BindingIdent {
                bindings.match_binding(n, s, mode)
            } else {
                bindings.match_ref(n, s, mode)
            };
            if !consistent {
                bindings.fail(nid, sid, TraceCategory::Binding, || {
                    match bindings.expected_ident(n) {
                        Some(expected) => {
                            format!(
                                "identifier {n:?} previously bound to {expected:?}, found {s:?}"
                            )
                        }
                        None => format!("identifier {n:?} cannot bind to {s:?}"),
                    }
                });
                return Ok(false);
            }
        }
        (None, None) => {}
        _ => {
            bindings.fail(nid, sid, TraceCategory::Shape, || {
                "identifier presence differs".into()
            });
            return Ok(false);
        }
    }

    // A `Class` node's superclass (`extends`) is a separate relation, not a child
    // member, so compare it explicitly before the body — both present (and
    // matched) or both absent. `Class` introduces no alpha frame, so the
    // superclass reference resolves against the enclosing scope.
    if nkind == NodeKind::Class {
        match (needle.super_class_of(nid), subject.super_class_of(sid)) {
            (Some(n_super), Some(s_super)) => {
                if !homo(needle, n_super, subject, s_super, mode, bindings)? {
                    return Ok(false);
                }
            }
            (None, None) => {}
            _ => {
                bindings.fail(nid, sid, TraceCategory::Shape, || {
                    "class superclass presence differs".into()
                });
                return Ok(false);
            }
        }
    }

    if mode != Mode::AlphaAll {
        return match_children(needle, nid, nkind, subject, sid, mode, bindings);
    }
    // `var` declarators bind into the enclosing function frame; a nested
    // function or class expression starts over.
    let outer_in_var_decl = bindings.in_var_decl;
    if nkind == NodeKind::VarDecl {
        bindings.in_var_decl = needle.operator_of(nid) == Some("var");
    }
    let result = match alpha_frame(nkind) {
        Some(frame) => {
            if frame == FrameKind::Function
                || matches!(nkind, NodeKind::FnExpr | NodeKind::ClassExpr)
            {
                bindings.in_var_decl = false;
            }
            bindings.push_scope(frame);
            let result = if matches!(nkind, NodeKind::FnExpr | NodeKind::ClassExpr) {
                match_named_expression(needle, nid, subject, sid, mode, bindings)
            } else {
                match_children(needle, nid, nkind, subject, sid, mode, bindings)
            };
            bindings.pop_scope();
            result
        }
        None => match_children(needle, nid, nkind, subject, sid, mode, bindings),
    };
    bindings.in_var_decl = outer_in_var_decl;
    result
}

fn brief_label(label: Option<&str>) -> String {
    match label {
        Some(value) => {
            let prefix: String = value.chars().take(80).collect();
            if value.chars().count() > 80 {
                format!("{prefix:?}…")
            } else {
                format!("{prefix:?}")
            }
        }
        None => "<absent>".into(),
    }
}

/// A named function/class expression's name is bound in its own frame, visible
/// only inside the expression: `const f = function g() { g(); }` does not see an
/// outer `g`. Children are `[name?, function-or-class]`.
fn match_named_expression(
    needle: &Index,
    nid: NodeId,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let (nkids, skids) = (needle.children_of(nid), subject.children_of(sid));
    if nkids.len() != skids.len() {
        bindings.fail(nid, sid, TraceCategory::Shape, || {
            format!("expected {} children, found {}", nkids.len(), skids.len())
        });
        return Ok(false);
    }
    let (Some((&nbody, nname)), Some((&sbody, sname))) = (nkids.split_last(), skids.split_last())
    else {
        return Ok(true);
    };
    if let (Some(&nname), Some(&sname)) = (nname.first(), sname.first()) {
        match (needle.ident_of(nname), subject.ident_of(sname)) {
            (Some(n), Some(s)) if !is_single_node_hole(needle, nname) => {
                if !bindings.match_binding(n, s, mode) {
                    bindings.fail(nname, sname, TraceCategory::Binding, || {
                        format!("identifier {n:?} cannot bind to {s:?}")
                    });
                    return Ok(false);
                }
            }
            _ => {
                if !homo(needle, nname, subject, sname, mode, bindings)? {
                    return Ok(false);
                }
            }
        }
    }
    homo(needle, nbody, subject, sbody, mode, bindings)
}

/// Match the children of two same-kind nodes: a positional prefix (callee /
/// discriminant), then the tail list — positionally when the needle carries no
/// run hole, or as an ordered subsequence with gaps when it does.
fn match_children(
    needle: &Index,
    nid: NodeId,
    nkind: NodeKind,
    subject: &Index,
    sid: NodeId,
    mode: Mode,
    bindings: &mut Bindings,
) -> Result<bool, Unsupported> {
    let nchildren = needle.children_of(nid);
    let schildren = subject.children_of(sid);
    let prefix = list_prefix_len(nkind);
    let nprefix = prefix.min(nchildren.len());
    let sprefix = prefix.min(schildren.len());
    if nprefix != sprefix {
        bindings.fail(nid, sid, TraceCategory::Shape, || {
            "required child prefix differs".into()
        });
        return Ok(false);
    }
    for i in 0..nprefix {
        if !homo(needle, nchildren[i], subject, schildren[i], mode, bindings)? {
            return Ok(false);
        }
    }
    let nlist = &nchildren[nprefix..];
    let slist = &schildren[sprefix..];
    if nlist
        .iter()
        .any(|&c| is_run_hole_carrier(needle, nkind, c) || is_floating_declarator(needle, nkind, c))
    {
        return match_list_with_holes(needle, nlist, subject, slist, nkind, mode, bindings);
    }
    if nlist.len() != slist.len() {
        bindings.fail(nid, sid, TraceCategory::Shape, || {
            format!("expected {} children, found {}", nlist.len(), slist.len())
        });
        return Ok(false);
    }
    for (nc, sc) in nlist.iter().zip(slist) {
        if !homo(needle, *nc, subject, *sc, mode, bindings)? {
            return Ok(false);
        }
    }
    Ok(true)
}
