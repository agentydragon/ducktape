//! Fact-based near-miss diagnostics for `source_match_body_debt`.
//!
//! This reproduces the `hints.rs` `first_mismatch_reason` reason family — the
//! scored "first structural divergence" between a non-matching candidate and the
//! selector needle — **over the fact model** (`selector_match::Index` /
//! `chunk_facts`), not by re-walking ASTs with `AstWildcardMatcher`. It is the F5
//! residual: the last `body_debt` surface still served by the AST matcher.
//!
//! The divergence walk mirrors `selector_match`'s own `homo` / `match_children` /
//! `align_var_declarators` descent — the same order in which the fact matcher's
//! recursive match short-circuits to `Ok(false)`: top-level item kind, then
//! module-decl kind / shape, statement kind / shape, declaration kind / shape,
//! class/function name, var-decl keyword, declarator alignment (count /
//! pinned-order / hole placement), and the class member-in-order scan. Each early
//! divergence is one reason variant with its fixed score. The fact matcher
//! discards the divergence location by collapsing to `Result<bool>`; this is that
//! same descent instrumented to return *where and why* the first `false` fired,
//! reading exactly the EDB relations `homo` reads (node kind, ordered children,
//! ident/prop/operator labels).
//!
//! Node *kinds* and the var-decl keyword are read straight from facts and mapped
//! to the same label vocabulary `hints.rs` uses, so the reason strings are
//! byte-identical to the AST path. Declarator labels and the candidate's declared
//! bindings are rendered by the existing pure-AST helpers (`render_var_declarator_label`,
//! `declared_bindings`) — those are not `AstWildcardMatcher` (they walk the
//! candidate/needle item, which the fact model also carries node-for-node);
//! reusing them keeps the rendered leaves identical. The fact path is proven
//! byte-identical to the AST `first_mismatch_reason` path by
//! `fact_near_miss_byte_identical_per_variant` (one input per reachable variant)
//! and `fact_near_miss_declarator_hole_fallback_is_byte_identical`.

use super::*;
use chunk_facts::NodeId;
use selector_match::{Index, Mode};

/// Identifier-matching mode for a selector, as the fact matcher expects it.
fn fact_mode(selector: &AnonymousStatementSelector) -> Mode {
    match selector.identifiers {
        SourceMatchIdentifierMode::Exact => Mode::Exact,
        SourceMatchIdentifierMode::AlphaAll => Mode::AlphaAll,
    }
}

/// Per-statement facts for one top-level item — the same projection the
/// production fact resolver (`datalog_resolver::item_facts`) builds. `None` for a
/// non-extractable item (which has no root and so matches nothing).
fn item_index(item: &ModuleItem) -> Option<Index> {
    chunk_facts::extract_facts_items(std::slice::from_ref(item))
        .ok()
        .map(|facts| Index::build(&facts))
}

/// The fact-model twin of [`source_match_body_debt`]'s near-miss rows: the scored
/// "first structural divergence" for every non-matching top-level candidate
/// scoring `>= min_score`, computed by [`fact_first_mismatch_reason`] over the
/// fact matcher's descent instead of `AstWildcardMatcher`'s. `exact_groups` is
/// the unchanged exact-matcher alignment (already fact-derivable, status (a) in
/// the F5 feasibility note); only the near-miss reasons move to facts here. Rows
/// are sorted and truncated identically to [`source_match_body_debt`].
pub fn fact_source_match_body_debt(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    min_score: usize,
    limit: usize,
) -> Result<SourceMatchBodyDebt> {
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "source_match",
        format!("<source_match debt in {request_id}>"),
        &selector.match_source,
        "source_match",
    )?;
    let exact_groups = find_matching_body_group_alignments(runtime_module, &parsed.body, selector);
    let [needle] = parsed.body.as_slice() else {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    };
    if module_item_list_hole_name(needle).is_some() {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    }
    let exact_body_indices = exact_groups
        .iter()
        .flat_map(|group| group.iter().flatten().copied())
        .collect::<BTreeSet<_>>();
    let Some(needle_index) = item_index(needle) else {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    };
    let mode = fact_mode(selector);
    let mut near_misses = Vec::new();
    for (body_idx, candidate) in runtime_module.body.iter().enumerate() {
        if exact_body_indices.contains(&body_idx) {
            continue;
        }
        let Some(reason) = fact_first_mismatch_reason(needle, &needle_index, candidate, mode)?
        else {
            continue;
        };
        if reason.score < min_score {
            continue;
        }
        let declared_bindings = declared_bindings(candidate)
            .into_iter()
            .map(|binding| binding.binding_name)
            .collect::<Vec<_>>();
        near_misses.push(SourceMatchNearMiss {
            body_idx,
            declared_bindings,
            score: reason.score,
            reason: reason.reason,
        });
    }
    near_misses.sort_by(|left, right| {
        right
            .score
            .cmp(&left.score)
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });
    if limit > 0 {
        near_misses.truncate(limit);
    }
    Ok(SourceMatchBodyDebt {
        exact_groups,
        near_misses,
    })
}

