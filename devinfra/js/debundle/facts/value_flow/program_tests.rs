//! Domain/projection invariants supplement the CLI's behavior/refusal controls.
use super::*;
use crate::program_bindings::{ModuleBindings, ResolvedImport};
use swc_common::SyntaxContext;

fn id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

/// One module of a hand-built program: parsed source plus the import and
/// export maps that program binding resolves.
struct TestModule {
    name: &'static str,
    ast: swc_ecma_ast::Module,
    imports: BTreeMap<String, ResolvedImport>,
    exports: BTreeMap<String, String>,
    fresh_factory_imports: BTreeSet<String>,
}

impl TestModule {
    fn new(name: &'static str, source: &str) -> Self {
        Self {
            name,
            ast: raw_js_test_support::parse(source),
            imports: BTreeMap::new(),
            exports: BTreeMap::new(),
            fresh_factory_imports: BTreeSet::new(),
        }
    }

    /// Imports each of `names` from module `from` under the same name.
    fn importing(mut self, from: &str, names: &[&str]) -> Self {
        for &name in names {
            self.imports.insert(
                name.to_string(),
                ResolvedImport {
                    module: from.into(),
                    export: name.into(),
                    source_specifier: format!("./{from}.js"),
                },
            );
        }
        self
    }

    /// Exports each of `names` under the same name.
    fn exporting(mut self, names: &[&str]) -> Self {
        self.exports.extend(
            names
                .iter()
                .map(|&name| (name.to_string(), name.to_string())),
        );
        self
    }
}

fn run_program(modules: &[TestModule]) -> ProgramValueFlows {
    let bodies: Vec<_> = modules
        .iter()
        .map(|module| crate::facts::top_level_item_views(&module.ast.body))
        .collect();
    let bindings = ProgramBindings::new(
        modules.iter().map(|module| ModuleBindings {
            module: module.name,
            imports: &module.imports,
            exports: &module.exports,
        }),
        [],
    );
    let no_members = BTreeMap::new();
    let inferred = BTreeMap::new();
    program_value_flows(
        &bindings,
        modules
            .iter()
            .zip(&bodies)
            .map(|(module, body)| ProgramFlowModule {
                module: module.name,
                body,
                fresh_factory_imports: &module.fresh_factory_imports,
                fresh_factory_members: &no_members,
                native_operation_inputs: &[],
                inferred_sets: &inferred,
            }),
        &crate::diagnostics::Diagnostics::default(),
    )
}

fn flow(source: &str) -> Arc<ChunkValueFlow> {
    program(source).flows.remove("caller").unwrap()
}

fn program(source: &str) -> ProgramValueFlows {
    program_and_caller(source).0
}

fn program_and_caller(source: &str) -> (ProgramValueFlows, swc_ecma_ast::Module) {
    let modules = [
        TestModule::new("caller", source).importing("provider", &["relay", "wrap"]),
        TestModule::new(
            "provider",
            "export function relay(value) { return value; }\nexport function wrap(value) { return { box: value }; }\n",
        )
        .exporting(&["relay", "wrap"]),
    ];
    let flows = run_program(&modules);
    let [caller, _] = modules;
    (flows, caller.ast)
}

fn variable_initializer<'a>(module: &'a swc_ecma_ast::Module, name: &str) -> &'a Expr {
    for item in &module.body {
        let swc_ecma_ast::ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(swc_ecma_ast::Decl::Var(
            declaration,
        ))) = item
        else {
            continue;
        };
        for declarator in &declaration.decls {
            let swc_ecma_ast::Pat::Ident(binding) = &declarator.name else {
                continue;
            };
            if binding.id.sym == name {
                return declarator
                    .init
                    .as_deref()
                    .expect("queried binding has an initializer");
            }
        }
    }
    panic!("missing variable initializer for {name}");
}

