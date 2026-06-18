//! The Datalog-side `SelectorResolver`: resolve a selector to its claimed
//! owner/binding using the fact-based matcher (`selector_match::matches` over
//! `chunk_facts`) as the per-statement match oracle, then reuse the **same**
//! production binding-extraction (`declared_bindings`, `selector_binding_location`)
//! that `AstWildcardResolver` uses. Only the match decision is swapped, so wherever
//! both resolvers handle a selector they agree by construction with the matcher
//! differential (`selector_match_differential_test`, `corpus_match_differential`)
//! that already proves the per-statement verdicts equal.
//!
//! Fail-closed: a construct this resolver does not yet handle (a multi-statement
//! needle, a var-declarator/declarator-hole target, a binding group, an
//! `Unsupported` needle) returns an error rather than a wrong claim — it never
//! under-resolves silently. `DifferentialResolver<AstWildcard, Datalog>` compares
//! the two; a fail-closed error is only agreement when production also rejects.

use super::*;

use swc_common::DUMMY_SP;

/// The fact-based resolver. See module docs.
pub struct DatalogResolver;

fn datalog_mode(selector: &AnonymousStatementSelector) -> selector_match::Mode {
    match selector.identifiers {
        SourceMatchIdentifierMode::Exact => selector_match::Mode::Exact,
        SourceMatchIdentifierMode::AlphaAll => selector_match::Mode::AlphaAll,
    }
}

/// Facts for a single top-level statement (wrapped in a one-item module so the
/// extractor's owner-ordinal join and the matcher's root anchoring see one root).
fn item_facts(item: &ModuleItem) -> Option<chunk_facts::ChunkFacts> {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    chunk_facts::extract_facts(&module).ok()
}

/// Parse a selector's `match_source` to exactly one top-level needle item, or
/// fail closed (multi-statement needles are a separate, not-yet-handled path).
fn single_needle(request_id: &str, selector: &AnonymousStatementSelector) -> Result<ModuleItem> {
    let module = js_ast::parse_js_module_ast(
        &format!("<datalog needle in {request_id}>"),
        &selector.match_source,
    )?;
    match module.body.into_iter().collect::<Vec<_>>().as_slice() {
        [single] => Ok(single.clone()),
        items => bail!(
            "datalog resolver: selector source parsed to {} top-level statements; only \
             single-statement needles are handled:\n{}",
            items.len(),
            selector.match_source,
        ),
    }
}

/// Top-level body indices whose statement the needle matches under the fact
/// matcher. Fails closed if the needle itself is `Unsupported`.
fn matching_body_indices(
    module: &Module,
    needle_facts: &chunk_facts::ChunkFacts,
    mode: selector_match::Mode,
) -> Result<Vec<usize>> {
    // Probe the needle once: an unsupported construct errors uniformly.
    selector_match::matches(needle_facts, needle_facts, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut indices = Vec::new();
    for (body_idx, item) in module.body.iter().enumerate() {
        // chunk_facts is 100% on the corpus; a non-extractable statement is
        // skipped (it cannot be the matched owner of a faithfully-projected
        // needle), and any resulting divergence would surface in the differential.
        let Some(facts) = item_facts(item) else {
            continue;
        };
        if selector_match::matches(needle_facts, &facts, mode)
            .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?
        {
            indices.push(body_idx);
        }
    }
    Ok(indices)
}

/// A synthetic single-declarator version of a var-decl `item` keeping only
/// `declarator`, cloned from the real item so span/context stay valid. Matching
/// the single-declarator needle against this *is* the per-declarator match the
/// production resolver does. Counting matches across every declarator of every
/// owner keeps categoricity faithful: a single-declarator needle that matches
/// one declarator of a multi-declarator owner is a real match a whole-statement
/// match would miss — which would mis-count and turn a production-ambiguous case
/// into a spurious unique resolution.
fn single_declarator_item(item: &ModuleItem, declarator: &VarDeclarator) -> ModuleItem {
    let mut cloned = item.clone();
    match &mut cloned {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var.decls = vec![declarator.clone()],
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            if let Decl::Var(var) = &mut export.decl {
                var.decls = vec![declarator.clone()];
            }
        }
        _ => {}
    }
    cloned
}

