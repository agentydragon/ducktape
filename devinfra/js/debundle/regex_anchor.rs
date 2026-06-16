//! Regex-over-string-literal anchors (`STR_LITERAL_MATCHING_RE("<pattern>")`).
//!
//! A var-binding selector that pins an exact string literal breaks whenever a
//! rebuild perturbs a volatile fragment of that literal (a content hash, build
//! counter, or other generated tail). When the *stable* part of the literal
//! already discriminates the target from its siblings, we can pin that stable
//! structure with a regex and wildcard the volatile fragment, so the selector
//! survives the rebuild.
//!
//! Derivation rule (intentionally conservative — see
//! `selector_minimizer_discrimination.md`):
//!
//!   * We only derive a pattern when the literal ends in a *volatile tail*: a
//!     trailing run of hex/digits. The tail must be at least
//!     `MIN_VOLATILE_TAIL_LEN` chars so a one- to three-char numeric suffix
//!     (which is more likely meaningful than generated) is left alone. Any
//!     separator before the tail (`chunk-`, `main.`) stays in the pinned prefix.
//!   * The derived pattern is `^<escaped stable prefix><tail class>$`, anchored
//!     so `Regex::is_match` (which is otherwise a substring test) pins the whole
//!     value. The stable prefix is escaped with `regex::escape`, so every
//!     metacharacter in the literal is matched literally; only the volatile tail
//!     becomes a character-class wildcard (`[0-9A-Fa-f]+` for a hex tail,
//!     `[0-9]+` for a pure-digit tail).
//!   * If the whole literal would be the volatile tail (no stable prefix), we
//!     return `None`: a bare `^[0-9]+$` pins nothing meaningful and would almost
//!     never discriminate.
//!
//! Limits we deliberately accept: this recognizes only trailing hex/digit
//! volatility (the dominant bundler pattern: `chunk-a1b2c3`, `main.4f3a2b.js`,
//! `vendor_1024`). It does not model embedded volatile fragments, GUID shapes,
//! or base64 hashes. The cover never *requires* a regex anchor — it is offered
//! only as an upgrade of an already-kept exact literal, and is taken only when
//! the upgraded selector still resolves uniquely (so an over-broad pattern is
//! rejected, never emitted). A pattern that fails to compile as `regex::Regex`
//! is likewise never emitted.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use source_match_holes::STRING_LITERAL_REGEX_PREDICATE;
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use crate::render::{AnchorSpan, ident_node, span_key};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SynthesizedTargetBinding, prove_synthesized_selector,
};

/// Minimum length of a trailing hex/digit run for it to count as a volatile
/// fragment worth wildcarding. Short numeric suffixes (`v2`, `s3`) are more
/// often meaningful than generated, so they stay pinned exactly.
const MIN_VOLATILE_TAIL_LEN: usize = 4;

/// Derive an anchored `STR_LITERAL_MATCHING_RE` pattern that pins the stable
/// prefix of `value` and wildcards a trailing volatile hex/digit fragment, or
/// `None` when no meaningful stable-prefix/volatile-tail split exists. The
/// returned pattern is always a valid `regex::Regex` (the only metacharacters
/// it introduces are the anchors and a character class; the prefix is escaped).
fn regex_anchor_pattern(value: &str) -> Option<String> {
    let chars: Vec<char> = value.chars().collect();
    // Length of the trailing run of hex digits.
    let hex_tail = chars
        .iter()
        .rev()
        .take_while(|c| c.is_ascii_hexdigit())
        .count();
    if hex_tail < MIN_VOLATILE_TAIL_LEN {
        return None;
    }
    // Prefer a pure-digit tail class when the whole tail is decimal; otherwise
    // a hex class. (Hex is a superset of digits, so the digit class is the
    // tighter, more honest wildcard when applicable.)
    let tail_is_decimal = chars[chars.len() - hex_tail..]
        .iter()
        .all(char::is_ascii_digit);
    let tail_class = if tail_is_decimal {
        "[0-9]+"
    } else {
        "[0-9A-Fa-f]+"
    };
    let stable_prefix: String = chars[..chars.len() - hex_tail].iter().collect();
    // A regex anchor must pin *something* stable: an empty or separator-only
    // prefix discriminates nothing, so decline. The trailing separator (if any)
    // stays in the pinned, escaped prefix — `chunk-a1b2c3` pins `chunk-`, not
    // `chunk`, which is the conservative choice (one fewer wildcarded char).
    if stable_prefix.is_empty() || stable_prefix.chars().all(|c| matches!(c, '-' | '_' | '.')) {
        return None;
    }
    let pattern = format!("^{}{tail_class}$", regex::escape(&stable_prefix));
    // Guard the invariant directly: never hand the matcher a pattern it cannot
    // compile (the matcher silently treats an uncompilable pattern as a
    // non-match, which would make the selector match nothing).
    regex::Regex::new(&pattern).ok()?;
    Some(pattern)
}

/// Build the `STR_LITERAL_MATCHING_RE("<pattern>")` call expression the matcher
/// interprets as a regex-over-string-literal predicate.
fn regex_predicate_call(pattern: &str) -> Expr {
    Expr::Call(CallExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Callee::Expr(Box::new(Expr::Ident(ident_node(
            STRING_LITERAL_REGEX_PREDICATE,
        )))),
        args: vec![ExprOrSpread {
            spread: None,
            expr: Box::new(Expr::Lit(Lit::Str(Str {
                span: DUMMY_SP,
                value: pattern.into(),
                raw: None,
            }))),
        }],
        type_args: None,
    })
}

