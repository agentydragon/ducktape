//! Finite positive closures for value classes used by purity rules.
//!
//! These rules only publish facts that have been proven by existing syntax
//! matchers. Whole-chunk write, escape, and method scans remain admission gates
//! in `plain_data`.

use super::*;
use crate::inference::{FactContext, IterativeInference, IterativeRules};

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ValueKey {
    Fluent(String),
    PlainArray(String),
    Const(ConstBindingKey),
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum ValueFact {
    Known,
    ConstClasses { primitive: bool, symbol: bool },
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct ConstBindingKey {
    source_index: usize,
    name: String,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct ConstProducer {
    target: ConstBindingKey,
    previous_binding: Option<ConstBindingKey>,
    referenced_predecessors: Vec<ConstBindingKey>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ValueRule {
    Fluent {
        target: String,
        root: String,
    },
    PlainArray {
        target: String,
        source: Option<String>,
    },
    Const(ConstProducer),
}

struct ValueRules<'a> {
    const_inits: BTreeMap<ConstBindingKey, &'a Expr>,
    shadowed: Option<&'a BTreeSet<&'static str>>,
}

impl IterativeRules for ValueRules<'_> {
    type Key = ValueKey;
    type Fact = ValueFact;
    type Rule = ValueRule;

    fn evaluate(
        &self,
        rule: &Self::Rule,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        match rule {
            ValueRule::Fluent { target, root } => {
                if context.read(ValueKey::Fluent(root.clone())).is_some() {
                    context.propose(ValueKey::Fluent(target.clone()), ValueFact::Known);
                }
            }
            ValueRule::PlainArray { target, source } => match source {
                None => {
                    context.propose(ValueKey::PlainArray(target.clone()), ValueFact::Known);
                }
                Some(source) => {
                    if context.read(ValueKey::PlainArray(source.clone())).is_some() {
                        context.propose(ValueKey::PlainArray(target.clone()), ValueFact::Known);
                    }
                }
            },
            ValueRule::Const(producer) => self.evaluate_const(producer, context),
        }
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        match (key, current, contribution) {
            (ValueKey::Fluent(_) | ValueKey::PlainArray(_), ValueFact::Known, ValueFact::Known) => {
                false
            }
            (
                ValueKey::Const(_),
                ValueFact::ConstClasses {
                    primitive: current_primitive,
                    symbol: current_symbol,
                },
                ValueFact::ConstClasses {
                    primitive: next_primitive,
                    symbol: next_symbol,
                },
            ) => {
                let changed =
                    (!*current_primitive && next_primitive) || (!*current_symbol && next_symbol);
                *current_primitive |= next_primitive;
                *current_symbol |= next_symbol;
                changed
            }
            _ => unreachable!("value rule proposed a fact for the wrong key kind"),
        }
    }
}

impl ValueRules<'_> {
    fn evaluate_const(
        &self,
        producer: &ConstProducer,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        // Source-order const classification is position-sensitive. Wait for
        // every earlier declaration that the initializer references, including
        // unsupported consts whose completed fact is explicitly false. Never
        // publish a temporary negative result while one of these is absent.
        let mut complete = true;
        let mut previous_primitives = BTreeSet::new();
        let mut previous_symbols = BTreeSet::new();
        let previous_binding = match &producer.previous_binding {
            Some(key) => match context.read(ValueKey::Const(key.clone())) {
                Some(fact) => Some(fact.clone()),
                None => {
                    complete = false;
                    None
                }
            },
            None => None,
        };
        for predecessor in &producer.referenced_predecessors {
            match context.read(ValueKey::Const(predecessor.clone())) {
                Some(ValueFact::ConstClasses { primitive, symbol }) => {
                    if *primitive {
                        previous_primitives.insert(predecessor.name.clone());
                    }
                    if *symbol {
                        previous_symbols.insert(predecessor.name.clone());
                    }
                }
                None => complete = false,
                Some(ValueFact::Known) => unreachable!("const predecessor has a value fact"),
            }
        }
        if !complete {
            return;
        }

        let Some(init) = self.const_inits.get(&producer.target) else {
            unreachable!("const producer has no initializer");
        };
        let primitive = is_result_primitive(init, &previous_primitives, &BTreeSet::new());
        let symbol = !primitive
            && is_symbol_const_init(
                init,
                &previous_symbols,
                self.shadowed
                    .expect("const inference has global shadow facts"),
            );

        // Duplicate declarations are invalid JavaScript, but retaining the
        // original set-accumulation behavior keeps malformed-input handling
        // conservative and source ordered.
        let (primitive, symbol) = match previous_binding {
            Some(ValueFact::ConstClasses {
                primitive: previous_primitive,
                symbol: previous_symbol,
            }) => (primitive || previous_primitive, symbol || previous_symbol),
            Some(ValueFact::Known) => unreachable!("const predecessor has a value fact"),
            None => (primitive, symbol),
        };
        context.propose(
            ValueKey::Const(producer.target.clone()),
            ValueFact::ConstClasses { primitive, symbol },
        );
    }
}

pub(super) fn infer_fluent_const_bindings(
    body: &[TopLevelItemView<'_>],
    seed: &BTreeSet<String>,
) -> BTreeSet<String> {
    if seed.is_empty() {
        return BTreeSet::new();
    }
    let mut inference = IterativeInference::new(ValueRules {
        const_inits: BTreeMap::new(),
        shadowed: None,
    });
    for root in seed {
        inference.seed(ValueKey::Fluent(root.clone()), ValueFact::Known);
    }
    let producers = body.iter().flat_map(|item| {
        plain_data_var_candidates(item.as_module_item())
            .into_iter()
            .filter_map(|(target, init, kind)| {
                (kind == VarDeclKind::Const)
                    .then(|| fluent_chain_root(init))
                    .flatten()
                    .map(|root| ValueRule::Fluent {
                        target,
                        root: root.to_owned(),
                    })
            })
    });
    inference
        .solve(producers)
        .into_values()
        .into_keys()
        .filter_map(|key| match key {
            ValueKey::Fluent(name) => Some(name),
            ValueKey::PlainArray(_) | ValueKey::Const(_) => None,
        })
        .collect()
}

pub(super) fn infer_plain_array_candidates(body: &[TopLevelItemView<'_>]) -> BTreeSet<String> {
    let inference = IterativeInference::new(ValueRules {
        const_inits: BTreeMap::new(),
        shadowed: None,
    });
    let producers = body.iter().flat_map(|item| {
        plain_data_var_candidates(item.as_module_item())
            .into_iter()
            .filter_map(|(target, init, kind)| {
                if kind != VarDeclKind::Const {
                    return None;
                }
                plain_array_source(init).map(|source| ValueRule::PlainArray { target, source })
            })
    });
    inference
        .solve(producers)
        .into_values()
        .into_keys()
        .filter_map(|key| match key {
            ValueKey::PlainArray(name) => Some(name),
            ValueKey::Fluent(_) | ValueKey::Const(_) => None,
        })
        .collect()
}

fn plain_array_source(expr: &Expr) -> Option<Option<String>> {
    match expr.unwrap_parens() {
        Expr::Array(_) => Some(None),
        Expr::Ident(ident) => Some(Some(ident.sym.to_string())),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
                return None;
            };
            if !matches!(
                &member.prop,
                MemberProp::Ident(prop) if ["filter", "flatMap", "map", "slice"].contains(&prop.sym.as_ref())
            ) {
                return None;
            }
            plain_array_source(member.obj.as_ref())
        }
        _ => None,
    }
}

pub(super) fn infer_const_value_classes(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> (BTreeSet<String>, BTreeSet<String>) {
    let mut const_inits = BTreeMap::new();
    let mut previous_by_name = BTreeMap::new();
    let mut producers = Vec::new();
    for (source_index, item) in body.iter().enumerate() {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            if kind != VarDeclKind::Const {
                continue;
            }
            let target = ConstBindingKey { source_index, name };
            const_inits.insert(target.clone(), init);
            let previous_binding = previous_by_name.get(&target.name).cloned();

            let mut identifiers = IdentifierCollector::default();
            init.visit_with(&mut identifiers);
            let referenced_predecessors = identifiers
                .0
                .into_iter()
                .filter_map(|name| previous_by_name.get(&name).cloned())
                .collect();
            producers.push(ValueRule::Const(ConstProducer {
                target: target.clone(),
                previous_binding,
                referenced_predecessors,
            }));
            previous_by_name.insert(target.name.clone(), target);
        }
    }

    let facts = IterativeInference::new(ValueRules {
        const_inits,
        shadowed: Some(shadowed),
    })
    .solve(producers)
    .into_values();
    let mut primitives = BTreeSet::new();
    let mut symbols = BTreeSet::new();
    for (key, fact) in facts {
        let ValueKey::Const(binding) = key else {
            continue;
        };
        let ValueFact::ConstClasses { primitive, symbol } = fact else {
            unreachable!("const key has a value fact");
        };
        if primitive {
            primitives.insert(binding.name.clone());
        }
        if symbol {
            symbols.insert(binding.name);
        }
    }
    (primitives, symbols)
}

#[derive(Default)]
struct IdentifierCollector(BTreeSet<String>);

impl Visit for IdentifierCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        self.0.insert(ident.sym.to_string());
    }
}
