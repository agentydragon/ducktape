//! Resolve readable source-pattern selectors against parsed JavaScript ASTs.
//!
//! `spec` owns the YAML-facing selector data. `js_ast` owns parsing and other
//! low-level AST helpers. This module is the bridge that interprets selector
//! semantics such as alpha-equivalent identifier matching.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use spec::{AnonymousStatementSelector, BindingSourceKind, SourceMatchIdentifierMode};
use swc_atoms::{Atom, Wtf8Atom};
use swc_common::{EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::{
    Decl, ExportDecl, ImportSpecifier, MemberExpr, MemberProp, Module, ModuleDecl, ModuleItem,
    Prop, PropName, Stmt, Str, VarDecl, VarDeclarator,
};
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

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
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        for declarator in &candidate_var.decls {
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !module_items_match(needle, &candidate_item, selector) {
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
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        for declarator in &candidate_var.decls {
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !module_items_match(needle, &candidate_item, selector) {
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
    SyntaxContext::within_ignored_ctxt(|| {
        runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, item)| {
                module_items_match(needle, item, selector).then_some(body_idx)
            })
            .collect()
    })
}

fn find_matching_body_ranges(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> Vec<usize> {
    if needles.is_empty() || needles.len() > runtime_module.body.len() {
        return Vec::new();
    }
    runtime_module
        .body
        .windows(needles.len())
        .enumerate()
        .filter_map(|(body_idx, candidates)| {
            needles
                .iter()
                .zip(candidates)
                .all(|(needle, candidate)| module_items_match(needle, candidate, selector))
                .then_some(body_idx)
        })
        .collect()
}

fn module_items_match(
    needle: &ModuleItem,
    candidate: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> bool {
    SyntaxContext::within_ignored_ctxt(|| {
        let mut needle = needle.clone();
        let mut candidate = candidate.clone();
        if selector.identifiers == SourceMatchIdentifierMode::AlphaAll {
            needle.visit_mut_with(&mut AlphaIdentCanonicalizer::default());
            candidate.visit_mut_with(&mut AlphaIdentCanonicalizer::default());
        }
        if selector.wildcard_string_literals.is_empty() {
            return needle.eq_ignore_span(&candidate);
        }
        module_items_match_with_string_wildcards(
            &needle,
            &candidate,
            &selector.wildcard_string_literals,
        )
    })
}

fn module_items_match_with_string_wildcards(
    needle: &ModuleItem,
    candidate: &ModuleItem,
    wildcard_string_literals: &BTreeSet<String>,
) -> bool {
    let mut collector = StringLiteralCollector::default();
    candidate.visit_with(&mut collector);
    if collector.values.is_empty() {
        return false;
    }
    let wildcard_values: Vec<String> = wildcard_string_literals.iter().cloned().collect();
    let candidate_values: Vec<Wtf8Atom> = collector.values.into_iter().collect();
    try_string_wildcard_replacements(
        needle,
        candidate,
        wildcard_string_literals,
        &wildcard_values,
        &candidate_values,
        &mut BTreeMap::new(),
    )
}

fn try_string_wildcard_replacements(
    needle: &ModuleItem,
    candidate: &ModuleItem,
    wildcard_string_literals: &BTreeSet<String>,
    wildcard_values: &[String],
    candidate_values: &[Wtf8Atom],
    replacements: &mut BTreeMap<String, Wtf8Atom>,
) -> bool {
    let Some((wildcard, rest)) = wildcard_values.split_first() else {
        let mut needle = needle.clone();
        needle.visit_mut_with(&mut WildcardStringSubstituter {
            wildcard_string_literals,
            replacements,
        });
        return needle.eq_ignore_span(candidate);
    };
    for candidate_value in candidate_values {
        replacements.insert(wildcard.clone(), candidate_value.clone());
        if try_string_wildcard_replacements(
            needle,
            candidate,
            wildcard_string_literals,
            rest,
            candidate_values,
            replacements,
        ) {
            return true;
        }
    }
    replacements.remove(wildcard);
    false
}

#[derive(Default)]
struct AlphaIdentCanonicalizer {
    next: usize,
    names: BTreeMap<Atom, Atom>,
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
struct StringLiteralCollector {
    values: BTreeSet<Wtf8Atom>,
}

impl Visit for StringLiteralCollector {
    fn visit_str(&mut self, lit: &Str) {
        self.values.insert(lit.value.clone());
    }
}

struct WildcardStringSubstituter<'a> {
    wildcard_string_literals: &'a BTreeSet<String>,
    replacements: &'a BTreeMap<String, Wtf8Atom>,
}

impl VisitMut for WildcardStringSubstituter<'_> {
    fn visit_mut_str(&mut self, lit: &mut Str) {
        let value = lit.value.to_string_lossy();
        if self.wildcard_string_literals.contains(value.as_ref())
            && let Some(replacement) = self.replacements.get(value.as_ref())
        {
            lit.value = replacement.clone();
            lit.raw = None;
        }
    }
}
