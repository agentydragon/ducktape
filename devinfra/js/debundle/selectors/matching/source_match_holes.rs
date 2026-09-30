//! Syntactic-hole keyword vocabulary shared across the source-match language.
//!
//! `source_match` interprets these holes when resolving selectors, the
//! `selector_codemod` minimizer emits them when rendering candidate
//! selectors, and `selector_candidate_index` recognizes them when building
//! its prefilter. Keeping the keyword spellings in one place stops producer
//! and consumer code from drifting apart on string literals.
//!
//! Holes may be bare (`EXPR`) or carry a suffixed readability label
//! (`EXPR_value`). The suffix is cosmetic and never binds: two occurrences with
//! the same suffix still match independently.
//!
//! `EXPR` matches one arbitrary expression and `STMT` one arbitrary
//! statement. `ARGS`, `STMT_LIST`, `ARRAY_ELEMENTS`, `CASE_REST`, and
//! `DECLARATORS` are variable-length list holes: `ARGS` absorbs a run of
//! call/new arguments, `STMT_LIST` absorbs a run of block statements (or
//! top-level anonymous selector statements), `ARRAY_ELEMENTS` absorbs a run of
//! array-literal elements (including spreads/elisions), `CASE_REST` absorbs a
//! run of `case`/`default` clauses inside one `switch` statement, and
//! `DECLARATORS` absorbs a run of variable declarators inside one
//! `var`/`let`/`const` declaration.
//!
//! `ARRAY_ELEMENTS` is spelled as a bare identifier element
//! (`[firstStable, ARRAY_ELEMENTS, lastStable]`): an array has no
//! shorthand-property form, and `ANYTHING` in array-element position is one
//! `EXPR` (a single element), not a run — so the array-run hole has only its
//! typed `ARRAY_ELEMENTS` spelling, no `ANYTHING` sugar.
//!
//! Labels exist for readability and for parse positions where duplicate bare
//! identifiers would be invalid JavaScript.
//!
//! The `CASE_REST` list hole is spelled as a `case CASE_REST:` clause with no
//! body: a selector like
//! `switch (ANYTHING) { case CASE_REST: case "x": STMT_LIST; case CASE_REST: }`
//! matches a `switch` that has a `case "x":` arm preceded/followed by any
//! other `case`/`default` clauses.
//!
//! `ANYTHING` is parse-position polymorphic sugar for the anonymous typed
//! hole at positions where plain JavaScript can parse it. In an expression
//! position it behaves like `EXPR`; as a bare expression statement it behaves
//! like `STMT`; as a variable declarator name it behaves like `DECLARATORS`;
//! as a non-declarator binding pattern it matches any pattern; as an
//! object-literal shorthand property — or a destructure-pattern shorthand
//! property — it absorbs a run of object/destructured properties/spreads; as
//! a class field with no initializer it absorbs a run of class members. The
//! object-property and class-member runs have no typed spelling: `ANYTHING` is
//! the only one. Use the typed spellings when the position would otherwise be
//! ambiguous. `STMT_LIST` must be checked before `STMT`, since `STMT` is a
//! keyword-prefix of it.

pub const ANYTHING_HOLE_KEYWORD: &str = "ANYTHING";
pub const EXPR_HOLE_KEYWORD: &str = "EXPR";
pub const STMT_HOLE_KEYWORD: &str = "STMT";
pub const STMT_LIST_HOLE_KEYWORD: &str = "STMT_LIST";
pub const CASE_REST_HOLE_KEYWORD: &str = "CASE_REST";
pub const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
pub const ARGS_HOLE_KEYWORD: &str = "ARGS";
pub const ARRAY_ELEMENTS_HOLE_KEYWORD: &str = "ARRAY_ELEMENTS";

/// Callee name of the string-literal regex predicate sugar
/// `STR_LITERAL_MATCHING_RE("<pattern>")`, which matches a string literal
/// whose value matches the given pattern instead of an exact spelling.
pub const STRING_LITERAL_REGEX_PREDICATE: &str = "STR_LITERAL_MATCHING_RE";

/// If `name` is the bare `keyword` hole or a suffixed readability label
/// beginning with `keyword_`, returns `name`; otherwise `None`. A legacy empty
/// label (`keyword_`) is accepted as cosmetic and behaves like the bare keyword.
pub fn hole_name_for<'a>(name: &'a str, keyword: &str) -> Option<&'a str> {
    if name == keyword {
        return Some(name);
    }
    if keyword == STMT_HOLE_KEYWORD
        && (name == STMT_LIST_HOLE_KEYWORD || name.starts_with("STMT_LIST_"))
    {
        return None;
    }
    name.strip_prefix(keyword)?.strip_prefix('_')?;
    Some(name)
}

/// Alias for readability at call sites that specifically handle run holes.
pub fn labeled_hole_name_for<'a>(name: &'a str, keyword: &str) -> Option<&'a str> {
    hole_name_for(name, keyword)
}
