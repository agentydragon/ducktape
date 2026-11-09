//! Read-only observations of the real analysis driver. No fixture-specific
//! execution path or completion authority is introduced in production builds.

use super::*;
use crate::facts::abstract_domain::value::HeapModuleScope;
use std::cell::RefCell;
use swc_ecma_ast::{Decl, ModuleItem, Stmt};

enum Observation {
    Call(Option<u32>),
    Diagnostic(String),
    FunctionMethod {
        target: Option<u32>,
        method: FunctionCallMethod,
        arguments: Vec<Values>,
    },
    UnselectedCall {
        target: u32,
        site: SourceOperationSite,
    },
    CallReceipt {
        site: SourceOperationSite,
        selected: bool,
    },
    Fork(LocalExecutionScope, usize),
    Activation(
        LocalExecutionScope,
        super::super::local_transfer::LocalActivation,
    ),
    ArrayCallback {
        index: usize,
        input_branch: AlternativeBranchId,
        callback_branch: AlternativeBranchId,
        outcome: Outcome,
        state: ExecutionStateSnapshot,
    },
    ClassMemberPromotion(Option<u32>),
    Complete(LocalExecutionScope, Outcome, ExecutionStateSnapshot),
}

thread_local! {
    static OBSERVATIONS: RefCell<Option<Vec<Observation>>> = const { RefCell::new(None) };
    static UNSELECTED_TARGET: RefCell<Option<u32>> = const { RefCell::new(None) };
}

struct UnselectedTargetGuard;

impl Drop for UnselectedTargetGuard {
    fn drop(&mut self) {
        UNSELECTED_TARGET.with(|target| *target.borrow_mut() = None);
    }
}

fn force_unselected_target(target: u32) -> UnselectedTargetGuard {
    UNSELECTED_TARGET.with(|selected| *selected.borrow_mut() = Some(target));
    UnselectedTargetGuard
}

fn record(observation: impl FnOnce() -> Observation) {
    OBSERVATIONS.with(|events| {
        if let Some(events) = events.borrow_mut().as_mut() {
            events.push(observation());
        }
    });
}

pub(super) fn observe_diagnostic(message: String) {
    record(|| Observation::Diagnostic(message));
}

fn diagnostic_trace(events: &[Observation]) -> String {
    events
        .iter()
        .filter_map(|event| match event {
            Observation::Diagnostic(message) => Some(message.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn drop_selection_for(executor: &Executor<'_, '_>, call: &mut LocalCallRequest) -> bool {
    let Some(expected) = UNSELECTED_TARGET.with(|target| *target.borrow()) else {
        return false;
    };
    if !executor
        .local_call_target(call.callee.targets())
        .is_some_and(|(target, _)| target == expected)
        && executor.local_class_constructor_target(call.callee.targets()) != Some(expected)
    {
        return false;
    }
    match &mut call.callee {
        EvaluatedCallee::Direct(selected) => {
            selected.selection = None;
            true
        }
        EvaluatedCallee::Member {
            selection_dependencies,
            ..
        } => {
            *selection_dependencies = None;
            true
        }
        _ => false,
    }
}

pub(super) fn observe_call(
    executor: &Executor<'_, '_>,
    call: &LocalCallRequest,
    forced_unselected: bool,
    pending_argument: Option<String>,
) {
    let target = executor
        .local_call_target(call.callee.targets())
        .map(|(key, _)| key)
        .or_else(|| executor.local_class_constructor_target(call.callee.targets()));
    let callee = match &call.callee {
        EvaluatedCallee::Direct(selected) => format!("direct targets={:?}", selected.values),
        EvaluatedCallee::Member {
            receiver,
            targets,
            array_method,
            array_iteration_refusal,
            ..
        } => {
            let method = match array_method.as_deref() {
                Some(ArrayMethodSelection::Push(selection)) => {
                    format!("push object={:?}", selection.object)
                }
                Some(ArrayMethodSelection::Iteration(selection)) => format!(
                    "iteration object={:?} row={} site={:?}",
                    selection.object, selection.row.member, selection.site
                ),
                None => "ordinary-member".to_owned(),
            };
            format!(
                "member receiver={receiver:?} targets={targets:?} selection={method} pre_argument_refusal={array_iteration_refusal:?}"
            )
        }
        EvaluatedCallee::Native { targets, .. } => format!("native targets={targets:?}"),
        EvaluatedCallee::FunctionCallMethod {
            method,
            target,
            receiver,
            ..
        } => format!(
            "function-method method={method:?} target={:?} receiver={receiver:?}",
            target.values
        ),
    };
    record(|| {
        Observation::Diagnostic(format!(
            "call-suspend span={:?} target={target:?} args={} callee={callee} forced_unselected={forced_unselected} pending_argument={pending_argument:?} branch={:?}",
            call.span,
            call.arguments.len(),
            executor.queries.branch_lineage(),
        ))
    });
    record(|| Observation::Call(target));
    if let EvaluatedCallee::FunctionCallMethod { method, .. } = &call.callee {
        record(|| Observation::FunctionMethod {
            target: executor
                .local_call_target(call.callee.targets())
                .map(|(key, _)| key),
            method: *method,
            arguments: call
                .arguments
                .iter()
                .map(|argument| argument.values.clone())
                .collect(),
        });
    }
    if forced_unselected
        && let Some(target) = executor
            .local_call_target(call.callee.targets())
            .map(|(key, _)| key)
            .or_else(|| executor.local_class_constructor_target(call.callee.targets()))
    {
        record(|| Observation::UnselectedCall {
            target,
            site: call.source_site.clone(),
        });
    }
}

pub(super) fn observe_activation(
    scope: LocalExecutionScope,
    activation: &super::super::local_transfer::LocalActivation,
) {
    record(|| Observation::Activation(scope, activation.clone()));
}

pub(super) fn observe_fork(scope: LocalExecutionScope, count: usize) {
    record(|| Observation::Fork(scope, count));
}

pub(super) fn observe_array_callback(
    executor: &Executor<'_, '_>,
    index: usize,
    input_branch: AlternativeBranchId,
    callback_branch: AlternativeBranchId,
    outcome: &Outcome,
) {
    record(|| Observation::ArrayCallback {
        index,
        input_branch,
        callback_branch,
        outcome: outcome.clone(),
        state: executor.state.0.as_ref().unwrap().clone(),
    });
}

pub(super) fn observe_complete(
    executor: &Executor<'_, '_>,
    scope: LocalExecutionScope,
    outcome: &Outcome,
) {
    record(|| {
        Observation::Complete(
            scope,
            outcome.clone(),
            executor.state.0.as_ref().unwrap().clone(),
        )
    });
}

pub(super) fn observe_class_member_promotion(
    executor: &Executor<'_, '_>,
    class: ClassDefinitionId,
) {
    record(|| Observation::ClassMemberPromotion(executor.local_class_origin(class)));
}

pub(super) fn observe_call_receipt(site: &SourceOperationSite, selected: bool) {
    record(|| Observation::CallReceipt {
        site: site.clone(),
        selected,
    });
}

fn is_callable(scope: &LocalExecutionScope, key: u32) -> bool {
    matches!(scope, LocalExecutionScope::Callable(actual) if *actual == key)
}

fn primitive_kind(values: &Values, kind: analysis_model::PrimKinds) -> bool {
    values == &Values::from([AbstractValue::Value(Value::Prim(kind))])
}

fn completed_for(
    events: &[Observation],
    callable: u32,
) -> Vec<(&Outcome, &ExecutionStateSnapshot)> {
    events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, callable) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect()
}

fn returned_object_origin(outcome: &Outcome) -> crate::facts::abstract_domain::value::HeapAddress {
    outcome
        .ledger
        .may_normal()
        .and_then(|selected| {
            selected.values.iter().find_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, address)) => Some(*address),
                _ => None,
            })
        })
        .expect("normal projection retains the source aggregate allocation")
}