/// Fact-model twin of [`first_mismatch_reason`]: confirm non-match with the fact
/// matcher, then dispatch by the top-level item's fact kind, mirroring the AST
/// dispatch in `hints.rs`. `candidate` (and `needle`) are the AST items only so
/// the leaf renderers (declarator labels) can run; every divergence *decision*
/// reads the fact indices. `Ok(None)` when the candidate matches (no near-miss
/// row) or projects to no facts.
pub(crate) fn fact_first_mismatch_reason(
    needle: &ModuleItem,
    needle_index: &Index,
    candidate: &ModuleItem,
    mode: Mode,
) -> Result<Option<MismatchReason>> {
    let Some(candidate_index) = item_index(candidate) else {
        return Ok(None);
    };
    let (Some(nroot), Some(croot)) = (needle_index.root(), candidate_index.root()) else {
        return Ok(None);
    };
    // The fact matcher is the non-match oracle (it agrees with AstWildcardMatcher
    // on the supported subset by the corpus differential); a match means no row.
    if selector_match::matches_indexed(needle_index, &candidate_index, mode)
        .map_err(|unsupported| anyhow::anyhow!("fact near-miss: {}", unsupported.reason))?
    {
        return Ok(None);
    }
    let nkind = needle_index.kind(nroot);
    let ckind = candidate_index.kind(croot);
    let reason = match (is_module_decl_kind(nkind), is_module_decl_kind(ckind)) {
        (false, false) => first_stmt_divergence(
            needle,
            needle_index,
            nroot,
            candidate,
            &candidate_index,
            croot,
            mode,
        )?,
        (true, true) => first_module_decl_divergence(
            needle,
            needle_index,
            nroot,
            candidate,
            &candidate_index,
            croot,
            mode,
        )?,
        _ => MismatchReason {
            score: 1,
            reason: format!(
                "top-level item kind differs: selector is {}, candidate is {}",
                module_item_kind_label(nkind),
                module_item_kind_label(ckind),
            ),
        },
    };
    Ok(Some(reason))
}

/// The module-declaration fact root kinds (the extractor's tags for the
/// `ModuleDecl` variants). Everything else the extractor projects at top level is
/// a statement, so this is the `(Stmt, ModuleDecl)` split `module_item_kind`
/// reports.
fn is_module_decl_kind(kind: &str) -> bool {
    matches!(
        kind,
        "Import"
            | "ExportDecl"
            | "ExportNamed"
            | "ExportDefault"
            | "ExportDefaultDecl"
            | "ExportAll"
    )
}

/// Label matching `hints.rs::module_item_kind`.
fn module_item_kind_label(kind: &str) -> &'static str {
    if is_module_decl_kind(kind) {
        "module declaration"
    } else {
        "statement"
    }
}

/// Label matching `hints.rs::module_decl_kind`. The fact extractor only projects
/// the JS (non-TS) module declarations; a TS-only variant would fail extraction
/// upstream, so it cannot reach here.
fn module_decl_kind_label(kind: &str) -> &'static str {
    match kind {
        "Import" => "import",
        "ExportDecl" => "export declaration",
        "ExportNamed" => "named export",
        "ExportDefaultDecl" => "default declaration export",
        "ExportDefault" => "default expression export",
        "ExportAll" => "export all",
        other => panic!("fact near-miss: non-module-declaration kind {other}"),
    }
}

/// Label matching `hints.rs::stmt_kind`. The extractor projects a declaration
/// statement as its inner declaration node directly, so `ClassDecl`/`FnDecl`/
/// `VarDecl` map to `"declaration"` (the AST `Stmt::Decl` label), and every other
/// statement node (`ExprStmt` and the control-flow kinds) maps as in `stmt_kind`.
fn stmt_kind_label(kind: &str) -> &'static str {
    match kind {
        "Block" => "block",
        "Empty" => "empty",
        "Debugger" => "debugger",
        "Return" => "return",
        "Labeled" => "labeled",
        "Break" => "break",
        "Continue" => "continue",
        "If" => "if",
        "Switch" => "switch",
        "Throw" => "throw",
        "Try" => "try",
        "While" => "while",
        "DoWhile" => "do while",
        "For" => "for",
        "ForIn" => "for in",
        "ForOf" => "for of",
        _ if is_decl_kind(kind) => "declaration",
        // `ExprStmt` and any other statement node.
        _ => "expression",
    }
}

/// Label matching `hints.rs::decl_kind`.
fn decl_kind_label(kind: &str) -> &'static str {
    match kind {
        "ClassDecl" => "class",
        "FnDecl" => "function",
        "VarDecl" => "variable",
        other => panic!("fact near-miss: non-declaration kind {other}"),
    }
}

/// The fact root kinds the extractor uses for a `Stmt::Decl` (the kinds
/// `first_decl_divergence` dispatches on). At top level the extractor projects a
/// declaration statement as its inner declaration node directly.
fn is_decl_kind(kind: &str) -> bool {
    matches!(kind, "ClassDecl" | "FnDecl" | "VarDecl")
}