/// Replace each kept string literal whose span is a chosen regex anchor with the
/// `STR_LITERAL_MATCHING_RE` predicate call. Runs as a post-pass over the holed
/// selector AST: holed literals keep their original source span, so matching by
/// span here is exact and never touches a literal the cover did not select.
pub(crate) struct RegexAnchorSubstitution<'a> {
    pub(crate) patterns: &'a BTreeMap<AnchorSpan, String>,
}

impl VisitMut for RegexAnchorSubstitution<'_> {
    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        if let Expr::Lit(Lit::Str(str_lit)) = expr {
            if let Some(pattern) = self.patterns.get(&span_key(str_lit.span)) {
                *expr = regex_predicate_call(pattern);
                return;
            }
        }
        expr.visit_mut_children_with(self);
    }
}

/// Candidate regex anchors for the var-binding minimizer: the `(span, pattern)`
/// of each string literal in a target slot's init for which `regex_anchor_pattern`
/// yields a wildcarding pattern. Only literals already in the kept set are ever
/// upgraded, so this is a superset filtered against `kept` at upgrade time.
pub(crate) fn collect_regex_anchor_candidates(init: &Expr) -> BTreeMap<AnchorSpan, String> {
    #[derive(Default)]
    struct Collector {
        candidates: BTreeMap<AnchorSpan, String>,
    }
    impl Visit for Collector {
        fn visit_expr(&mut self, expr: &Expr) {
            if let Expr::Lit(Lit::Str(str_lit)) = expr {
                if let Some(pattern) =
                    regex_anchor_pattern(str_lit.value.to_string_lossy().as_ref())
                {
                    self.candidates.insert(span_key(str_lit.span), pattern);
                }
            }
            expr.visit_children_with(self);
        }
    }
    let mut collector = Collector::default();
    init.visit_with(&mut collector);
    collector.candidates
}

/// Among kept string literals (`candidates` = span → derivable volatile-tail
/// pattern), accept each `STR_LITERAL_MATCHING_RE` upgrade iff the upgraded
/// selector *still* resolves uniquely (gate 1). The exact-literal form is the
/// default; regex is opt-in by merit. Upgrades are applied one literal at a time
/// so a too-broad pattern on one literal never blocks a sound upgrade on another.
/// Shared by the read-off single-target var path and the keep-shallow group path.
pub(crate) fn accepted_regex_anchors(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    candidates: &BTreeMap<AnchorSpan, String>,
    kept: &BTreeSet<AnchorSpan>,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>, &BTreeMap<AnchorSpan, String>) -> Result<String>,
) -> Result<BTreeMap<AnchorSpan, String>> {
    let mut regex_anchors: BTreeMap<AnchorSpan, String> = BTreeMap::new();
    for (&span, pattern) in candidates {
        if !kept.contains(&span) {
            continue;
        }
        regex_anchors.insert(span, pattern.clone());
        if prove_synthesized_selector(index, decl, targets, &render_with(kept, &regex_anchors)?)
            .is_err()
        {
            // The regex anchor broke uniqueness (over-broad among siblings), so
            // back it out and keep the exact literal.
            regex_anchors.remove(&span);
        }
    }
    Ok(regex_anchors)
}

#[cfg(test)]
mod regex_anchor_pattern_tests {
    use super::*;

    #[test]
    fn pins_stable_prefix_and_wildcards_hex_tail() {
        let pattern = regex_anchor_pattern("chunk-a1b2c3").expect("derivable hex tail");
        assert_eq!(pattern, "^chunk\\-[0-9A-Fa-f]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        // The pattern matches the seen value and rebuild variants of the same
        // stable prefix, but not a different prefix.
        assert!(re.is_match("chunk-a1b2c3"));
        assert!(re.is_match("chunk-ffffff"));
        assert!(!re.is_match("widget-a1b2c3"));
        // Anchored: a longer string sharing the prefix must not match.
        assert!(!re.is_match("chunk-a1b2c3-extra"));
    }

    #[test]
    fn prefers_decimal_class_for_pure_digit_tail() {
        let pattern = regex_anchor_pattern("vendor_1024").expect("derivable digit tail");
        assert_eq!(pattern, "^vendor_[0-9]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        assert!(re.is_match("vendor_1024"));
        assert!(re.is_match("vendor_9999"));
        // The decimal class is tighter than hex: a hex-only rebuild would not
        // match, which is fine — we wildcard only what we observed (digits).
        assert!(!re.is_match("vendor_abcd"));
    }

    #[test]
    fn escapes_regex_metacharacters_in_the_prefix() {
        let pattern = regex_anchor_pattern("main.bundle.4f3a2b").expect("derivable");
        assert_eq!(pattern, "^main\\.bundle\\.[0-9A-Fa-f]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        assert!(re.is_match("main.bundle.4f3a2b"));
        // The `.`s are literal, not any-char wildcards.
        assert!(!re.is_match("mainXbundleY4f3a2b"));
    }

    #[test]
    fn declines_short_numeric_suffixes() {
        // A two/three-char numeric suffix is more likely meaningful than
        // generated, so no pattern is offered (`MIN_VOLATILE_TAIL_LEN`).
        assert_eq!(regex_anchor_pattern("v2"), None);
        assert_eq!(regex_anchor_pattern("step3"), None);
        assert_eq!(regex_anchor_pattern("h2o"), None);
    }

    #[test]
    fn declines_when_no_stable_prefix_remains() {
        // The whole literal is the volatile tail: a bare digit/hex anchor pins
        // nothing meaningful.
        assert_eq!(regex_anchor_pattern("123456"), None);
        assert_eq!(regex_anchor_pattern("deadbeef"), None);
        // Separator-only prefix is likewise rejected.
        assert_eq!(regex_anchor_pattern("-123456"), None);
    }
}
