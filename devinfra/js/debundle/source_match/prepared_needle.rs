use super::*;

/// Needle-derived matching state hoisted out of the per-candidate
/// loops. The previous `module_items_match` recomputed the needle's
/// wildcard-ident set — and, in alpha mode without wildcards, cloned
/// and re-canonicalized BOTH trees — once per candidate comparison;
/// the needle side of that work is invariant across candidates.
pub(crate) struct PreparedNeedle<'a> {
    pub(crate) needle: &'a ModuleItem,
    pub(crate) selector: &'a AnonymousStatementSelector,
    pub(crate) wildcard_idents: WildcardIdents,
    pub(crate) string_literal_regexes: CompiledStringLiteralRegexes,
    pub(crate) alpha: bool,
    /// Neither string-literal nor syntactic-hole wildcards/predicates
    /// present — the plain structural-equality fast path applies.
    pub(crate) no_wildcards: bool,
    /// The needle pre-canonicalized once for the no-wildcard alpha
    /// path (`None` otherwise).
    pub(crate) canonical_needle: Option<ModuleItem>,
}

impl<'a> PreparedNeedle<'a> {
    pub(crate) fn new(needle: &'a ModuleItem, selector: &'a AnonymousStatementSelector) -> Self {
        SyntaxContext::within_ignored_ctxt(|| {
            let wildcard_idents = wildcard_ident_names(needle);
            let string_literal_regexes = CompiledStringLiteralRegexes::for_module_item(needle);
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
                string_literal_regexes,
                alpha,
                no_wildcards,
                canonical_needle,
            }
        })
    }

    pub(crate) fn matches(&self, candidate: &ModuleItem) -> bool {
        SyntaxContext::within_ignored_ctxt(|| {
            if self.no_wildcards {
                // No wildcards: plain structural equality. The cheap
                // shape prefilter rejects most candidates before the
                // alpha path's per-candidate clone + canonicalize.
                if !no_wildcard_shape_prefilter(self.needle, candidate) {
                    return false;
                }
                // Without wildcards the two trees have identical shape,
                // so scoped alpha-canonicalization can stay on the cheap
                // clone-and-compare path.
                if let Some(canonical_needle) = &self.canonical_needle {
                    if alpha_shorthand_sensitive(self.needle)
                        || alpha_shorthand_sensitive(candidate)
                    {
                        return AstWildcardMatcher::new(
                            self.selector,
                            &self.wildcard_idents,
                            self.alpha,
                        )
                        .match_module_item(self.needle, candidate);
                    }
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
            AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            )
            .match_module_item(self.needle, candidate)
        })
    }

    pub(crate) fn matches_single_var_declarator(
        &self,
        candidate_item: &ModuleItem,
        candidate_declarator: &VarDeclarator,
    ) -> bool {
        SyntaxContext::within_ignored_ctxt(|| {
            let mut matcher = AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            );
            matcher.match_single_var_declarator_item(
                self.needle,
                candidate_item,
                candidate_declarator,
            )
        })
    }

    pub(crate) fn matches_with_prebound_binding(
        &self,
        candidate: &ModuleItem,
        selector_binding: &str,
        candidate_binding: &str,
    ) -> bool {
        SyntaxContext::within_ignored_ctxt(|| {
            let mut matcher = AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            );
            matcher.prebind_alpha_sym(selector_binding, candidate_binding)
                && matcher.match_module_item(self.needle, candidate)
        })
    }

    pub(crate) fn var_declarator_alignment(
        &self,
        needle: &VarDecl,
        candidate: &VarDecl,
    ) -> Option<Vec<Option<usize>>> {
        SyntaxContext::within_ignored_ctxt(|| {
            let mut matcher = AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            );
            matcher.match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
        })
    }

    pub(crate) fn var_declarator_alignment_with_prebound_binding(
        &self,
        needle: &VarDecl,
        candidate: &VarDecl,
        selector_binding: &str,
        candidate_binding: &str,
    ) -> Option<Vec<Option<usize>>> {
        SyntaxContext::within_ignored_ctxt(|| {
            let mut matcher = AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            );
            if !matcher.prebind_alpha_sym(selector_binding, candidate_binding) {
                return None;
            }
            matcher.match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
        })
    }
}

/// Cheap top-level shape check, sound only in **no-wildcard** mode
/// (where matching is structural equality): a `false` return proves
/// the full comparison cannot succeed. Compares the item/statement/
/// declaration discriminants and, for variable declarations, the
/// `var`/`let`/`const` kind and declarator count.
pub(crate) fn no_wildcard_shape_prefilter(needle: &ModuleItem, candidate: &ModuleItem) -> bool {
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

pub(crate) fn alpha_shorthand_sensitive(item: &ModuleItem) -> bool {
    let mut visitor = AlphaShorthandSensitiveVisitor::default();
    item.visit_with(&mut visitor);
    visitor.found
}

#[derive(Default)]
pub(crate) struct AlphaShorthandSensitiveVisitor {
    found: bool,
}

impl Visit for AlphaShorthandSensitiveVisitor {
    fn visit_prop(&mut self, prop: &Prop) {
        if self.found {
            return;
        }
        match prop {
            Prop::Shorthand(_) => {
                self.found = true;
            }
            Prop::KeyValue(prop) if key_value_prop_ident_value(prop).is_some() => {
                self.found = true;
            }
            _ => prop.visit_children_with(self),
        }
    }

    fn visit_object_pat_prop(&mut self, prop: &ObjectPatProp) {
        if self.found {
            return;
        }
        match prop {
            ObjectPatProp::Assign(prop) if prop.value.is_none() => {
                self.found = true;
            }
            ObjectPatProp::KeyValue(prop) if key_value_pat_binding_ident_value(prop).is_some() => {
                self.found = true;
            }
            _ => prop.visit_children_with(self),
        }
    }
}