fn first_module_decl_divergence(
    needle: &ModuleItem,
    needle_index: &Index,
    nroot: NodeId,
    candidate: &ModuleItem,
    candidate_index: &Index,
    croot: NodeId,
    mode: Mode,
) -> Result<MismatchReason> {
    let nkind = needle_index.kind(nroot);
    let ckind = candidate_index.kind(croot);
    if nkind == "ExportDecl" && ckind == "ExportDecl" {
        // Both export declarations: descend into the inner declaration, mirroring
        // `first_module_decl_mismatch_reason`'s ExportDecl arm. The inner decl is
        // ExportDecl child ordinal 0.
        let ndecl = needle_index.children(nroot)[0];
        let cdecl = candidate_index.children(croot)[0];
        return first_decl_divergence(
            export_decl_inner(needle),
            needle_index,
            ndecl,
            export_decl_inner(candidate),
            candidate_index,
            cdecl,
            mode,
        );
    }
    Ok(if nkind != ckind {
        MismatchReason {
            score: 10,
            reason: format!(
                "module declaration kind differs: selector is {}, candidate is {}",
                module_decl_kind_label(nkind),
                module_decl_kind_label(ckind),
            ),
        }
    } else {
        MismatchReason {
            score: 20,
            reason: "module declaration shape differs".to_string(),
        }
    })
}

fn first_stmt_divergence(
    needle: &ModuleItem,
    needle_index: &Index,
    nroot: NodeId,
    candidate: &ModuleItem,
    candidate_index: &Index,
    croot: NodeId,
    mode: Mode,
) -> Result<MismatchReason> {
    let nkind = needle_index.kind(nroot);
    let ckind = candidate_index.kind(croot);
    if is_decl_kind(nkind) && is_decl_kind(ckind) {
        return first_decl_divergence(
            stmt_decl_inner(needle),
            needle_index,
            nroot,
            stmt_decl_inner(candidate),
            candidate_index,
            croot,
            mode,
        );
    }
    Ok(if nkind != ckind {
        MismatchReason {
            score: 10,
            reason: format!(
                "statement kind differs: selector is {}, candidate is {}",
                stmt_kind_label(nkind),
                stmt_kind_label(ckind),
            ),
        }
    } else {
        MismatchReason {
            score: 20,
            reason: "statement shape differs".to_string(),
        }
    })
}

/// Mirror of `first_decl_mismatch_reason`. `nroot`/`croot` are the declaration
/// fact nodes (`ClassDecl`/`FnDecl`/`VarDecl`); `needle_decl`/`candidate_decl` are
/// the matching AST `Decl`s (only the var path's declarator-label leaf reads
/// them).
fn first_decl_divergence(
    needle_decl: &Decl,
    needle_index: &Index,
    nroot: NodeId,
    candidate_decl: &Decl,
    candidate_index: &Index,
    croot: NodeId,
    mode: Mode,
) -> Result<MismatchReason> {
    let nkind = needle_index.kind(nroot);
    let ckind = candidate_index.kind(croot);
    let alpha = mode == Mode::AlphaAll;
    Ok(match (nkind, ckind) {
        ("ClassDecl", "ClassDecl") => {
            // ClassDecl children: [Ident(name), Class]. The name (exact, not
            // alpha-renamable in exact mode) gates first.
            let nname = needle_index.ident(needle_index.children(nroot)[0]);
            let cname = candidate_index.ident(candidate_index.children(croot)[0]);
            if !alpha && nname != cname {
                MismatchReason {
                    score: 40,
                    reason: format!(
                        "class name differs: selector `{}`, candidate `{}`",
                        nname.unwrap_or_default(),
                        cname.unwrap_or_default(),
                    ),
                }
            } else {
                let nclass = needle_index.children(nroot)[1];
                let cclass = candidate_index.children(croot)[1];
                first_class_divergence(needle_index, nclass, candidate_index, cclass, mode)?
            }
        }
        ("FnDecl", "FnDecl") => {
            let nname = needle_index.ident(needle_index.children(nroot)[0]);
            let cname = candidate_index.ident(candidate_index.children(croot)[0]);
            if !alpha && nname != cname {
                MismatchReason {
                    score: 40,
                    reason: format!(
                        "function name differs: selector `{}`, candidate `{}`",
                        nname.unwrap_or_default(),
                        cname.unwrap_or_default(),
                    ),
                }
            } else {
                MismatchReason {
                    score: 35,
                    reason: "function signature or body differs".to_string(),
                }
            }
        }
        ("VarDecl", "VarDecl") => first_var_decl_divergence(
            decl_var(needle_decl),
            needle_index,
            nroot,
            decl_var(candidate_decl),
            candidate_index,
            croot,
            mode,
        )?,
        _ if nkind != ckind => MismatchReason {
            score: 30,
            reason: format!(
                "declaration kind differs: selector is {}, candidate is {}",
                decl_kind_label(nkind),
                decl_kind_label(ckind),
            ),
        },
        _ => MismatchReason {
            score: 35,
            reason: "declaration shape differs".to_string(),
        },
    })
}