#[test]
fn opaque_prefix_resumes_aggregate_suffix_without_closed_layout_receipts() {
    let module = raw_js_test_support::parse(
        r#"
function exactRecord() { return { value: 1 }; }
function opaqueRecord() { unknown_record_prefix; return { value: 1 }; }
function exactArray() { return [1]; }
function opaqueArray() { unknown_array_prefix; return [1]; }
function unreachableSuffix() { return 3; }
function opaqueThenThrow() {
  unknown_throw_prefix;
  throw 7;
  unreachableSuffix();
}
const records = [exactRecord(), opaqueRecord()];
const arrays = [exactArray(), opaqueArray()];
opaqueThenThrow();
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());

    let exact_records = completed_for(&events, keys["exactRecord"]);
    assert!(!exact_records.is_empty());
    for (outcome, state) in exact_records {
        assert!(outcome.ledger.is_exact_normal());
        let origin = returned_object_origin(outcome);
        assert!(matches!(
            state.world.get().writable_prefix.get(&origin),
            Some(super::super::allocation_transfer::WritablePrefixState::Proven(properties))
                if properties == &BTreeSet::from(["value".to_owned()])
        ));
    }

    let opaque_records = completed_for(&events, keys["opaqueRecord"]);
    assert!(!opaque_records.is_empty());
    for (outcome, state) in opaque_records {
        assert!(!outcome.ledger.is_exact_normal());
        assert!(outcome.ledger.closed_effect_dependencies().is_none());
        let origin = returned_object_origin(outcome);
        assert!(
            state
                .world
                .get()
                .prefix_object_properties
                .get(&origin)
                .is_some_and(|properties| properties.contains_key(&Some("value".to_owned())))
        );
        assert!(matches!(
            state.world.get().writable_prefix.get(&origin),
            Some(super::super::allocation_transfer::WritablePrefixState::Unknown)
        ));
    }

    let exact_arrays = completed_for(&events, keys["exactArray"]);
    assert!(!exact_arrays.is_empty());
    for (outcome, state) in exact_arrays {
        assert!(outcome.ledger.is_exact_normal());
        let origin = returned_object_origin(outcome);
        assert!(matches!(
            state.world.get().writable_prefix.get(&origin),
            Some(super::super::allocation_transfer::WritablePrefixState::DenseArray { length: 1 })
        ));
        assert!(state.world.get().dense_arrays.contains_key(&origin));
    }

    let opaque_arrays = completed_for(&events, keys["opaqueArray"]);
    assert!(!opaque_arrays.is_empty());
    for (outcome, state) in opaque_arrays {
        assert!(!outcome.ledger.is_exact_normal());
        assert!(outcome.ledger.closed_effect_dependencies().is_none());
        let origin = returned_object_origin(outcome);
        assert!(matches!(
            state.world.get().writable_prefix.get(&origin),
            Some(super::super::allocation_transfer::WritablePrefixState::Unknown)
        ));
        assert!(!state.world.get().dense_arrays.contains_key(&origin));
    }

    let abrupt = completed_for(&events, keys["opaqueThenThrow"]);
    assert!(!abrupt.is_empty());
    assert!(abrupt.iter().all(|(outcome, _)| {
        outcome.ledger.may_normal().is_none()
            && !outcome
                .ledger
                .completion
                .clone()
                .into_alternatives()
                .1
                .is_empty()
            && outcome.ledger.closed_effect_dependencies().is_none()
    }));
    assert!(!events.iter().any(
        |event| matches!(event, Observation::Call(Some(key)) if *key == keys["unreachableSuffix"])
    ));
}

