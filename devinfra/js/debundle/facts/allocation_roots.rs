//! Chunk-top allocations value flow registers as candidate roots before its
//! single solve: plain-data object and array literal initializers (the heap
//! scan's tracked allocations) and every array literal (heap contents).

use super::*;
use crate::purity::is_ts_enum_iife_init_for_binding;
use analysis_vocabulary::value::{Kind, Value};

/// Chunk-top initializers per binding name. `var` may repeat.
pub(crate) type InitGroups<'a> = BTreeMap<String, Vec<(&'a Expr, VarDeclKind)>>;

pub(crate) fn init_groups<'a>(body: &'a [TopLevelItemView<'_>]) -> InitGroups<'a> {
    let mut inits_by_name = BTreeMap::new();
    for item in body {
        for (name, init, kind) in top_level_initializers(item.as_module_item()) {
            inits_by_name
                .entry(name)
                .or_insert_with(Vec::new)
                .push((init, kind));
        }
    }
    inits_by_name
}

/// The allocation an initializer creates, keyed by its start offset as value
/// flow keys object and array literals.
pub(crate) fn site(init: &Expr) -> Value {
    Value::Alloc(Kind::Object, init.unwrap_parens().span().lo.0)
}

/// Bindings whose every initializer is plain data, with those allocations.
pub(crate) fn tracked_bindings(
    inits_by_name: &InitGroups<'_>,
) -> BTreeMap<String, BTreeSet<Value>> {
    inits_by_name
        .iter()
        .filter(|(name, inits)| {
            inits
                .iter()
                .all(|(init, kind)| is_plain_data_init(init, name, *kind))
        })
        .map(|(name, inits)| {
            (
                name.clone(),
                inits.iter().map(|(init, _)| site(init)).collect(),
            )
        })
        .collect()
}

/// Literal allocations value flow follows. A binding with a TS enum IIFE
/// initializer contributes none: only its own name reaches it.
pub(crate) fn literal_roots(
    inits_by_name: &InitGroups<'_>,
    bindings: &BTreeMap<String, BTreeSet<Value>>,
) -> BTreeSet<Value> {
    bindings
        .iter()
        .filter(|(name, _)| {
            inits_by_name[*name]
                .iter()
                .all(|(init, _)| matches!(init.unwrap_parens(), Expr::Object(_) | Expr::Array(_)))
        })
        .flat_map(|(_, sites)| sites.iter().copied())
        .collect()
}

/// Allocations value flow must register as candidate roots before its single
/// solve, so the heap scan can query what reaches them.
pub fn tracked_allocation_roots(body: &[TopLevelItemView<'_>]) -> BTreeSet<Value> {
    let inits_by_name = init_groups(body);
    literal_roots(&inits_by_name, &tracked_bindings(&inits_by_name))
}

/// Pure syntactic check for a plain-data initializer: an object literal whose
/// own properties are exclusively `KeyValue` / `Shorthand` / object spread
/// (`{...src}`) with non-computed, non-`__proto__` keys, an array literal
/// (holes read as `undefined`, no getter), or for a `var` a TS enum IIFE.
///
/// **Spreads are permitted.** `{...src}` evaluates `src`'s own enumerable
/// properties via `CopyDataProperties` and writes the resulting values to the
/// target via `CreateDataPropertyOrThrow`, which always produces a data
/// descriptor regardless of the source's descriptor shape. The spread itself
/// fires source getters at init (classified independently by the
/// `ObjectSpread`/`ArraySpread` rules), but the resulting receiver has no
/// accessor channels, which is what this check gates.
///
/// Value sub-expressions are not validated: the declaration's own purity is
/// classified separately, and post-init member reads depend only on the
/// receiver's shape and its heap state.
pub(crate) fn is_plain_data_init(expr: &Expr, binding: &str, kind: VarDeclKind) -> bool {
    match expr {
        Expr::Paren(p) => is_plain_data_init(&p.expr, binding, kind),
        Expr::Object(obj) => obj.props.iter().all(is_plain_data_prop),
        Expr::Array(_) => true,
        Expr::Call(_) if kind == VarDeclKind::Var => {
            is_ts_enum_iife_init_for_binding(expr, binding)
        }
        _ => false,
    }
}

pub(crate) fn is_plain_data_prop(prop: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(prop) = prop else {
        // `{...src}`: see `is_plain_data_init`.
        return true;
    };
    match prop.as_ref() {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => match &kv.key {
            // `{__proto__: X}` in an object literal SETS the prototype
            // (ES262 §13.2.5.5); if X carries accessor properties, reads on
            // the literal fire them. The computed form `{["__proto__"]: X}`
            // does not set the prototype, but computed keys are rejected.
            PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
            PropName::Str(s) => s.value.to_string_lossy() != "__proto__",
            PropName::Num(_) | PropName::BigInt(_) => true,
            PropName::Computed(_) => false,
        },
        // Methods, getters, setters, and `{key=value}` shorthand assign are
        // not data properties.
        Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
    }
}

pub(crate) fn array_candidate_roots(body: &[TopLevelItemView<'_>]) -> BTreeSet<Value> {
    let mut origins = ArrayOriginCollector::default();
    for item in body {
        item.as_module_item().visit_with(&mut origins);
    }
    array_candidate_roots_from_origins(&origins.0)
}

pub(crate) fn array_candidate_roots_from_origins(origins: &BTreeSet<u32>) -> BTreeSet<Value> {
    origins
        .iter()
        .copied()
        .map(|site| Value::Alloc(Kind::Object, site))
        .collect()
}

#[derive(Default)]
pub(crate) struct ArrayOriginCollector(pub(crate) BTreeSet<u32>);

impl Visit for ArrayOriginCollector {
    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.0.insert(node.span.lo.0);
        node.visit_children_with(self);
    }
}