/// Mirror of `first_var_decl_mismatch_reason`. The var-decl keyword (`var`/`let`/
/// `const`) is the `operator` fact on the `VarDecl` node; the declarator
/// alignment is the fact matcher's own [`selector_match::var_declarator_alignment_indexed`].
fn first_var_decl_divergence(
    needle_var: &VarDecl,
    needle_index: &Index,
    nroot: NodeId,
    candidate_var: &VarDecl,
    candidate_index: &Index,
    croot: NodeId,
    mode: Mode,
) -> Result<MismatchReason> {
    let nkeyword = needle_index.operator(nroot);
    let ckeyword = candidate_index.operator(croot);
    if nkeyword != ckeyword {
        return Ok(MismatchReason {
            score: 45,
            reason: format!(
                "variable declaration kind differs: selector is {}, candidate is {}",
                nkeyword.unwrap_or_default(),
                ckeyword.unwrap_or_default(),
            ),
        });
    }
    // Declarator alignment over facts (wrapper symmetry + keyword already hold at
    // this point, so a `None` is purely a declarator-list mismatch — exactly
    // `match_var_declarator_slice_with_alignment().is_none()` on the AST side).
    let aligns =
        selector_match::var_declarator_alignment_indexed(needle_index, candidate_index, mode, None)
            .map_err(|unsupported| anyhow::anyhow!("fact near-miss: {}", unsupported.reason))?
            .is_some();
    if aligns {
        return Ok(MismatchReason {
            score: 35,
            reason: "variable declaration shape differs".to_string(),
        });
    }
    let ndecls = needle_index.children(nroot);
    let cdecls = candidate_index.children(croot);
    let reason = if ndecls
        .iter()
        .any(|&d| selector_match::is_declarator_run_hole(needle_index, d))
    {
        first_pinned_var_declarator_divergence(
            needle_var,
            needle_index,
            ndecls,
            candidate_var,
            candidate_index,
            cdecls,
            mode,
        )?
    } else {
        format!(
            "variable declarators differ: selector has {} declarator(s), candidate has {}",
            ndecls.len(),
            cdecls.len(),
        )
    };
    Ok(MismatchReason { score: 55, reason })
}

/// Mirror of `first_pinned_var_declarator_mismatch_reason`: which pin failed to
/// place, or (if all pins placed) which hole-placement gap is unmatched. The
/// alignment decision is fact-derived (the threaded-`Bindings` greedy scan
/// [`selector_match::pinned_declarator_matches_in_order`]); declarator *labels*
/// reuse the pure-AST `render_var_declarator_label` over the candidate/needle
/// `VarDeclarator`s (the fact model carries the same data — see module docs). The
/// needle's AST declarators line up 1:1 with the fact declarator children, both
/// in source order, so a fact `(needle_idx, _)` indexes `needle_var.decls`.
fn first_pinned_var_declarator_divergence(
    needle_var: &VarDecl,
    needle_index: &Index,
    ndecls: &[NodeId],
    candidate_var: &VarDecl,
    candidate_index: &Index,
    cdecls: &[NodeId],
    mode: Mode,
) -> Result<String> {
    let matches = selector_match::pinned_declarator_matches_in_order(
        needle_index,
        ndecls,
        candidate_index,
        cdecls,
        mode,
    )
    .map_err(|unsupported| anyhow::anyhow!("fact near-miss: {}", unsupported.reason))?;
    let pinned_indices = needle_var
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            declarator_list_hole_name(declarator)
                .is_none()
                .then_some(idx)
        })
        .collect::<Vec<_>>();
    if let Some(&needle_idx) = pinned_indices.get(matches.len()) {
        let candidate_start = matches
            .last()
            .map(|(_, candidate_idx)| candidate_idx + 1)
            .unwrap_or(0);
        let remaining = candidate_var
            .decls
            .iter()
            .skip(candidate_start)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        let remaining_hint = if remaining.is_empty() {
            "no candidate declarators remain".to_string()
        } else {
            format!(
                "remaining candidate declarators: {}",
                render_var_declarator_labels(&remaining)
            )
        };
        return Ok(format!(
            "selector pinned declarator #{needle_idx} {} was not found in order ({remaining_hint})",
            render_var_declarator_label(&needle_var.decls[needle_idx]),
        ));
    }
    Ok(first_var_declarator_hole_placement_mismatch_reason(
        needle_var,
        candidate_var,
        &matches,
    ))
}