#[test]
fn abstract_call_branches_keep_catch_worlds_prefixes_and_captured_roots() {
    let module = raw_js_test_support::parse(
        r#"
let state = null;
function prefix() { return 0; }
function choose(flag) {
  if (flag) {
    state = "thrown";
    const capturedPayload = "live";
    throw () => capturedPayload;
  }
  state = false;
  return 3;
}
function run(flag) {
  prefix();
  try {
    choose(flag);
    state = 4;
    return () => state;
  } catch (payload) {
    return payload;
  }
}
const result = run(true);
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    // This domain deliberately forgets literal boolean truthiness. Assert
    // the abstract input, then require the actual production driver to fork.
    assert!(matches!(
        allocation_transfer::truthiness(&Values::from([AbstractValue::Value(Value::Prim(
            analysis_model::PrimKinds::BOOLEAN
        ))])),
        Some(allocation_transfer::Truthiness::Maybe)
    ));
    let _unselected_target = force_unselected_target(keys["choose"]);
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());
    let choose = keys["choose"];
    let run = keys["run"];
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event, Observation::Call(Some(key)) if *key == keys["prefix"]))
            .count(),
        1,
        "the evaluated prefix must not replay in the two child worlds"
    );
    assert_eq!(
        events
            .iter()
            .filter(
                |event| matches!(event, Observation::Fork(scope, 2) if is_callable(scope, choose))
            )
            .count(),
        1,
        "the unknown test must reach a real family fork, not an opaque body refusal"
    );
    let choose_leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, choose) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(choose_leaves.len(), 2);
    let unselected_call_sites = events
        .iter()
        .filter_map(|event| match event {
            Observation::UnselectedCall { target, site } if *target == choose => Some(site),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        unselected_call_sites.len(),
        1,
        "the singleton route must be exercised with selection evidence removed"
    );
    let unselected_call_site = unselected_call_sites[0];
    assert_eq!(
        choose_leaves
            .iter()
            .filter(|(outcome, _)| matches!(outcome.ledger.completion, Completion::Normal(_)))
            .count(),
        1
    );
    assert_eq!(
        choose_leaves
            .iter()
            .filter(|(outcome, _)| matches!(outcome.ledger.completion, Completion::Abrupt(_)))
            .count(),
        1
    );
    let run_leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, run) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        run_leaves.len(),
        2,
        "both caller continuations must finish separately"
    );
    let state_binding = ("state".into(), swc_common::SyntaxContext::empty());
    let payload_binding = ("capturedPayload".into(), swc_common::SyntaxContext::empty());
    let mut normal = 0;
    let mut caught = 0;
    for (outcome, state) in run_leaves {
        assert!(
            !outcome
                .ledger
                .effects
                .has_local_call(&unselected_call_site.body, unselected_call_site),
            "family execution without selection evidence must not mint an exact local-call receipt"
        );
        let Completion::Normal(selected) = &outcome.ledger.completion else {
            panic!("the caller must return normally on each handled path")
        };
        let values = &state.world.get().prefix_values[&state_binding];
        assert!(
            !values.contains(&AbstractValue::Value(Value::Top)),
            "retained source worlds must not be widened merely for incomplete skip coverage"
        );
        if values.contains(&AbstractValue::Value(Value::Prim(
            analysis_model::PrimKinds::NUMBER,
        ))) {
            assert!(
                !values.contains(&AbstractValue::Value(Value::Prim(
                    analysis_model::PrimKinds::STRING
                ))),
                "normal world must not import the sibling throw store"
            );
            normal += 1;
        } else {
            assert!(
                values.contains(&AbstractValue::Value(Value::Prim(
                    analysis_model::PrimKinds::STRING
                ))),
                "the throw world must skip the numeric suffix store"
            );
            caught += 1;
            let Some(AbstractValue::Closure(_, captured)) = selected.values.first() else {
                panic!("catch must return the actual thrown closure")
            };
            let cell = captured
                .values
                .cells
                .get(&payload_binding)
                .expect("the thrown closure retains its departed lexical region");
            assert!(primitive_kind(
                &state.world.get().lexical_cells[cell.0].values,
                analysis_model::PrimKinds::STRING
            ));
        }
        // Missing branch skip receipts still forbid a closed-effect claim;
        // exact completion and retained worlds are independently observable.
        assert!(outcome.ledger.closed_effect_dependencies().is_none());
    }
    assert_eq!((normal, caught), (1, 1));
}

