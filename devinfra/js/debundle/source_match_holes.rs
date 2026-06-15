//! Syntactic-hole keyword vocabulary shared across the source-match language.
//!
//! `source_match` interprets these holes when resolving selectors, the
//! `selector_codemod` minimizer emits them when rendering candidate
//! selectors, and `selector_candidate_index` recognizes them when building
//! its prefilter. Keeping the keyword spellings in one place stops producer
//! and consumer code from drifting apart on string literals.
//!
//! For single-node holes, the **bare keyword** is the anonymous form: it
//! matches independently at every occurrence and never binds, so authors
//! don't have to mint a unique name per throwaway placeholder. A
//! `<keyword>_<name>` identifier is the **named** form, which binds for
//! cross-occurrence equality — the same name must match the same
//! subtree/statement everywhere it appears.
//!
//! `EXPR` matches one arbitrary expression and `STMT` one arbitrary
//! statement. `ARGS`, `STMT_LIST`, `OBJECT_PROPS`, `CLASS_REST`, and
//! `DECLARATORS` are variable-length list holes: `ARGS` absorbs a run of
//! call/new arguments, `STMT_LIST` absorbs a run of block statements (or
//! top-level anonymous selector statements), `OBJECT_PROPS` absorbs a run of
//! object literal properties/spreads, `CLASS_REST` absorbs a run of class
//! members, and `DECLARATORS` absorbs a run of variable declarators inside
//! one `var`/`let`/`const` declaration. List-hole suffixes are labels for
//! readability; they do not bind the absorbed sequence for cross-occurrence
//! equality.
//!
//! `ANYTHING` is parse-position polymorphic sugar for the anonymous typed
//! hole at positions where plain JavaScript can parse it. In an expression
//! position it behaves like `EXPR`; as a bare expression statement it behaves
//! like `STMT`; as a variable declarator name it behaves like `DECLARATORS`;
//! as a non-declarator binding pattern it matches any pattern; as an
//! object-literal shorthand property it absorbs object properties/spreads; as
//! a class field with no initializer it behaves like `CLASS_REST`. Use the
//! typed spellings when a named hole is helpful or when the position would
//! otherwise be ambiguous. `STMT_LIST` must be checked before `STMT`, since
//! `STMT` is a keyword-prefix of it.

pub const ANYTHING_HOLE_KEYWORD: &str = "ANYTHING";
pub const EXPR_HOLE_KEYWORD: &str = "EXPR";
pub const STMT_HOLE_KEYWORD: &str = "STMT";
pub const STMT_LIST_HOLE_KEYWORD: &str = "STMT_LIST";
pub const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";
pub const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
pub const ARGS_HOLE_KEYWORD: &str = "ARGS";
pub const OBJECT_PROPS_HOLE_KEYWORD: &str = "OBJECT_PROPS";

/// Callee name of the string-literal regex predicate sugar
/// `STR_LITERAL_MATCHING_RE("<pattern>")`, which matches a string literal
/// whose value matches the given pattern instead of an exact spelling.
pub const STRING_LITERAL_REGEX_PREDICATE: &str = "STR_LITERAL_MATCHING_RE";

/// If `name` is the bare `keyword` or a named `<keyword>_<suffix>` hole,
/// returns `name`; otherwise `None`. A trailing segment must start with `_`
/// so that distinct keywords sharing a prefix (`STMT` vs `STMT_LIST`) never
/// alias.
pub fn hole_name_for<'a>(name: &'a str, keyword: &str) -> Option<&'a str> {
    let rest = name.strip_prefix(keyword)?;
    (rest.is_empty() || rest.starts_with('_')).then_some(name)
}
