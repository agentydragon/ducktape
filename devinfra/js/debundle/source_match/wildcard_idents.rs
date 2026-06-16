use super::*;

#[derive(Default)]
pub(crate) struct WildcardIdents {
    pub(crate) expressions: BTreeSet<String>,
    pub(crate) statements: BTreeSet<String>,
    /// `STMT_LIST` statement-list hole names (reserved from
    /// alpha-canonicalization like the single-node holes).
    pub(crate) statement_lists: BTreeSet<String>,
    /// `DECLARATORS` variable-declarator-list hole names. These are
    /// pseudo-declarators and must not be alpha-canonicalized as real
    /// bindings.
    pub(crate) declarator_lists: BTreeSet<String>,
    /// `ARGS` argument-list hole names. These are pseudo-arguments and
    /// must route to ordered-subsequence argument matching.
    pub(crate) argument_lists: BTreeSet<String>,
    /// `OBJECT_PROPS` object-literal property-list hole names. These are
    /// pseudo-properties and must route to ordered-subsequence property
    /// matching.
    pub(crate) object_property_lists: BTreeSet<String>,
    /// Anonymous pattern hole names. These are binding positions and must
    /// not be alpha-canonicalized as real bindings.
    pub(crate) patterns: BTreeSet<String>,
    /// Whether the selector contains any `STR_LITERAL_MATCHING_RE(...)`
    /// expression predicate. This is not an identifier hole, but it
    /// changes expression shape (`CallExpr` in the selector,
    /// `Lit::Str` in the candidate), so the selector still needs the
    /// wildcard matcher.
    pub(crate) string_literal_regex_present: bool,
    /// Whether the selector contains any `CLASS_REST` class-member
    /// hole. The marker is a class field key (an `IdentName`, not an
    /// `Ident`), so it survives alpha-canonicalization without being
    /// reserved — only presence matters for routing.
    pub(crate) class_rest_present: bool,
    /// Whether the selector contains any `case CASE_REST:` switch-case
    /// hole. The marker is a `case` test identifier; only presence
    /// matters for routing to the structural matcher.
    pub(crate) case_rest_present: bool,
}

impl WildcardIdents {
    /// True when the selector carries no holes of any kind, so the
    /// caller can take the plain `eq_ignore_span` fast path. List holes
    /// count: a selector with only list holes still needs the structural
    /// matcher.
    pub(crate) fn is_empty(&self) -> bool {
        self.expressions.is_empty()
            && self.statements.is_empty()
            && self.statement_lists.is_empty()
            && self.declarator_lists.is_empty()
            && self.argument_lists.is_empty()
            && self.object_property_lists.is_empty()
            && self.patterns.is_empty()
            && !self.string_literal_regex_present
            && !self.class_rest_present
            && !self.case_rest_present
    }
}

pub(crate) fn wildcard_ident_names_for_module_items(needles: &[ModuleItem]) -> WildcardIdents {
    let mut collector = WildcardIdentCollector::default();
    for needle in needles {
        needle.visit_with(&mut collector);
    }
    collector.idents
}

pub(crate) fn wildcard_ident_names(needle: &ModuleItem) -> WildcardIdents {
    let mut collector = WildcardIdentCollector::default();
    needle.visit_with(&mut collector);
    collector.idents
}

#[derive(Default)]
pub(crate) struct WildcardIdentCollector {
    idents: WildcardIdents,
}

impl Visit for WildcardIdentCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if string_literal_regex_pattern(expr).is_some() {
            self.idents.string_literal_regex_present = true;
            return;
        }
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
            if hole_name == ANYTHING_HOLE_KEYWORD {
                self.idents.statements.insert(hole_name.to_string());
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

    fn visit_switch_case(&mut self, case: &SwitchCase) {
        if is_case_rest_hole(case) {
            self.idents.case_rest_present = true;
            return;
        }
        case.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Some(hole_name) = declarator_list_hole_name(declarator) {
            self.idents.declarator_lists.insert(hole_name.to_string());
            return;
        }
        declarator.visit_children_with(self);
    }

    fn visit_pat(&mut self, pat: &Pat) {
        if is_anything_pat_hole(pat) {
            self.idents
                .patterns
                .insert(ANYTHING_HOLE_KEYWORD.to_string());
            return;
        }
        pat.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, expr_or_spread: &ExprOrSpread) {
        if let Some(hole_name) = argument_list_hole_name(expr_or_spread) {
            self.idents.argument_lists.insert(hole_name.to_string());
            return;
        }
        expr_or_spread.visit_children_with(self);
    }

    fn visit_prop_or_spread(&mut self, prop_or_spread: &PropOrSpread) {
        if let Some(hole_name) = object_property_list_hole_name(prop_or_spread) {
            self.idents
                .object_property_lists
                .insert(hole_name.to_string());
            return;
        }
        prop_or_spread.visit_children_with(self);
    }
}
