use super::*;

#[derive(Clone, Default)]
pub(crate) struct WildcardReplacements {
    strings: BTreeMap<String, Wtf8Atom>,
    expressions: BTreeMap<String, Expr>,
    statements: BTreeMap<String, Stmt>,
}

#[derive(Clone, Default)]
pub(crate) struct AlphaMatchScope {
    forward: BTreeMap<Atom, Atom>,
    backward: BTreeMap<Atom, Atom>,
}

pub(crate) struct AstWildcardMatcher<'a> {
    selector: &'a AnonymousStatementSelector,
    wildcard_idents: &'a WildcardIdents,
    string_literal_regexes: Option<&'a CompiledStringLiteralRegexes>,
    replacements: WildcardReplacements,
    /// Whether value/binding identifiers are alpha-renamable. When true,
    /// identifier equality is tracked as a bijection built incrementally
    /// at structurally-corresponding positions, instead of pre-renaming
    /// both trees. Because holes accept (and never recurse into) the
    /// subtrees they absorb, absorbed identifiers never enter the
    /// bijection — so a hole no longer desyncs the numbering of the nodes
    /// after it, and there is no per-comparison clone + canonicalize.
    alpha: bool,
    alpha_scopes: Vec<AlphaMatchScope>,
}

/// A clone of the matcher's mutable binding state, captured before a
/// tentative segment placement during ordered-subsequence (multi-hole)
/// list matching and restored when that placement fails — so a
/// half-applied segment never leaks identifier or wildcard bindings into
/// the next attempt.
#[derive(Clone)]
pub(crate) struct MatcherState {
    replacements: WildcardReplacements,
    alpha_scopes: Vec<AlphaMatchScope>,
}

/// Loop-invariant inputs for the recursive ordered-subsequence search in
/// [`AstWildcardMatcher::match_list_with_holes`]. Only the `seg_idx` and
/// `cand_min` cursor arguments change as the search descends.
pub(crate) struct SegmentSearch<'a, T> {
    pub(crate) needle: &'a [T],
    pub(crate) candidate: &'a [T],
    /// `(needle_start, len)` of each maximal fixed (non-hole) run, in
    /// source order.
    pub(crate) segments: &'a [(usize, usize)],
    /// Whether the first segment is pinned to the candidate's start
    /// (true unless a hole leads the needle list).
    pub(crate) anchored_left: bool,
    /// Whether the last segment is pinned to the candidate's end (true
    /// unless a hole trails the needle list).
    pub(crate) anchored_right: bool,
}

impl<'a> AstWildcardMatcher<'a> {
    pub(crate) fn new(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        alpha: bool,
    ) -> Self {
        Self::new_impl(selector, wildcard_idents, None, alpha)
    }

    pub(crate) fn new_with_string_literal_regexes(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        string_literal_regexes: &'a CompiledStringLiteralRegexes,
        alpha: bool,
    ) -> Self {
        Self::new_impl(
            selector,
            wildcard_idents,
            Some(string_literal_regexes),
            alpha,
        )
    }

    fn new_impl(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        string_literal_regexes: Option<&'a CompiledStringLiteralRegexes>,
        alpha: bool,
    ) -> Self {
        Self {
            selector,
            wildcard_idents,
            string_literal_regexes,
            replacements: WildcardReplacements::default(),
            alpha,
            alpha_scopes: vec![AlphaMatchScope::default()],
        }
    }

    fn with_alpha_scope(&mut self, f: impl FnOnce(&mut Self) -> bool) -> bool {
        if !self.alpha {
            return f(self);
        }
        self.alpha_scopes.push(AlphaMatchScope::default());
        let ok = f(self);
        self.alpha_scopes.pop();
        ok
    }

    /// Match two identifier references. In alpha mode, references first
    /// consult the visible lexical scope stack, then create a mapping in
    /// the current scope if neither side is known yet.
    fn match_sym(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        for scope in self.alpha_scopes.iter().rev() {
            if let Some(mapped) = scope.forward.get(needle) {
                return mapped == candidate;
            }
            if scope.backward.contains_key(candidate) {
                return false;
            }
        }
        if !self.alpha {
            return needle == candidate;
        }
        self.bind_alpha_sym_in_current_scope(needle, candidate)
    }