#[test]
fn optional_member_and_callee_leaves_skip_arguments_and_keep_receiver() {
    let module = raw_js_test_support::parse(
        r#"
function target(value) { return this; }
function receiver(flag) {
  if (flag) return null;
  return { method: target };
}
function holder(flag) {
  if (flag) return { method: undefined };
  return { method: target };
}
function argument() { return 1; }
function runReceiver(flag) { return receiver(flag)?.method(argument()); }
function runCallee(flag) { return holder(flag).method?.(argument()); }
function runRetained() {
  let current = { method: target };
  return current.method?.(current = null);
}
const receiverResult = runReceiver(true);
const calleeResult = runCallee(true);
const retainedResult = runRetained();
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());
    for (name, count) in [
        ("receiver", 1),
        ("holder", 1),
        ("argument", 2),
        ("target", 3),
    ] {
        assert_eq!(
            events
                .iter()
                .filter(|event| matches!(event, Observation::Call(Some(key)) if *key == keys[name]))
                .count(),
            count,
            "{name}: skipped leaves must not evaluate operands or replay receivers\n{}",
            diagnostic_trace(&events),
        );
    }
    let retained = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, _) if is_callable(scope, keys["runRetained"]) => {
                Some(outcome)
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(retained.len(), 1);
    assert!(
        matches!(&retained[0].ledger.completion, Completion::Normal(selected)
        if selected.values.len() == 1 && matches!(selected.values.first(), Some(AbstractValue::Value(Value::Alloc(Kind::Object, _))))),
        "argument rebinding must not replace the selected member receiver"
    );
    for name in ["runReceiver", "runCallee"] {
        let leaves = events
            .iter()
            .filter_map(|event| match event {
                Observation::Complete(scope, outcome, _) if is_callable(scope, keys[name]) => {
                    Some(outcome)
                }
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(
            leaves.len(),
            2,
            "{name}: both optional completions retain independent worlds"
        );
        let mut skipped = 0;
        let mut invoked = 0;
        for outcome in leaves {
            let Completion::Normal(selected) = &outcome.ledger.completion else {
                panic!("{name}: supported optional paths must complete exactly");
            };
            if selected.values == Values::from([AbstractValue::UNDEFINED]) {
                skipped += 1;
            } else {
                assert_eq!(selected.values.len(), 1);
                assert!(
                    matches!(
                        selected.values.first(),
                        Some(AbstractValue::Value(Value::Alloc(Kind::Object, _)))
                    ),
                    "the member call returns its retained receiver as this"
                );
                invoked += 1;
            }
        }
        assert_eq!((skipped, invoked), (1, 1), "{name}");
    }
}

#[test]
fn conditional_and_logical_operands_resume_only_selected_expression_paths() {
    let module = raw_js_test_support::parse(
        r#"
function yes() { return "selected"; }
function no() { return 1; }
function conditional(flag) { return flag ? yes() : no(); }
function and(flag) { return flag && yes(); }
function or(flag) { return flag || yes(); }
function coalesce(flag) { return (flag ? null : false) ?? yes(); }
const a = conditional(true);
const b = and(true);
const c = or(true);
const d = coalesce(true);
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());
    for (name, count) in [("yes", 4), ("no", 1)] {
        assert_eq!(
            events
                .iter()
                .filter(|event| matches!(event, Observation::Call(Some(key)) if *key == keys[name]))
                .count(),
            count,
            "{name}: only selected operands may execute"
        );
    }
    for (name, skipped_kind) in [
        ("conditional", analysis_model::PrimKinds::NUMBER),
        ("and", analysis_model::PrimKinds::BOOLEAN),
        ("or", analysis_model::PrimKinds::BOOLEAN),
        ("coalesce", analysis_model::PrimKinds::BOOLEAN),
    ] {
        let leaves = events
            .iter()
            .filter_map(|event| match event {
                Observation::Complete(scope, outcome, _) if is_callable(scope, keys[name]) => {
                    Some(outcome)
                }
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(
            leaves.len(),
            2,
            "{name}: source choices must retain both exact worlds"
        );
        let mut skipped = 0;
        let mut selected = 0;
        for outcome in leaves {
            let Completion::Normal(value) = &outcome.ledger.completion else {
                panic!("{name}: selected expressions must complete exactly");
            };
            if primitive_kind(&value.values, skipped_kind) {
                skipped += 1;
            } else {
                assert!(primitive_kind(
                    &value.values,
                    analysis_model::PrimKinds::STRING
                ));
                selected += 1;
            }
            assert!(outcome.ledger.closed_effect_dependencies().is_none());
        }
        assert_eq!((skipped, selected), (1, 1), "{name}");
    }
}

#[test]
fn array_callback_family_keeps_abrupt_leaf_terminal_and_resumes_normal_leaf() {
    let module = raw_js_test_support::parse(
        r#"
let state = null;
function callback(flag) {
  if (flag) {
    state = "thrown";
    const capturedPayload = "live";
    throw () => capturedPayload;
  }
  state = false;
  return null;
}
function run(flag) {
  const entries = [flag, undefined];
  try {
    entries.some(callback);
    state = 4;
    return () => state;
  } catch (payload) {
    return payload;
  }
}
const result = run(true);
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    assert!(matches!(
        allocation_transfer::truthiness(&Values::from([AbstractValue::Value(Value::Prim(
            analysis_model::PrimKinds::BOOLEAN
        ))])),
        Some(allocation_transfer::Truthiness::Maybe)
    ));

    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());
    let callback = keys["callback"];
    let run = keys["run"];
    let state_binding = ("state".into(), swc_common::SyntaxContext::empty());
    let payload_binding = ("capturedPayload".into(), swc_common::SyntaxContext::empty());
    let first_index = events
        .iter()
        .filter_map(|event| match event {
            Observation::ArrayCallback {
                index: 0,
                input_branch,
                callback_branch,
                outcome,
                state,
                ..
            } => Some((*input_branch, *callback_branch, outcome, state)),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        first_index.len(),
        2,
        "the first callback's abstract Boolean must produce both typed paths\n{}",
        diagnostic_trace(&events),
    );
    assert_eq!(
        first_index[0].0, first_index[1].0,
        "the normal and abrupt leaves come from one callback invocation"
    );
    let normal_branch = first_index
        .iter()
        .find(|(_, _, outcome, _)| outcome.ledger.is_exact_normal())
        .map(|(_, branch, _, _)| *branch)
        .expect("one callback leaf continues normally");
    let abrupt_branch = first_index
        .iter()
        .find(|(_, _, outcome, _)| outcome.ledger.is_exact_abrupt())
        .map(|(_, branch, _, _)| *branch)
        .expect("one callback leaf throws exactly");
    assert_ne!(normal_branch, abrupt_branch);
    for (_, _, outcome, state) in &first_index {
        let values = &state.world.get().prefix_values[&state_binding];
        if outcome.ledger.is_exact_normal() {
            assert!(
                outcome
                    .ledger
                    .may_normal()
                    .is_some_and(|normal| !normal.values.is_empty())
            );
            assert!(values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::BOOLEAN,
            ))));
            assert!(!values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING,
            ))));
        } else {
            let Completion::Abrupt(thrown) = &outcome.ledger.completion else {
                panic!("callback throw remains an exact abrupt leaf")
            };
            assert!(values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING,
            ))));
            let Some(AbstractValue::Closure(_, captured)) = thrown.value.values.first() else {
                panic!("the exact callback throw retains its closure payload")
            };
            let cell = captured
                .values
                .cells
                .get(&payload_binding)
                .expect("the thrown callback closure retains its departed lexical cell");
            assert!(primitive_kind(
                &state.world.get().lexical_cells[cell.0].values,
                analysis_model::PrimKinds::STRING
            ));
        }
    }

    let second_index = events
        .iter()
        .filter_map(|event| match event {
            Observation::ArrayCallback {
                index: 1,
                input_branch,
                outcome,
                ..
            } => Some((*input_branch, outcome)),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        second_index.len(),
        1,
        "the exact throw leaf must not invoke the callback at the next index"
    );
    assert_eq!(second_index[0].0, normal_branch);
    assert!(second_index[0].1.ledger.is_exact_normal());
    assert!(!first_index.iter().any(|(_, branch, outcome, _)| {
        outcome.ledger.is_exact_abrupt() && *branch == second_index[0].0
    }));

    let run_leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, run) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(run_leaves.len(), 2, "caller catch keeps both worlds");
    let mut normal = 0;
    let mut caught = 0;
    for (outcome, state) in run_leaves {
        let Completion::Normal(selected) = &outcome.ledger.completion else {
            panic!("run handles the callback throw on its catch path")
        };
        let values = &state.world.get().prefix_values[&state_binding];
        if values.contains(&AbstractValue::Value(Value::Prim(
            analysis_model::PrimKinds::NUMBER,
        ))) {
            assert!(!values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING,
            ))));
            normal += 1;
        } else {
            assert!(values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING,
            ))));
            let Some(AbstractValue::Closure(_, captured)) = selected.values.first() else {
                panic!("the catch result retains the actually thrown callback closure")
            };
            let cell = captured
                .values
                .cells
                .get(&payload_binding)
                .expect("the thrown callback closure retains its departed lexical cell");
            assert!(primitive_kind(
                &state.world.get().lexical_cells[cell.0].values,
                analysis_model::PrimKinds::STRING
            ));
            caught += 1;
        }
    }
    assert_eq!((normal, caught), (1, 1));
    assert!(
        events.iter().any(|event| matches!(event,
            Observation::ArrayCallback { outcome, .. } if outcome.body_invoked
        )),
        "the row observer must capture an actually invoked, not merely ordered, callback"
    );
    assert!(events.iter().any(|event| matches!(event,
        Observation::Complete(scope, _, _) if is_callable(scope, callback)
    )));
}

