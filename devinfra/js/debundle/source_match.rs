//! Resolve readable source-pattern selectors against parsed JavaScript ASTs.
//!
//! `spec` owns the YAML-facing selector data. `js_ast` owns parsing and other
//! low-level AST helpers. This module is the bridge that interprets selector
//! semantics such as alpha-equivalent identifier matching.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use spec::{
    AnonymousStatementSelector, BindingGroup, BindingGroupAdoptNames, BindingSourceKind,
    SourceMatch, SourceMatchIdentifierMode,
};
use swc_atoms::{Atom, Wtf8Atom};
use swc_common::{EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

/// Syntactic-hole keywords. The **bare keyword** is the anonymous form:
/// it matches independently at every occurrence and never binds, so
/// authors don't have to mint a unique name per throwaway placeholder. A
/// `<keyword>_<name>` identifier is the **named** form, which binds for
/// cross-occurrence equality — the same name must match the same
/// subtree/statement everywhere it appears.
///
/// `EXPR` matches one arbitrary expression and `STMT` one arbitrary
/// statement. `STMT_LIST` and `CLASS_REST` are variable-length list holes
/// (see [`AstWildcardMatcher::match_list_with_holes`]): `STMT_LIST`
/// absorbs a run of block statements and `CLASS_REST` a run of class
/// members; several may appear in one list, splitting the pinned
/// elements into an ordered subsequence with gaps. `STMT_LIST` must be
/// checked before `STMT`, since `STMT` is a keyword-prefix of it.
const EXPR_HOLE_KEYWORD: &str = "EXPR";
const STMT_HOLE_KEYWORD: &str = "STMT";
const STMT_LIST_HOLE_KEYWORD: &str = "STMT_LIST";
const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBinding {
    pub binding_name: String,
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
struct MemberBindingMatch {
    body_idx: usize,
    binding: ResolvedMemberBinding,
}

pub fn source_match_declared_binding_names(
    request_id: &str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<binding group source_match in {request_id}>"),
        &source_match.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: binding_groups[].source_match did not parse as JS:\n{}",
            source_match.match_source
        )
    })?;
    Ok(parsed
        .body
        .iter()
        .flat_map(declared_bindings)
        .map(|binding| binding.binding_name)
        .collect())
}

/// Expand one `binding_groups[]` entry into `(export_name,
/// member-form selector)` pairs — each selector is the group's
/// `source_match` with `target_binding` set to one selector-local
/// binding. This is the single expansion both the run pipeline's
/// member assembly (`lowering::build_members`) and the CLI edit gate
/// consume, so the two always agree on which owners a binding group
/// claims.
pub fn binding_group_member_selectors(
    request_id: &str,
    group: &BindingGroup,
) -> Result<Vec<(String, AnonymousStatementSelector)>> {
    if group.source_match.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match must not include \
             `target_binding`; use the `exports` keys to choose selector-local bindings"
        );
    }
    let exports = effective_binding_group_exports(group, request_id)?;
    Ok(exports
        .into_iter()
        .map(|(target_binding, export_name)| {
            let mut selector = group.source_match.selector();
            selector.target_binding = Some(target_binding);
            (export_name, selector)
        })
        .collect())
}

fn effective_binding_group_exports(
    group: &BindingGroup,
    request_id: &str,
) -> Result<BTreeMap<String, String>> {
    let mut exports = match &group.adopt_names {
        BindingGroupAdoptNames::None | BindingGroupAdoptNames::All(false) => BTreeMap::new(),
        BindingGroupAdoptNames::All(true) => {
            let names = declared_selector_binding_names(group, request_id)?;
            names
                .into_iter()
                .map(|name| (name.clone(), name))
                .collect::<BTreeMap<_, _>>()
        }
        BindingGroupAdoptNames::Names(names) => {
            let declared = declared_selector_binding_names(group, request_id)?;
            let declared_set = declared.into_iter().collect::<BTreeSet<_>>();
            let mut adopted = BTreeMap::new();
            for name in names {
                if !declared_set.contains(name) {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names entry \
                         `{name}` is not declared by source_match.match"
                    );
                }
                if adopted.insert(name.clone(), name.clone()).is_some() {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names repeats \
                         `{name}`"
                    );
                }
            }
            adopted
        }
    };
    exports.extend(group.exports.clone());
    if exports.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[] must include non-empty `exports` \
             or `adopt_names`"
        );
    }
    Ok(exports)
}

fn declared_selector_binding_names(group: &BindingGroup, request_id: &str) -> Result<Vec<String>> {
    let names = source_match_declared_binding_names(request_id, &group.source_match)?;
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for name in &names {
        if !seen.insert(name.clone()) {
            duplicates.insert(name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match declares duplicate \
             selector-local binding names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    if names.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].adopt_names found no declared \
             bindings in source_match.match"
        );
    }
    Ok(names)
}

/// All member-binding candidates a member-form selector matches in
/// `runtime_module`, without the exactly-one arbitration
/// [`resolve_member_binding`] applies. Used by callers that
/// aggregate matches across several source files (the CLI edit
/// gate) before deciding uniqueness.
pub fn member_binding_candidates(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ResolvedMemberBinding>> {
    Ok(
        find_member_binding_matches(runtime_module, request_id, selector)?
            .into_iter()
            .map(|matched| matched.binding)
            .collect(),
    )
}

pub fn resolve_anonymous_statement_body_index(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<usize> {
    let matches = find_anonymous_statement_body_indices(runtime_module, request_id, selector)?;
    match matches.as_slice() {
        [single] => Ok(*single),
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match did not match any \
             top-level statement in the chunk. Selector:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].match is ambiguous — \
             matched {} top-level statements at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple,
            match_source = selector.match_source,
        ),
    }
}

pub fn find_anonymous_statement_body_indices(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<usize>> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<anonymous_statement match in {request_id}>"),
        &selector.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: anonymous_statements[].match did not parse as JS:\n{}",
            selector.match_source
        )
    })?;
    let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
    let needle = match parsed_items.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to zero \
             statements; selector source must contain exactly one top-level \
             statement:\n{match_source}",
            match_source = selector.match_source,
        ),
        _ => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to {} \
             statements; selector source must contain exactly one top-level \
             statement:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    };
    Ok(find_matching_body_indices(runtime_module, needle, selector))
}

pub fn resolve_member_binding(
    runtime_module: &Module,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let matches = find_member_binding_matches(runtime_module, request_id, selector)?;
    let target_binding_hint = selector
        .target_binding
        .as_deref()
        .map(|target| format!(" target_binding `{target}`"))
        .unwrap_or_default();
    match matches.as_slice() {
        [single] => Ok(single.binding.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}`{target_binding_hint} did not match any top-level declaration in the chunk. \
             Selector:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}`{target_binding_hint} is ambiguous — matched {} top-level statements at body \
             indices {:?} (bindings: {}). Refine the selector. Source:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .map(|matched| matched.body_idx)
                .collect::<Vec<_>>(),
            multiple
                .iter()
                .map(|matched| matched.binding.binding_name.as_str())
                .collect::<Vec<_>>()
                .join(", "),
            match_source = selector.match_source,
        ),
    }
}

fn find_member_binding_matches(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<member source_match in {request_id}>"),
        &selector.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: members[].selector.source_match did not parse as JS:\n{}",
            selector.match_source
        )
    })?;
    if let Some(target_binding) = &selector.target_binding {
        if parsed.body.is_empty() {
            bail!(
                "logical_module {request_id}: members[].selector.source_match parsed to zero \
                 statements; selector source must contain at least one top-level statement \
                 when target_binding is used:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        if parsed.body.len() == 1 && selector_single_var_declarator(&parsed.body[0]).is_some() {
            return find_matching_target_var_declarators(
                runtime_module,
                request_id,
                &parsed.body[0],
                selector,
                target_binding,
            );
        }
        return find_matching_target_bindings(
            runtime_module,
            request_id,
            &parsed.body,
            selector,
            target_binding,
        );
    }
    let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
    let needle = match parsed_items.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to zero \
             statements; selector source must contain exactly one top-level declaration:\n{match_source}",
            match_source = selector.match_source,
        ),
        _ => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to {} \
             statements; selector source must contain exactly one top-level declaration unless \
             target_binding is used:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    };
    if selector_single_var_declarator(needle).is_some() {
        return find_matching_var_declarators(runtime_module, needle, selector);
    }
    let mut matches = Vec::new();
    for body_idx in find_matching_body_indices(runtime_module, needle, selector) {
        let item = runtime_module.body.get(body_idx).with_context(|| {
            format!("body index {body_idx} disappeared while resolving source_match")
        })?;
        let declared = declared_bindings(item);
        match declared.as_slice() {
            [single] => matches.push(MemberBindingMatch {
                body_idx,
                binding: single.clone(),
            }),
            [] => {}
            _ => bail!(
                "logical_module {request_id}: members[].selector.source_match matched \
                 top-level statement at body index {body_idx}, but that statement declares \
                 {} bindings ({}). Use a single-declarator selector or refine the match. \
                 Source:\n{match_source}",
                declared.len(),
                declared
                    .iter()
                    .map(|binding| binding.binding_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", "),
                match_source = selector.match_source,
            ),
        }
    }
    Ok(matches)
}