    /// Match two binding identifiers. Unlike references, a binding is
    /// allowed to shadow an outer binding with the same spelling, so only
    /// the current lexical frame is consulted before creating the pair.
    fn match_binding_sym(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        let scope = self
            .alpha_scopes
            .last()
            .expect("alpha matcher always has a root scope");
        if let Some(mapped) = scope.forward.get(needle) {
            return mapped == candidate;
        }
        if scope.backward.contains_key(candidate) {
            return false;
        }
        if !self.alpha {
            return needle == candidate;
        }
        self.bind_alpha_sym_in_current_scope(needle, candidate)
    }

    pub(crate) fn prebind_alpha_sym(&mut self, needle: &str, candidate: &str) -> bool {
        self.bind_alpha_sym_in_current_scope(&Atom::from(needle), &Atom::from(candidate))
    }

    fn bind_alpha_sym_in_current_scope(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        let scope = self
            .alpha_scopes
            .last_mut()
            .expect("alpha matcher always has a root scope");
        match (scope.forward.get(needle), scope.backward.get(candidate)) {
            (Some(mapped), _) => mapped == candidate,
            (None, Some(_)) => false,
            (None, None) => {
                scope.forward.insert(needle.clone(), candidate.clone());
                scope.backward.insert(candidate.clone(), needle.clone());
                true
            }
        }
    }

    fn match_ident(&mut self, needle: &Ident, candidate: &Ident) -> bool {
        needle.optional == candidate.optional && self.match_sym(&needle.sym, &candidate.sym)
    }

    fn match_binding_ident(&mut self, needle: &Ident, candidate: &Ident) -> bool {
        needle.optional == candidate.optional && self.match_binding_sym(&needle.sym, &candidate.sym)
    }