#[test]
fn class_static_rhs_families_commit_only_normal_fields_and_restore_receiver() {
    let module = raw_js_test_support::parse(
        r#"
let state = null;
function prefix() { return 0; }
function choose(flag) {
  if (flag) {
    state = "thrown";
    const capturedPayload = "live";
    throw () => capturedPayload;
  }
  state = false;
  return 3;
}
function later() { state = 4; return true; }
function run(flag) {
  try {
    const Defined = class {
      static first = prefix();
      static middle = choose(flag);
      static { later(); }
      static captured = () => this;
    };
    return () => (this, Defined);
  } catch (payload) {
    return () => (this, payload);
  }
}
const receiver = { build: run };
const result = receiver.build(true);
"#,
    );
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());
    for name in ["prefix", "choose", "later"] {
        assert_eq!(
            events
                .iter()
                .filter(|event| matches!(event,
                    Observation::Call(Some(key)) if *key == keys[name]
                ))
                .count(),
            1,
            "{name} executes once: the throw leaf skips later static steps"
        );
    }
    assert!(
        events.iter().any(|event| matches!(event,
            Observation::Fork(scope, 2) if is_callable(scope, keys["choose"])
        )),
        "the static RHS must actually fork through the production driver"
    );
    let leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, keys["run"]) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        leaves.len(),
        2,
        "class normal and catch paths retain separate worlds"
    );
    let id = |name: &str| -> swc_ecma_ast::Id { (name.into(), swc_common::SyntaxContext::empty()) };
    let mut completed = 0;
    let mut caught = 0;
    for (outcome, state) in leaves {
        let Completion::Normal(selected) = &outcome.ledger.completion else {
            panic!("the caller catches the actual static initializer throw")
        };
        let Some(AbstractValue::Closure(_, result_capture)) = selected.values.first() else {
            panic!("the result retains the class or caught payload")
        };
        let world = state.world.get();
        assert_eq!(
            result_capture.values.receiver.as_ref(),
            Some(&world.prefix_values[&id("receiver")]),
            "both exits restore the surrounding method's this before creating its arrow"
        );
        let is_completed = result_capture.values.cells.contains_key(&id("Defined"));
        let class_values = if is_completed {
            let cell = result_capture.values.cells[&id("Defined")];
            &world.lexical_cells[cell.0].values
        } else {
            &outcome.reached
        };
        let class = class_values
            .iter()
            .find_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Class, object)) => Some(*object),
                _ => None,
            })
            .expect("the completed or unfinished class stays rooted in its own world");
        let wrote =
            |property: &str| {
                outcome.effects.heap_contents.writes.contains(
                    &crate::facts::EffectCell::OwnProperty {
                        origin: class,
                        property: property.to_owned(),
                    },
                )
            };
        assert!(
            wrote("first"),
            "the completed prefix is retained on both paths"
        );
        let state_values = &world.prefix_values[&id("state")];
        assert!(!state_values.contains(&AbstractValue::Value(Value::Top)));
        if is_completed {
            completed += 1;
            assert!(wrote("middle") && wrote("captured"));
            assert!(state_values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::NUMBER
            ))));
            assert!(!state_values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING
            ))));
            let field = &world.prefix_object_properties[&class][&Some("captured".to_owned())];
            let Some(AbstractValue::Closure(_, class_capture)) = field.first() else {
                panic!("static arrow field retains the definition receiver")
            };
            assert_eq!(
                class_capture.values.receiver.as_ref(),
                Some(&Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Class,
                    class
                )),]))
            );
        } else {
            caught += 1;
            assert!(
                !wrote("middle") && !wrote("captured"),
                "no DefineField runs after the throw"
            );
            assert!(
                !result_capture.values.cells.contains_key(&id("Defined")),
                "unfinished class never publishes its declaration binding"
            );
            assert!(state_values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::STRING
            ))));
            assert!(!state_values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::NUMBER
            ))));
            let payload_cell = result_capture.values.cells[&id("payload")];
            let Some(AbstractValue::Closure(_, thrown_capture)) =
                world.lexical_cells[payload_cell.0].values.first()
            else {
                panic!("catch retains the actual thrown closure")
            };
            let captured_cell = thrown_capture.values.cells[&id("capturedPayload")];
            assert!(primitive_kind(
                &world.lexical_cells[captured_cell.0].values,
                analysis_model::PrimKinds::STRING
            ));
        }
        assert!(
            outcome.ledger.closed_effect_dependencies().is_none(),
            "missing skip receipts still prevent closed effect authority"
        );
    }
    assert_eq!((completed, caught), (1, 1));
}

