//! Single-node and run-hole carrier recognition over facts.

use chunk_facts::{NodeId, NodeKind};
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, ARRAY_ELEMENTS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, SEQ_EXPRS_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD, expr_hole_name,
    hole_name_for, stmt_hole_name,
};

use crate::index::Index;

/// True iff `node` is an `ANYTHING;` class-rest member hole under a `Class`
/// parent (a class field whose key is the exact keyword and which has no
/// initializer). The class-member near-miss scan skips these.
pub fn is_class_rest_member(index: &Index, node: NodeId) -> bool {
    is_run_hole_carrier(index, NodeKind::Class, node)
}

/// True iff `node` is a `DECLARATORS` / `ANYTHING` declarator run-hole carrier
/// under a `VarDecl` parent (a declarator whose name binding is the keyword). The
/// pinned-declarator near-miss alignment skips these.
pub fn is_declarator_run_hole(index: &Index, node: NodeId) -> bool {
    is_run_hole_carrier(index, NodeKind::VarDecl, node)
}

/// `EXPR[_label]` or `ANYTHING[_label]`: a single-node hole in **expression**
/// position (an `Ident`). Labels are cosmetic and never bind.
fn is_expr_single_hole(name: &str) -> bool {
    expr_hole_name(name).is_some()
}

/// `STMT[_label]` or `ANYTHING[_label]`: a single-node hole in **statement**
/// position. Labels are cosmetic and never bind.
fn is_stmt_single_hole(name: &str) -> bool {
    stmt_hole_name(name).is_some()
}

/// A single-node hole borne by `node`, parse-position polymorphic: an expression
/// `Ident` (`EXPR`/`ANYTHING`), a binding `BindingIdent` pattern (`ANYTHING` only,
/// mirroring `is_anything_pat_hole`), or an expression-statement `ExprStmt`
/// (`STMT`/`ANYTHING`, matching any statement kind). Suffixed labels are
/// readability-only and do not bind.
pub(crate) fn is_single_node_hole(index: &Index, node: NodeId) -> bool {
    match index.kind_of(node) {
        NodeKind::Ident => index.ident_of(node).is_some_and(is_expr_single_hole),
        NodeKind::BindingIdent => index
            .ident_of(node)
            .is_some_and(|n| hole_name_for(n, ANYTHING_HOLE_KEYWORD).is_some()),
        NodeKind::ExprStmt => {
            let kids = index.children_of(node);
            kids.len() == 1
                && index.kind_of(kids[0]) == NodeKind::Ident
                && index.ident_of(kids[0]).is_some_and(is_stmt_single_hole)
        }
        _ => false,
    }
}

fn node_ident_hole(index: &Index, node: NodeId, keyword: &str) -> bool {
    index
        .ident_of(node)
        .is_some_and(|name| hole_name_for(name, keyword).is_some())
}