#[test]
fn returned_descendant_accesses_do_not_become_invocation_witnesses() {
    let program =
        program("var cell = 0;\nfunction outer() { return () => { cell = 1; return cell; }; }\n");
    let flow = &program.flows["caller"];
    let view = flow.program.as_ref().unwrap();
    let Value::Alloc(Kind::Function, outer) =
        flow.values_of_binding(&id("outer")).iter().next().unwrap()
    else {
        panic!("function identity");
    };
    let function = view.sites[&outer];
    let cell = view.bindings[&id("cell")].0;
    let invoked = &program.cell_effects[&FlowKey::InvokedCells(function)];
    let retained = &program.cell_effects[&FlowKey::RetainedCells(function)];
    assert!(!invoked.reads.contains(cell));
    assert!(!invoked.writes.contains(cell));
    assert!(retained.reads.contains(cell));
    assert!(retained.writes.contains(cell));
}

#[test]
fn foreign_returns_and_properties_recover_owned_origins_without_erasing_top() {
    let flow = flow(
        "import { relay, wrap } from './provider.js';\nfunction callback() { return 7; }\nconst forwarded = relay(callback);\nconst input = { run: callback };\nconst carrier = wrap(input);\nconst recovered = carrier.box.run;\n",
    );
    let callback = flow.values_of_binding(&id("callback"));
    for name in ["forwarded", "recovered"] {
        let values = flow.values_of_binding(&id(name));
        assert!(
            callback.iter().all(|origin| values.contains(&origin)),
            "{name}: {values:?}"
        );
        assert!(
            values.contains(&Value::Top),
            "foreign execution stays opaque"
        );
    }
    let input = flow.values_of_binding(&id("input"));
    let candidates = input.iter().collect();
    let index = flow.candidate_object_index(&candidates);
    let mut reached = BTreeSet::new();
    index.for_binding(&id("carrier"), |origin| {
        reached.insert(origin);
    });
    assert_eq!(
        reached, candidates,
        "foreign carrier must retain the local object"
    );
    let carrier = flow.values_of_binding(&id("carrier"));
    assert!(carrier.contains(&Value::Top));
}

#[test]
fn completed_aliases_share_one_local_translation() {
    let mut source = String::from("const input = {};\n");
    for index in 0..512 {
        source.push_str(&format!("const alias{index} = input;\n"));
    }
    let flow = flow(&source);
    let input = flow.values_of_binding(&id("input"));
    for index in 0..512 {
        assert!(input.shares_bitmap(&flow.values_of_binding(&id(&format!("alias{index}")))));
    }
    assert_eq!(
        flow.program.as_ref().unwrap().projected.len(),
        1,
        "one completed shared set requires one translation, regardless of alias count"
    );
}