    fn match_binding_binding_ident(
        &mut self,
        needle: &BindingIdent,
        candidate: &BindingIdent,
    ) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && self.match_binding_ident(&needle.id, &candidate.id)
    }

    fn match_binding_ident_as_ref(
        &mut self,
        needle: &BindingIdent,
        candidate: &BindingIdent,
    ) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && self.match_ident(&needle.id, &candidate.id)
    }

    fn match_opt_ident(&mut self, needle: &Option<Ident>, candidate: &Option<Ident>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_ident(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_opt_binding_ident(
        &mut self,
        needle: &Option<Ident>,
        candidate: &Option<Ident>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_binding_ident(needle, candidate),
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
        // The bare keyword `EXPR` (and universal `ANYTHING`) is an
        // anonymous wildcard: every occurrence matches independently, so
        // authors don't have to mint a unique name per placeholder.
        // Named holes (`EXPR_FOO`) keep their cross-occurrence equality.
        if hole_is_anonymous(wildcard, EXPR_HOLE_KEYWORD) || wildcard == ANYTHING_HOLE_KEYWORD {
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
        // Bare `STMT` and universal `ANYTHING` are anonymous; see
        // [`Self::bind_expr`].
        if hole_is_anonymous(wildcard, STMT_HOLE_KEYWORD) || wildcard == ANYTHING_HOLE_KEYWORD {
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

    pub(crate) fn match_module_item(
        &mut self,
        needle: &ModuleItem,
        candidate: &ModuleItem,
    ) -> bool {
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

    pub(crate) fn match_single_var_declarator_item(
        &mut self,
        needle: &ModuleItem,
        candidate_item: &ModuleItem,
        candidate_declarator: &VarDeclarator,
    ) -> bool {
        match (needle, candidate_item) {
            (
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(needle_var))),
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(candidate_var))),
            ) => self.match_single_var_declarator_decl(
                needle_var,
                candidate_var,
                candidate_declarator,
            ),
            (
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(needle_export)),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(candidate_export)),
            ) => match (&needle_export.decl, &candidate_export.decl) {
                (Decl::Var(needle_var), Decl::Var(candidate_var)) => self
                    .match_single_var_declarator_decl(
                        needle_var,
                        candidate_var,
                        candidate_declarator,
                    ),
                _ => false,
            },
            _ => false,
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

    pub(crate) fn match_decl(&mut self, needle: &Decl, candidate: &Decl) -> bool {
        match (needle, candidate) {
            (Decl::Var(needle), Decl::Var(candidate)) => self.match_var_decl(needle, candidate),
            (Decl::Fn(needle), Decl::Fn(candidate)) => {
                self.match_binding_ident(&needle.ident, &candidate.ident)
                    && needle.declare == candidate.declare
                    && self.match_function(&needle.function, &candidate.function)
            }
            (Decl::Class(needle), Decl::Class(candidate)) => {
                self.match_binding_ident(&needle.ident, &candidate.ident)
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
                self.match_opt_binding_ident(&needle.ident, &candidate.ident)
                    && self.match_class(&needle.class, &candidate.class)
            }
            (DefaultDecl::Fn(needle), DefaultDecl::Fn(candidate)) => {
                self.match_opt_binding_ident(&needle.ident, &candidate.ident)
                    && self.match_function(&needle.function, &candidate.function)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_function(&mut self, needle: &Function, candidate: &Function) -> bool {
        self.match_slice(
            &needle.decorators,
            &candidate.decorators,
            Self::match_decorator,
        ) && needle.is_generator == candidate.is_generator
            && needle.is_async == candidate.is_async
            && needle.type_params.eq_ignore_span(&candidate.type_params)
            && needle.return_type.eq_ignore_span(&candidate.return_type)
            && self.with_alpha_scope(|matcher| {
                matcher.match_slice(&needle.params, &candidate.params, Self::match_param)
                    && matcher.match_option_block_stmt(&needle.body, &candidate.body)
            })
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
            && self
                .match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
                .is_some()
    }

    fn match_single_var_declarator_decl(
        &mut self,
        needle: &VarDecl,
        candidate: &VarDecl,
        candidate_declarator: &VarDeclarator,
    ) -> bool {
        needle.kind == candidate.kind
            && needle.declare == candidate.declare
            && needle.decls.len() == 1
            && self
                .match_var_declarator_slice_with_alignment(
                    &needle.decls,
                    std::slice::from_ref(candidate_declarator),
                )
                .is_some()
    }

    fn match_using_decl(&mut self, needle: &UsingDecl, candidate: &UsingDecl) -> bool {
        needle.is_await == candidate.is_await
            && self.match_slice(&needle.decls, &candidate.decls, Self::match_var_declarator)
    }

    pub(crate) fn match_var_declarator(
        &mut self,
        needle: &VarDeclarator,
        candidate: &VarDeclarator,
    ) -> bool {
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
                    && self.match_switch_case_slice(&needle.cases, &candidate.cases)
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
        // Parentheses are syntactically insignificant grouping; see through them on
        // either side. The renderer drops redundant parens when holing (`hole_expr`'s
        // `Expr::Paren` arm), so a holed needle — e.g. a holed comma sequence, whose
        // source form is the parenthesized `(a, b, c)` — must still match the
        // parenthesized source expression; hand-written selectors may likewise include
        // or omit them. Strict superset of the former paren-vs-paren-only arm, and it
        // keeps selectors robust to a rebuild adding or dropping redundant parens.
        if let Expr::Paren(needle) = needle {
            return self.match_expr(&needle.expr, candidate);
        }
        if let Expr::Paren(candidate) = candidate {
            return self.match_expr(needle, &candidate.expr);
        }
        if let Some(pattern) = string_literal_regex_pattern(needle) {
            return match candidate {
                Expr::Lit(Lit::Str(candidate)) => self.string_literal_regexes.map_or_else(
                    || string_literal_matches_regex(&pattern, &candidate.value),
                    |regexes| regexes.matches(&pattern, &candidate.value),
                ),
                _ => false,
            };
        }
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
                self.match_prop_or_spread_slice(&needle.props, &candidate.props)
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
                needle.is_async == candidate.is_async
                    && needle.is_generator == candidate.is_generator
                    && needle.type_params.eq_ignore_span(&candidate.type_params)
                    && needle.return_type.eq_ignore_span(&candidate.return_type)
                    && self.with_alpha_scope(|matcher| {
                        matcher.match_slice(&needle.params, &candidate.params, Self::match_pat)
                            && matcher.match_block_stmt_or_expr(&needle.body, &candidate.body)
                    })
            }
            (Expr::Yield(needle), Expr::Yield(candidate)) => {
                needle.delegate == candidate.delegate
                    && self.match_option_box_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Await(needle), Expr::Await(candidate)) => {
                self.match_expr(&needle.arg, &candidate.arg)
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
        self.with_alpha_scope(|matcher| {
            matcher.match_option_pat(&needle.param, &candidate.param)
                && matcher.match_block_stmt(&needle.body, &candidate.body)
        })
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
            (ForHead::Pat(needle), ForHead::Pat(candidate)) => {
                self.match_ref_pat(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_pat(&mut self, needle: &Pat, candidate: &Pat) -> bool {
        if is_anything_pat_hole(needle)
            && self
                .wildcard_idents
                .patterns
                .contains(ANYTHING_HOLE_KEYWORD)
        {
            return true;
        }
        match (needle, candidate) {
            (Pat::Array(needle), Pat::Array(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(&needle.elems, &candidate.elems, Self::match_option_pat)
            }
            (Pat::Object(needle), Pat::Object(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_object_pat_prop_slice(&needle.props, &candidate.props)
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
                self.match_binding_binding_ident(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_assign_pat(&mut self, needle: &AssignPat, candidate: &AssignPat) -> bool {
        self.match_pat(&needle.left, &candidate.left)
            && self.match_expr(&needle.right, &candidate.right)
    }

    /// Match a destructuring object-pattern property list. An `OBJECT_PROPS`
    /// hole (`const { OBJECT_PROPS, x } = …`) splits the needle into fixed
    /// segments matched as an ordered subsequence with gaps (see
    /// [`Self::match_list_with_holes`]); with no hole this is an exact
    /// element-wise match. Mirrors [`Self::match_prop_or_spread_slice`] for the
    /// object-literal case.
    fn match_object_pat_prop_slice(
        &mut self,
        needle: &[ObjectPatProp],
        candidate: &[ObjectPatProp],
    ) -> bool {
        if needle
            .iter()
            .any(|prop| object_pat_prop_list_hole_name(prop).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |prop| object_pat_prop_list_hole_name(prop).is_some(),
                Self::match_object_pat_prop,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_object_pat_prop)
        }
    }

    fn match_object_pat_prop(&mut self, needle: &ObjectPatProp, candidate: &ObjectPatProp) -> bool {
        match (needle, candidate) {
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_prop_name_exact(&needle.key, &candidate.key)
                    && self.match_pat(&needle.value, &candidate.value)
            }
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_key_value_pat_against_assign_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_assign_pat_against_key_value_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::Assign(candidate)) => {
                // A shorthand destructure key is a stable source property name:
                // it matches by exact spelling, like a KeyValue pattern key
                // (`match_prop_name_exact`), an object-literal key, and the
                // KeyValue↔Assign cross-forms below — never as an
                // alpha-renameable binding. `match_binding_binding_ident` then
                // registers the introduced local in the alpha scope so later
                // references resolve. Without the exact-spelling gate a wide
                // `{ OBJECT_PROPS, foo }` selector would alpha-bind `foo` to any
                // sibling's property and stop discriminating.
                needle.key.id.sym == candidate.key.id.sym
                    && self.match_binding_binding_ident(&needle.key, &candidate.key)
                    && self.match_option_box_expr(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Rest(needle), ObjectPatProp::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_pat(&needle.arg, &candidate.arg)
            }
            _ => false,
        }
    }

    fn match_ref_pat(&mut self, needle: &Pat, candidate: &Pat) -> bool {
        if is_anything_pat_hole(needle)
            && self
                .wildcard_idents
                .patterns
                .contains(ANYTHING_HOLE_KEYWORD)
        {
            return true;
        }
        match (needle, candidate) {
            (Pat::Array(needle), Pat::Array(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(&needle.elems, &candidate.elems, Self::match_option_ref_pat)
            }
            (Pat::Object(needle), Pat::Object(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ref_object_pat_prop_slice(&needle.props, &candidate.props)
            }
            (Pat::Assign(needle), Pat::Assign(candidate)) => {
                self.match_ref_assign_pat(needle, candidate)
            }
            (Pat::Rest(needle), Pat::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ref_pat(&needle.arg, &candidate.arg)
            }
            (Pat::Expr(needle), Pat::Expr(candidate)) => self.match_expr(needle, candidate),
            (Pat::Ident(needle), Pat::Ident(candidate)) => {
                self.match_binding_ident_as_ref(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_option_ref_pat(&mut self, needle: &Option<Pat>, candidate: &Option<Pat>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_ref_pat(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_ref_assign_pat(&mut self, needle: &AssignPat, candidate: &AssignPat) -> bool {
        self.match_ref_pat(&needle.left, &candidate.left)
            && self.match_expr(&needle.right, &candidate.right)
    }

    /// Reference-position analog of [`Self::match_object_pat_prop_slice`] (an
    /// assignment-target destructure such as `({ OBJECT_PROPS, x } = …)`).
    fn match_ref_object_pat_prop_slice(
        &mut self,
        needle: &[ObjectPatProp],
        candidate: &[ObjectPatProp],
    ) -> bool {
        if needle
            .iter()
            .any(|prop| object_pat_prop_list_hole_name(prop).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |prop| object_pat_prop_list_hole_name(prop).is_some(),
                Self::match_ref_object_pat_prop,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_ref_object_pat_prop)
        }
    }

    fn match_ref_object_pat_prop(
        &mut self,
        needle: &ObjectPatProp,
        candidate: &ObjectPatProp,
    ) -> bool {
        match (needle, candidate) {
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_prop_name_exact(&needle.key, &candidate.key)
                    && self.match_ref_pat(&needle.value, &candidate.value)
            }
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_key_value_ref_pat_against_assign_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_assign_pat_against_key_value_ref_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::Assign(candidate)) => {
                // The destructure key is a stable property name (exact spelling);
                // see the binding-position note in `match_object_pat_prop`.
                needle.key.id.sym == candidate.key.id.sym
                    && self.match_binding_ident_as_ref(&needle.key, &candidate.key)
                    && self.match_option_box_expr(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Rest(needle), ObjectPatProp::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ref_pat(&needle.arg, &candidate.arg)
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
            && self.match_expr_or_spread_slice(&needle.args, &candidate.args)
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

    /// Match a call/new argument list. `ARGS` / `ARGS_*` holes split
    /// the needle into fixed argument segments matched as an ordered
    /// subsequence with gaps; with no hole this is an exact element-wise
    /// match.
    fn match_expr_or_spread_slice(
        &mut self,
        needle: &[ExprOrSpread],
        candidate: &[ExprOrSpread],
    ) -> bool {
        if needle
            .iter()
            .any(|arg| argument_list_hole_name(arg).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |arg| argument_list_hole_name(arg).is_some(),
                Self::match_expr_or_spread,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_expr_or_spread)
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

    /// Match object literal properties. `OBJECT_PROPS` /
    /// `OBJECT_PROPS_*` or anonymous `ANYTHING` shorthand properties
    /// split the needle into fixed property segments matched as an
    /// ordered subsequence with gaps; with no hole this is an exact
    /// element-wise match.
    fn match_prop_or_spread_slice(
        &mut self,
        needle: &[PropOrSpread],
        candidate: &[PropOrSpread],
    ) -> bool {
        if needle
            .iter()
            .any(|prop| object_property_list_hole_name(prop).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |prop| object_property_list_hole_name(prop).is_some(),
                Self::match_prop_or_spread,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_prop_or_spread)
        }
    }

    fn match_prop(&mut self, needle: &Prop, candidate: &Prop) -> bool {
        match (needle, candidate) {
            (Prop::Shorthand(needle), Prop::Shorthand(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (Prop::Shorthand(needle), Prop::KeyValue(candidate)) => {
                self.match_shorthand_against_key_value_prop(needle, candidate)
            }
            (Prop::KeyValue(needle), Prop::Shorthand(candidate)) => {
                self.match_key_value_against_shorthand_prop(needle, candidate)
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
                    && self.with_alpha_scope(|matcher| {
                        matcher.match_pat(&needle.param, &candidate.param)
                            && matcher.match_option_block_stmt(&needle.body, &candidate.body)
                    })
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

    fn match_prop_name_exact(&mut self, needle: &PropName, candidate: &PropName) -> bool {
        match (needle, candidate) {
            (PropName::Computed(needle), PropName::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_shorthand_against_key_value_prop(
        &mut self,
        needle: &Ident,
        candidate: &KeyValueProp,
    ) -> bool {
        self.key_value_prop_is_shorthand_equivalent(candidate, needle)
            && self.match_ident(
                needle,
                key_value_prop_ident_value(candidate)
                    .expect("checked by key_value_prop_is_shorthand_equivalent"),
            )
    }

    fn match_key_value_against_shorthand_prop(
        &mut self,
        needle: &KeyValueProp,
        candidate: &Ident,
    ) -> bool {
        self.key_value_prop_is_shorthand_equivalent(needle, candidate)
            && self.match_ident(
                key_value_prop_ident_value(needle)
                    .expect("checked by key_value_prop_is_shorthand_equivalent"),
                candidate,
            )
    }

    fn key_value_prop_is_shorthand_equivalent(
        &mut self,
        prop: &KeyValueProp,
        shorthand: &Ident,
    ) -> bool {
        prop_name_matches_ident_key(&prop.key, shorthand)
            && key_value_prop_ident_value(prop).is_some_and(|value| {
                value.sym == shorthand.sym && value.optional == shorthand.optional
            })
    }

    fn match_key_value_pat_against_assign_pat(
        &mut self,
        needle: &KeyValuePatProp,
        candidate: &AssignPatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(needle, candidate)
            && self.match_pat(&needle.value, &Pat::Ident(candidate.key.clone()))
    }

    fn match_assign_pat_against_key_value_pat(
        &mut self,
        needle: &AssignPatProp,
        candidate: &KeyValuePatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(candidate, needle)
            && self.match_pat(&Pat::Ident(needle.key.clone()), &candidate.value)
    }

    fn match_key_value_ref_pat_against_assign_pat(
        &mut self,
        needle: &KeyValuePatProp,
        candidate: &AssignPatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(needle, candidate)
            && self.match_ref_pat(&needle.value, &Pat::Ident(candidate.key.clone()))
    }

    fn match_assign_pat_against_key_value_ref_pat(
        &mut self,
        needle: &AssignPatProp,
        candidate: &KeyValuePatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(candidate, needle)
            && self.match_ref_pat(&Pat::Ident(needle.key.clone()), &candidate.value)
    }

    fn key_value_pat_is_assign_equivalent(
        &mut self,
        prop: &KeyValuePatProp,
        shorthand: &AssignPatProp,
    ) -> bool {
        shorthand.value.is_none()
            && prop_name_matches_binding_key(&prop.key, &shorthand.key)
            && key_value_pat_binding_ident_value(prop).is_some_and(|value| {
                value.id.sym == shorthand.key.sym
                    && value.id.optional == shorthand.key.optional
                    && value.type_ann.is_none()
            })
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
                self.match_slice(&needle.elems, &candidate.elems, Self::match_option_ref_pat)
            }
            (AssignTargetPat::Object(needle), AssignTargetPat::Object(candidate)) => self
                .match_slice(
                    &needle.props,
                    &candidate.props,
                    Self::match_ref_object_pat_prop,
                ),
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
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ident(&needle.id, &candidate.id)
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
                    && self.match_expr_or_spread_slice(&needle.args, &candidate.args)
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

    pub(crate) fn match_class_member(
        &mut self,
        needle: &ClassMember,
        candidate: &ClassMember,
    ) -> bool {
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
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_optional == candidate.is_optional
            && self.with_alpha_scope(|matcher| {
                matcher.match_slice(
                    &needle.params,
                    &candidate.params,
                    Self::match_param_or_ts_param_prop,
                ) && matcher.match_option_block_stmt(&needle.body, &candidate.body)
            })
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
                self.match_binding_binding_ident(needle, candidate)
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
            (Some(needle), Some(candidate)) => self.match_expr_or_spread_slice(needle, candidate),
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

    /// Match a `switch` case list. `case CASE_REST:` holes split the
    /// needle into fixed segments matched as an ordered subsequence with
    /// gaps (see [`Self::match_list_with_holes`]); with no hole this is an
    /// exact element-wise match. The run absorbed by a hole may contain
    /// `case`/`default` clauses freely.
    fn match_switch_case_slice(&mut self, needle: &[SwitchCase], candidate: &[SwitchCase]) -> bool {
        if needle.iter().any(is_case_rest_hole) {
            self.match_list_with_holes(
                needle,
                candidate,
                is_case_rest_hole,
                Self::match_switch_case,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_switch_case)
        }
    }

    /// Match a variable declarator list. `DECLARATORS` /
    /// `DECLARATORS_*` holes split the needle into fixed declarator
    /// segments matched as an ordered subsequence with gaps. The return
    /// value maps each needle declarator index to the candidate
    /// declarator index it matched; list-hole entries are `None`.
    pub(crate) fn match_var_declarator_slice_with_alignment(
        &mut self,
        needle: &[VarDeclarator],
        candidate: &[VarDeclarator],
    ) -> Option<Vec<Option<usize>>> {
        let mut alignment = vec![None; needle.len()];
        if !needle
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
        {
            if needle.len() != candidate.len() {
                return None;
            }
            for (idx, (needle, candidate)) in needle.iter().zip(candidate).enumerate() {
                if !self.match_var_declarator(needle, candidate) {
                    return None;
                }
                alignment[idx] = Some(idx);
            }
            return Some(alignment);
        }

        let mut segments: Vec<(usize, usize)> = Vec::new();
        let mut idx = 0;
        while idx < needle.len() {
            if declarator_list_hole_name(&needle[idx]).is_some() {
                idx += 1;
                continue;
            }
            let start = idx;
            while idx < needle.len() && declarator_list_hole_name(&needle[idx]).is_none() {
                idx += 1;
            }
            segments.push((start, idx - start));
        }
        if segments.is_empty() {
            return Some(alignment);
        }

        let search = SegmentSearch {
            needle,
            candidate,
            segments: &segments,
            anchored_left: declarator_list_hole_name(&needle[0]).is_none(),
            anchored_right: declarator_list_hole_name(&needle[needle.len() - 1]).is_none(),
        };
        self.place_var_declarator_segments(&search, 0, 0, &mut alignment)
            .then_some(alignment)
    }

    fn place_var_declarator_segments(
        &mut self,
        search: &SegmentSearch<VarDeclarator>,
        seg_idx: usize,
        cand_min: usize,
        alignment: &mut [Option<usize>],
    ) -> bool {
        let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
            return true;
        };
        let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
        let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
            return false;
        };
        let mut lo = cand_min;
        let mut hi = latest_start;
        if seg_idx == 0 && search.anchored_left {
            hi = hi.min(0);
        }
        if seg_idx == search.segments.len() - 1 && search.anchored_right {
            lo = lo.max(latest_start);
        }
        for start in lo..=hi {
            let snapshot = self.snapshot();
            let alignment_snapshot = alignment.to_vec();
            let mut segment_ok = true;
            for offset in 0..seg_len {
                let needle_idx = needle_start + offset;
                let candidate_idx = start + offset;
                if !self.match_var_declarator(
                    &search.needle[needle_idx],
                    &search.candidate[candidate_idx],
                ) {
                    segment_ok = false;
                    break;
                }
                alignment[needle_idx] = Some(candidate_idx);
            }
            if segment_ok
                && self.place_var_declarator_segments(
                    search,
                    seg_idx + 1,
                    start + seg_len,
                    alignment,
                )
            {
                return true;
            }
            self.restore(snapshot);
            alignment.copy_from_slice(&alignment_snapshot);
        }
        false
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

    pub(crate) fn snapshot(&self) -> MatcherState {
        MatcherState {
            replacements: self.replacements.clone(),
            alpha_scopes: self.alpha_scopes.clone(),
        }
    }

    pub(crate) fn restore(&mut self, state: MatcherState) {
        self.replacements = state.replacements;
        self.alpha_scopes = state.alpha_scopes;
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

pub(crate) fn place_module_item_segments(
    matcher: &mut AstWildcardMatcher<'_>,
    search: &SegmentSearch<'_, ModuleItem>,
    seg_idx: usize,
    cand_min: usize,
    alignment: &mut [Option<usize>],
    matches: &mut Vec<Vec<Option<usize>>>,
) {
    let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
        matches.push(alignment.to_vec());
        return;
    };
    let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
        return;
    };
    for start in cand_min..=latest_start {
        let snapshot = matcher.snapshot();
        let alignment_snapshot = alignment.to_vec();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            let needle_idx = needle_start + offset;
            let candidate_idx = start + offset;
            if !matcher
                .match_module_item(&search.needle[needle_idx], &search.candidate[candidate_idx])
            {
                segment_ok = false;
                break;
            }
            alignment[needle_idx] = Some(candidate_idx);
        }
        if segment_ok {
            place_module_item_segments(
                matcher,
                search,
                seg_idx + 1,
                start + seg_len,
                alignment,
                matches,
            );
        }
        matcher.restore(snapshot);
        alignment.copy_from_slice(&alignment_snapshot);
    }
}