/// True iff `child` is a variable-length run-hole carrier in a list under a
/// parent of `parent_kind` — the per-list-type hole-carrier rules over facts. A
/// carrier is consumed by list placement and never visited by [`homo`].
pub(crate) fn is_run_hole_carrier(index: &Index, parent_kind: NodeKind, child: NodeId) -> bool {
    let ck = index.kind_of(child);
    match parent_kind {
        // `STMT_LIST;` — an expression statement whose sole child is the keyword.
        // A `SwitchCase`'s body is a statement list too; its leading `case` test
        // (when present) is a non-carrier, so it falls out as an anchored-left
        // fixed segment under the same placement.
        NodeKind::Block | NodeKind::SwitchCase => {
            ck == NodeKind::ExprStmt && {
                let kids = index.children_of(child);
                kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
            }
        }
        // `ARGS` — a bare identifier argument (the callee is split off before
        // this list, so any `ARGS` here is in argument position).
        NodeKind::Call | NodeKind::New | NodeKind::OptCall => {
            ck == NodeKind::Ident && node_ident_hole(index, child, ARGS_HOLE_KEYWORD)
        }
        // `ANYTHING` — a shorthand object-literal property.
        NodeKind::Object => {
            ck == NodeKind::Shorthand && node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD)
        }
        // `ANYTHING` in a destructuring pattern — a shorthand (no default)
        // destructure property.
        NodeKind::ObjectPat => {
            ck == NodeKind::PatAssign
                && index.children_of(child).is_empty()
                && node_ident_hole(index, child, ANYTHING_HOLE_KEYWORD)
        }
        // `ARRAY_ELEMENTS` — a bare identifier array element (the keyword in
        // element position). Spread / elision elements project to other node kinds
        // (`Spread` / `Elision`), so a carrier is unambiguously the keyword
        // identifier. No `ANYTHING` form: `ANYTHING` in element position is a
        // single-element `EXPR`, not a run.
        NodeKind::Array => {
            ck == NodeKind::Ident && node_ident_hole(index, child, ARRAY_ELEMENTS_HOLE_KEYWORD)
        }
        // `SEQ_EXPRS` — a bare identifier element of a comma-sequence
        // expression. SWC parses `a, b, c` as one `Seq` of three elements and
        // never builds a `Seq` with fewer than two, so the keyword in element
        // position is unambiguously the run hole; a needle whose only sequence
        // element is the hole pins nothing (it matches any sequence). No
        // `ANYTHING` form: `ANYTHING` in a sequence element is one `EXPR`.
        NodeKind::Seq => {
            ck == NodeKind::Ident && node_ident_hole(index, child, SEQ_EXPRS_HOLE_KEYWORD)
        }
        // `ANYTHING;` — a class field, no initializer, whose key is the keyword.
        NodeKind::Class => {
            ck == NodeKind::ClassProp && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index
                        .prop_name_of(kids[0])
                        .is_some_and(|name| hole_name_for(name, ANYTHING_HOLE_KEYWORD).is_some())
            }
        }
        // `case CASE_REST[_label]:` — a switch clause, no body, whose sole child
        // is the keyword test. The optional label is cosmetic.
        NodeKind::Switch => {
            ck == NodeKind::SwitchCase && {
                let kids = index.children_of(child);
                kids.len() == 1
                    && index
                        .ident_of(kids[0])
                        .is_some_and(|name| hole_name_for(name, CASE_REST_HOLE_KEYWORD).is_some())
            }
        }
        // `const DECLARATORS` / `ANYTHING` — a declarator whose name binding is
        // the keyword. An `ANYTHING` declarator is a run only when its
        // initializer carries nothing (absent, or itself a hole, as in
        // `const ANYTHING = ANYTHING`); otherwise it floats (see
        // [`is_floating_declarator`]).
        NodeKind::VarDecl => {
            ck == NodeKind::VarDeclarator && {
                let kids = index.children_of(child);
                !kids.is_empty()
                    && index.kind_of(kids[0]) == NodeKind::BindingIdent
                    && (node_ident_hole(index, kids[0], DECLARATORS_HOLE_KEYWORD)
                        || (node_ident_hole(index, kids[0], ANYTHING_HOLE_KEYWORD)
                            && !has_informative_init(index, child)))
            }
        }
        _ => false,
    }
}

/// A declarator whose initializer is more than a placeholder: present and not
/// itself a hole.
fn has_informative_init(index: &Index, declarator: NodeId) -> bool {
    index
        .children_of(declarator)
        .get(1)
        .is_some_and(|&init| !is_single_node_hole(index, init))
}

/// `ANYTHING = <init>` with an informative initializer: one declarator of any
/// name whose initializer must match, with any declarators around it. It is
/// placed as its own segment that never anchors either end of the list, so
/// `const ANYTHING = x.y;` finds `x.y` inside `const a = 1, b = x.y, c = 2;`.
pub(crate) fn is_floating_declarator(index: &Index, parent_kind: NodeKind, child: NodeId) -> bool {
    parent_kind == NodeKind::VarDecl
        && index.kind_of(child) == NodeKind::VarDeclarator
        && index.children_of(child).first().is_some_and(|&name| {
            index.kind_of(name) == NodeKind::BindingIdent
                && node_ident_hole(index, name, ANYTHING_HOLE_KEYWORD)
        })
        && has_informative_init(index, child)
}

/// Number of leading children that are *not* part of a run-hole-bearing list and
/// so are matched positionally: the callee of a call/new, the discriminant of a
/// switch. Every other node's children form one matchable list.
pub(crate) fn list_prefix_len(parent_kind: NodeKind) -> usize {
    matches!(
        parent_kind,
        NodeKind::Call | NodeKind::New | NodeKind::OptCall | NodeKind::Switch
    ) as usize
}

/// True iff a needle root (one statement's facts) is a module-level `STMT_LIST`
/// hole — an expression statement whose sole child is the keyword. These
/// separate the needle's fixed segments at the top level (mirrors
/// `holes.rs::module_item_list_hole_name`).
pub(crate) fn is_module_stmt_list_hole(index: &Index) -> bool {
    let Some(&root) = index.roots.first() else {
        return false;
    };
    index.kind_of(root) == NodeKind::ExprStmt && {
        let kids = index.children_of(root);
        kids.len() == 1 && node_ident_hole(index, kids[0], STMT_LIST_HOLE_KEYWORD)
    }
}
