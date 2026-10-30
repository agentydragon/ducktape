//! Structural callback argument evidence and its shallow native-census consumer.

use super::*;
use crate::analysis_inputs::CallableSummary;
use crate::reports::schema::NativeUnreachableBranch;

#[derive(Debug)]
pub(super) struct InlineCallbackEntry {
    pub import: Id,
    pub function: Span,
    pub call: Span,
}

#[derive(Debug)]
pub(super) enum Predicate {
    TypeOf(Id, BinaryOp, String),
    And(Box<Self>, Box<Self>),
}

impl Predicate {
    pub(super) fn from_expr(expression: &Expr) -> Option<Self> {
        let Expr::Bin(binary) = expression.unwrap_parens() else {
            return None;
        };
        if binary.op == BinaryOp::LogicalAnd {
            return Some(Self::And(
                Box::new(Self::from_expr(&binary.left)?),
                Box::new(Self::from_expr(&binary.right)?),
            ));
        }
        if !matches!(binary.op, BinaryOp::EqEqEq | BinaryOp::EqEq | BinaryOp::Lt) {
            return None;
        }
        let Expr::Unary(unary) = binary.left.unwrap_parens() else {
            return None;
        };
        let Expr::Ident(binding) = unary.arg.unwrap_parens() else {
            return None;
        };
        let Expr::Lit(Lit::Str(literal)) = binary.right.unwrap_parens() else {
            return None;
        };
        (unary.op == UnaryOp::TypeOf).then(|| {
            Self::TypeOf(
                binding.to_id(),
                binary.op,
                literal.value.to_string_lossy().into_owned(),
            )
        })
    }

