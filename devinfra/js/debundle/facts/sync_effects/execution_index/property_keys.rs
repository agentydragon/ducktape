//! Property-key expressions and the string domains they prove.

use super::super::value_model::static_prop;
use super::IntrinsicRoots;
use analysis::analysis_inputs::NativeIntrinsic;
use analysis::reports::schema::NativePropertyKey;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::{AssignOp, BinaryOp, Callee, Expr, Id, Lit};

/// Property-key facts affect only the native write census. They make no claim
/// about evaluating the key, calling a factory, or consuming its return value.
#[derive(Debug, Clone)]
pub(super) enum PropertyKeyExpr {
    String(String),
    /// A canonical array index from a numeric literal: a key, not a string
    /// operand of `+`.
    Index(String),
    /// An untagged template with substitutions: a string between its first
    /// and last literal parts.
    Template {
        prefix: String,
        suffix: String,
    },
    /// `left + right`.
    Concat(Box<PropertyKeyExpr>, Box<PropertyKeyExpr>),
    SymbolIterator(Id),
    Binding(Id),
    SymbolFor(Id),
    EntriesKeys(Id, BTreeSet<String>),
    Unknown,
}

impl PropertyKeyExpr {
    pub(super) fn from_expr(expression: &Expr) -> Self {
        match expression.unwrap_parens() {
            Expr::Lit(Lit::Str(value)) => Self::String(value.value.to_string_lossy().into_owned()),
            Expr::Lit(Lit::Num(value))
                if value.value >= 0.0
                    && value.value <= u32::MAX as f64
                    && value.value.fract() == 0.0 =>
            {
                Self::Index((value.value as u32).to_string())
            }
            Expr::Tpl(template) => {
                let cooked = |index: usize| {
                    template.quasis[index]
                        .cooked
                        .as_ref()
                        .map(|part| part.to_string_lossy().into_owned())
                };
                let (Some(prefix), Some(suffix)) = (cooked(0), cooked(template.quasis.len() - 1))
                else {
                    return Self::Unknown;
                };
                if template.exprs.is_empty() {
                    Self::String(prefix)
                } else {
                    Self::Template { prefix, suffix }
                }
            }
            Expr::Bin(binary) if binary.op == BinaryOp::Add => Self::Concat(
                Box::new(Self::from_expr(&binary.left)),
                Box::new(Self::from_expr(&binary.right)),
            ),
            // Simple assignment returns its RHS, independent of an inherited
            // setter's return value. Keep coercive and compound forms opaque.
            Expr::Assign(assignment)
                if assignment.op == AssignOp::Assign
                    && matches!(assignment.right.unwrap_parens(), Expr::Lit(_)) =>
            {
                Self::from_expr(&assignment.right)
            }
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Member(member)
                if static_prop(&member.prop).as_deref() == Some("iterator")
                    && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol") =>
            {
                let Expr::Ident(symbol) = member.obj.unwrap_parens() else {
                    unreachable!()
                };
                Self::SymbolIterator(symbol.to_id())
            }
            // `Symbol.for(value)` returns a Symbol whatever its argument
            // converts to; a conversion that throws leaves no key.
            Expr::Call(call) if call.args.len() == 1 && call.args[0].spread.is_none() => {
                match &call.callee {
                    Callee::Expr(callee) => match callee.unwrap_parens() {
                        Expr::Member(member)
                            if static_prop(&member.prop).as_deref() == Some("for") =>
                        {
                            match member.obj.unwrap_parens() {
                                Expr::Ident(ident) if ident.sym == "Symbol" => {
                                    Self::SymbolFor(ident.to_id())
                                }
                                _ => Self::Unknown,
                            }
                        }
                        _ => Self::Unknown,
                    },
                    _ => Self::Unknown,
                }
            }
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) enum PropertyKeyDomain {
    FiniteStrings(BTreeSet<String>),
    /// Strings with this constant prefix and suffix, not both empty.
    Affixed {
        prefix: String,
        suffix: String,
    },
    SymbolIterator,
    Symbol,
    Unknown,
}

#[derive(Debug, Clone)]
pub(super) struct PropertyKeyEvidence {
    pub(super) domain: PropertyKeyDomain,
    pub(super) dependencies: IntrinsicRoots,
}

impl PropertyKeyEvidence {
    pub(super) fn unknown() -> Self {
        Self {
            domain: PropertyKeyDomain::Unknown,
            dependencies: BTreeSet::new(),
        }
    }
}

pub(super) fn property_key_evidence<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    declared: &BTreeSet<Id>,
) -> PropertyKeyEvidence {
    let mut expression = expression;
    let mut seen = BTreeSet::new();
    loop {
        match expression {
            PropertyKeyExpr::Binding(binding) => {
                if !seen.insert(binding) {
                    return PropertyKeyEvidence::unknown();
                }
                match assignments.get(binding).map(Vec::as_slice) {
                    Some([value]) => expression = value,
                    _ => return PropertyKeyEvidence::unknown(),
                }
            }
            PropertyKeyExpr::String(key) | PropertyKeyExpr::Index(key) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::FiniteStrings(BTreeSet::from([key.clone()])),
                    dependencies: BTreeSet::new(),
                };
            }
            PropertyKeyExpr::Template { .. } | PropertyKeyExpr::Concat(..) => {
                let Some((prefix, suffix)) = string_affixes(expression, assignments, &seen) else {
                    return PropertyKeyEvidence::unknown();
                };
                if prefix.is_empty() && suffix.is_empty() {
                    return PropertyKeyEvidence::unknown();
                }
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::Affixed { prefix, suffix },
                    dependencies: BTreeSet::new(),
                };
            }
            PropertyKeyExpr::SymbolIterator(namespace) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::SymbolIterator,
                    dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
                };
            }
            PropertyKeyExpr::SymbolFor(namespace) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::Symbol,
                    dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
                };
            }
            PropertyKeyExpr::EntriesKeys(namespace, keys) if !declared.contains(namespace) => {
                return PropertyKeyEvidence {
                    domain: PropertyKeyDomain::FiniteStrings(keys.clone()),
                    // Both the outer entries array and each destructured pair use
                    // the Array iterator, so the premise covers both protocols.
                    dependencies: BTreeSet::from([NativeIntrinsic::Object, NativeIntrinsic::Array]),
                };
            }
            _ => return PropertyKeyEvidence::unknown(),
        }
    }
}