#[test]
fn property_reads_use_the_shared_local_projection() {
    let flow = flow("const callback = () => 1;\nconst receiver = { callback };\n");
    let [Value::Alloc(Kind::Object, receiver)] = flow
        .values_of_binding(&id("receiver"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("receiver should have one object identity");
    };

    let callback = flow.values_of_binding(&id("callback"));
    assert_eq!(
        flow.values_of_property(receiver, "callback"),
        Some(&callback)
    );
    assert!(flow.values_of_property(receiver, "missing").is_none());
    assert!(
        flow.properties.is_empty(),
        "program-backed property rows must stay in the shared projection cache"
    );

    let mut view = flow.program.as_ref().unwrap().clone();
    let raw = view
        .shared
        .values
        .get(&FlowKey::Property(
            view.sites[&receiver],
            view.shared.property_slots["callback"],
        ))
        .expect("completed callback property row")
        .clone();
    let cache_key = CompletedSet::of(&raw);
    let cache_len = view.projected.len();
    let first = view.prepare_projection(&raw, flow.as_ref());
    let second = view.prepare_projection(&raw, flow.as_ref());
    assert!(first.shares_bitmap(&second));
    assert_eq!(view.projected.len(), cache_len);
    assert!(view.projected.contains_key(&cache_key));
}

#[test]
fn completed_value_index_matches_ordered_facts_and_keeps_property_rows_sorted() {
    let flows = program(
        "let empty;\nconst first = { alpha: () => 1 };\nconst second = { beta: () => 2 };\nfirst.gamma = second.beta;\nconst known = first.alpha;\nconst missing = first.nope;\nconst key = 'alpha';\nconst dynamic = first[key];\n",
    );
    for (module, flow) in &flows.flows {
        let view = flow.program.as_ref().unwrap();
        let mut indexed_rows = 0;
        for (key, expected) in &view.shared.ordered_value_oracle {
            let actual = view
                .shared
                .values
                .get(key)
                .unwrap_or_else(|| panic!("missing indexed row in {module}: {key:?}"));
            assert_eq!(actual, expected, "indexed value row in {module}: {key:?}");
            assert!(
                Arc::ptr_eq(&actual.bitmap, &expected.bitmap),
                "index must move rather than copy the value bitmap"
            );
            indexed_rows += 1;
        }
        assert_eq!(indexed_rows, view.shared.values.iter_values().count());
        assert_eq!(indexed_rows, view.shared.ordered_value_oracle.len());
        assert!(
            view.shared
                .values
                .property_ranges
                .iter()
                .filter(|range| range.start < range.end)
                .count()
                >= 2,
            "fixture must populate property rows at multiple sites"
        );
        for site in 0..view.shared.sites.sources.len() as u32 {
            let properties: Vec<_> = view.shared.values.properties_at(site).collect();
            assert!(properties.windows(2).all(|pair| pair[0].0 < pair[1].0));
            for (property, values) in properties {
                let expected = view
                    .shared
                    .ordered_value_oracle
                    .get(&FlowKey::Property(site, property))
                    .unwrap_or_else(|| {
                        panic!("missing enumerated row in {module}: {site}/{property:?}")
                    });
                assert_eq!(values, expected);
                assert!(Arc::ptr_eq(&values.bitmap, &expected.bitmap));
            }
        }
    }

    let flow = &flows.flows["caller"];
    let view = flow.program.as_ref().unwrap();
    let [Value::Alloc(Kind::Object, first_offset)] = flow
        .values_of_binding(&id("first"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("first should have one object allocation");
    };
    let site = view.sites[&first_offset];
    let row_slots: Vec<_> = view
        .shared
        .values
        .properties_at(site)
        .map(|(property, _)| property.0)
        .collect();
    assert!(row_slots.windows(2).any(|pair| pair[1] > pair[0] + 1));
    assert!(
        view.shared
            .values
            .get(&FlowKey::Property(site, view.shared.property_slots["nope"]))
            .is_none()
    );

    // The source solve normally omits empty values. The index still represents
    // the distinct stored state exactly when a completed row is explicitly empty.
    let mut store = CompletedValueStore::with_domains(1, 1);
    let origins = Arc::new(OriginTable::default());
    store.insert(
        FlowKey::Property(0, PropertySlot(9)),
        Values::empty(origins),
    );
    store.seal();
    assert!(
        store
            .get(&FlowKey::Property(0, PropertySlot(9)))
            .is_some_and(Values::is_empty)
    );
    assert!(store.get(&FlowKey::Property(0, PropertySlot(8))).is_none());
}

#[test]
#[should_panic(expected = "duplicate completed property value")]
fn completed_property_index_rejects_duplicate_key() {
    let mut store = CompletedValueStore::with_domains(0, 1);
    let origins = Arc::new(OriginTable::default());
    store.insert(
        FlowKey::Property(0, PropertySlot(3)),
        Values::empty(origins.clone()),
    );
    store.insert(
        FlowKey::Property(0, PropertySlot(3)),
        Values::empty(origins),
    );
}

#[test]
fn completed_expression_queries_preserve_static_missing_and_dynamic_top() {
    let source = "const callback = () => 1;\nconst receiver = { callback, other: callback };\nconst known = receiver.callback;\nconst missing = receiver.absent;\nconst key = 'callback';\nconst dynamic = receiver[key];\n";
    let (flows, caller) = program_and_caller(source);
    let flow = &flows.flows["caller"];
    let known = flow.values_of_expr(variable_initializer(&caller, "known"));
    let missing = flow.values_of_expr(variable_initializer(&caller, "missing"));
    let dynamic = flow.values_of_expr(variable_initializer(&caller, "dynamic"));

    let callback = flow.values_of_binding(&id("callback"));
    assert!(callback.iter().all(|value| known.contains(&value)));
    assert!(!known.contains(&Value::Top));
    assert!(missing.contains(&Value::Top));
    assert!(dynamic.contains(&Value::Top));
    assert!(callback.iter().all(|value| dynamic.contains(&value)));
}

#[test]
fn local_property_return_summaries_keep_module_slot_names() {
    let modules = [
        TestModule::new("caller", "const marker = { alpha: 0 };\n"),
        TestModule::new(
            "provider",
            "const callback = () => 1;\nfunction read(context) { return context.value; }\nconst result = read({ value: callback });\n",
        ),
    ];
    let flows = run_program(&modules);
    let [_, provider] = &modules;
    let flow = &flows.flows["provider"];
    let view = flow.program.as_ref().unwrap();
    let local_value_slot = flow
        .property_names_by_slot
        .iter()
        .position(|name| name == "value")
        .expect("provider interns the summarized property");
    let shared_value_slot = view.shared.property_slots["value"].0 as usize;
    assert_ne!(
        local_value_slot, shared_value_slot,
        "the earlier caller property must make the shared slot differ from provider-local slots"
    );

    let callback = flow.values_of_binding(&id("callback"));
    let result = flow.values_of_binding(&id("result"));
    assert!(
        callback.iter().all(|origin| result.contains(&origin)),
        "the local return summary transfers the selected property even when the conservative return row also has Top"
    );
    let result_expr = variable_initializer(&provider.ast, "result");
    let shared_query = flow.values_of_expr(result_expr);
    assert!(
        callback.iter().all(|origin| shared_query.contains(&origin)),
        "the shared summary uses globally rekeyed property slots"
    );
    let provider_views = crate::facts::top_level_item_views(&provider.ast.body);
    let reference = ChunkValueFlow::analyze_reference(&provider_views);
    let local_query = reference.values_of_expr(result_expr);
    let callback_values: BTreeSet<_> = callback.iter().collect();
    let local_values: BTreeSet<_> = local_query.iter().collect();
    assert!(
        callback_values.is_subset(&local_values),
        "the local reference summary must retain its own property-slot names"
    );
}

#[test]
fn property_producer_census_uses_rekeyed_object_sites_across_modules() {
    let flows = run_program(&[
        TestModule::new("caller", "const box = {};\nconst early = box.run;\n"),
        TestModule::new(
            "provider",
            "const box = { run: target };\nfunction target() {}\n",
        ),
    ]);

    let caller_flow = &flows.flows["caller"];
    let values = caller_flow.values_of_binding(&id("early"));
    assert!(values.contains(&Value::Top));
    assert!(
        !values
            .iter()
            .any(|value| matches!(value, Value::Alloc(Kind::Function, _)))
    );
    let provider_flow = &flows.flows["provider"];
    let target = provider_flow.values_of_binding(&id("target"));
    let [Value::Alloc(Kind::Object, provider_box)] = provider_flow
        .values_of_binding(&id("box"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("provider box should have one object identity");
    };
    let property = provider_flow
        .values_of_property(provider_box, "run")
        .expect("provider object literal should produce run");
    assert!(target.iter().all(|value| property.contains(&value)));

    // Both ASTs put their first object literal at the same source byte offset.
    // The provider's fixed writer therefore only proves a receiver-specific
    // key after program event sites have been rekeyed.
    assert_eq!(flows.flow_work_stats.property_impossible_reads_skipped, 1);
}

fn reference_translation(
    view: &ChunkProgramFlow,
    flow: &ChunkValueFlow,
    values: &Values,
) -> BTreeSet<Value> {
    let mut translated = BTreeSet::new();
    for value in values {
        match value {
            Value::Alloc(kind, site) => {
                let (owner, offset) = view.shared.sites.sources[site as usize];
                if owner == view.module {
                    translated.insert(Value::Alloc(kind, offset));
                } else {
                    translated.insert(Value::Top);
                    if let Some(bits) = view.shared.reachability.get(&view.shared.origins.id(value))
                    {
                        for global in bits.iter() {
                            if let Some(local) = view.candidates.get(&global) {
                                translated.insert(flow.candidate_objects[*local as usize]);
                            }
                        }
                    }
                }
            }
            other => {
                translated.insert(other);
            }
        }
    }
    translated
}

fn distinct_foreign_summaries(view: &ChunkProgramFlow, values: &Values) -> usize {
    values
        .iter()
        .filter_map(|value| match value {
            Value::Alloc(_, site) if view.shared.sites.sources[site as usize].0 != view.module => {
                view.shared
                    .reachability
                    .get(&view.shared.origins.id(value))
                    .map(|bits| Arc::as_ptr(bits) as usize)
            }
            _ => None,
        })
        .collect::<BTreeSet<_>>()
        .len()
}

fn reference_candidate_maps(
    view: &ChunkProgramFlow,
    flow: &ChunkValueFlow,
) -> (
    BTreeMap<Value, BTreeSet<Value>>,
    BTreeMap<Id, BTreeSet<Value>>,
) {
    let mut reachability = BTreeMap::new();
    for local_origin in &flow.origin_table.origins {
        let global_origin = match *local_origin {
            Value::Alloc(kind, offset) => Value::Alloc(kind, view.sites[&offset]),
            other => other,
        };
        let Some(bits) = view
            .shared
            .reachability
            .get(&view.shared.origins.id(global_origin))
        else {
            continue;
        };
        let projected: BTreeSet<_> = bits
            .iter()
            .filter_map(|global| view.candidates.get(&global))
            .map(|local| flow.candidate_objects[*local as usize])
            .collect();
        if !projected.is_empty() {
            reachability.insert(*local_origin, projected);
        }
    }

    let mut bindings = BTreeMap::new();
    for (id, slot) in &view.bindings {
        let Some(values) = view.shared.values.get(&FlowKey::Binding(*slot)) else {
            continue;
        };
        let mut projected = BTreeSet::new();
        for value in values {
            let Some(bits) = view.shared.reachability.get(&view.shared.origins.id(value)) else {
                continue;
            };
            projected.extend(
                bits.iter()
                    .filter_map(|global| view.candidates.get(&global))
                    .map(|local| flow.candidate_objects[*local as usize]),
            );
        }
        if !projected.is_empty() {
            bindings.insert(id.clone(), projected);
        }
    }
    (reachability, bindings)
}

fn shared_scc_program() -> (ProgramValueFlows, swc_ecma_ast::Module) {
    let modules = [
        TestModule::new(
            "caller",
            "import { left, right } from './provider.js';\n\
             export const local = { own: true };\n\
             const ownLeft = { payload: local };\n\
             const ownRight = { payload: local, back: ownLeft };\n\
             ownLeft.back = ownRight;\n\
             const combined = left || right;\n\
             left || right;\n",
        )
        .importing("provider", &["left", "right"])
        .exporting(&["local"]),
        TestModule::new(
            "provider",
            "import { local } from './caller.js';\n\
             const left = { payload: local };\n\
             const right = { payload: local, back: left };\n\
             left.back = right;\n\
             export { left, right };\n",
        )
        .importing("caller", &["local"])
        .exporting(&["left", "right"]),
        TestModule::new(
            "empty",
            "import { left, right } from './provider.js';\n\
             const combined = left || right;\n",
        )
        .importing("provider", &["left", "right"]),
    ];
    let flows = run_program(&modules);
    let [caller, ..] = modules;
    (flows, caller.ast)
}

#[test]
fn shared_candidate_projections_preserve_indices_sharing_and_transient_queries() {
    let (flows, caller) = shared_scc_program();
    let caller_flow = &flows.flows["caller"];
    let caller_view = caller_flow.program.as_ref().unwrap();
    let provider_flow = &flows.flows["provider"];
    let provider_view = provider_flow.program.as_ref().unwrap();
    let empty_flow = &flows.flows["empty"];
    let empty_view = empty_flow.program.as_ref().unwrap();

    let local_candidates = BTreeSet::from_iter(caller_flow.candidate_objects.iter().copied());
    let local = caller_flow.values_of_binding(&id("local"));
    let [Value::Alloc(Kind::Object, local_site)] = local.iter().collect::<Vec<_>>()[..] else {
        panic!("the caller's local candidate should have one allocation identity");
    };
    assert!(local_candidates.contains(&Value::Alloc(Kind::Object, local_site)));

    let own_left = caller_flow.values_of_binding(&id("ownLeft"));
    let own_right = caller_flow.values_of_binding(&id("ownRight"));
    let [Value::Alloc(Kind::Object, own_left_site)] = own_left.iter().collect::<Vec<_>>()[..]
    else {
        panic!("ownLeft should have one allocation identity");
    };
    let [Value::Alloc(Kind::Object, own_right_site)] = own_right.iter().collect::<Vec<_>>()[..]
    else {
        panic!("ownRight should have one allocation identity");
    };
    let own_left_bits =
        &caller_flow.candidate_reachability[&Value::Alloc(Kind::Object, own_left_site)];
    let own_right_bits =
        &caller_flow.candidate_reachability[&Value::Alloc(Kind::Object, own_right_site)];
    assert!(
        Arc::ptr_eq(own_left_bits, own_right_bits),
        "local origins in one retained SCC must share one projected summary"
    );

    let left_slot = provider_view.bindings[&id("left")];
    let right_slot = provider_view.bindings[&id("right")];
    let left_raw = provider_view
        .shared
        .values
        .get(&FlowKey::Binding(left_slot))
        .unwrap()
        .iter()
        .find(|value| matches!(value, Value::Alloc(Kind::Object, _)))
        .unwrap();
    let right_raw = provider_view
        .shared
        .values
        .get(&FlowKey::Binding(right_slot))
        .unwrap()
        .iter()
        .find(|value| matches!(value, Value::Alloc(Kind::Object, _)))
        .unwrap();
    let left_summary =
        &provider_view.shared.reachability[&provider_view.shared.origins.id(left_raw)];
    let right_summary =
        &provider_view.shared.reachability[&provider_view.shared.origins.id(right_raw)];
    assert!(Arc::ptr_eq(left_summary, right_summary));

    let combined_slot = caller_view.bindings[&id("combined")];
    let combined_raw = caller_view
        .shared
        .values
        .get(&FlowKey::Binding(combined_slot))
        .unwrap();
    let raw_carriers: BTreeSet<_> = combined_raw
        .iter()
        .filter(|value| matches!(value, Value::Alloc(Kind::Object, _)))
        .collect();
    assert_eq!(
        raw_carriers.len(),
        2,
        "combined retains both foreign carriers"
    );
    let expected = reference_translation(caller_view, caller_flow, combined_raw);
    let actual: BTreeSet<_> = caller_flow
        .values_of_binding(&id("combined"))
        .iter()
        .collect();
    assert_eq!(actual, expected);
    assert!(actual.contains(&Value::Top));
    assert!(actual.contains(&Value::Alloc(Kind::Object, local_site)));
    assert!(
        actual
            .iter()
            .all(|value| local_candidates.contains(value) || *value == Value::Top),
        "global candidate positions must be filtered through the caller's local map"
    );

    let summary_ids: BTreeSet<_> = raw_carriers
        .iter()
        .filter_map(|value| {
            caller_view
                .shared
                .reachability
                .get(&caller_view.shared.origins.id(*value))
                .map(|bits| Arc::as_ptr(bits) as usize)
        })
        .collect();
    assert_eq!(
        summary_ids.len(),
        1,
        "both carriers belong to one summary SCC"
    );
    assert_eq!(
        caller_view.projection_work.prepared_summary_unions[&CompletedSet::of(combined_raw)],
        summary_ids.len(),
        "a translated Values set unions each distinct summary once"
    );
    assert_eq!(
        caller_view.projection_work.retained_summary_remaps,
        caller_view.projected_reachability.len(),
        "each retained summary identity is remapped once per source view"
    );

    let transient_expr = caller
        .body
        .iter()
        .rev()
        .find_map(|item| match item {
            swc_ecma_ast::ModuleItem::Stmt(swc_ecma_ast::Stmt::Expr(statement)) => {
                Some(statement.expr.as_ref())
            }
            _ => None,
        })
        .expect("fixture expression query");
    let projected_sets_before = caller_view.projected.len();
    let first = caller_flow.values_of_expr(transient_expr);
    let second = caller_flow.values_of_expr(transient_expr);
    assert_eq!(first, second);
    assert_eq!(first, caller_flow.values_of_binding(&id("combined")));
    assert_eq!(caller_view.projected.len(), projected_sets_before);
    assert_eq!(
        caller_view.projection_work.retained_summary_remaps,
        caller_view.projected_reachability.len(),
        "transient query results do not retain new summary projections"
    );

    let mut cold_view = caller_view.clone();
    cold_view.projected.clear();
    cold_view.projected_reachability.clear();
    let cold_projected_before = cold_view.projected.len();
    let cold_reachability_before = cold_view.projected_reachability.len();
    let cold_first = cold_view.values_of_expr(transient_expr, caller_flow);
    let cold_second = cold_view.values_of_expr(transient_expr, caller_flow);
    assert_eq!(cold_first, cold_second);
    assert_eq!(
        cold_first.iter().collect::<BTreeSet<_>>(),
        expected,
        "a cold transient query uses the same local/global projection as completed facts"
    );
    assert_eq!(cold_view.projected.len(), cold_projected_before);
    assert_eq!(
        cold_view.projected_reachability.len(),
        cold_reachability_before,
        "query-only summary projections stay within each translation"
    );

    for (module, flow) in &flows.flows {
        let view = flow.program.as_ref().unwrap();
        for values in view.shared.values.iter_values() {
            let (translated, unions) = view.translate(values, flow);
            assert_eq!(
                translated.iter().collect::<BTreeSet<_>>(),
                reference_translation(view, flow, values),
                "exact projection for every retained Values row in {module}"
            );
            assert_eq!(
                unions,
                distinct_foreign_summaries(view, values),
                "one candidate-summary union per distinct retained source Arc in {module}"
            );
        }

        let candidates = BTreeSet::from_iter(flow.candidate_objects.iter().copied());
        assert_eq!(
            flow.candidate_object_index_maps_for_test(&candidates),
            reference_candidate_maps(view, flow),
            "candidate index view for {module}"
        );
    }

    assert!(empty_flow.candidate_objects.is_empty());
    assert!(empty_flow.candidate_bindings.is_empty());
    assert!(empty_flow.candidate_reachability.is_empty());
    assert!(
        empty_view
            .projected_reachability
            .values()
            .all(|bits| bits.is_empty())
    );
    let empty_combined = empty_flow.values_of_binding(&id("combined"));
    assert_eq!(
        empty_combined.iter().collect::<BTreeSet<_>>(),
        reference_translation(
            empty_view,
            empty_flow,
            empty_view
                .shared
                .values
                .get(&FlowKey::Binding(empty_view.bindings[&id("combined")]))
                .unwrap()
        )
    );
    assert!(empty_combined.contains(&Value::Top));
    assert_eq!(
        empty_view.projected_reachability.len(),
        empty_view.projection_work.retained_summary_remaps
    );
}

#[test]
fn declared_factory_result_exclusion_preserves_member_aliases_unknown_and_provider_flow() {
    // Supplementary transfer invariant: eligibility itself is checked by the
    // real CLI fixtures, including provider backedges and unsafe alias writes.
    let modules = [
        TestModule {
            fresh_factory_imports: BTreeSet::from(["factory".into()]),
            ..TestModule::new(
                "caller",
                "import { factory } from './provider.js'; const retained = factory(() => 7); const alias = retained.invoke;",
            )
            .importing("provider", &["factory"])
        },
        TestModule::new(
            "provider",
            "export function factory(callback) { if (typeof callback !== 'function') return callback; return { invoke: callback }; }",
        )
        .exporting(&["factory"]),
    ];
    let flows = run_program(&modules);
    let [caller, _] = &modules;
    let caller_flow = &flows.flows["caller"];
    let provider_flow = &flows.flows["provider"];
    let Expr::Call(call) = variable_initializer(&caller.ast, "retained") else {
        panic!("factory call");
    };
    let Expr::Arrow(callback) = call.args[0].expr.as_ref() else {
        panic!("inline callback");
    };
    let callback = Value::Alloc(Kind::Function, callback.span.lo.0);
    for values in [
        caller_flow.values_of_binding(&id("retained")),
        caller_flow.values_of_expr(variable_initializer(&caller.ast, "retained")),
    ] {
        assert!(
            !values.iter().any(|value| value == callback),
            "direct result must not alias the input"
        );
        assert!(
            values.iter().any(|value| value == Value::Top),
            "foreign and opaque alternatives remain unknown"
        );
    }
    assert!(
        caller_flow
            .values_of_binding(&id("alias"))
            .iter()
            .any(|value| value == callback),
        "a member can still expose the original callback"
    );
    let caller_view = caller_flow.program.as_ref().unwrap();
    let provider_view = provider_flow.program.as_ref().unwrap();
    let Value::Alloc(_, callback_site) = callback else {
        unreachable!()
    };
    let global_callback = Value::Alloc(Kind::Function, caller_view.sites[&callback_site]);
    let Value::Alloc(_, factory_site) = provider_flow
        .values_of_binding(&id("factory"))
        .iter()
        .next()
        .unwrap()
    else {
        panic!("factory identity");
    };
    let global_factory = provider_view.sites[&factory_site];
    assert!(
        provider_view
            .shared
            .values
            .get(&FlowKey::Return(global_factory))
            .unwrap()
            .iter()
            .any(|value| value == global_callback),
        "ordinary global Return facts remain unfiltered"
    );
    let formal = provider_view.bindings[&id("callback")];
    assert!(
        provider_view
            .shared
            .values
            .get(&FlowKey::Binding(formal))
            .unwrap()
            .iter()
            .any(|value| value == global_callback),
        "ordinary argument-to-formal flow remains unfiltered"
    );
}

#[test]
fn numeric_property_aggregate_is_available_in_local_program_projection() {
    let source = r#"
function numeric_value() {}
function named_value() {}
const items = { 0: numeric_value, ref: named_value };
const projected = items[items.length - 1];
"#;
    let flows = program(source);
    let flow = &flows.flows["caller"];
    let numeric = flow.values_of_binding(&id("numeric_value"));
    let projected = flow.values_of_binding(&id("projected"));
    assert!(numeric.iter().all(|value| projected.contains(&value)));
    assert!(projected.contains(&Value::Top));
    assert!(
        !flow
            .values_of_binding(&id("named_value"))
            .iter()
            .any(|value| projected.contains(&value))
    );

    let probe = raw_js_test_support::parse("items[items.length - 1];");
    let ModuleItem::Stmt(Stmt::Expr(statement)) = &probe.body[0] else {
        panic!("probe should be an expression statement");
    };
    let reprojected = flow.values_of_expr(&statement.expr);
    assert!(numeric.iter().all(|value| reprojected.contains(&value)));
    assert!(reprojected.contains(&Value::Top));
    assert!(
        !flow
            .values_of_binding(&id("named_value"))
            .iter()
            .any(|value| reprojected.contains(&value))
    );
}

#[test]
fn counted_loop_certificate_survives_program_expression_queries_only_at_its_sites() {
    let source = r#"
function numeric_value() {}
function named_value() {}
const items = { 0: numeric_value, ref: named_value, length: 1 };
function visit(items) {
  for (var index = 0; index < items.length; index++) {
    var inside = items[index];
  }
  index = "ref";
  const outside = items[index];
}
visit(items);
"#;
    let flows = program(source);
    let flow = &flows.flows["caller"];
    let numeric = flow.values_of_binding(&id("numeric_value"));
    let named = flow.values_of_binding(&id("named_value"));
    let inside = flow.values_of_binding(&id("inside"));
    let outside = flow.values_of_binding(&id("outside"));
    assert!(inside.contains(&Value::Top));
    assert!(numeric.iter().all(|value| inside.contains(&value)));
    assert!(!named.iter().any(|value| inside.contains(&value)));
    assert!(named.iter().all(|value| outside.contains(&value)));

    #[derive(Default)]
    struct Reads(Vec<Expr>);
    impl Visit for Reads {
        fn visit_expr(&mut self, expression: &Expr) {
            if let Expr::Member(member) = expression
                && let MemberProp::Computed(key) = &member.prop
                && matches!(key.expr.as_ref(), Expr::Ident(identifier) if identifier.sym == *"index")
            {
                self.0.push(expression.clone());
            }
            expression.visit_children_with(self);
        }
    }
    // Reparse the same source so sites and binding identities match; parsing
    // just a probe expression would lose the per-site proof premise.
    let module = raw_js_test_support::parse(source);
    let mut reads = Reads::default();
    module.visit_with(&mut reads);
    assert_eq!(reads.0.len(), 2);
    assert_eq!(flow.values_of_expr(&reads.0[0]), inside);
    assert_eq!(flow.values_of_expr(&reads.0[1]), outside);
}