/// Resolve a single-declarator var-decl member selector by matching its
/// declarator against every declarator of every var-decl owner (production's
/// per-declarator path), so the match count — hence categoricity — is faithful.
fn resolve_var_declarator_member(
    module: &Module,
    needle: &ModuleItem,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let needle_facts = item_facts(needle)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let mode = datalog_mode(selector);
    // Probe the needle once: an unsupported construct errors uniformly.
    selector_match::matches(&needle_facts, &needle_facts, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    // Which of the needle declarator's declared bindings is the target.
    let target_binding_idx = match &selector.target_binding {
        Some(target_binding) => {
            let declared = declared_bindings(needle);
            let indices: Vec<usize> = declared
                .iter()
                .enumerate()
                .filter_map(|(idx, binding)| {
                    (binding.binding_name == *target_binding).then_some(idx)
                })
                .collect();
            match indices.as_slice() {
                [single] => *single,
                [] => bail!(
                    "logical_module {request_id}: target_binding `{target_binding}` is not \
                     declared by the selector source"
                ),
                _ => bail!(
                    "logical_module {request_id}: target_binding `{target_binding}` is \
                     ambiguous within the selector source"
                ),
            }
        }
        None => 0,
    };
    let mut matches: Vec<ResolvedMemberBinding> = Vec::new();
    for item in &module.body {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        for declarator in &candidate_var.decls {
            let Some(facts) = item_facts(&single_declarator_item(item, declarator)) else {
                continue;
            };
            if !selector_match::matches(&needle_facts, &facts, mode).map_err(|unsupported| {
                anyhow::anyhow!("datalog resolver: {}", unsupported.reason)
            })? {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            if selector.target_binding.is_none() && declared.len() != 1 {
                bail!(
                    "datalog resolver: export `{export_name}` matched a declarator binding {} \
                     names; needs a single-binding declarator or target_binding",
                    declared.len(),
                );
            }
            let Some(binding) = declared.into_iter().nth(target_binding_idx) else {
                bail!("datalog resolver: target binding index out of range for matched declarator");
            };
            matches.push(binding);
        }
    }
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` did not match any declarator in the chunk"
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` is ambiguous — matched {} declarators",
            multiple.len(),
        ),
    }
}

