//! The allocation-only provider consumer of the shared value transfers.

use super::super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, LiteralHeap, LiteralLayout,
};
use super::super::local_transfer::{self, LocalContext, LocalFrame};
use super::super::value_model::{
    AbstractValue, Context, DenseArray, OwnPropertyShape, ProviderInput, ProviderScopeId,
    SymbolicInputId, ValueExpr, Values,
};
use super::ExecutionIndex;
use crate::analysis_inputs::{ForeignInvocation, ForeignReturn};
use crate::facts::abstract_domain::foreign_call::DerivedForeignCall;
use crate::facts::abstract_domain::value::{Kind, Value};
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

struct ProviderContext<'a> {
    index: &'a ExecutionIndex,
    scope: ProviderScopeId,
    bindings: Context,
    properties: OrdMap<u32, BTreeMap<Option<String>, Values>>,
    arrays: OrdMap<u32, DenseArray>,
    revision: u64,
}

impl AllocationContext for ProviderContext<'_> {
    fn source_covered(&self) -> bool {
        true
    }
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        allocation_transfer::closed_binding(&self.bindings, binding, self.scope)
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        allocation_transfer::transfer(self, expression)
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.index.allocation_layout(allocation)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        LiteralHeap {
            properties: &mut self.properties,
            arrays: &mut self.arrays,
            revision: &mut self.revision,
        }
    }
}

impl LocalContext for ProviderContext<'_> {
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<AllocationValue> {
        // The frame starts with scoped arguments and checked private var
        // captures. Other entries come from the covered local operations.
        Some(AllocationValue {
            values: frame.environment.get(binding)?.clone(),
            judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
        })
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.index.allocation_layout(allocation)
    }
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.index.ordinary_record_shape(allocation)
    }
    fn object_properties_are_settled(&self, _allocation: u32) -> bool {
        // This isolated query starts with an empty heap and refuses every
        // operation capable of external mutation or publication.
        true
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        AllocationContext::heap(self)
    }
    fn unproved_field_write(
        &mut self,
        _frame: &mut LocalFrame,
        _receivers: &Values,
        _values: &Values,
        _span: Span,
    ) {
        // The shared kernel returns None; there is no application state to
        // publish into, and this query cannot certify the body.
    }
    fn publish_throw(&mut self, _values: &Values) {
        // The shared abrupt result makes the allocation judgment opaque.
    }
}

