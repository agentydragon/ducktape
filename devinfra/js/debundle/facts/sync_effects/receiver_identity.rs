//! Direct receiver identity at a confined local use, independent of call effects.

use super::value_model::ValueExpr;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::{Callee, Expr, Id};

#[derive(Debug, Clone)]
pub(super) enum ReceiverOrigin {
    ObjectLiteral,
    Constructor {
        binding: Id,
        span: Span,
    },
    Wrapper {
        binding: Id,
        span: Span,
        constructor: Id,
        constructor_span: Span,
    },
}

#[derive(Debug, Clone)]
pub(super) struct LocalReceiverProof {
    pub(super) binding: Id,
    pub(super) initializer: Span,
    pub(super) origins: Vec<ReceiverOrigin>,
}

struct Definition {
    callable: Option<u32>,
    span: Span,
    initializer: Option<ReceiverInitializer>,
}

/// Identity needs neither literal contents nor call arguments. Keeping only
/// these producers avoids duplicating the executor's retained value graph.
enum ReceiverInitializer {
    ObjectLiteral,
    Construction(Id),
    WrapperCall(Id),
    Alternatives(Vec<Self>),
}

impl ReceiverInitializer {
    // The executor's ValueExpr intentionally erases assignment operators.
    // Identity cannot use that projection: `slot ||= {}` may return a native.
    fn from_expression(value: &Expr) -> Option<Self> {
        match value.unwrap_parens() {
            Expr::Object(_) => Some(Self::ObjectLiteral),
            Expr::New(construction)
                if construction
                    .args
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .all(|argument| argument.spread.is_none()) =>
            {
                match construction.callee.unwrap_parens() {
                    Expr::Ident(binding) => Some(Self::Construction(binding.to_id())),
                    _ => None,
                }
            }
            Expr::Call(call) if call.args.iter().all(|argument| argument.spread.is_none()) => {
                match &call.callee {
                    Callee::Expr(callee) => match callee.unwrap_parens() {
                        Expr::Ident(binding) => Some(Self::WrapperCall(binding.to_id())),
                        _ => None,
                    },
                    _ => None,
                }
            }
            Expr::Cond(condition) => Some(Self::Alternatives(vec![
                Self::from_expression(&condition.cons)?,
                Self::from_expression(&condition.alt)?,
            ])),
            _ => None,
        }
    }
}

struct FunctionDeclaration {
    callable: Option<u32>,
    span: Span,
    no_returns: bool,
    returned_construction: Option<Id>,
}

/// Collected by ExecutionIndex's canonical visit. Source order is used only in
/// activations without backedges, exceptional transfers or captured rebinding.
#[derive(Default)]
pub(super) struct ReceiverIdentityFacts {
    definitions: BTreeMap<Id, Vec<Definition>>,
    functions: BTreeMap<Id, Vec<FunctionDeclaration>>,
    parameters: BTreeMap<u32, BTreeSet<Id>>,
    unsupported: BTreeSet<u32>,
}

impl ReceiverIdentityFacts {
    pub(super) fn callable(&mut self, key: u32, parameters: BTreeSet<Id>) {
        self.parameters.insert(key, parameters);
    }

    pub(super) fn unsupported(&mut self, callable: Option<u32>) {
        if let Some(callable) = callable {
            self.unsupported.insert(callable);
        }
    }

    pub(super) fn definition(
        &mut self,
        binding: Id,
        callable: Option<u32>,
        span: Span,
        initializer: Option<&Expr>,
    ) {
        self.definitions
            .entry(binding)
            .or_default()
            .push(Definition {
                callable,
                span,
                initializer: initializer.and_then(ReceiverInitializer::from_expression),
            });
    }

    pub(super) fn function(
        &mut self,
        binding: Id,
        callable: Option<u32>,
        span: Span,
        no_returns: bool,
        returned_construction: Option<Id>,
    ) {
        self.functions
            .entry(binding)
            .or_default()
            .push(FunctionDeclaration {
                callable,
                span,
                no_returns,
                returned_construction,
            });
    }

    fn stable_function(&self, binding: &Id) -> Option<&FunctionDeclaration> {
        let [function] = self.functions.get(binding)?.as_slice() else {
            return None;
        };
        // Any source write, even in another activation, defeats the exact call
        // target. Parameter cells can also change through sloppy arguments.
        if self.definitions.contains_key(binding)
            || function
                .callable
                .is_some_and(|key| self.parameters[&key].contains(binding))
        {
            return None;
        }
        Some(function)
    }

    fn origins(&self, value: &ReceiverInitializer) -> Option<Vec<ReceiverOrigin>> {
        match value {
            ReceiverInitializer::ObjectLiteral => Some(vec![ReceiverOrigin::ObjectLiteral]),
            ReceiverInitializer::Construction(binding) => {
                let function = self.stable_function(binding)?;
                function.no_returns.then(|| {
                    vec![ReceiverOrigin::Constructor {
                        binding: binding.clone(),
                        span: function.span,
                    }]
                })
            }
            ReceiverInitializer::WrapperCall(binding) => {
                let wrapper = self.stable_function(binding)?;
                let constructor = wrapper.returned_construction.as_ref()?;
                let function = self.stable_function(constructor)?;
                function.no_returns.then(|| {
                    vec![ReceiverOrigin::Wrapper {
                        binding: binding.clone(),
                        span: wrapper.span,
                        constructor: constructor.clone(),
                        constructor_span: function.span,
                    }]
                })
            }
            ReceiverInitializer::Alternatives(options) => {
                let mut origins = Vec::new();
                for option in options {
                    origins.extend(self.origins(option)?);
                }
                Some(origins)
            }
        }
    }

    pub(super) fn receiver(
        &self,
        callable: Option<u32>,
        receiver: &ValueExpr,
        read_span: Span,
    ) -> Option<LocalReceiverProof> {
        let callable = callable?;
        let ValueExpr::Binding(binding) = receiver else {
            // The identity of an allocation says nothing about its members.
            return None;
        };
        if self.unsupported.contains(&callable) || self.parameters[&callable].contains(binding) {
            return None;
        }
        let definitions = self.definitions.get(binding)?;
        if definitions
            .iter()
            .any(|definition| definition.callable != Some(callable))
        {
            return None;
        }
        let definition = definitions
            .iter()
            .filter(|definition| definition.span.hi <= read_span.lo)
            .max_by_key(|definition| definition.span.hi)?;
        let value = definition.initializer.as_ref()?;
        // A definition whose evaluation overlaps the receiver read (e.g.
        // `receiver = receiver.flag`) cannot be an earlier overwrite kill.
        if definitions
            .iter()
            .any(|other| other.span.lo > definition.span.lo && other.span.lo <= read_span.lo)
        {
            return None;
        }
        Some(LocalReceiverProof {
            binding: binding.clone(),
            initializer: definition.span,
            origins: self.origins(value)?,
        })
    }
}