impl SelectorResolver for DatalogResolver {
    fn resolve_member(
        &self,
        module: &Module,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        let needle = single_needle(request_id, selector)?;
        // Declarator-hole var-decls need alignment-aware extraction (a DECLARATORS
        // run hole absorbs declarators, so the owner's binding index no longer
        // lines up with the needle's) — not yet mirrored, fail closed.
        if selector_var_decl_has_declarator_holes(&needle) {
            bail!("datalog resolver: declarator-hole member target not yet handled");
        }
        // A single-declarator var-decl resolves at declarator granularity (so a
        // match inside a multi-declarator owner is counted) — production's path.
        if selector_single_var_declarator(&needle).is_some() {
            return resolve_var_declarator_member(
                module,
                &needle,
                request_id,
                export_name,
                selector,
            );
        }
        let target_binding_idx = match &selector.target_binding {
            Some(target_binding) => {
                let (target_item_idx, binding_idx) = selector_binding_location(
                    std::slice::from_ref(&needle),
                    request_id,
                    selector,
                    target_binding,
                )?;
                debug_assert_eq!(target_item_idx, 0, "single-statement needle");
                binding_idx
            }
            None => 0,
        };
        let needle_facts = item_facts(&needle)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
        let indices = matching_body_indices(module, &needle_facts, datalog_mode(selector))?;
        let [body_idx] = indices.as_slice() else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match for export \
                 `{export_name}` resolved to {} top-level statements; expected exactly one",
                indices.len(),
            );
        };
        let declared = declared_bindings(&module.body[*body_idx]);
        if selector.target_binding.is_none() && declared.len() != 1 {
            bail!(
                "datalog resolver: export `{export_name}` matched a statement declaring {} \
                 bindings; needs a single-declarator selector or target_binding",
                declared.len(),
            );
        }
        declared
            .into_iter()
            .nth(target_binding_idx)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: target binding index out of range"))
    }

    fn resolve_member_group(
        &self,
        _module: &Module,
        _request_id: &str,
        _selector: &AnonymousStatementSelector,
        _exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup> {
        bail!("datalog resolver: binding-group resolution not yet handled")
    }

    fn resolve_anonymous_groups(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        if selector.target_statements.is_some() {
            bail!(
                "datalog resolver: multi-statement (target_statements) selectors not yet handled"
            );
        }
        let needle = single_needle(request_id, selector)?;
        let needle_facts = item_facts(&needle)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
        Ok(
            matching_body_indices(module, &needle_facts, datalog_mode(selector))?
                .into_iter()
                .map(|body_idx| vec![body_idx])
                .collect(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct CollectingSink(RefCell<Vec<ResolverDisagreement>>);

    impl DisagreementSink for CollectingSink {
        fn record(&self, disagreement: ResolverDisagreement) {
            self.0.borrow_mut().push(disagreement);
        }
    }

    fn member(match_source: &str, target_binding: Option<&str>) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: target_binding.map(str::to_string),
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    fn module(src: &str) -> Module {
        js_ast::parse_js_module_ast("<test>", src).unwrap()
    }

    #[test]
    fn datalog_resolver_resolves_member_like_production() {
        js_ast::with_swc_globals(|| {
            let chunk = module("function alpha(n) { return n + 1; }\nconst beta = alpha(2);\n");
            // A function with a body the alpha selector matches structurally.
            let selector = member("function f(x) { return x + 1; }", Some("f"));
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("datalog resolves the function");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("production resolves the function");
            assert_eq!(datalog.binding_name, "alpha");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn differential_is_silent_when_datalog_agrees_with_production() {
        js_ast::with_swc_globals(|| {
            // A function member (not a var declarator — that path routes through
            // per-declarator matching the datalog resolver fail-closes on).
            let chunk = module("function alpha() { return 7; }\nfunction beta() { return 8; }\n");
            let selector = member("function f() { return 7; }", None);
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            let resolved = differential
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("primary resolves");
            assert_eq!(resolved.binding_name, "alpha");
            assert!(
                sink.0.borrow().is_empty(),
                "datalog and production must agree, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_declarator_inside_multi_declarator_owner() {
        js_ast::with_swc_globals(|| {
            // The target init lives in the second declarator of a multi-declarator
            // statement — only declarator-level matching finds it.
            let chunk = module("const a = 1, target = compute();\nconst other = 2;\n");
            let selector = member("const x = compute();", Some("x"));
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "X", &selector)
                .expect("datalog resolves the inner declarator");
            assert_eq!(datalog.binding_name, "target");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "X", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_var_declarator_categoricity_matches_production() {
        js_ast::with_swc_globals(|| {
            // The same init appears in two declarators (one inside a
            // multi-declarator owner) — ambiguous. The resolver must count both
            // (declarator-level), so it rejects exactly as production does; a
            // whole-statement match would miss the multi-declarator one and
            // spuriously resolve unique.
            let chunk = module("const a = 1, b = compute();\nconst c = compute();\n");
            let selector = member("const x = compute();", Some("x"));
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            assert!(
                differential
                    .resolve_member(&chunk, "test", "X", &selector)
                    .is_err(),
                "two declarators with the same init are ambiguous",
            );
            assert!(
                sink.0.borrow().is_empty(),
                "datalog must detect the same ambiguity as production, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_anonymous_statement() {
        js_ast::with_swc_globals(|| {
            let chunk = module("init();\nregister(widget);\nteardown();\n");
            let selector = member("register(ANYTHING);", None);
            let groups = DatalogResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("datalog resolves the anonymous statement");
            // matches exactly the `register(widget);` statement at body index 1.
            assert_eq!(groups, vec![vec![1]]);
            let production = AstWildcardResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("production resolves");
            assert_eq!(groups, production);
        });
    }
}