fn find_matching_target_bindings(
    runtime_module: &Module,
    request_id: &str,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<Vec<MemberBindingMatch>> {
    let selector_binding_locations: Vec<(usize, usize)> = needles
        .iter()
        .enumerate()
        .flat_map(|(item_idx, item)| {
            declared_bindings(item).into_iter().enumerate().filter_map(
                move |(binding_idx, binding)| {
                    (binding.binding_name == target_binding).then_some((item_idx, binding_idx))
                },
            )
        })
        .collect();
    let (target_item_idx, target_binding_idx) = match selector_binding_locations.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is not declared by the selector source:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is ambiguous within the selector source at statement/binding \
             indices {:?}. Refine the selector source:\n{match_source}",
            multiple,
            match_source = selector.match_source,
        ),
    };
    let mut matches = Vec::new();
    for body_idx in find_matching_body_ranges(runtime_module, needles, selector) {
        let matched_body_idx = body_idx + target_item_idx;
        let item = runtime_module.body.get(matched_body_idx).with_context(|| {
            format!("body index {matched_body_idx} disappeared while resolving source_match")
        })?;
        let declared = declared_bindings(item);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` matched top-level statement at body index {matched_body_idx}, but \
                 the matched statement declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        matches.push(MemberBindingMatch {
            body_idx: matched_body_idx,
            binding: binding.clone(),
        });
    }
    Ok(matches)
}

fn find_matching_target_var_declarators(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<Vec<MemberBindingMatch>> {
    let selector_bindings = declared_bindings(needle);
    let selector_binding_indices: Vec<usize> = selector_bindings
        .iter()
        .enumerate()
        .filter_map(|(idx, binding)| (binding.binding_name == target_binding).then_some(idx))
        .collect();
    let target_binding_idx = match selector_binding_indices.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is not declared by the selector source:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is ambiguous within the selector source at declared-binding \
             indices {:?}. Refine the selector source:\n{match_source}",
            multiple,
            match_source = selector.match_source,
        ),
    };
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !prepared.matches(&candidate_item) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            let Some(binding) = declared.get(target_binding_idx) else {
                bail!(
                    "logical_module {request_id}: members[].selector.source_match target_binding \
                     `{target_binding}` matched a variable declarator at body index {body_idx}, \
                     but that declarator declares only {} bindings. Source:\n{match_source}",
                    declared.len(),
                    match_source = selector.match_source,
                );
            };
            matches.push(MemberBindingMatch {
                body_idx,
                binding: binding.clone(),
            });
        }
    }
    Ok(matches)
}

fn selector_single_var_declarator(needle: &ModuleItem) -> Option<&VarDecl> {
    match needle {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() == 1 => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) if matches!(&export.decl, Decl::Var(var) if var.decls.len() == 1) => {
            match &export.decl {
                Decl::Var(var) => Some(var),
                _ => None,
            }
        }
        _ => None,
    }
}

fn find_matching_var_declarators(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !prepared.matches(&candidate_item) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            match declared.as_slice() {
                [single] => matches.push(MemberBindingMatch {
                    body_idx,
                    binding: single.clone(),
                }),
                [] => {}
                _ => bail!(
                    "members[].selector.source_match matched a variable declarator at body \
                     index {body_idx}, but that declarator binds multiple names ({}). \
                     Refine the selector to a single-binding declarator.",
                    declared
                        .iter()
                        .map(|binding| binding.binding_name.as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
            }
        }
    }
    Ok(matches)
}

/// Cheap candidate prefilters for the per-declarator matching loops.
/// Both `find_matching_var_declarators` paths clone a fresh
/// single-declarator `ModuleItem` per candidate declarator before
/// running the full structural match; these keys reject most
/// candidates before that clone.
struct VarDeclaratorPrefilter {
    /// `var`/`let`/`const` of the needle's declaration. Both the
    /// wildcard matcher (`match_var_decl`) and plain structural
    /// equality require kind equality, so a kind mismatch can never
    /// match — sound in every mode.
    needle_kind: VarDeclKind,
    /// The needle declarator's plain-`Ident` binding name, when the
    /// match is exact (no wildcards, no alpha renaming). In that mode
    /// equality requires the candidate declarator to bind the same
    /// plain ident, so a name mismatch can never match. `None`
    /// disables the name key (wildcards, alpha mode, or a
    /// destructuring pattern).
    needle_ident: Option<Atom>,
}

impl VarDeclaratorPrefilter {
    fn new(needle: &ModuleItem, prepared: &PreparedNeedle) -> Self {
        let needle_var = selector_single_var_declarator(needle)
            .expect("caller checked the needle is a single-declarator var decl");
        let needle_ident = (prepared.no_wildcards && !prepared.alpha)
            .then(|| match &needle_var.decls[0].name {
                Pat::Ident(ident) => Some(ident.id.sym.clone()),
                _ => None,
            })
            .flatten();
        Self {
            needle_kind: needle_var.kind,
            needle_ident,
        }
    }

    fn var_decl_can_match(&self, candidate: &VarDecl) -> bool {
        candidate.kind == self.needle_kind
    }

    fn declarator_can_match(&self, declarator: &VarDeclarator) -> bool {
        let Some(needle_sym) = &self.needle_ident else {
            return true;
        };
        matches!(&declarator.name, Pat::Ident(ident) if ident.id.sym == *needle_sym)
    }
}

fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

fn module_item_for_single_var_declarator(
    source_item: &ModuleItem,
    declarator: &VarDeclarator,
) -> ModuleItem {
    let (source_var, export_span) = match source_item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => (var.as_ref(), None),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => (var.as_ref(), Some(export.span)),
            _ => unreachable!("item_var_decl already filtered source_item"),
        },
        _ => unreachable!("item_var_decl already filtered source_item"),
    };
    let var_decl = Decl::Var(Box::new(VarDecl {
        span: source_var.span,
        ctxt: source_var.ctxt,
        kind: source_var.kind,
        declare: source_var.declare,
        decls: vec![declarator.clone()],
    }));
    match export_span {
        Some(span) => ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            span,
            decl: var_decl,
        })),
        None => ModuleItem::Stmt(Stmt::Decl(var_decl)),
    }
}

fn declared_bindings(item: &ModuleItem) -> Vec<ResolvedMemberBinding> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declared_bindings_for_decl(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            declared_bindings_for_decl(&export.decl)
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            })
            .map(|binding_name| ResolvedMemberBinding {
                binding_name,
                kind: Some(BindingSourceKind::ImportSpecifier),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn declared_bindings_for_decl(decl: &Decl) -> Vec<ResolvedMemberBinding> {
    match decl {
        Decl::Fn(f) => vec![ResolvedMemberBinding {
            binding_name: f.ident.sym.to_string(),
            kind: Some(BindingSourceKind::FunctionDeclaration),
        }],
        Decl::Class(c) => vec![ResolvedMemberBinding {
            binding_name: c.ident.sym.to_string(),
            kind: Some(BindingSourceKind::ClassDeclaration),
        }],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(declared_bindings_for_var_declarator)
            .collect(),
        _ => Vec::new(),
    }
}

fn declared_bindings_for_var_declarator(declarator: &VarDeclarator) -> Vec<ResolvedMemberBinding> {
    binding_targets::binding_name_strings(&declarator.name)
        .into_iter()
        .map(|binding_name| ResolvedMemberBinding {
            binding_name,
            kind: Some(BindingSourceKind::VariableDeclarator),
        })
        .collect()
}

fn find_matching_body_indices(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> Vec<usize> {
    let prepared = PreparedNeedle::new(needle, selector);
    runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, item)| prepared.matches(item).then_some(body_idx))
        .collect()
}

fn find_matching_body_ranges(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> Vec<usize> {
    if needles.is_empty() || needles.len() > runtime_module.body.len() {
        return Vec::new();
    }
    let prepared: Vec<PreparedNeedle> = needles
        .iter()
        .map(|needle| PreparedNeedle::new(needle, selector))
        .collect();
    runtime_module
        .body
        .windows(needles.len())
        .enumerate()
        .filter_map(|(body_idx, candidates)| {
            prepared
                .iter()
                .zip(candidates)
                .all(|(needle, candidate)| needle.matches(candidate))
                .then_some(body_idx)
        })
        .collect()
}

/// Needle-derived matching state hoisted out of the per-candidate
/// loops. The previous `module_items_match` recomputed the needle's
/// wildcard-ident set — and, in alpha mode without wildcards, cloned
/// and re-canonicalized BOTH trees — once per candidate comparison;
/// the needle side of that work is invariant across candidates.
struct PreparedNeedle<'a> {
    needle: &'a ModuleItem,
    selector: &'a AnonymousStatementSelector,
    wildcard_idents: WildcardIdents,
    alpha: bool,
    /// Neither string-literal nor syntactic-hole wildcards present —
    /// the plain structural-equality fast path applies.
    no_wildcards: bool,
    /// The needle pre-canonicalized once for the no-wildcard alpha
    /// path (`None` otherwise).
    canonical_needle: Option<ModuleItem>,
}

impl<'a> PreparedNeedle<'a> {
    fn new(needle: &'a ModuleItem, selector: &'a AnonymousStatementSelector) -> Self {
        SyntaxContext::within_ignored_ctxt(|| {
            let wildcard_idents = wildcard_ident_names(needle);
            let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
            let no_wildcards =
                selector.wildcard_string_literals.is_empty() && wildcard_idents.is_empty();
            let canonical_needle = (no_wildcards && alpha).then(|| {
                let mut canonical = needle.clone();
                canonical.visit_mut_with(&mut AlphaIdentCanonicalizer::new(&wildcard_idents));
                canonical
            });
            Self {
                needle,
                selector,
                wildcard_idents,
                alpha,
                no_wildcards,
                canonical_needle,
            }
        })
    }

    fn matches(&self, candidate: &ModuleItem) -> bool {
        SyntaxContext::within_ignored_ctxt(|| {
            if self.no_wildcards {
                // No wildcards: plain structural equality. The cheap
                // shape prefilter rejects most candidates before the
                // alpha path's per-candidate clone + canonicalize.
                if !no_wildcard_shape_prefilter(self.needle, candidate) {
                    return false;
                }
                // Without wildcards the two trees have identical shape,
                // so global alpha-canonicalization cannot desync — keep
                // that cheap path.
                if let Some(canonical_needle) = &self.canonical_needle {
                    let mut candidate = candidate.clone();
                    candidate
                        .visit_mut_with(&mut AlphaIdentCanonicalizer::new(&self.wildcard_idents));
                    return canonical_needle.eq_ignore_span(&candidate);
                }
                return self.needle.eq_ignore_span(candidate);
            }
            // Wildcards present: the structural matcher tracks an identifier
            // bijection for alpha mode (see `AstWildcardMatcher::alpha`), so
            // holes that absorb identifier-bearing subtrees don't desync the
            // identifiers after them — and it walks borrowed trees with no
            // per-comparison clone + canonicalize.
            AstWildcardMatcher::new(self.selector, &self.wildcard_idents, self.alpha)
                .match_module_item(self.needle, candidate)
        })
    }
}

/// Cheap top-level shape check, sound only in **no-wildcard** mode
/// (where matching is structural equality): a `false` return proves
/// the full comparison cannot succeed. Compares the item/statement/
/// declaration discriminants and, for variable declarations, the
/// `var`/`let`/`const` kind and declarator count.
fn no_wildcard_shape_prefilter(needle: &ModuleItem, candidate: &ModuleItem) -> bool {
    fn decl_shape(n: &Decl, c: &Decl) -> bool {
        if std::mem::discriminant(n) != std::mem::discriminant(c) {
            return false;
        }
        match (n, c) {
            (Decl::Var(nv), Decl::Var(cv)) => {
                nv.kind == cv.kind && nv.decls.len() == cv.decls.len()
            }
            _ => true,
        }
    }
    match (needle, candidate) {
        (ModuleItem::Stmt(n), ModuleItem::Stmt(c)) => {
            std::mem::discriminant(n) == std::mem::discriminant(c)
                && match (n, c) {
                    (Stmt::Decl(nd), Stmt::Decl(cd)) => decl_shape(nd, cd),
                    _ => true,
                }
        }
        (ModuleItem::ModuleDecl(n), ModuleItem::ModuleDecl(c)) => {
            std::mem::discriminant(n) == std::mem::discriminant(c)
                && match (n, c) {
                    (ModuleDecl::ExportDecl(ne), ModuleDecl::ExportDecl(ce)) => {
                        decl_shape(&ne.decl, &ce.decl)
                    }
                    _ => true,
                }
        }
        _ => false,
    }
}

#[derive(Clone, Default)]
struct WildcardReplacements {
    strings: BTreeMap<String, Wtf8Atom>,
    expressions: BTreeMap<String, Expr>,
    statements: BTreeMap<String, Stmt>,
}

struct AstWildcardMatcher<'a> {
    selector: &'a AnonymousStatementSelector,
    wildcard_idents: &'a WildcardIdents,
    replacements: WildcardReplacements,
    /// Whether value/binding identifiers are alpha-renamable. When true,
    /// identifier equality is tracked as a bijection built incrementally
    /// at structurally-corresponding positions, instead of pre-renaming
    /// both trees. Because holes accept (and never recurse into) the
    /// subtrees they absorb, absorbed identifiers never enter the
    /// bijection — so a hole no longer desyncs the numbering of the nodes
    /// after it, and there is no per-comparison clone + canonicalize.
    alpha: bool,
    ident_forward: BTreeMap<Atom, Atom>,
    ident_backward: BTreeMap<Atom, Atom>,
}

/// A clone of the matcher's mutable binding state, captured before a
/// tentative segment placement during ordered-subsequence (multi-hole)
/// list matching and restored when that placement fails — so a
/// half-applied segment never leaks identifier or wildcard bindings into
/// the next attempt.
#[derive(Clone)]
struct MatcherState {
    replacements: WildcardReplacements,
    ident_forward: BTreeMap<Atom, Atom>,
    ident_backward: BTreeMap<Atom, Atom>,
}

/// Loop-invariant inputs for the recursive ordered-subsequence search in
/// [`AstWildcardMatcher::match_list_with_holes`]. Only the `seg_idx` and
/// `cand_min` cursor arguments change as the search descends.
struct SegmentSearch<'a, T> {
    needle: &'a [T],
    candidate: &'a [T],
    /// `(needle_start, len)` of each maximal fixed (non-hole) run, in
    /// source order.
    segments: &'a [(usize, usize)],
    /// Whether the first segment is pinned to the candidate's start
    /// (true unless a hole leads the needle list).
    anchored_left: bool,
    /// Whether the last segment is pinned to the candidate's end (true
    /// unless a hole trails the needle list).
    anchored_right: bool,
}

impl<'a> AstWildcardMatcher<'a> {
    fn new(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        alpha: bool,
    ) -> Self {
        Self {
            selector,
            wildcard_idents,
            replacements: WildcardReplacements::default(),
            alpha,
            ident_forward: BTreeMap::new(),
            ident_backward: BTreeMap::new(),
        }
    }

    /// Match two value/binding identifier symbols. In exact mode they
    /// must be equal; in alpha mode they must be consistent with the
    /// identifier bijection (each needle symbol maps to exactly one
    /// candidate symbol and vice versa).
    fn match_sym(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        if !self.alpha {
            return needle == candidate;
        }
        match (
            self.ident_forward.get(needle),
            self.ident_backward.get(candidate),
        ) {
            (Some(mapped), _) => mapped == candidate,
            (None, Some(_)) => false,
            (None, None) => {
                self.ident_forward.insert(needle.clone(), candidate.clone());
                self.ident_backward
                    .insert(candidate.clone(), needle.clone());
                true
            }
        }
    }

    fn match_ident(&mut self, needle: &Ident, candidate: &Ident) -> bool {
        needle.optional == candidate.optional && self.match_sym(&needle.sym, &candidate.sym)
    }

    fn match_opt_ident(&mut self, needle: &Option<Ident>, candidate: &Option<Ident>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_ident(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn bind_string(&mut self, wildcard: &str, candidate_value: &Wtf8Atom) -> bool {
        match self.replacements.strings.get(wildcard) {
            Some(existing) => existing == candidate_value,
            None => {
                self.replacements
                    .strings
                    .insert(wildcard.to_string(), candidate_value.clone());
                true
            }
        }
    }

    fn bind_expr(&mut self, wildcard: &str, candidate: &Expr) -> bool {
        // The bare keyword `EXPR` is an anonymous wildcard: every
        // occurrence matches independently, so authors don't have to mint
        // a unique name per placeholder. Named holes (`EXPR_FOO`) keep
        // their cross-occurrence equality.
        if hole_is_anonymous(wildcard, EXPR_HOLE_KEYWORD) {
            return true;
        }
        match self.replacements.expressions.get(wildcard) {
            Some(existing) => existing.eq_ignore_span(candidate),
            None => {
                self.replacements
                    .expressions
                    .insert(wildcard.to_string(), candidate.clone());
                true
            }
        }
    }

    fn bind_stmt(&mut self, wildcard: &str, candidate: &Stmt) -> bool {
        // Bare `STMT` is anonymous; see [`Self::bind_expr`].
        if hole_is_anonymous(wildcard, STMT_HOLE_KEYWORD) {
            return true;
        }
        match self.replacements.statements.get(wildcard) {
            Some(existing) => existing.eq_ignore_span(candidate),
            None => {
                self.replacements
                    .statements
                    .insert(wildcard.to_string(), candidate.clone());
                true
            }
        }
    }

    fn match_module_item(&mut self, needle: &ModuleItem, candidate: &ModuleItem) -> bool {
        match (needle, candidate) {
            (ModuleItem::Stmt(needle), ModuleItem::Stmt(candidate)) => {
                self.match_stmt(needle, candidate)
            }
            (ModuleItem::ModuleDecl(needle), ModuleItem::ModuleDecl(candidate)) => {
                self.match_module_decl(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_module_decl(&mut self, needle: &ModuleDecl, candidate: &ModuleDecl) -> bool {
        match (needle, candidate) {
            (ModuleDecl::Import(needle), ModuleDecl::Import(candidate)) => {
                needle.specifiers.eq_ignore_span(&candidate.specifiers)
                    && needle.type_only == candidate.type_only
                    && needle.phase.eq_ignore_span(&candidate.phase)
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::ExportDecl(needle), ModuleDecl::ExportDecl(candidate)) => {
                self.match_decl(&needle.decl, &candidate.decl)
            }
            (ModuleDecl::ExportDefaultDecl(needle), ModuleDecl::ExportDefaultDecl(candidate)) => {
                self.match_default_decl(&needle.decl, &candidate.decl)
            }
            (ModuleDecl::ExportDefaultExpr(needle), ModuleDecl::ExportDefaultExpr(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (ModuleDecl::ExportAll(needle), ModuleDecl::ExportAll(candidate)) => {
                needle.type_only == candidate.type_only
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::ExportNamed(needle), ModuleDecl::ExportNamed(candidate)) => {
                needle.specifiers.eq_ignore_span(&candidate.specifiers)
                    && needle.type_only == candidate.type_only
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_option_box_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::TsExportAssignment(needle), ModuleDecl::TsExportAssignment(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_decl(&mut self, needle: &Decl, candidate: &Decl) -> bool {
        match (needle, candidate) {
            (Decl::Var(needle), Decl::Var(candidate)) => self.match_var_decl(needle, candidate),
            (Decl::Fn(needle), Decl::Fn(candidate)) => {
                self.match_ident(&needle.ident, &candidate.ident)
                    && needle.declare == candidate.declare
                    && self.match_function(&needle.function, &candidate.function)
            }
            (Decl::Class(needle), Decl::Class(candidate)) => {
                self.match_ident(&needle.ident, &candidate.ident)
                    && needle.declare == candidate.declare
                    && self.match_class(&needle.class, &candidate.class)
            }
            (Decl::Using(needle), Decl::Using(candidate)) => {
                self.match_using_decl(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_default_decl(&mut self, needle: &DefaultDecl, candidate: &DefaultDecl) -> bool {
        match (needle, candidate) {
            (DefaultDecl::Class(needle), DefaultDecl::Class(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_class(&needle.class, &candidate.class)
            }
            (DefaultDecl::Fn(needle), DefaultDecl::Fn(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_function(&needle.function, &candidate.function)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_function(&mut self, needle: &Function, candidate: &Function) -> bool {
        self.match_slice(&needle.params, &candidate.params, Self::match_param)
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle.is_generator == candidate.is_generator
            && needle.is_async == candidate.is_async
            && needle.type_params.eq_ignore_span(&candidate.type_params)
            && needle.return_type.eq_ignore_span(&candidate.return_type)
            && self.match_option_block_stmt(&needle.body, &candidate.body)
    }

    fn match_param(&mut self, needle: &Param, candidate: &Param) -> bool {
        self.match_slice(
            &needle.decorators,
            &candidate.decorators,
            Self::match_decorator,
        ) && self.match_pat(&needle.pat, &candidate.pat)
    }

    fn match_var_decl(&mut self, needle: &VarDecl, candidate: &VarDecl) -> bool {
        needle.kind == candidate.kind
            && needle.declare == candidate.declare
            && self.match_slice(&needle.decls, &candidate.decls, Self::match_var_declarator)
    }

    fn match_using_decl(&mut self, needle: &UsingDecl, candidate: &UsingDecl) -> bool {
        needle.is_await == candidate.is_await
            && self.match_slice(&needle.decls, &candidate.decls, Self::match_var_declarator)
    }

    fn match_var_declarator(&mut self, needle: &VarDeclarator, candidate: &VarDeclarator) -> bool {
        needle.definite == candidate.definite
            && self.match_pat(&needle.name, &candidate.name)
            && self.match_option_box_expr(&needle.init, &candidate.init)
    }

    fn match_stmt(&mut self, needle: &Stmt, candidate: &Stmt) -> bool {
        if let Some(hole_name) = statement_hole_name(needle)
            && self.wildcard_idents.statements.contains(hole_name)
        {
            return self.bind_stmt(hole_name, candidate);
        }
        match (needle, candidate) {
            (Stmt::Block(needle), Stmt::Block(candidate)) => {
                self.match_block_stmt(needle, candidate)
            }
            (Stmt::With(needle), Stmt::With(candidate)) => {
                self.match_expr(&needle.obj, &candidate.obj)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::Return(needle), Stmt::Return(candidate)) => {
                self.match_option_box_expr(&needle.arg, &candidate.arg)
            }
            (Stmt::Labeled(needle), Stmt::Labeled(candidate)) => {
                needle.label.eq_ignore_span(&candidate.label)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::If(needle), Stmt::If(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.cons, &candidate.cons)
                    && self.match_option_box_stmt(&needle.alt, &candidate.alt)
            }
            (Stmt::Switch(needle), Stmt::Switch(candidate)) => {
                self.match_expr(&needle.discriminant, &candidate.discriminant)
                    && self.match_slice(&needle.cases, &candidate.cases, Self::match_switch_case)
            }
            (Stmt::Throw(needle), Stmt::Throw(candidate)) => {
                self.match_expr(&needle.arg, &candidate.arg)
            }
            (Stmt::Try(needle), Stmt::Try(candidate)) => {
                self.match_block_stmt(&needle.block, &candidate.block)
                    && self.match_option_catch_clause(&needle.handler, &candidate.handler)
                    && self.match_option_block_stmt(&needle.finalizer, &candidate.finalizer)
            }
            (Stmt::While(needle), Stmt::While(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::DoWhile(needle), Stmt::DoWhile(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::For(needle), Stmt::For(candidate)) => {
                self.match_option_var_decl_or_expr(&needle.init, &candidate.init)
                    && self.match_option_box_expr(&needle.test, &candidate.test)
                    && self.match_option_box_expr(&needle.update, &candidate.update)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::ForIn(needle), Stmt::ForIn(candidate)) => {
                self.match_for_head(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::ForOf(needle), Stmt::ForOf(candidate)) => {
                needle.is_await == candidate.is_await
                    && self.match_for_head(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::Decl(needle), Stmt::Decl(candidate)) => self.match_decl(needle, candidate),
            (Stmt::Expr(needle), Stmt::Expr(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_expr(&mut self, needle: &Expr, candidate: &Expr) -> bool {
        if let Some(hole_name) = expression_hole_name(needle)
            && self.wildcard_idents.expressions.contains(hole_name)
        {
            return self.bind_expr(hole_name, candidate);
        }
        match (needle, candidate) {
            (Expr::Array(needle), Expr::Array(candidate)) => self.match_slice(
                &needle.elems,
                &candidate.elems,
                Self::match_option_expr_or_spread,
            ),
            (Expr::Object(needle), Expr::Object(candidate)) => {
                self.match_slice(&needle.props, &candidate.props, Self::match_prop_or_spread)
            }
            (Expr::Ident(needle), Expr::Ident(candidate)) => self.match_ident(needle, candidate),
            (Expr::Fn(needle), Expr::Fn(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_function(&needle.function, &candidate.function)
            }
            (Expr::Class(needle), Expr::Class(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_class(&needle.class, &candidate.class)
            }
            (Expr::Unary(needle), Expr::Unary(candidate)) => {
                needle.op == candidate.op && self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Update(needle), Expr::Update(candidate)) => {
                needle.op == candidate.op
                    && needle.prefix == candidate.prefix
                    && self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Bin(needle), Expr::Bin(candidate)) => {
                needle.op == candidate.op
                    && self.match_expr(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
            }
            (Expr::Assign(needle), Expr::Assign(candidate)) => {
                needle.op == candidate.op
                    && self.match_assign_target(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
            }
            (Expr::Member(needle), Expr::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (Expr::SuperProp(needle), Expr::SuperProp(candidate)) => {
                self.match_super_prop(&needle.prop, &candidate.prop)
            }
            (Expr::Cond(needle), Expr::Cond(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_expr(&needle.cons, &candidate.cons)
                    && self.match_expr(&needle.alt, &candidate.alt)
            }
            (Expr::Call(needle), Expr::Call(candidate)) => self.match_call_expr(needle, candidate),
            (Expr::New(needle), Expr::New(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.callee, &candidate.callee)
                    && self.match_option_expr_or_spread_vec(&needle.args, &candidate.args)
            }
            (Expr::Seq(needle), Expr::Seq(candidate)) => self.match_slice(
                &needle.exprs,
                &candidate.exprs,
                |matcher, needle, candidate| matcher.match_expr(needle, candidate),
            ),
            (Expr::Lit(needle), Expr::Lit(candidate)) => self.match_lit(needle, candidate),
            (Expr::Tpl(needle), Expr::Tpl(candidate)) => {
                needle.quasis.eq_ignore_span(&candidate.quasis)
                    && self.match_slice(
                        &needle.exprs,
                        &candidate.exprs,
                        |matcher, needle, candidate| matcher.match_expr(needle, candidate),
                    )
            }
            (Expr::TaggedTpl(needle), Expr::TaggedTpl(candidate)) => {
                needle.type_params.eq_ignore_span(&candidate.type_params)
                    && self.match_expr(&needle.tag, &candidate.tag)
                    && self.match_tpl(&needle.tpl, &candidate.tpl)
            }
            (Expr::Arrow(needle), Expr::Arrow(candidate)) => {
                self.match_slice(&needle.params, &candidate.params, Self::match_pat)
                    && needle.is_async == candidate.is_async
                    && needle.is_generator == candidate.is_generator
                    && needle.type_params.eq_ignore_span(&candidate.type_params)
                    && needle.return_type.eq_ignore_span(&candidate.return_type)
                    && self.match_block_stmt_or_expr(&needle.body, &candidate.body)
            }
            (Expr::Yield(needle), Expr::Yield(candidate)) => {
                needle.delegate == candidate.delegate
                    && self.match_option_box_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Await(needle), Expr::Await(candidate)) => {
                self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Paren(needle), Expr::Paren(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::JSXElement(needle), Expr::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (Expr::JSXFragment(needle), Expr::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            (Expr::TsConstAssertion(needle), Expr::TsConstAssertion(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsNonNull(needle), Expr::TsNonNull(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsAs(needle), Expr::TsAs(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsSatisfies(needle), Expr::TsSatisfies(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsTypeAssertion(needle), Expr::TsTypeAssertion(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsInstantiation(needle), Expr::TsInstantiation(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::OptChain(needle), Expr::OptChain(candidate)) => {
                needle.optional == candidate.optional
                    && self.match_opt_chain_base(&needle.base, &candidate.base)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_jsx_element(&mut self, needle: &JSXElement, candidate: &JSXElement) -> bool {
        self.match_jsx_opening_element(&needle.opening, &candidate.opening)
            && self.match_slice(
                &needle.children,
                &candidate.children,
                Self::match_jsx_element_child,
            )
            && self.match_option_jsx_closing_element(&needle.closing, &candidate.closing)
    }

    fn match_jsx_opening_element(
        &mut self,
        needle: &JSXOpeningElement,
        candidate: &JSXOpeningElement,
    ) -> bool {
        needle.name.eq_ignore_span(&candidate.name)
            && needle.self_closing == candidate.self_closing
            && needle.type_args.eq_ignore_span(&candidate.type_args)
            && self.match_slice(
                &needle.attrs,
                &candidate.attrs,
                Self::match_jsx_attr_or_spread,
            )
    }

    fn match_jsx_attr_or_spread(
        &mut self,
        needle: &JSXAttrOrSpread,
        candidate: &JSXAttrOrSpread,
    ) -> bool {
        match (needle, candidate) {
            (JSXAttrOrSpread::JSXAttr(needle), JSXAttrOrSpread::JSXAttr(candidate)) => {
                self.match_jsx_attr(needle, candidate)
            }
            (JSXAttrOrSpread::SpreadElement(needle), JSXAttrOrSpread::SpreadElement(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_jsx_attr(&mut self, needle: &JSXAttr, candidate: &JSXAttr) -> bool {
        needle.name.eq_ignore_span(&candidate.name)
            && self.match_option_jsx_attr_value(&needle.value, &candidate.value)
    }

    fn match_option_jsx_attr_value(
        &mut self,
        needle: &Option<JSXAttrValue>,
        candidate: &Option<JSXAttrValue>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_jsx_attr_value(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_jsx_attr_value(&mut self, needle: &JSXAttrValue, candidate: &JSXAttrValue) -> bool {
        match (needle, candidate) {
            (JSXAttrValue::Str(needle), JSXAttrValue::Str(candidate)) => {
                self.match_str(needle, candidate)
            }
            (JSXAttrValue::JSXExprContainer(needle), JSXAttrValue::JSXExprContainer(candidate)) => {
                self.match_jsx_expr_container(needle, candidate)
            }
            (JSXAttrValue::JSXElement(needle), JSXAttrValue::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (JSXAttrValue::JSXFragment(needle), JSXAttrValue::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_jsx_expr_container(
        &mut self,
        needle: &JSXExprContainer,
        candidate: &JSXExprContainer,
    ) -> bool {
        self.match_jsx_expr(&needle.expr, &candidate.expr)
    }

    fn match_jsx_expr(&mut self, needle: &JSXExpr, candidate: &JSXExpr) -> bool {
        match (needle, candidate) {
            (JSXExpr::JSXEmptyExpr(needle), JSXExpr::JSXEmptyExpr(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (JSXExpr::Expr(needle), JSXExpr::Expr(candidate)) => self.match_expr(needle, candidate),
            _ => false,
        }
    }

    fn match_jsx_element_child(
        &mut self,
        needle: &JSXElementChild,
        candidate: &JSXElementChild,
    ) -> bool {
        match (needle, candidate) {
            (JSXElementChild::JSXText(needle), JSXElementChild::JSXText(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (
                JSXElementChild::JSXExprContainer(needle),
                JSXElementChild::JSXExprContainer(candidate),
            ) => self.match_jsx_expr_container(needle, candidate),
            (
                JSXElementChild::JSXSpreadChild(needle),
                JSXElementChild::JSXSpreadChild(candidate),
            ) => self.match_expr(&needle.expr, &candidate.expr),
            (JSXElementChild::JSXElement(needle), JSXElementChild::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (JSXElementChild::JSXFragment(needle), JSXElementChild::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_jsx_fragment(&mut self, needle: &JSXFragment, candidate: &JSXFragment) -> bool {
        needle.opening.eq_ignore_span(&candidate.opening)
            && needle.closing.eq_ignore_span(&candidate.closing)
            && self.match_slice(
                &needle.children,
                &candidate.children,
                Self::match_jsx_element_child,
            )
    }

    fn match_option_jsx_closing_element(
        &mut self,
        needle: &Option<JSXClosingElement>,
        candidate: &Option<JSXClosingElement>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => needle.eq_ignore_span(candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_block_stmt(&mut self, needle: &BlockStmt, candidate: &BlockStmt) -> bool {
        self.match_stmt_slice(&needle.stmts, &candidate.stmts)
    }

    fn match_switch_case(&mut self, needle: &SwitchCase, candidate: &SwitchCase) -> bool {
        self.match_option_box_expr(&needle.test, &candidate.test)
            && self.match_stmt_slice(&needle.cons, &candidate.cons)
    }

    fn match_catch_clause(&mut self, needle: &CatchClause, candidate: &CatchClause) -> bool {
        self.match_option_pat(&needle.param, &candidate.param)
            && self.match_block_stmt(&needle.body, &candidate.body)
    }

    fn match_var_decl_or_expr(
        &mut self,
        needle: &VarDeclOrExpr,
        candidate: &VarDeclOrExpr,
    ) -> bool {
        match (needle, candidate) {
            (VarDeclOrExpr::VarDecl(needle), VarDeclOrExpr::VarDecl(candidate)) => {
                self.match_var_decl(needle, candidate)
            }
            (VarDeclOrExpr::Expr(needle), VarDeclOrExpr::Expr(candidate)) => {
                self.match_expr(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_for_head(&mut self, needle: &ForHead, candidate: &ForHead) -> bool {
        match (needle, candidate) {
            (ForHead::VarDecl(needle), ForHead::VarDecl(candidate)) => {
                self.match_var_decl(needle, candidate)
            }
            (ForHead::Pat(needle), ForHead::Pat(candidate)) => self.match_pat(needle, candidate),
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_pat(&mut self, needle: &Pat, candidate: &Pat) -> bool {
        match (needle, candidate) {
            (Pat::Array(needle), Pat::Array(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(&needle.elems, &candidate.elems, Self::match_option_pat)
            }
            (Pat::Object(needle), Pat::Object(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(
                        &needle.props,
                        &candidate.props,
                        Self::match_object_pat_prop,
                    )
            }
            (Pat::Assign(needle), Pat::Assign(candidate)) => {
                self.match_assign_pat(needle, candidate)
            }
            (Pat::Rest(needle), Pat::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_pat(&needle.arg, &candidate.arg)
            }
            (Pat::Expr(needle), Pat::Expr(candidate)) => self.match_expr(needle, candidate),
            (Pat::Ident(needle), Pat::Ident(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ident(&needle.id, &candidate.id)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_assign_pat(&mut self, needle: &AssignPat, candidate: &AssignPat) -> bool {
        self.match_pat(&needle.left, &candidate.left)
            && self.match_expr(&needle.right, &candidate.right)
    }

    fn match_object_pat_prop(&mut self, needle: &ObjectPatProp, candidate: &ObjectPatProp) -> bool {
        match (needle, candidate) {
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::KeyValue(candidate)) => {
                needle.key.eq_ignore_span(&candidate.key)
                    && self.match_pat(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::Assign(candidate)) => {
                needle.key.eq_ignore_span(&candidate.key)
                    && self.match_option_box_expr(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Rest(needle), ObjectPatProp::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_pat(&needle.arg, &candidate.arg)
            }
            _ => false,
        }
    }

    fn match_member_expr(&mut self, needle: &MemberExpr, candidate: &MemberExpr) -> bool {
        self.match_expr(&needle.obj, &candidate.obj)
            && self.match_member_prop(&needle.prop, &candidate.prop)
    }

    fn match_member_prop(&mut self, needle: &MemberProp, candidate: &MemberProp) -> bool {
        match (needle, candidate) {
            (MemberProp::Ident(needle), MemberProp::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (MemberProp::PrivateName(needle), MemberProp::PrivateName(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (MemberProp::Computed(needle), MemberProp::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_super_prop(&mut self, needle: &SuperProp, candidate: &SuperProp) -> bool {
        match (needle, candidate) {
            (SuperProp::Ident(needle), SuperProp::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (SuperProp::Computed(needle), SuperProp::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_call_expr(&mut self, needle: &CallExpr, candidate: &CallExpr) -> bool {
        needle.type_args.eq_ignore_span(&candidate.type_args)
            && self.match_callee(&needle.callee, &candidate.callee)
            && self.match_slice(&needle.args, &candidate.args, Self::match_expr_or_spread)
    }

    fn match_callee(&mut self, needle: &Callee, candidate: &Callee) -> bool {
        match (needle, candidate) {
            (Callee::Super(_), Callee::Super(_)) => true,
            (Callee::Import(needle), Callee::Import(candidate)) => {
                needle.phase.eq_ignore_span(&candidate.phase)
            }
            (Callee::Expr(needle), Callee::Expr(candidate)) => self.match_expr(needle, candidate),
            _ => false,
        }
    }

    fn match_expr_or_spread(&mut self, needle: &ExprOrSpread, candidate: &ExprOrSpread) -> bool {
        needle.spread.is_some() == candidate.spread.is_some()
            && self.match_expr(&needle.expr, &candidate.expr)
    }

    fn match_option_expr_or_spread(
        &mut self,
        needle: &Option<ExprOrSpread>,
        candidate: &Option<ExprOrSpread>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_expr_or_spread(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_prop_or_spread(&mut self, needle: &PropOrSpread, candidate: &PropOrSpread) -> bool {
        match (needle, candidate) {
            (PropOrSpread::Spread(needle), PropOrSpread::Spread(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (PropOrSpread::Prop(needle), PropOrSpread::Prop(candidate)) => {
                self.match_prop(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_prop(&mut self, needle: &Prop, candidate: &Prop) -> bool {
        match (needle, candidate) {
            (Prop::Shorthand(needle), Prop::Shorthand(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (Prop::KeyValue(needle), Prop::KeyValue(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && self.match_expr(&needle.value, &candidate.value)
            }
            (Prop::Assign(needle), Prop::Assign(candidate)) => {
                needle.key.eq_ignore_span(&candidate.key)
                    && self.match_expr(&needle.value, &candidate.value)
            }
            (Prop::Getter(needle), Prop::Getter(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_option_block_stmt(&needle.body, &candidate.body)
            }
            (Prop::Setter(needle), Prop::Setter(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && needle.this_param.eq_ignore_span(&candidate.this_param)
                    && self.match_pat(&needle.param, &candidate.param)
                    && self.match_option_block_stmt(&needle.body, &candidate.body)
            }
            (Prop::Method(needle), Prop::Method(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && self.match_function(&needle.function, &candidate.function)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_prop_name(&mut self, needle: &PropName, candidate: &PropName) -> bool {
        match (needle, candidate) {
            (PropName::Str(needle), PropName::Str(candidate)) => self.match_str(needle, candidate),
            (PropName::Computed(needle), PropName::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_lit(&mut self, needle: &Lit, candidate: &Lit) -> bool {
        match (needle, candidate) {
            (Lit::Str(needle), Lit::Str(candidate)) => self.match_str(needle, candidate),
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_str(&mut self, needle: &Str, candidate: &Str) -> bool {
        let wildcard = needle.value.to_string_lossy();
        if self
            .selector
            .wildcard_string_literals
            .contains(wildcard.as_ref())
        {
            return self.bind_string(wildcard.as_ref(), &candidate.value);
        }
        needle.eq_ignore_span(candidate)
    }

    fn match_tpl(&mut self, needle: &Tpl, candidate: &Tpl) -> bool {
        needle.quasis.eq_ignore_span(&candidate.quasis)
            && self.match_slice(
                &needle.exprs,
                &candidate.exprs,
                |matcher, needle, candidate| matcher.match_expr(needle, candidate),
            )
    }

    fn match_block_stmt_or_expr(
        &mut self,
        needle: &BlockStmtOrExpr,
        candidate: &BlockStmtOrExpr,
    ) -> bool {
        match (needle, candidate) {
            (BlockStmtOrExpr::BlockStmt(needle), BlockStmtOrExpr::BlockStmt(candidate)) => {
                self.match_block_stmt(needle, candidate)
            }
            (BlockStmtOrExpr::Expr(needle), BlockStmtOrExpr::Expr(candidate)) => {
                self.match_expr(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_assign_target(&mut self, needle: &AssignTarget, candidate: &AssignTarget) -> bool {
        match (needle, candidate) {
            (AssignTarget::Simple(needle), AssignTarget::Simple(candidate)) => {
                self.match_simple_assign_target(needle, candidate)
            }
            (AssignTarget::Pat(needle), AssignTarget::Pat(candidate)) => {
                self.match_assign_target_pat(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_assign_target_pat(
        &mut self,
        needle: &AssignTargetPat,
        candidate: &AssignTargetPat,
    ) -> bool {
        match (needle, candidate) {
            (AssignTargetPat::Array(needle), AssignTargetPat::Array(candidate)) => {
                self.match_slice(&needle.elems, &candidate.elems, Self::match_option_pat)
            }
            (AssignTargetPat::Object(needle), AssignTargetPat::Object(candidate)) => {
                self.match_slice(&needle.props, &candidate.props, Self::match_object_pat_prop)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_simple_assign_target(
        &mut self,
        needle: &SimpleAssignTarget,
        candidate: &SimpleAssignTarget,
    ) -> bool {
        match (needle, candidate) {
            (SimpleAssignTarget::Ident(needle), SimpleAssignTarget::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (SimpleAssignTarget::Member(needle), SimpleAssignTarget::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (SimpleAssignTarget::SuperProp(needle), SimpleAssignTarget::SuperProp(candidate)) => {
                self.match_super_prop(&needle.prop, &candidate.prop)
            }
            (SimpleAssignTarget::Paren(needle), SimpleAssignTarget::Paren(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (SimpleAssignTarget::TsAs(needle), SimpleAssignTarget::TsAs(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsSatisfies(needle),
                SimpleAssignTarget::TsSatisfies(candidate),
            ) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (SimpleAssignTarget::TsNonNull(needle), SimpleAssignTarget::TsNonNull(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsTypeAssertion(needle),
                SimpleAssignTarget::TsTypeAssertion(candidate),
            ) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsInstantiation(needle),
                SimpleAssignTarget::TsInstantiation(candidate),
            ) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_opt_chain_base(&mut self, needle: &OptChainBase, candidate: &OptChainBase) -> bool {
        match (needle, candidate) {
            (OptChainBase::Member(needle), OptChainBase::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (OptChainBase::Call(needle), OptChainBase::Call(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.callee, &candidate.callee)
                    && self.match_slice(&needle.args, &candidate.args, Self::match_expr_or_spread)
            }
            _ => false,
        }
    }

    fn match_class(&mut self, needle: &Class, candidate: &Class) -> bool {
        needle.is_abstract == candidate.is_abstract
            && needle.type_params.eq_ignore_span(&candidate.type_params)
            && needle
                .super_type_params
                .eq_ignore_span(&candidate.super_type_params)
            && needle.implements.eq_ignore_span(&candidate.implements)
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && self.match_option_box_expr(&needle.super_class, &candidate.super_class)
            && self.match_class_member_slice(&needle.body, &candidate.body)
    }

    fn match_decorator(&mut self, needle: &Decorator, candidate: &Decorator) -> bool {
        self.match_expr(&needle.expr, &candidate.expr)
    }

    fn match_class_member(&mut self, needle: &ClassMember, candidate: &ClassMember) -> bool {
        match (needle, candidate) {
            (ClassMember::Constructor(needle), ClassMember::Constructor(candidate)) => {
                self.match_constructor(needle, candidate)
            }
            (ClassMember::Method(needle), ClassMember::Method(candidate)) => {
                self.match_class_method(needle, candidate)
            }
            (ClassMember::PrivateMethod(needle), ClassMember::PrivateMethod(candidate)) => {
                self.match_private_method(needle, candidate)
            }
            (ClassMember::ClassProp(needle), ClassMember::ClassProp(candidate)) => {
                self.match_class_prop(needle, candidate)
            }
            (ClassMember::PrivateProp(needle), ClassMember::PrivateProp(candidate)) => {
                self.match_private_prop(needle, candidate)
            }
            (ClassMember::StaticBlock(needle), ClassMember::StaticBlock(candidate)) => {
                self.match_block_stmt(&needle.body, &candidate.body)
            }
            (ClassMember::AutoAccessor(needle), ClassMember::AutoAccessor(candidate)) => {
                self.match_auto_accessor(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_constructor(&mut self, needle: &Constructor, candidate: &Constructor) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && self.match_slice(
                &needle.params,
                &candidate.params,
                Self::match_param_or_ts_param_prop,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_optional == candidate.is_optional
            && self.match_option_block_stmt(&needle.body, &candidate.body)
    }

    fn match_param_or_ts_param_prop(
        &mut self,
        needle: &ParamOrTsParamProp,
        candidate: &ParamOrTsParamProp,
    ) -> bool {
        match (needle, candidate) {
            (ParamOrTsParamProp::Param(needle), ParamOrTsParamProp::Param(candidate)) => {
                self.match_param(needle, candidate)
            }
            (
                ParamOrTsParamProp::TsParamProp(needle),
                ParamOrTsParamProp::TsParamProp(candidate),
            ) => self.match_ts_param_prop(needle, candidate),
            _ => false,
        }
    }

    fn match_ts_param_prop(&mut self, needle: &TsParamProp, candidate: &TsParamProp) -> bool {
        needle
            .accessibility
            .eq_ignore_span(&candidate.accessibility)
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && self.match_ts_param_prop_param(&needle.param, &candidate.param)
    }

    fn match_ts_param_prop_param(
        &mut self,
        needle: &TsParamPropParam,
        candidate: &TsParamPropParam,
    ) -> bool {
        match (needle, candidate) {
            (TsParamPropParam::Ident(needle), TsParamPropParam::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (TsParamPropParam::Assign(needle), TsParamPropParam::Assign(candidate)) => {
                self.match_assign_pat(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_class_method(&mut self, needle: &ClassMethod, candidate: &ClassMethod) -> bool {
        needle.kind == candidate.kind
            && needle.is_static == candidate.is_static
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && self.match_prop_name(&needle.key, &candidate.key)
            && self.match_function(&needle.function, &candidate.function)
    }

    fn match_private_method(&mut self, needle: &PrivateMethod, candidate: &PrivateMethod) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && needle.kind == candidate.kind
            && needle.is_static == candidate.is_static
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && self.match_function(&needle.function, &candidate.function)
    }

    fn match_class_prop(&mut self, needle: &ClassProp, candidate: &ClassProp) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && needle.declare == candidate.declare
            && needle.definite == candidate.definite
            && self.match_prop_name(&needle.key, &candidate.key)
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_private_prop(&mut self, needle: &PrivateProp, candidate: &PrivateProp) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && needle.definite == candidate.definite
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_auto_accessor(&mut self, needle: &AutoAccessor, candidate: &AutoAccessor) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_override == candidate.is_override
            && needle.definite == candidate.definite
            && self.match_key(&needle.key, &candidate.key)
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_key(&mut self, needle: &Key, candidate: &Key) -> bool {
        match (needle, candidate) {
            (Key::Public(needle), Key::Public(candidate)) => {
                self.match_prop_name(needle, candidate)
            }
            (Key::Private(needle), Key::Private(candidate)) => needle.eq_ignore_span(candidate),
            _ => false,
        }
    }

    fn match_option_box_expr(
        &mut self,
        needle: &Option<Box<Expr>>,
        candidate: &Option<Box<Expr>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_expr(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_box_stmt(
        &mut self,
        needle: &Option<Box<Stmt>>,
        candidate: &Option<Box<Stmt>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_stmt(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_block_stmt(
        &mut self,
        needle: &Option<BlockStmt>,
        candidate: &Option<BlockStmt>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_block_stmt(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_var_decl_or_expr(
        &mut self,
        needle: &Option<VarDeclOrExpr>,
        candidate: &Option<VarDeclOrExpr>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_var_decl_or_expr(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_catch_clause(
        &mut self,
        needle: &Option<CatchClause>,
        candidate: &Option<CatchClause>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_catch_clause(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_pat(&mut self, needle: &Option<Pat>, candidate: &Option<Pat>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_pat(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_box_str(
        &mut self,
        needle: &Option<Box<Str>>,
        candidate: &Option<Box<Str>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_str(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_expr_or_spread_vec(
        &mut self,
        needle: &Option<Vec<ExprOrSpread>>,
        candidate: &Option<Vec<ExprOrSpread>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => {
                self.match_slice(needle, candidate, Self::match_expr_or_spread)
            }
            (None, None) => true,
            _ => false,
        }
    }

    /// Match a statement list. `STMT_LIST;` holes split the needle into
    /// fixed segments matched as an ordered subsequence with gaps (see
    /// [`Self::match_list_with_holes`]); with no hole this is an exact
    /// element-wise match.
    fn match_stmt_slice(&mut self, needle: &[Stmt], candidate: &[Stmt]) -> bool {
        if needle
            .iter()
            .any(|stmt| statement_list_hole_name(stmt).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |stmt| statement_list_hole_name(stmt).is_some(),
                Self::match_stmt,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_stmt)
        }
    }

    /// Match a class member list. `CLASS_REST;` holes split the needle
    /// into fixed segments matched as an ordered subsequence with gaps
    /// (see [`Self::match_list_with_holes`]); with no hole this is an
    /// exact element-wise match.
    fn match_class_member_slice(
        &mut self,
        needle: &[ClassMember],
        candidate: &[ClassMember],
    ) -> bool {
        if needle.iter().any(is_class_rest_hole) {
            self.match_list_with_holes(
                needle,
                candidate,
                is_class_rest_hole,
                Self::match_class_member,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_class_member)
        }
    }

    /// Match a needle list carrying one or more list-holes against a
    /// candidate list as an **ordered subsequence with gaps**. The holes
    /// partition the needle into maximal fixed-element segments; each
    /// segment must appear in the candidate as a contiguous block, the
    /// segments in source order and non-overlapping, with the gaps
    /// between them (plus any leading/trailing hole) absorbing arbitrary
    /// runs of candidate elements — including empty runs. A leading hole
    /// un-anchors the first segment from the candidate's start; a
    /// trailing hole un-anchors the last segment from its end. A single
    /// interior hole degenerates to the old contiguous prefix/suffix
    /// match (both ends anchored, one gap in the middle).
    ///
    /// Matching is greedy-leftmost and commits the first placement under
    /// which every segment matches, keeping the identifier/wildcard
    /// bindings it accumulated. This is a pure existence check: the
    /// interior alignment chosen when several placements are possible
    /// never changes *which* enclosing declaration matched, and the
    /// "matched more than one top-level declaration" ambiguity is still a
    /// hard error in the caller that counts those matches.
    fn match_list_with_holes<T>(
        &mut self,
        needle: &[T],
        candidate: &[T],
        is_hole: impl Fn(&T) -> bool,
        match_item: fn(&mut Self, &T, &T) -> bool,
    ) -> bool {
        let mut segments: Vec<(usize, usize)> = Vec::new();
        let mut idx = 0;
        while idx < needle.len() {
            if is_hole(&needle[idx]) {
                idx += 1;
                continue;
            }
            let start = idx;
            while idx < needle.len() && !is_hole(&needle[idx]) {
                idx += 1;
            }
            segments.push((start, idx - start));
        }
        // An all-holes needle pins nothing, so it matches any candidate
        // run (including an empty one).
        if segments.is_empty() {
            return true;
        }
        let search = SegmentSearch {
            needle,
            candidate,
            segments: &segments,
            anchored_left: !is_hole(&needle[0]),
            anchored_right: !is_hole(&needle[needle.len() - 1]),
        };
        self.place_segments(&search, 0, 0, match_item)
    }

    /// Recursive ordered-subsequence search backing
    /// [`Self::match_list_with_holes`]. Places `segments[seg_idx..]` into
    /// `candidate[cand_min..]`, trying the leftmost feasible start first
    /// and rolling the matcher state back after each failed attempt.
    /// Returns true — leaving the committed bindings in place — once
    /// every remaining segment is placed.
    fn place_segments<T>(
        &mut self,
        search: &SegmentSearch<T>,
        seg_idx: usize,
        cand_min: usize,
        match_item: fn(&mut Self, &T, &T) -> bool,
    ) -> bool {
        let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
            return true; // every segment placed
        };
        let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
        // The latest start that still leaves room for this and every
        // following segment; `None` means the candidate is too short.
        let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
            return false;
        };
        let mut lo = cand_min;
        let mut hi = latest_start;
        if seg_idx == 0 && search.anchored_left {
            // The first segment must start at the candidate's first element.
            hi = hi.min(0);
        }
        if seg_idx == search.segments.len() - 1 && search.anchored_right {
            // The last segment must end at the candidate's last element.
            lo = lo.max(latest_start);
        }
        // An empty `lo..=hi` (e.g. an anchor pushed `lo` past `hi`) means
        // no feasible placement, so the search backtracks.
        for start in lo..=hi {
            let snapshot = self.snapshot();
            let mut segment_ok = true;
            for offset in 0..seg_len {
                if !match_item(
                    self,
                    &search.needle[needle_start + offset],
                    &search.candidate[start + offset],
                ) {
                    segment_ok = false;
                    break;
                }
            }
            if segment_ok && self.place_segments(search, seg_idx + 1, start + seg_len, match_item) {
                return true;
            }
            self.restore(snapshot);
        }
        false
    }

    fn snapshot(&self) -> MatcherState {
        MatcherState {
            replacements: self.replacements.clone(),
            ident_forward: self.ident_forward.clone(),
            ident_backward: self.ident_backward.clone(),
        }
    }

    fn restore(&mut self, state: MatcherState) {
        self.replacements = state.replacements;
        self.ident_forward = state.ident_forward;
        self.ident_backward = state.ident_backward;
    }

    fn match_slice<T>(
        &mut self,
        needle: &[T],
        candidate: &[T],
        mut match_item: impl FnMut(&mut Self, &T, &T) -> bool,
    ) -> bool {
        needle.len() == candidate.len()
            && needle
                .iter()
                .zip(candidate)
                .all(|(needle, candidate)| match_item(self, needle, candidate))
    }
}

#[derive(Default)]
struct AlphaIdentCanonicalizer {
    next: usize,
    names: BTreeMap<Atom, Atom>,
    reserved_idents: BTreeSet<String>,
}

impl AlphaIdentCanonicalizer {
    fn new(wildcard_idents: &WildcardIdents) -> Self {
        Self {
            reserved_idents: wildcard_idents
                .expressions
                .iter()
                .chain(&wildcard_idents.statements)
                .chain(&wildcard_idents.statement_lists)
                .cloned()
                .collect(),
            ..Self::default()
        }
    }
}

impl AlphaIdentCanonicalizer {
    fn canonical(&mut self, sym: &Atom) -> Atom {
        if let Some(existing) = self.names.get(sym) {
            return existing.clone();
        }
        let canonical = Atom::from(format!("__debundle_alpha_{}", self.next));
        self.next += 1;
        self.names.insert(sym.clone(), canonical.clone());
        canonical
    }
}

impl VisitMut for AlphaIdentCanonicalizer {
    fn visit_mut_ident(&mut self, ident: &mut swc_ecma_ast::Ident) {
        if self.reserved_idents.contains(ident.sym.as_ref()) {
            return;
        }
        ident.sym = self.canonical(&ident.sym);
    }

    fn visit_mut_member_expr(&mut self, member: &mut MemberExpr) {
        member.obj.visit_mut_with(self);
        match &mut member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => {}
            MemberProp::Computed(prop) => prop.expr.visit_mut_with(self),
        }
    }

    fn visit_mut_prop(&mut self, prop: &mut Prop) {
        match prop {
            Prop::Shorthand(_) => {}
            Prop::KeyValue(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.value.visit_mut_with(self);
            }
            Prop::Assign(prop) => {
                prop.value.visit_mut_with(self);
            }
            Prop::Getter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.type_ann.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Setter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.this_param.visit_mut_with(self);
                prop.param.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Method(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.function.visit_mut_with(self);
            }
        }
    }
}

fn visit_computed_prop_name(prop_name: &mut PropName, visitor: &mut AlphaIdentCanonicalizer) {
    if let PropName::Computed(prop_name) = prop_name {
        prop_name.expr.visit_mut_with(visitor);
    }
}

#[derive(Default)]
struct WildcardIdents {
    expressions: BTreeSet<String>,
    statements: BTreeSet<String>,
    /// `STMT_LIST` statement-list hole names (reserved from
    /// alpha-canonicalization like the single-node holes).
    statement_lists: BTreeSet<String>,
    /// Whether the selector contains any `CLASS_REST` class-member
    /// hole. The marker is a class field key (an `IdentName`, not an
    /// `Ident`), so it survives alpha-canonicalization without being
    /// reserved — only presence matters for routing.
    class_rest_present: bool,
}

impl WildcardIdents {
    /// True when the selector carries no holes of any kind, so the
    /// caller can take the plain `eq_ignore_span` fast path. List holes
    /// count: a selector with only `STMT_LIST` / `CLASS_REST` holes still
    /// needs the structural matcher.
    fn is_empty(&self) -> bool {
        self.expressions.is_empty()
            && self.statements.is_empty()
            && self.statement_lists.is_empty()
            && !self.class_rest_present
    }
}

fn wildcard_ident_names(needle: &ModuleItem) -> WildcardIdents {
    let mut collector = WildcardIdentCollector::default();
    needle.visit_with(&mut collector);
    collector.idents
}

#[derive(Default)]
struct WildcardIdentCollector {
    idents: WildcardIdents,
}

impl Visit for WildcardIdentCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Some(hole_name) = expression_hole_name(expr) {
            self.idents.expressions.insert(hole_name.to_string());
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_stmt(&mut self, stmt: &Stmt) {
        if let Some(hole_name) = statement_hole_name(stmt) {
            // `STMT` is a keyword-prefix of `STMT_LIST`, so the list hole
            // must win first.
            if hole_name_for(hole_name, STMT_LIST_HOLE_KEYWORD).is_some() {
                self.idents.statement_lists.insert(hole_name.to_string());
                return;
            }
            if hole_name_for(hole_name, STMT_HOLE_KEYWORD).is_some() {
                self.idents.statements.insert(hole_name.to_string());
                return;
            }
        }
        stmt.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if is_class_rest_hole(member) {
            self.idents.class_rest_present = true;
            return;
        }
        member.visit_children_with(self);
    }
}

fn expression_hole_name(expr: &Expr) -> Option<&str> {
    let Expr::Ident(ident) = expr else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), EXPR_HOLE_KEYWORD)
}

fn statement_hole_name(stmt: &Stmt) -> Option<&str> {
    let Stmt::Expr(ExprStmt { expr, .. }) = stmt else {
        return None;
    };
    let Expr::Ident(ident) = expr.as_ref() else {
        return None;
    };
    Some(ident.sym.as_ref())
}

/// The name of a statement-list hole (`STMT_LIST` / `STMT_LIST_*;`) if
/// `stmt` is one.
fn statement_list_hole_name(stmt: &Stmt) -> Option<&str> {
    hole_name_for(statement_hole_name(stmt)?, STMT_LIST_HOLE_KEYWORD)
}

/// If `name` is a hole identifier for `keyword` — the bare keyword
/// (anonymous) or a `<keyword>_<suffix>` (named) form — return it. The
/// keyword must be followed by end-of-string or `_`, so `EXPR` and
/// `EXPR_FOO` match for keyword `EXPR` but `EXPRESSION` does not.
fn hole_name_for<'a>(name: &'a str, keyword: &str) -> Option<&'a str> {
    let rest = name.strip_prefix(keyword)?;
    (rest.is_empty() || rest.starts_with('_')).then_some(name)
}

/// Whether a hole name is the anonymous form: the bare keyword, or the
/// keyword with a trailing underscore but no suffix. Anonymous holes
/// match independently at every occurrence instead of binding for
/// cross-occurrence equality.
fn hole_is_anonymous(name: &str, keyword: &str) -> bool {
    matches!(name.strip_prefix(keyword), Some("") | Some("_"))
}

/// Whether `member` is a `CLASS_REST;` class-member hole: a class field
/// whose key is exactly the keyword and which has no initializer. Matched
/// as an exact token (not a `CLASS_REST_*` prefix) so it never collides
/// with a real field whose name merely starts with `CLASS_REST`; since it
/// never binds, a suffix would carry no meaning anyway.
fn is_class_rest_hole(member: &ClassMember) -> bool {
    let ClassMember::ClassProp(prop) = member else {
        return false;
    };
    prop.value.is_none()
        && matches!(
            &prop.key,
            PropName::Ident(ident) if ident.sym.as_ref() == CLASS_REST_HOLE_KEYWORD
        )
}
