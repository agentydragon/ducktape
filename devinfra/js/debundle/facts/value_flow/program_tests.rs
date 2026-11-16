//! Domain/projection invariants supplement the CLI's behavior/refusal controls.
use super::*;
use crate::program_bindings::{ModuleBindings, ResolvedImport};
use swc_common::SyntaxContext;

fn id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

fn flow(source: &str) -> Arc<ChunkValueFlow> {
    let caller = raw_js_test_support::parse(source);
    let provider = raw_js_test_support::parse(
        "export function relay(value) { return value; }\nexport function wrap(value) { return { box: value }; }\n",
    );
    let caller_views = crate::facts::top_level_item_views(&caller.body);
    let provider_views = crate::facts::top_level_item_views(&provider.body);
    let imports = ["relay", "wrap"]
        .into_iter()
        .map(|name| {
            (
                name.to_string(),
                ResolvedImport {
                    module: "provider".into(),
                    export: name.into(),
                    source_specifier: "./provider.js".into(),
                },
            )
        })
        .collect();
    let exports = ["relay", "wrap"]
        .into_iter()
        .map(|name| (name.to_string(), name.to_string()))
        .collect();
    let empty = BTreeMap::new();
    let bindings = ProgramBindings::new(
        [
            ModuleBindings {
                module: "caller",
                imports: &imports,
                exports: &empty,
            },
            ModuleBindings {
                module: "provider",
                imports: &BTreeMap::new(),
                exports: &exports,
            },
        ],
        [],
    );
    let inferred = BTreeMap::new();
    program_value_flows(
        &bindings,
        [
            ProgramFlowModule {
                module: "caller",
                body: &caller_views,
                native_operation_inputs: &[],
                inferred_sets: &inferred,
            },
            ProgramFlowModule {
                module: "provider",
                body: &provider_views,
                native_operation_inputs: &[],
                inferred_sets: &inferred,
            },
        ],
    )
    .flows
    .remove("caller")
    .unwrap()
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