/// The constant prefix and suffix of a string-valued expression, or `None`
/// when it may not be a string. A template is a string; `left + right` is a
/// string concatenation when either operand is, whose result starts with a
/// string left operand's prefix and ends with a string right operand's suffix
/// (the other operand's ToPrimitive/ToString may run user code but still
/// yields the concatenation, or throws before any key exists).
fn string_affixes<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    initially_seen: &BTreeSet<&'a Id>,
) -> Option<(String, String)> {
    let mut visited = initially_seen.clone();
    if !string_leaf_reachable(expression, assignments, &mut visited) {
        return None;
    }

    Some((
        string_affix(
            expression,
            assignments,
            initially_seen,
            StringAffixSide::Prefix,
        )
        .unwrap_or_default(),
        string_affix(
            expression,
            assignments,
            initially_seen,
            StringAffixSide::Suffix,
        )
        .unwrap_or_default(),
    ))
}

/// Test whether any string-valued leaf is reachable. Binding assignments form
/// a graph, so expand each binding once instead of recursively re-walking
/// shared concatenation branches.
fn string_leaf_reachable<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    visited: &mut BTreeSet<&'a Id>,
) -> bool {
    let mut pending = vec![expression];
    let mut found_string = false;
    while let Some(expression) = pending.pop() {
        match expression {
            PropertyKeyExpr::String(_) | PropertyKeyExpr::Template { .. } => {
                found_string = true;
            }
            PropertyKeyExpr::Binding(binding) if visited.insert(binding) => {
                if let Some([value]) = assignments.get(binding).map(Vec::as_slice) {
                    pending.push(value);
                }
            }
            PropertyKeyExpr::Concat(left, right) => {
                pending.push(left);
                pending.push(right);
            }
            _ => {}
        }
    }
    found_string
}

#[derive(Clone, Copy)]
enum StringAffixSide {
    Prefix,
    Suffix,
}

/// Follow the one concatenation edge that determines this affix. The initial
/// bindings are already on the caller's recursion path, and revisiting any
/// binding ends this path just as the former recursive analysis did.
fn string_affix<'a>(
    expression: &'a PropertyKeyExpr,
    assignments: &'a BTreeMap<Id, Vec<PropertyKeyExpr>>,
    initially_seen: &BTreeSet<&'a Id>,
    side: StringAffixSide,
) -> Option<String> {
    let mut expression = expression;
    let mut seen = initially_seen.clone();
    loop {
        match expression {
            PropertyKeyExpr::String(value) => return Some(value.clone()),
            PropertyKeyExpr::Template { prefix, suffix } => {
                return Some(match side {
                    StringAffixSide::Prefix => prefix.clone(),
                    StringAffixSide::Suffix => suffix.clone(),
                });
            }
            PropertyKeyExpr::Binding(binding) => {
                if !seen.insert(binding) {
                    return None;
                }
                let Some([value]) = assignments.get(binding).map(Vec::as_slice) else {
                    return None;
                };
                expression = value;
            }
            PropertyKeyExpr::Concat(left, right) => {
                expression = match side {
                    StringAffixSide::Prefix => left,
                    StringAffixSide::Suffix => right,
                };
            }
            _ => return None,
        }
    }
}

