//! Source-shape candidates for an exact, contiguous primitive-array spread.
//!
//! This reads the original declaration before `TopLevelItemView` splits it.
//! A certificate still needs native iterator integrity (or an exact author
//! premise) and graph co-location of every producer with its consumer.

use super::*;
use source_identity::SourceUnitOrdinal;

#[derive(Debug, Clone)]
pub(crate) struct StableArraySpreadShape {
    pub(crate) consumer: SourceUnitOrdinal,
    pub(crate) producers: BTreeSet<SourceUnitOrdinal>,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct StableArraySpreadShapes {
    pub(crate) by_initializer: BTreeMap<u32, StableArraySpreadShape>,
    pub(crate) by_site: BTreeMap<u32, u32>,
}

impl StableArraySpreadShapes {
    pub(crate) fn collect(original: &[ModuleItem], body: &[TopLevelItemView<'_>]) -> Self {
        let ordinals: BTreeMap<_, _> = body
            .iter()
            .enumerate()
            .map(|(ordinal, item)| {
                (
                    item.as_module_item().span().lo.0,
                    SourceUnitOrdinal(ordinal),
                )
            })
            .collect();
        let mut shapes = Self::default();
        for item in original {
            // Exported declarations are externally observable during module
            // evaluation, so their source-prefix identity is not private.
            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = item else {
                continue;
            };
            if var.kind != VarDeclKind::Const || var.decls.len() < 2 {
                continue;
            }
            let mut previous = BTreeMap::<Id, SourceUnitOrdinal>::new();
            for decl in &var.decls {
                let Pat::Ident(binding) = &decl.name else {
                    break;
                };
                let Some(Expr::Array(array)) = decl.init.as_deref().map(Expr::unwrap_parens) else {
                    break;
                };
                let Some(&ordinal) = ordinals.get(&decl.span.lo.0) else {
                    break;
                };
                if primitive_array_literal(array) {
                    previous.insert(binding.id.to_id(), ordinal);
                    continue;
                }
                let mut producers = BTreeSet::new();
                let mut tokens = BTreeSet::new();
                let valid = array.elems.iter().all(|element| {
                    let Some(element) = element else { return false };
                    if let Some(token) = element.spread {
                        let Expr::Ident(source) = element.expr.unwrap_parens() else {
                            return false;
                        };
                        let Some(&producer) = previous.get(&source.to_id()) else {
                            return false;
                        };
                        producers.insert(producer);
                        tokens.insert(token.lo.0);
                        tokens.insert(element.expr.span().lo.0);
                        true
                    } else {
                        primitive_literal(&element.expr)
                    }
                });
                if valid && !tokens.is_empty() {
                    let initializer = array.span.lo.0;
                    for token in &tokens {
                        shapes.by_site.insert(*token, initializer);
                    }
                    shapes.by_initializer.insert(
                        initializer,
                        StableArraySpreadShape {
                            consumer: ordinal,
                            producers,
                        },
                    );
                }
                // Nothing after a spread initializer belongs to the inert
                // source interval, even if the value it built is an array.
                break;
            }
        }
        shapes
    }
}

fn primitive_array_literal(array: &ArrayLit) -> bool {
    array.elems.iter().all(|element| {
        element
            .as_ref()
            .is_some_and(|element| element.spread.is_none() && primitive_literal(&element.expr))
    })
}

fn primitive_literal(expr: &Expr) -> bool {
    matches!(
        expr.unwrap_parens(),
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
    )
}