#[test]
fn constructor_family_keeps_field_and_body_worlds_through_caller_catch() {
    let module = raw_js_test_support::parse(
        r#"
let state = null;
const unusedRead = 9;
function fieldValue() { state = true; return 1; }
function bodyValue(flag) {
  if (flag) {
    state = "throw";
    const capturedPayload = "retained";
    throw () => capturedPayload;
  }
  state = false;
  return 2;
}

class Box {
  field = fieldValue();
  constructor(flag) { this.body = bodyValue(flag); }
  unused() { return unusedRead; }
}
function caller(flag) {
  try {
    const result = new Box(flag).body;
    state = 4;
    return result;
  } catch (payload) {
    return payload;
  }
}
const result = caller(true);
"#,
    );
    let mut callables = BTreeMap::new();
    let mut class = None;
    for item in &module.body {
        let ModuleItem::Stmt(Stmt::Decl(declaration)) = item else {
            continue;
        };
        match declaration {
            Decl::Fn(declaration) => {
                callables.insert(
                    declaration.ident.sym.to_string(),
                    declaration.function.span.lo.0,
                );
            }
            Decl::Class(declaration) if declaration.ident.sym == "Box" => {
                class = Some(declaration.class.span.lo.0);
            }
            _ => {}
        }
    }
    let class = class.expect("fixture declares Box");
    let _unselected_constructor = force_unselected_target(class);
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());

    let constructor_sites = events
        .iter()
        .filter_map(|event| match event {
            Observation::UnselectedCall { target, site } if *target == class => Some(site),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        constructor_sites.len(),
        1,
        "new Box must take the family path"
    );
    let constructor_site = constructor_sites[0];
    assert_eq!(constructor_site.step, OperationStep::CallInvocation);
    let receipt_observations = events
        .iter()
        .filter_map(|event| match event {
            Observation::CallReceipt { site, selected } if site == constructor_site => {
                Some(*selected)
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert!(!receipt_observations.is_empty());
    assert!(
        receipt_observations.iter().all(|selected| !selected),
        "an unselected constructor family must not invent selection evidence"
    );

    for (name, count) in [("fieldValue", 1), ("bodyValue", 1)] {
        assert_eq!(
            events
                .iter()
                .filter(
                    |event| matches!(event, Observation::Call(Some(key)) if *key == callables[name])
                )
                .count(),
            count,
            "{name} executes once; constructor body does not replay field-prefix effects"
        );
    }

    let constructor_leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(LocalExecutionScope::Construction(actual), outcome, state)
                if *actual == class =>
            {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(constructor_leaves.len(), 2);
    assert_eq!(
        constructor_leaves
            .iter()
            .filter(|(outcome, _)| outcome.ledger.is_exact_normal())
            .count(),
        1,
        "only the normal body leaf publishes an instance"
    );
    assert_eq!(
        constructor_leaves
            .iter()
            .filter(|(outcome, _)| outcome.ledger.is_exact_abrupt())
            .count(),
        1,
        "the throwing body leaf remains terminal"
    );

    let caller = callables["caller"];
    let state_binding = ("state".into(), swc_common::SyntaxContext::empty());
    let payload_binding = ("capturedPayload".into(), swc_common::SyntaxContext::empty());
    let caller_leaves = events
        .iter()
        .filter_map(|event| match event {
            Observation::Complete(scope, outcome, state) if is_callable(scope, caller) => {
                Some((outcome, state))
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        caller_leaves.len(),
        2,
        "caller catch retains both constructor worlds"
    );
    let mut normal = 0;
    let mut caught = 0;
    for (outcome, state) in caller_leaves {
        assert!(matches!(outcome.ledger.completion, Completion::Normal(_)));
        assert!(
            !outcome
                .ledger
                .effects
                .has_local_call(&constructor_site.body, constructor_site),
            "removing constructor selection must not mint a selected-call receipt"
        );
        let values = &state.world.get().prefix_values[&state_binding];
        assert!(!values.contains(&AbstractValue::Value(Value::Top)));
        if values.contains(&AbstractValue::Value(Value::Prim(
            analysis_model::PrimKinds::STRING,
        ))) {
            caught += 1;
            assert!(!values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::NUMBER,
            ))));
            let Completion::Normal(selected) = &outcome.ledger.completion else {
                unreachable!()
            };
            let Some(AbstractValue::Closure(_, captured)) = selected.values.first() else {
                panic!("catch returns the actual constructor-body throw payload")
            };
            let cell = captured
                .values
                .cells
                .get(&payload_binding)
                .expect("the thrown closure retains its departed lexical cell");
            assert!(primitive_kind(
                &state.world.get().lexical_cells[cell.0].values,
                analysis_model::PrimKinds::STRING
            ));
        } else {
            normal += 1;
            assert!(values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::NUMBER,
            ))));
            assert!(values.contains(&AbstractValue::Value(Value::Prim(
                analysis_model::PrimKinds::BOOLEAN,
            ))));
            let Completion::Normal(selected) = &outcome.ledger.completion else {
                unreachable!()
            };
            assert!(
                primitive_kind(&selected.values, analysis_model::PrimKinds::NUMBER),
                "the normal caller leaf reads the constructed receiver's body field"
            );
        }
    }
    assert_eq!((normal, caught), (1, 1));
    assert!(
        !events.iter().any(|event| matches!(
            event,
            Observation::ClassMemberPromotion(Some(actual)) if *actual == class
        )),
        "a checked direct construction does not promote the uninvoked method"
    );
    let result_binding = ("result".into(), swc_common::SyntaxContext::empty());
    let root_calls = analysis
        .facts
        .iter()
        .find(|fact| fact.declared.contains(&result_binding))
        .and_then(|fact| fact.synchronous_calls.as_ref())
        .expect("the analyzed root result retains synchronous-call facts");
    assert!(
        !root_calls
            .reads
            .iter()
            .any(|(binding, _)| binding.0 == "unusedRead"),
        "the constructor owner must not inherit a read from an uninvoked method"
    );
}