/// Mirror of `first_class_mismatch_reason`: the in-order class-member scan over
/// facts. For each pinned (non-rest, labeled) needle member, find the
/// same-labeled candidate member in order; if found but the member subtrees do
/// not match ([`selector_match::nodes_match`]) → variant 14; if the label is not
/// found in order → variant 15; otherwise → variant 16.
fn first_class_divergence(
    needle_index: &Index,
    nclass: NodeId,
    candidate_index: &Index,
    cclass: NodeId,
    mode: Mode,
) -> Result<MismatchReason> {
    let nmembers = needle_index.children(nclass);
    let cmembers = candidate_index.children(cclass);
    let mut candidate_start = 0;
    for &nmember in nmembers {
        if selector_match::is_class_rest_member(needle_index, nmember) {
            continue;
        }
        let Some(needle_label) = fact_class_member_label(needle_index, nmember) else {
            continue;
        };
        let mut found_label = false;
        for (candidate_idx, &cmember) in cmembers.iter().enumerate().skip(candidate_start) {
            if fact_class_member_label(candidate_index, cmember).as_deref()
                != Some(needle_label.as_str())
            {
                continue;
            }
            found_label = true;
            candidate_start = candidate_idx + 1;
            if !selector_match::nodes_match(needle_index, nmember, candidate_index, cmember, mode)
                .map_err(|unsupported| anyhow::anyhow!("fact near-miss: {}", unsupported.reason))?
            {
                return Ok(MismatchReason {
                    score: 65,
                    reason: format!(
                        "class member `{needle_label}` matched by name, but its signature or body differs"
                    ),
                });
            }
            break;
        }
        if !found_label {
            return Ok(MismatchReason {
                score: 70,
                reason: format!(
                    "selector class pinned member `{needle_label}` was not found in the candidate class body in order"
                ),
            });
        }
    }
    Ok(MismatchReason {
        score: 45,
        reason: "class heritage, decorators, or member order differs".to_string(),
    })
}

/// Fact-model twin of `hints.rs::class_member_label`. A `Class` member fact node
/// is a `Constructor`/`Method`/`ClassProp`/`StaticBlock` (the extractor's class
/// members); its label is the constructor/static-block tag, or the key's
/// `prop_name` (a computed key projects to a `ComputedKey` node with no
/// `prop_name`, labeled `<computed>` to match `prop_name_label`). The private and
/// auto-accessor members the AST helper also handles are not projected by the
/// extractor, so they never reach the near-miss path.
fn fact_class_member_label(index: &Index, member: NodeId) -> Option<String> {
    match index.kind(member) {
        "Constructor" => Some("constructor".to_string()),
        "StaticBlock" => Some("static block".to_string()),
        "Method" | "ClassProp" => {
            let &key = index.children(member).first()?;
            fact_prop_name_label(index, key)
        }
        _ => None,
    }
}

/// Fact-model twin of `hints.rs::prop_name_label` for a class-member key node: a
/// `PropName` node carries the (ident/string/number) name as a `prop_name` fact;
/// a `ComputedKey` node carries none and labels `<computed>`.
fn fact_prop_name_label(index: &Index, key: NodeId) -> Option<String> {
    match index.kind(key) {
        "PropName" => index.prop_name(key).map(str::to_string),
        "ComputedKey" => Some("<computed>".to_string()),
        _ => None,
    }
}

/// The candidate/needle AST `Decl::Var` reached on the `(VarDecl, VarDecl)` decl
/// arm — the matching item is known to be a var-decl statement or `export`
/// var-decl, so this navigation always succeeds.
fn decl_var(decl: &Decl) -> &VarDecl {
    match decl {
        Decl::Var(var) => var,
        other => panic!("fact near-miss: expected a variable declaration, got {other:?}"),
    }
}

/// The inner `Decl` of a `Stmt::Decl` item.
fn stmt_decl_inner(item: &ModuleItem) -> &Decl {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        other => panic!("fact near-miss: expected a declaration statement, got {other:?}"),
    }
}

