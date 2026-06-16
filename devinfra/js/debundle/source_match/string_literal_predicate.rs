use super::*;

#[derive(Clone)]
pub(crate) enum StringLiteralPredicate {
    Exact(Wtf8Atom),
    Regex(Option<Regex>),
}

impl StringLiteralPredicate {
    fn regex(pattern: String) -> Self {
        Self::Regex(Regex::new(&pattern).ok())
    }

    pub(crate) fn matches(&self, candidate_value: &Wtf8Atom) -> bool {
        match self {
            Self::Exact(expected) => candidate_value == expected,
            Self::Regex(compiled) => compiled
                .as_ref()
                .is_some_and(|regex| regex.is_match(candidate_value.to_string_lossy().as_ref())),
        }
    }
}

#[derive(Default)]
pub(crate) struct CompiledStringLiteralRegexes {
    patterns: BTreeMap<String, StringLiteralPredicate>,
}

impl CompiledStringLiteralRegexes {
    pub(crate) fn for_module_item(needle: &ModuleItem) -> Self {
        let mut collector = StringLiteralRegexPatternCollector::default();
        needle.visit_with(&mut collector);
        Self {
            patterns: collector
                .patterns
                .into_iter()
                .map(|pattern| (pattern.clone(), StringLiteralPredicate::regex(pattern)))
                .collect(),
        }
    }

    pub(crate) fn matches(&self, pattern: &str, candidate_value: &Wtf8Atom) -> bool {
        self.patterns
            .get(pattern)
            .cloned()
            .unwrap_or_else(|| StringLiteralPredicate::regex(pattern.to_string()))
            .matches(candidate_value)
    }

    fn predicate(&self, pattern: &str) -> StringLiteralPredicate {
        self.patterns
            .get(pattern)
            .cloned()
            .unwrap_or_else(|| StringLiteralPredicate::regex(pattern.to_string()))
    }
}

#[derive(Default)]
pub(crate) struct StringLiteralRegexPatternCollector {
    patterns: BTreeSet<String>,
}

impl Visit for StringLiteralRegexPatternCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Some(pattern) = string_literal_regex_pattern(expr) {
            self.patterns.insert(pattern);
            return;
        }
        expr.visit_children_with(self);
    }
}

pub(crate) fn string_literal_predicate_for_expr(
    expr: &Expr,
    selector: &AnonymousStatementSelector,
    string_literal_regexes: &CompiledStringLiteralRegexes,
) -> Option<StringLiteralPredicate> {
    if let Some(pattern) = string_literal_regex_pattern(expr) {
        return Some(string_literal_regexes.predicate(&pattern));
    }
    let Expr::Lit(Lit::Str(str_)) = expr else {
        return None;
    };
    let value = &str_.value;
    if selector
        .wildcard_string_literals
        .contains(value.to_string_lossy().as_ref())
    {
        return None;
    }
    Some(StringLiteralPredicate::Exact(value.clone()))
}

pub(crate) fn string_literal_expr_value(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Lit(Lit::Str(str_)) => Some(str_.value.to_string_lossy().to_string()),
        _ => None,
    }
}

pub(crate) fn string_literal_expr_value_ref(expr: &Expr) -> Option<&Wtf8Atom> {
    match expr {
        Expr::Lit(Lit::Str(str_)) => Some(&str_.value),
        _ => None,
    }
}