#[test]
fn constructor_family_accepts_an_evaluated_class_alias_without_a_receipt() {
    let module = raw_js_test_support::parse(
        r#"
class Box {}
const Alias = Box;
const result = new Alias();
"#,
    );
    let class = module
        .body
        .iter()
        .find_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration)))
                if declaration.ident.sym == "Box" =>
            {
                Some(declaration.class.span.lo.0)
            }
            _ => None,
        })
        .expect("fixture declares Box");
    let _unselected_constructor = force_unselected_target(class);
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());

    let site = events
        .iter()
        .find_map(|event| match event {
            Observation::UnselectedCall { target, site } if *target == class => Some(site),
            _ => None,
        })
        .expect("the class alias reaches the unselected constructor call");
    assert!(events.iter().any(|event| matches!(
        event,
        Observation::Complete(LocalExecutionScope::Construction(actual), _, _) if *actual == class
    )), "an evaluated singleton class alias uses the shared constructor family");
    let receipts = events
        .iter()
        .filter_map(|event| match event {
            Observation::CallReceipt {
                site: actual,
                selected,
            } if actual == site => Some(*selected),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert!(!receipts.is_empty());
    assert!(receipts.iter().all(|selected| !selected));
}

#[test]
fn checked_member_constructor_uses_its_existing_selection_dependencies() {
    let module = raw_js_test_support::parse(
        r#"
class Box {}
const holder = { Box };
const result = new holder.Box();
"#,
    );
    let class = module
        .body
        .iter()
        .find_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration)))
                if declaration.ident.sym == "Box" =>
            {
                Some(declaration.class.span.lo.0)
            }
            _ => None,
        })
        .expect("fixture declares Box");
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());

    let site = events
        .iter()
        .find_map(|event| match event {
            Observation::CallReceipt { site, .. } if site.step == OperationStep::CallInvocation => {
                Some(site)
            }
            _ => None,
        })
        .expect("the selected member construction records its canonical call site");
    assert!(events.iter().any(|event| matches!(
        event,
        Observation::Complete(LocalExecutionScope::Construction(actual), _, _) if *actual == class
    )));
    let receipts = events
        .iter()
        .filter_map(|event| match event {
            Observation::CallReceipt {
                site: actual,
                selected,
            } if actual == site => Some(*selected),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert!(!receipts.is_empty());
    assert!(receipts.iter().all(|selected| *selected));
}

#[test]
fn unchecked_member_constructor_does_not_enter_the_family() {
    let module = raw_js_test_support::parse(
        r#"
class Box {}
const holder = { Box };
const result = new holder.Box();
"#,
    );
    let class = module
        .body
        .iter()
        .find_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration)))
                if declaration.ident.sym == "Box" =>
            {
                Some(declaration.class.span.lo.0)
            }
            _ => None,
        })
        .expect("fixture declares Box");
    let _unselected_constructor = force_unselected_target(class);
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    let events = OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap());

    let site = events
        .iter()
        .find_map(|event| match event {
            Observation::UnselectedCall { target, site } if *target == class => Some(site),
            _ => None,
        })
        .expect("the opposing control reaches the member call with its selection removed");
    assert!(!events.iter().any(|event| matches!(
        event,
        Observation::Complete(LocalExecutionScope::Construction(actual), _, _) if *actual == class
    )), "an unchecked member selection is not enough to enter constructor-family execution");
    assert!(
        !events.iter().any(|event| matches!(
            event,
            Observation::CallReceipt { site: actual, .. } if actual == site
        )),
        "the refused member path does not publish a selected-call receipt"
    );
}

fn observe_activation_source(source: &str) -> (BTreeMap<String, u32>, Vec<Observation>) {
    let module = raw_js_test_support::parse(source);
    let keys = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some((
                declaration.ident.sym.to_string(),
                declaration.function.span.lo.0,
            )),
            _ => None,
        })
        .collect();
    OBSERVATIONS.with(|events| *events.borrow_mut() = Some(Vec::new()));
    let _analysis = crate::facts::analyze_chunk(
        &module,
        &crate::analysis_inputs::AnalysisInputs::default(),
        None,
        |_| None,
    );
    (
        keys,
        OBSERVATIONS.with(|events| events.borrow_mut().take().unwrap()),
    )
}

fn activations_for(
    events: &[Observation],
    key: u32,
) -> Vec<&super::super::local_transfer::LocalActivation> {
    events
        .iter()
        .filter_map(|event| match event {
            Observation::Activation(scope, activation) if is_callable(scope, key) => {
                Some(activation)
            }
            _ => None,
        })
        .collect()
}

#[test]
fn exact_throw_keeps_qualified_origin_across_nested_calls() {
    let (keys, events) = observe_activation_source(
        r"
function leaf() { throw 'payload'; }
function middle() { return leaf(); }
function outer() { return middle(); }
const value = outer();
",
    );
    let thrown = |name: &str| {
        events
            .iter()
            .find_map(|event| match event {
                Observation::Complete(scope, outcome, _) if is_callable(scope, keys[name]) => {
                    match &outcome.ledger.completion {
                        Completion::Abrupt(thrown) => Some(thrown.as_ref()),
                        _ => None,
                    }
                }
                _ => None,
            })
            .expect("each entered body propagates the exact throw")
    };
    let original = thrown("leaf");
    assert!(matches!(
        original.origin.scope,
        HeapModuleScope::LocalAnalysis(_)
    ));
    assert!(matches!(original.origin.site.body,
        LocalBodyIdentity::Callable { key, .. } if key == keys["leaf"]));
    for name in ["middle", "outer"] {
        let propagated = thrown(name);
        assert_eq!(propagated.origin, original.origin);
        assert_eq!(propagated.value.values, original.value.values);
        assert!(matches!(propagated.frontier.body,
            LocalBodyIdentity::Callable { key, .. } if key == keys[name]));
        assert_eq!(propagated.frontier.step, OperationStep::CallInvocation);
    }
}

