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
//! statement. `ARGS`, `STMT_LIST`, `OBJECT_PROPS`, `ARRAY_ELEMENTS`,
//! `CLASS_REST`, `CASE_REST`, and `DECLARATORS` are variable-length list holes:
//! `ARGS` absorbs a run of call/new arguments, `STMT_LIST` absorbs a run of block
//! statements (or top-level anonymous selector statements), `OBJECT_PROPS`
//! absorbs a run of object literal properties/spreads (or destructured
//! properties in an object binding/assignment pattern), `ARRAY_ELEMENTS` absorbs
//! a run of array-literal elements (including spreads/elisions), `CLASS_REST`
//! absorbs a run of class members, `CASE_REST` absorbs a run of `case`/`default`
//! clauses inside one `switch` statement, and `DECLARATORS` absorbs a run of
//! variable declarators inside one `var`/`let`/`const` declaration.
//! List-hole suffixes are labels for readability; they do not bind the
//! absorbed sequence for cross-occurrence equality.
//!
//! `ARRAY_ELEMENTS` is spelled as a bare identifier element
//! (`[firstStable, ARRAY_ELEMENTS, lastStable]`): unlike `OBJECT_PROPS`, an array
//! has no shorthand-property form, and `ANYTHING` in array-element position is one
//! `EXPR` (a single element), not a run — so the array-run hole has only its typed
//! `ARRAY_ELEMENTS` spelling, no `ANYTHING` sugar.
//!
//! The `CASE_REST` list hole is spelled as a `case CASE_REST:` clause with no
//! body (the switch analog of the `CLASS_REST;` class field): a selector like
//! `switch (ANYTHING) { case CASE_REST: case "x": STMT_LIST; case CASE_REST: }`
//! matches a `switch` that has a `case "x":` arm preceded/followed by any
//! other `case`/`default` clauses. Like `CLASS_REST`, it is matched as an
//! exact token with no suffix and never binds.
//!
//! `ANYTHING` is parse-position polymorphic sugar for the anonymous typed
//! hole at positions where plain JavaScript can parse it. In an expression
//! position it behaves like `EXPR`; as a bare expression statement it behaves
//! like `STMT`; as a variable declarator name it behaves like `DECLARATORS`;
//! as a non-declarator binding pattern it matches any pattern; as an
//! object-literal shorthand property — or a destructure-pattern shorthand
//! property — it absorbs object/destructured properties/spreads; as
//! a class field with no initializer it behaves like `CLASS_REST`. Use the
//! typed spellings when a named hole is helpful or when the position would
//! otherwise be ambiguous. `STMT_LIST` must be checked before `STMT`, since
//! `STMT` is a keyword-prefix of it.

pub const ANYTHING_HOLE_KEYWORD: &str = "ANYTHING";
pub const EXPR_HOLE_KEYWORD: &str = "EXPR";
pub const STMT_HOLE_KEYWORD: &str = "STMT";
pub const STMT_LIST_HOLE_KEYWORD: &str = "STMT_LIST";
pub const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";
pub const CASE_REST_HOLE_KEYWORD: &str = "CASE_REST";
pub const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
pub const ARGS_HOLE_KEYWORD: &str = "ARGS";
pub const OBJECT_PROPS_HOLE_KEYWORD: &str = "OBJECT_PROPS";
pub const ARRAY_ELEMENTS_HOLE_KEYWORD: &str = "ARRAY_ELEMENTS";

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