/// The canonical source collector retained whole-body syntax coverage before
/// value lowering. Direct returns and ordered bodies share their transfers
/// with application execution; refusal never retries the legacy AST evaluator.
pub(super) fn derive(index: &ExecutionIndex, callable: u32) -> Option<DerivedForeignCall> {
    let body = index.callables.get(&callable)?;
    let scope = ProviderScopeId::fresh()?;
    let mut bindings = body
        .params
        .iter()
        .enumerate()
        .map(|(parameter, param)| {
            Some((
                param.binding.clone()?,
                Values::from([AbstractValue::SymbolicInput(SymbolicInputId {
                    scope,
                    input: ProviderInput::Argument(parameter),
                })]),
            ))
        })
        .collect::<Option<Context>>()?;
    let mut reads = BTreeSet::new();
    if let Some(expression) = &body.allocation_return {
        expression.bindings(&mut reads);
    } else {
        reads = body.local_body.as_ref()?.reads();
        for binding in body.local_body.as_ref()?.bindings.keys() {
            reads.remove(binding);
        }
    }
    for binding in bindings.keys() {
        reads.remove(binding);
    }
    // Property-origin export inference is the next consumer for nested
    // callable keys. The current declaration-only export collector is not
    // widened by this body/capture query.
    let captures: Vec<_> = reads.into_iter().collect();
    for (capture, binding) in captures.iter().enumerate() {
        // This establishes lexical cell initialization, not value kind, heap
        // locality, immutability or privacy of anything the cell can contain.
        if !index.private_var_binding(binding) {
            return None;
        }
        bindings.insert(
            binding.clone(),
            Values::from([AbstractValue::SymbolicInput(SymbolicInputId {
                scope,
                input: ProviderInput::Capture(capture),
            })]),
        );
    }
    let mut context = ProviderContext {
        index,
        scope,
        bindings,
        properties: OrdMap::new(),
        arrays: OrdMap::new(),
        revision: 0,
    };
    let result = if let Some(expression) = &body.allocation_return {
        allocation_transfer::transfer(&mut context, expression)?
    } else {
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        let judgment =
            local_transfer::execute(&mut context, body.local_body.as_ref()?, &mut frame)?;
        AllocationValue {
            values: frame.returns,
            judgment,
        }
    };
    let AllocationJudgment::CoveredAllocationOnly(dependencies) = result.judgment else {
        return None;
    };
    // This tranche needs only closed inputs and literal semantics. A future
    // premise-dependent transfer must first carry its dependencies through
    // DerivedForeignCall; never silently discard a child's proof conditions.
    if !dependencies.is_empty() {
        return None;
    }
    let mut returns = BTreeSet::new();
    let mut retained_arguments = BTreeSet::new();
    let mut retained_provider_captures = BTreeSet::new();
    for value in result.values {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => {
                let ProviderInput::Argument(index) = input.input else {
                    // A capture itself is not a fresh return allocation. Its
                    // exact borrowed result contract has not been modeled.
                    return None;
                };
                returns.insert(ForeignReturn::Argument { index });
            }
            AbstractValue::Value(Value::Prim(_)) => {
                returns.insert(ForeignReturn::Primitive);
            }
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                returns.insert(ForeignReturn::FreshAllocation);
                let mut pending = vec![allocation];
                let mut visited = BTreeSet::new();
                while let Some(allocation) = pending.pop() {
                    if !visited.insert(allocation) {
                        continue;
                    }
                    for value in context.properties.get(&allocation)?.values().flatten() {
                        match value {
                            AbstractValue::SymbolicInput(input) if input.scope == scope => {
                                match input.input {
                                    ProviderInput::Argument(index) => {
                                        retained_arguments.insert(index);
                                    }
                                    ProviderInput::Capture(index) => {
                                        retained_provider_captures
                                            .insert(captures.get(index)?.0.to_string());
                                    }
                                }
                            }
                            AbstractValue::Value(Value::Alloc(Kind::Object, nested)) => {
                                pending.push(*nested)
                            }
                            AbstractValue::Value(Value::Prim(_)) => {}
                            _ => return None,
                        }
                    }
                }
            }
            _ => return None,
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Call,
        returns,
        retained_arguments,
        retained_provider_captures,
        native_constructions: BTreeSet::new(),
        record: None,
        closure: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::StableArraySpreadShapes;
    use crate::facts::item_views::top_level_item_views;
    use swc_ecma_ast::{Decl, Expr, ModuleItem, Stmt};

    fn query(source: &str, syntax_covered: bool) -> Option<DerivedForeignCall> {
        let module = js_ast::parse_js_module("provider.js", source)
            .unwrap()
            .module;
        let key = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => function.function.span.lo.0,
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(variable))) => {
                let Expr::Arrow(arrow) = variable.decls[0].init.as_deref().unwrap() else {
                    panic!("arrow fixture");
                };
                arrow.span.lo.0
            }
            _ => panic!("provider fixture"),
        };
        let views = top_level_item_views(&module.body);
        let prepared = super::super::prepare_callable_capabilities(
            &views,
            StableArraySpreadShapes::default(),
            BTreeSet::from(["make".into()]),
            Vec::new(),
            Vec::new(),
            Default::default(),
            Default::default(),
        );
        assert_eq!(
            prepared.index.callables[&key].allocation_return.is_some(),
            syntax_covered,
            "{source}"
        );
        derive(&prepared.index, key)
    }

    #[test]
    fn strict_operators_and_terminal_branches_use_the_central_query() {
        js_ast::with_swc_globals(|| {
            for (expression, retained) in [
                (
                    "{ type: input, compare: compare === void 0 ? null : compare }",
                    BTreeSet::from([0, 1]),
                ),
                ("{ value: input !== compare }", BTreeSet::new()),
                ("{ value: void input }", BTreeSet::new()),
                ("{ value: input ? null : compare }", BTreeSet::from([1])),
            ] {
                for source in [
                    format!("function make(input, compare) {{ return {expression}; }}"),
                    format!("const make = (input, compare) => ({expression});"),
                ] {
                    let summary =
                        query(&source, true).expect("shared strict transfer, no legacy evaluator");
                    assert_eq!(
                        summary.returns,
                        BTreeSet::from([ForeignReturn::FreshAllocation])
                    );
                    assert_eq!(summary.retained_arguments, retained, "{source}");
                }
            }
            for expression in ["void input", "input === compare", "input !== compare"] {
                let summary = query(
                    &format!("function make(input, compare) {{ return {expression}; }}"),
                    true,
                )
                .unwrap();
                assert_eq!(summary.returns, BTreeSet::from([ForeignReturn::Primitive]));
                assert!(summary.retained_arguments.is_empty());
            }
            // Covered syntax can still exceed the terminal-arm transfer. It
            // must decline centrally, not retry the unmigrated evaluator.
            assert!(
                query(
                    "function make(input) { return input ? { input } : {}; }",
                    true
                )
                .is_none()
            );
            assert!(query("function make(input) { return input === missing; }", true).is_none());
            for expression in [
                "input == 1",
                "void input()",
                "input.value === 1",
                "input() ? null : input",
                "true ? input : input()",
                "void (input(), input)",
            ] {
                assert!(
                    query(
                        &format!("function make(input) {{ return {expression}; }}"),
                        false
                    )
                    .is_none()
                );
            }
        });
    }

    #[test]
    fn function_and_const_arrow_keys_reach_the_central_query() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make(input) { return { input }; }",
                "const make = input => ({ input });",
            ] {
                let summary = query(source, true).expect("central query, without legacy fallback");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation])
                );
                assert_eq!(summary.retained_arguments, BTreeSet::from([0]));
            }
        });
    }
}