    fn evaluate(&self, objects: &BTreeSet<Id>) -> Option<bool> {
        match self {
            Self::TypeOf(binding, op, literal) if objects.contains(binding) => Some(match op {
                BinaryOp::EqEq | BinaryOp::EqEqEq => literal == "object",
                BinaryOp::Lt => "object" < literal.as_str(),
                _ => return None,
            }),
            Self::And(left, right) => Some(left.evaluate(objects)? && right.evaluate(objects)?),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub(super) struct GuardedRegion {
    pub callable: u32,
    pub guard: Span,
    pub region: Span,
    pub expected: bool,
    pub predicate: Predicate,
}

/// Strengthens a derived callback factory (`key` returning `closure`): no
/// direct use returns or stores the callback, and each invocation supplies the
/// same fresh module and its fresh own data object. The anonymous consumer
/// must separately establish callback entry coverage. No intrinsic-integrity
/// input participates.
pub(super) fn ordinary_callback_arguments(
    index: &ExecutionIndex,
    key: u32,
    closure: &Range<u32>,
) -> Option<BTreeSet<usize>> {
    let outer = index.callables.get(&key)?;
    let [callback, cache] = outer.params.as_slice() else {
        return None;
    };
    let callback = callback.binding.as_ref()?;
    let cache = cache.binding.as_ref()?;
    let inner = index.callables.get(&closure.start)?;
    let [(assigned, ValueExpr::ObjectLiteral(module, fields))] = inner.local_assignments.as_slice()
    else {
        return None;
    };
    if assigned != cache || !index.plain_data_objects.contains(module) {
        return None;
    }
    let [(Some(field), ValueExpr::ObjectLiteral(value, contents))] = fields.as_slice() else {
        return None;
    };
    if !contents.is_empty() || !index.plain_data_objects.contains(value) {
        return None;
    }
    if !matches!(inner.returns.as_slice(), [ValueExpr::Member(base, Some(property), true)]
        if matches!(base.as_ref(), ValueExpr::Binding(id) if id == cache) && property == field)
    {
        return None;
    }
    let invocations: Vec<_> = inner
        .calls
        .iter()
        .filter(|call| matches!(&call.callee, ValueExpr::Binding(id) if id == callback))
        .collect();
    let [call] = invocations.as_slice() else {
        return None;
    };
    let [
        ValueExpr::Member(base, Some(property), true),
        ValueExpr::Binding(second),
    ] = call.args.as_slice()
    else {
        return None;
    };
    if property != field
        || second != cache
        || !matches!(base.as_ref(), ValueExpr::ObjectLiteral(id, _) if id == module)
        || !inner.namespace_escape_values.is_empty()
    {
        return None;
    }
    Some(BTreeSet::from([0, 1]))
}

impl ExecutionIndex {
    pub(super) fn record_guard(&mut self, test: &Expr, region: Span, expected: bool) {
        if let Some(callable) = self.current
            && let Some(predicate) = Predicate::from_expr(test)
        {
            self.native_guarded_regions.push(GuardedRegion {
                callable,
                guard: test.span(),
                region,
                expected,
                predicate,
            });
        }
    }

    fn unreachable_native_branch(
        &self,
        write: Span,
        summaries: &BTreeMap<String, CallableSummary>,
    ) -> Option<NativeUnreachableBranch> {
        if !self.dynamic_scope_owners.is_empty() {
            return None;
        }
        // A guard on an outer function does not cover a nested deferred body.
        let callable = self
            .callable_spans
            .iter()
            .filter(|(_, span)| span.lo <= write.lo && write.hi <= span.hi)
            .min_by_key(|(_, span)| span.hi.0 - span.lo.0)?
            .0;
        for region in &self.native_guarded_regions {
            if region.callable != *callable
                || region.region.lo > write.lo
                || write.hi > region.region.hi
            {
                continue;
            }
            let mut ancestor = *callable;
            let mut visited = BTreeSet::new();
            while !self.inline_callback_entries.contains_key(&ancestor) {
                if !visited.insert(ancestor) {
                    break;
                }
                let Some(Some(parent)) = self.immediate_anonymous_callers.get(&ancestor) else {
                    break;
                };
                ancestor = *parent;
            }
            let Some(entry) = self.inline_callback_entries.get(&ancestor) else {
                continue;
            };
            if !self.imported_bindings.contains(&entry.import)
                || self.property_key_assignments.contains_key(&entry.import)
            {
                continue;
            }
            let Some(arguments) = summaries
                .get(entry.import.0.as_ref())
                .and_then(|summary| summary.callback_object_arguments.as_ref())
            else {
                continue;
            };
            let Some(callback) = self.callables.get(&ancestor) else {
                continue;
            };
            if callback.implicit_capture || callback.generator || self.async_callables.contains(&ancestor)
                || callback.params.iter().any(|parameter| parameter.binding.is_none() || parameter.default.is_some())
                // Anonymous callback identity must not escape through its
                // arguments object, including a nested arrow's capture.
                || self.callables.range(entry.function.lo.0..entry.function.hi.0).any(|(key, body)| {
                    body.reads.iter().any(|id| id.0 == "arguments") || self.async_callables.contains(key)
                })
            {
                continue;
            }
            let objects: BTreeSet<_> = arguments
                .object_arguments
                .iter()
                .filter_map(|position| {
                    let binding = callback.params.get(*position)?.binding.as_ref()?;
                    // The shared key-assignment index also retains updates,
                    // destructuring, loop targets and assignments after await.
                    (self
                        .property_key_assignments
                        .get(binding)
                        .is_some_and(|values| values.len() == 1))
                    .then(|| binding.clone())
                })
                .collect();
            if region
                .predicate
                .evaluate(&objects)
                .is_some_and(|truth| truth != region.expected)
            {
                return Some(NativeUnreachableBranch {
                    arguments: arguments.clone(),
                    imported_local: entry.import.0.to_string(),
                    wrapper_call_location: None,
                    callback_location: None,
                    guard_location: None,
                    guard_span: region.guard,
                    wrapper_call_span: entry.call,
                    callback_span: entry.function,
                });
            }
        }
        None
    }

    pub(super) fn native_branch_proofs(
        &self,
        summaries: &BTreeMap<String, CallableSummary>,
    ) -> Vec<Option<NativeUnreachableBranch>> {
        if self.native_guarded_regions.is_empty()
            || summaries
                .values()
                .all(|summary| summary.callback_object_arguments.is_none())
        {
            return vec![None; self.native_write_census.conditional.len()];
        }
        self.native_write_census
            .conditional
            .iter()
            .map(|write| {
                (!write.fallback.is_empty())
                    .then(|| self.unreachable_native_branch(write.witness.span, summaries))
                    .flatten()
            })
            .collect()
    }

    pub(super) fn refine_native_branches(&mut self, summaries: &BTreeMap<String, CallableSummary>) {
        let proofs = self.native_branch_proofs(summaries);
        for (write, proof) in self.native_write_census.conditional.iter_mut().zip(proofs) {
            write.witness.unreachable_branch = proof;
        }
    }
}