/// The inner `Decl` of an `export <decl>` item.
fn export_decl_inner(item: &ModuleItem) -> &Decl {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => &export.decl,
        other => panic!("fact near-miss: expected an export declaration, got {other:?}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_one(source: &str) -> ModuleItem {
        let mut module = js_ast::parse_js_module_ast("<fact-near-miss test>", source).unwrap();
        assert_eq!(
            module.body.len(),
            1,
            "test inputs are one statement: {source:?}"
        );
        module.body.remove(0)
    }

    fn selector(alpha: bool) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: String::new(),
            identifiers: if alpha {
                SourceMatchIdentifierMode::AlphaAll
            } else {
                SourceMatchIdentifierMode::Exact
            },
            target_binding: None,
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    /// One byte-identity case: the selector needle, a non-matching candidate, the
    /// identifier mode, the variant's expected score, and a marker substring that
    /// confirms the input actually reaches the intended variant (so a mis-built
    /// input is caught rather than silently testing the wrong variant).
    struct Case {
        variant: &'static str,
        needle_src: &'static str,
        candidate_src: &'static str,
        alpha: bool,
        expected_score: usize,
        reason_marker: &'static str,
    }

    /// Drive the AST `first_mismatch_reason` (the `hints.rs` matcher path) and the
    /// fact `fact_first_mismatch_reason` over one case and assert they agree
    /// byte-for-byte, then assert the case truly hit its intended variant.
    fn assert_byte_identical(case: &Case) {
        let sel = selector(case.alpha);
        let needle = parse_one(case.needle_src);
        let candidate = parse_one(case.candidate_src);
        let wildcard_idents = wildcard_ident_names(&needle);
        let alpha = case.alpha;

        let ast_reason = SyntaxContext::within_ignored_ctxt(|| {
            first_mismatch_reason(&needle, &candidate, &sel, &wildcard_idents, alpha)
        })
        .unwrap_or_else(|| {
            panic!(
                "[{}] AST path produced no mismatch (candidate matched?): needle {:?} vs {:?}",
                case.variant, case.needle_src, case.candidate_src,
            )
        });

        let needle_index = item_index(&needle).expect("test needle projects to facts");
        let fact_reason =
            fact_first_mismatch_reason(&needle, &needle_index, &candidate, fact_mode(&sel))
                .expect("fact near-miss does not error on supported needle")
                .unwrap_or_else(|| {
                    panic!(
                        "[{}] fact path produced no mismatch (matched?): needle {:?} vs {:?}",
                        case.variant, case.needle_src, case.candidate_src,
                    )
                });

        assert_eq!(
            fact_reason, ast_reason,
            "[{}] fact-path reason must be byte-identical to the AST-path reason\n  \
             needle:    {:?}\n  candidate: {:?}",
            case.variant, case.needle_src, case.candidate_src,
        );
        // The input reaches the intended variant (score + a discriminating marker).
        assert_eq!(
            ast_reason.score, case.expected_score,
            "[{}] expected score {} for this variant, got reason {:?}",
            case.variant, case.expected_score, ast_reason.reason,
        );
        assert!(
            ast_reason.reason.contains(case.reason_marker),
            "[{}] reason {:?} did not contain expected marker {:?}",
            case.variant,
            ast_reason.reason,
            case.reason_marker,
        );
    }

    #[test]
    fn fact_near_miss_byte_identical_per_variant() {
        js_ast::with_swc_globals(|| {
            for case in CASES {
                assert_byte_identical(case);
            }
        });
    }

    /// Compute both paths' reason for a `(needle, candidate)` pair, asserting
    /// byte-identity and that both either produced a row or did not. Returns the
    /// shared reason (if any) so a caller can probe which variant was hit.
    fn both_paths(needle_src: &str, candidate_src: &str, alpha: bool) -> Option<MismatchReason> {
        let sel = selector(alpha);
        let needle = parse_one(needle_src);
        let candidate = parse_one(candidate_src);
        let wildcard_idents = wildcard_ident_names(&needle);
        let ast = SyntaxContext::within_ignored_ctxt(|| {
            first_mismatch_reason(&needle, &candidate, &sel, &wildcard_idents, alpha)
        });
        let needle_index = item_index(&needle).expect("test needle projects to facts");
        let fact = fact_first_mismatch_reason(&needle, &needle_index, &candidate, fact_mode(&sel))
            .expect("supported needle");
        assert_eq!(
            fact, ast,
            "fact and AST near-miss must be byte-identical\n  needle:    {needle_src:?}\n  \
             candidate: {candidate_src:?}",
        );
        ast
    }

    /// Variant 21 — `first_var_declarator_hole_placement_mismatch_reason`'s final
    /// fallback ("pinned declarators matched in order, but DECLARATORS_* hole
    /// placement differed …"). Stress the declarator-hole alignment with several
    /// hole-bearing needle/candidate pairs (reorderings, repeated string anchors,
    /// holes on every side) and assert the fact path stays byte-identical to the
    /// AST path throughout. The fallback only fires when the greedy in-order pin
    /// scan and the segment placement disagree in a way the leading/between/
    /// trailing gap checks do not localize; whether any pair reaches it, the two
    /// paths must agree byte-for-byte on whatever variant fires (17-21), which is
    /// what this asserts. The boolean records whether the fallback string was
    /// observed, so the variant's reachability is documented by the run.
    #[test]
    fn fact_near_miss_declarator_hole_fallback_is_byte_identical() {
        js_ast::with_swc_globals(|| {
            const FALLBACK: &str = "DECLARATORS_* hole placement differed";
            // Pairs chosen to exercise the hole-placement decision tree (and try to
            // drive the residual fallback): repeated-anchor reorderings where the
            // greedy pin scan and the anchored segment placement can disagree.
            let pairs: &[(&str, &str)] = &[
                (
                    "const a = \"A\", DECLARATORS_GAP = null, b = \"B\";",
                    "const b = \"B\", a = \"A\";",
                ),
                (
                    "const a = \"A\", DECLARATORS_GAP = null, b = \"B\";",
                    "const a = \"A\", a2 = \"A\", b = \"B\";",
                ),
                (
                    "const DECLARATORS_BEFORE = null, a = \"DUP\", DECLARATORS_GAP = null, b = \"DUP\", DECLARATORS_AFTER = null;",
                    "const b = \"DUP\", a = \"DUP\";",
                ),
                (
                    "const a = \"A\", DECLARATORS_GAP = null, b = \"B\", DECLARATORS_AFTER = null;",
                    "const a = \"A\", b = \"B\", a2 = \"A\";",
                ),
                (
                    "const DECLARATORS_BEFORE = null, a = \"A\", b = \"B\";",
                    "const a = \"A\", x = \"X\", b = \"B\";",
                ),
            ];
            let mut saw_fallback = false;
            for (needle_src, candidate_src) in pairs {
                if let Some(reason) = both_paths(needle_src, candidate_src, true)
                    && reason.reason.contains(FALLBACK)
                {
                    assert_eq!(reason.score, 55, "fallback score");
                    saw_fallback = true;
                }
            }
            // Documenting the run: if no pair drove the residual fallback, it is the
            // defensive branch the analysis predicts (greedy-in-order + all gaps
            // hole-covered implies the segment placement also succeeds, so the only
            // reachable hole-placement failures are the localized leading/between/
            // trailing variants 18-20). Byte-identity above holds either way; this
            // line records which world we are in for the report.
            eprintln!(
                "fact_near_miss variant-21 fallback observed in stress pairs: {saw_fallback}"
            );
        });
    }

    /// One fact-representable input per near-miss reason variant of
    /// `hints.rs::first_mismatch_reason`. Variants 7 and 13 are intentionally
    /// absent — they are reachable in the AST path only via TypeScript-only
    /// constructs the fact model does not represent (see
    /// `fact_near_miss_ts_only_variants_are_outside_the_fact_subset`).
    const CASES: &[Case] = &[
        // 1: top-level item kind differs (statement vs module declaration).
        Case {
            variant: "1 top-level item kind",
            needle_src: "const a = 1;",
            candidate_src: "import x from \"m\";",
            alpha: true,
            expected_score: 1,
            reason_marker: "top-level item kind differs: selector is statement, candidate is module declaration",
        },
        // 2: module declaration kind differs (import vs export-all).
        Case {
            variant: "2 module decl kind",
            needle_src: "import a from \"m\";",
            candidate_src: "export * from \"n\";",
            alpha: true,
            expected_score: 10,
            reason_marker: "module declaration kind differs: selector is import, candidate is export all",
        },
        // 3: module declaration shape differs (both imports, differing source).
        Case {
            variant: "3 module decl shape",
            needle_src: "import a from \"m\";",
            candidate_src: "import a from \"n\";",
            alpha: true,
            expected_score: 20,
            reason_marker: "module declaration shape differs",
        },
        // 4: statement kind differs (expression statement vs if).
        Case {
            variant: "4 statement kind",
            needle_src: "foo();",
            candidate_src: "if (x) y();",
            alpha: false,
            expected_score: 10,
            reason_marker: "statement kind differs: selector is expression, candidate is if",
        },
        // 5: statement shape differs (both ifs, differing consequent).
        Case {
            variant: "5 statement shape",
            needle_src: "if (a) foo();",
            candidate_src: "if (a) bar();",
            alpha: false,
            expected_score: 20,
            reason_marker: "statement shape differs",
        },
        // 6: declaration kind differs (class vs function).
        Case {
            variant: "6 declaration kind",
            needle_src: "class A {}",
            candidate_src: "function b() {}",
            alpha: true,
            expected_score: 30,
            reason_marker: "declaration kind differs: selector is class, candidate is function",
        },
        // 8: class name differs (exact mode pins the name).
        Case {
            variant: "8 class name",
            needle_src: "class A { m() {} }",
            candidate_src: "class B { m() {} }",
            alpha: false,
            expected_score: 40,
            reason_marker: "class name differs: selector `A`, candidate `B`",
        },
        // 9: function name differs (exact mode pins the name).
        Case {
            variant: "9 function name",
            needle_src: "function f() {}",
            candidate_src: "function g() {}",
            alpha: false,
            expected_score: 40,
            reason_marker: "function name differs: selector `f`, candidate `g`",
        },
        // 10: function signature or body differs (alpha names match, body differs).
        Case {
            variant: "10 function body",
            needle_src: "function f() { return 1; }",
            candidate_src: "function g() { return 2; }",
            alpha: true,
            expected_score: 35,
            reason_marker: "function signature or body differs",
        },
        // 11: variable declaration keyword differs (const vs let).
        Case {
            variant: "11 var keyword",
            needle_src: "const a = 1;",
            candidate_src: "let a = 1;",
            alpha: true,
            expected_score: 45,
            reason_marker: "variable declaration kind differs: selector is const, candidate is let",
        },
        // 12: declarator count differs, no hole (1 vs 2 declarators).
        Case {
            variant: "12 declarator count",
            needle_src: "const a = f();",
            candidate_src: "const a = f(), b = g();",
            alpha: true,
            expected_score: 55,
            reason_marker: "variable declarators differ: selector has 1 declarator(s), candidate has 2",
        },
        // 14: class member matched by name, but body differs.
        Case {
            variant: "14 member body",
            needle_src: "class A { m() { return 1; } }",
            candidate_src: "class A { m() { return 2; } }",
            alpha: true,
            expected_score: 65,
            reason_marker: "class member `m` matched by name, but its signature or body differs",
        },
        // 15: class pinned member not found in order.
        Case {
            variant: "15 member missing",
            needle_src: "class A { important() {} }",
            candidate_src: "class A { other() {} }",
            alpha: true,
            expected_score: 70,
            reason_marker: "selector class pinned member `important` was not found in the candidate class body in order",
        },
        // 16: class heritage differs (members all match; superclass present only in candidate).
        Case {
            variant: "16 class heritage/order",
            needle_src: "class A { m() {} }",
            candidate_src: "class A extends B { m() {} }",
            alpha: true,
            expected_score: 45,
            reason_marker: "class heritage, decorators, or member order differs",
        },
        // 17: pinned declarator not found in order (hole present; the pin's
        // string-literal init — invariant under alpha — has no candidate match).
        Case {
            variant: "17 pinned declarator missing",
            needle_src: "const target = \"WANTED\", DECLARATORS_AFTER = null;",
            candidate_src: "const a = \"X\", b = \"Y\";",
            alpha: true,
            expected_score: 55,
            reason_marker: "was not found in order",
        },
        // 18: leading unmatched declarators before the first pin (no leading hole;
        // the pin matches a non-first candidate declarator).
        Case {
            variant: "18 leading unmatched",
            needle_src: "const target = \"WANTED\", DECLARATORS_AFTER = null;",
            candidate_src: "const lead = \"X\", m = \"WANTED\";",
            alpha: true,
            expected_score: 55,
            reason_marker: "unmatched leading declarator(s) before selector declarator",
        },
        // 19: unmatched declarators between two pins (pins adjacent in the needle,
        // so no hole between them; candidate has a declarator between the matches).
        Case {
            variant: "19 between unmatched",
            needle_src: "const a = \"A\", b = \"B\", DECLARATORS_AFTER = null;",
            candidate_src: "const a = \"A\", mid = \"M\", b = \"B\";",
            alpha: true,
            expected_score: 55,
            reason_marker: "unmatched declarator(s) between selector declarator",
        },
        // 20: trailing unmatched declarators after the last pin (leading hole + pin;
        // the pin matches the first candidate declarator, leaving a trailing one).
        Case {
            variant: "20 trailing unmatched",
            needle_src: "const DECLARATORS_BEFORE = null, target = \"WANTED\";",
            candidate_src: "const m = \"WANTED\", tail = \"X\";",
            alpha: true,
            expected_score: 55,
            reason_marker: "unmatched trailing declarator(s) after selector declarator",
        },
    ];

    /// Variants 7 (`declaration shape differs`) and 13 (`variable declaration
    /// shape differs`) of `hints.rs::first_mismatch_reason` are reachable in the
    /// AST path **only** through TypeScript-only constructs the fact model does
    /// not represent:
    ///
    /// - Variant 7 needs two same-discriminant declarations that are neither
    ///   class, function, nor variable — i.e. `using`, `TsInterface`,
    ///   `TsTypeAlias`, `TsEnum`, or `TsModule`. The fact extractor
    ///   (`chunk_facts::decl`) fails closed on every one of these, so such a needle
    ///   never projects to facts and `fact_source_match_body_debt` returns no row.
    /// - Variant 13 needs a `declare` modifier mismatch (`declare let a;` vs
    ///   `let a;`); every other `match_var_decl` failure is the keyword (variant
    ///   11) or the declarator alignment (variants 12/17-21). The fact extractor
    ///   (`chunk_facts::var_decl`) does not project the `declare` modifier, so the
    ///   fact matcher treats the two as equal and reports a match (no row).
    ///
    /// Both triggers are outside the fact model's supported subset — the same
    /// subset the corpus matcher differential gates on, which the 100%-extractable
    /// JS `tana/re` corpus never exercises. This is the pre-existing fact-matcher
    /// coverage boundary (the `declare`/TS analogue of the decorator caveat in
    /// `debug/f5_body_debt_feasibility.md`), not a near-miss-specific regression.
    /// This test pins that boundary: the fact path does not invent a divergent row
    /// for these inputs.
    #[test]
    fn fact_near_miss_ts_only_variants_are_outside_the_fact_subset() {
        js_ast::with_swc_globals(|| {
            // Variant 13 trigger: `declare` modifier. The fact extractor ignores
            // `declare`, so the needle projects to facts identically to `let a;`
            // and the fact matcher reports a match — no near-miss row.
            let declare_needle = parse_one("declare let a;");
            let plain_candidate = parse_one("let a;");
            let sel = selector(true);
            let wildcard_idents = wildcard_ident_names(&declare_needle);
            let ast = SyntaxContext::within_ignored_ctxt(|| {
                first_mismatch_reason(
                    &declare_needle,
                    &plain_candidate,
                    &sel,
                    &wildcard_idents,
                    true,
                )
            });
            assert_eq!(
                ast.as_ref().map(|reason| reason.score),
                Some(35),
                "AST path reaches variant 13 via the declare modifier",
            );
            let needle_index = item_index(&declare_needle).expect("declare needle still projects");
            let fact = fact_first_mismatch_reason(
                &declare_needle,
                &needle_index,
                &plain_candidate,
                fact_mode(&sel),
            )
            .expect("supported needle");
            assert!(
                fact.is_none(),
                "fact path treats `declare let a` == `let a` (declare unmodeled), so no \
                 divergent row — not a wrong string. Got {fact:?}",
            );

            // Variant 7 trigger: a `using` declaration. The fact extractor fails
            // closed on `Decl::Using`, so the needle does not project to facts.
            let using_needle = parse_one("using a = acquire();");
            assert!(
                item_index(&using_needle).is_none(),
                "fact extractor fails closed on `using` (a TS/Stage-3 decl), so the \
                 variant-7 trigger never enters the fact near-miss path",
            );
        });
    }
}