/// Whether `domain` may be the string `name`.
pub(super) fn key_domain_may_name(domain: &PropertyKeyDomain, name: &str) -> bool {
    match domain {
        PropertyKeyDomain::FiniteStrings(keys) => keys.contains(name),
        PropertyKeyDomain::Affixed { prefix, suffix } => NativePropertyKey::Affixed {
            prefix: prefix.clone(),
            suffix: suffix.clone(),
        }
        .may_be_string(name),
        PropertyKeyDomain::SymbolIterator | PropertyKeyDomain::Symbol => false,
        PropertyKeyDomain::Unknown => true,
    }
}

#[cfg(test)]
mod string_affixes_tests {
    use super::{PropertyKeyExpr, string_affixes, string_leaf_reachable};
    use std::collections::{BTreeMap, BTreeSet};
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    fn binding(binding: &Id) -> PropertyKeyExpr {
        PropertyKeyExpr::Binding(binding.clone())
    }

    fn concat(left: PropertyKeyExpr, right: PropertyKeyExpr) -> PropertyKeyExpr {
        PropertyKeyExpr::Concat(Box::new(left), Box::new(right))
    }

    #[test]
    fn cyclic_and_acyclic_affixes_preserve_blocked_bindings() {
        let first = id("first");
        let second = id("second");
        let acyclic = id("acyclic");
        let multiple = id("multiple");
        let assignments = BTreeMap::from([
            (
                first.clone(),
                vec![concat(
                    binding(&second),
                    PropertyKeyExpr::String("cycle-tail".into()),
                )],
            ),
            (
                second.clone(),
                vec![concat(
                    binding(&first),
                    PropertyKeyExpr::Template {
                        prefix: "cycle-prefix".into(),
                        suffix: "cycle-suffix".into(),
                    },
                )],
            ),
            (
                acyclic.clone(),
                vec![PropertyKeyExpr::String("acyclic-key".into())],
            ),
            (
                multiple.clone(),
                vec![
                    PropertyKeyExpr::String("first-value".into()),
                    PropertyKeyExpr::String("second-value".into()),
                ],
            ),
        ]);

        let blocked_expression = binding(&second);
        let mixed_expression = concat(binding(&multiple), binding(&acyclic));
        let unknown_expression = concat(
            PropertyKeyExpr::Unknown,
            PropertyKeyExpr::Template {
                prefix: "right-prefix".into(),
                suffix: "right-suffix".into(),
            },
        );
        let cases = [
            (binding(&first), Vec::new(), Some(("", "cycle-tail"))),
            (
                blocked_expression,
                vec![first.clone()],
                Some(("", "cycle-suffix")),
            ),
            (
                binding(&acyclic),
                Vec::new(),
                Some(("acyclic-key", "acyclic-key")),
            ),
            (binding(&multiple), Vec::new(), None),
            (mixed_expression, Vec::new(), Some(("", "acyclic-key"))),
            (unknown_expression, Vec::new(), Some(("", "right-suffix"))),
        ];
        for (expression, initially_seen, expected) in &cases {
            let seen = initially_seen.iter().collect();
            assert_eq!(
                string_affixes(expression, &assignments, &seen),
                expected.map(|(prefix, suffix)| (prefix.to_owned(), suffix.to_owned())),
            );
        }
    }

    #[test]
    fn shared_diamond_expands_each_binding_once() {
        let leaf = id("diamond-leaf");
        let mut assignments = BTreeMap::from([(
            leaf.clone(),
            vec![PropertyKeyExpr::Template {
                prefix: "shared-prefix".into(),
                suffix: "shared-suffix".into(),
            }],
        )]);
        let mut parent = leaf;
        let depth = 256;
        for level in 0..depth {
            let next = id(&format!("diamond-{level}"));
            assignments.insert(
                next.clone(),
                vec![concat(binding(&parent), binding(&parent))],
            );
            parent = next;
        }

        let expression = binding(&parent);
        let mut visited = BTreeSet::new();
        assert!(string_leaf_reachable(
            &expression,
            &assignments,
            &mut visited
        ));
        assert_eq!(visited.len(), depth + 1);
        assert_eq!(
            string_affixes(&expression, &assignments, &BTreeSet::new()),
            Some(("shared-prefix".into(), "shared-suffix".into()))
        );
    }
}