#[test]
fn activation_proof_tracks_actual_defaults_separately_from_body_opacity() {
    use super::super::local_transfer::LocalActivation;

    let (keys, events) = observe_activation_source(
        r"
function prefix() { return 1; }
function defaults(value = prefix()) { const local = value; return local; }
const first = defaults();
const supplied = defaults(3);
",
    );
    let proofs = activations_for(&events, keys["defaults"]);
    assert_eq!(
        proofs.len(),
        2,
        "both invocations must execute their own prologue"
    );
    assert!(proofs.iter().all(|proof| matches!(proof,
        LocalActivation::Transferred(transfer) if transfer.closed_effect_dependencies().is_some()
    )));
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event,
                Observation::Call(Some(key)) if *key == keys["prefix"]
            ))
            .count(),
        1,
        "a supplied argument skips the default and its evaluated prefix never replays"
    );

    let (keys, events) = observe_activation_source(
        r"
function opaqueBody(value) { value(); return value; }
const opaque = opaqueBody(4);
",
    );
    let proofs = activations_for(&events, keys["opaqueBody"]);
    assert!(!proofs.is_empty());
    assert!(proofs.iter().all(|proof| matches!(proof,
        LocalActivation::Transferred(transfer) if transfer.closed_effect_dependencies().is_some()
    )));
    let opaque = completed_for(&events, keys["opaqueBody"]);
    assert!(!opaque.is_empty());
    assert!(
        opaque
            .iter()
            .all(|(outcome, _)| outcome.ledger.closed_effect_dependencies().is_none())
    );

    let (keys, events) = observe_activation_source(
        r"
function fail() { throw 7; }
function bodyAfterDefault() { return 2; }
function abruptDefault(value = fail()) { bodyAfterDefault(); return value; }
const result = abruptDefault();
",
    );
    let abrupt = activations_for(&events, keys["abruptDefault"]);
    assert!(!abrupt.is_empty());
    assert!(abrupt.iter().all(|proof| matches!(proof,
        LocalActivation::Transferred(transfer) if matches!(transfer.completion, Completion::Abrupt(_))
    )));
    assert!(
        !events.iter().any(|event| matches!(event,
            Observation::Call(Some(key)) if *key == keys["bodyAfterDefault"]
        )),
        "a throwing default cannot enter the body"
    );

    let (keys, events) = observe_activation_source(
        r"
function uncertainDefault(source, value = source()) { return value; }
const uncertain = uncertainDefault(0);
",
    );
    let uncertain = activations_for(&events, keys["uncertainDefault"]);
    assert!(!uncertain.is_empty());
    assert!(
        uncertain.iter().all(|proof| match proof {
            LocalActivation::Transferred(transfer) =>
                transfer.closed_effect_dependencies().is_none(),
            LocalActivation::NotRequested
            | LocalActivation::Evaluating
            | LocalActivation::Refused => true,
        }),
        "an unproved default call cannot manufacture successful activation coverage"
    );
}

#[test]
fn array_callback_defaults_use_shared_activation_and_preserve_abrupt_exit() {
    use super::super::local_transfer::LocalActivation;

    let (keys, events) = observe_activation_source(
        r"
function prefix() { return 'default'; }
function callback(value = prefix()) { return value; }
function run() { return [void 0, 1].map(callback); }
const result = run();
",
    );
    let activations = activations_for(&events, keys["callback"]);
    assert_eq!(activations.len(), 2);
    assert!(activations.iter().all(|activation| matches!(activation,
        LocalActivation::Transferred(transfer) if transfer.closed_effect_dependencies().is_some()
    )));
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event,
                Observation::Call(Some(key)) if *key == keys["prefix"]
            ))
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event,
                Observation::ArrayCallback { outcome, .. } if outcome.body_invoked
            ))
            .count(),
        2,
        "native callback children must actually enter the shared activation"
    );

    let (keys, events) = observe_activation_source(
        r"
function fail() { const captured = 'payload'; throw () => captured; }
function later() { return true; }
function callback(value = fail()) { later(); return value; }
function run() { try { return [void 0, 1].map(callback); } catch (payload) { return payload; } }
const result = run();
",
    );
    let activations = activations_for(&events, keys["callback"]);
    assert_eq!(
        activations.len(),
        1,
        "the array suffix cannot invoke another callback after a throw"
    );
    assert!(
        matches!(activations[0], LocalActivation::Transferred(transfer)
        if matches!(&transfer.completion, Completion::Abrupt(thrown)
            if thrown.value.values.iter().any(|value| matches!(value, AbstractValue::Closure(..)))))
    );
    assert!(
        !events.iter().any(|event| matches!(event,
            Observation::Call(Some(key)) if *key == keys["later"]
        )),
        "an abrupt callback default skips its body"
    );
    let leaves = completed_for(&events, keys["run"]);
    assert!(!leaves.is_empty());
    assert!(leaves.iter().all(|(outcome, state)| matches!(&outcome.ledger.completion,
        Completion::Normal(selected) if selected.values.iter().any(|value| match value {
            AbstractValue::Closure(_, captured) => captured.values.cells.values().any(|cell|
                state.world.get().lexical_cells[cell.0].values.contains(&AbstractValue::Value(Value::Prim(PrimKinds::STRING)))),
            _ => false,
        })
    )), "caller catch retains the exact default-throw payload and its lexical cell");
}

#[test]
fn function_methods_forward_evaluated_rest_and_dense_apply_arguments() {
    let (keys, events) = observe_activation_source(
        r"
function target(value) { return value; }
function callRest(...args) { return target.call(void 0, ...args); }
function applyDense() { return target.apply(void 0, ['dense']); }
function applyNull() { return target.apply(void 0, null); }
const first = callRest('rest');
const second = applyDense();
const third = applyNull();
",
    );
    let methods = events
        .iter()
        .filter_map(|event| match event {
            Observation::FunctionMethod {
                target,
                method,
                arguments,
            } if *target == Some(keys["target"]) => Some((*method, arguments)),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        methods.len(),
        3,
        "actual GetValue proofs must reach normalized calls"
    );
    assert!(methods.iter().any(|(method, arguments)| *method == FunctionCallMethod::Call
        && matches!(arguments.as_slice(), [value] if primitive_kind(value, PrimKinds::STRING))));
    assert!(methods.iter().any(|(method, arguments)| *method == FunctionCallMethod::Apply
        && matches!(arguments.as_slice(), [value] if primitive_kind(value, PrimKinds::STRING))));
    assert!(methods.iter().any(|(method, arguments)| *method == FunctionCallMethod::Apply && arguments.is_empty()));
    for name in ["callRest", "applyDense", "applyNull"] {
        let leaves = completed_for(&events, keys[name]);
        assert!(!leaves.is_empty());
        assert!(
            leaves
                .iter()
                .all(|(outcome, _)| outcome.ledger.closed_effect_dependencies().is_some()),
            "{name} must consume actual activation, method and operand proofs"
        );
    }

    let (_, events) = observe_activation_source(
        r"
function target(value) { return value; }
function replacement() { return 'own method'; }
target.call = replacement;
const result = target.call(void 0, 1);
",
    );
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, Observation::FunctionMethod { .. })),
        "an overwritten own method must not acquire Function.prototype authority"
    );
}
