use std::sync::Arc;

use analysis_codec::{ContextOrdinals, decode, encode};
use analysis_codec_derive::Artifact;
use serde::{Deserialize, Serialize};
use swc_common::{GLOBALS, Globals};
use swc_ecma_ast::{Id, Ident, Module};
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Artifact, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Payload {
    Bindings { values: Vec<Id> },
}

#[derive(Artifact, Debug, PartialEq, Serialize, Deserialize)]
struct Record {
    metric: (String, u32),
    payload: Payload,
    #[serde(skip)]
    proof: u32,
}

fn bindings(module: &Module) -> Vec<Id> {
    #[derive(Default)]
    struct Names(Vec<Id>);
    impl Visit for Names {
        fn visit_ident(&mut self, ident: &Ident) {
            if ident.sym == "counter" {
                self.0.push(ident.to_id());
            }
        }
    }
    let mut names = Names::default();
    module.visit_with(&mut names);
    names.0
}

#[test]
fn typed_contexts_rebind_without_interpreting_ordinary_tuples_or_report_attributes() {
    GLOBALS.set(&Globals::new(), || {
        let source = "let counter = 0; function read() { let counter = 1; return counter; } export { read, counter };";
        let first = js_ast::parse_js_module("first.js", source).unwrap();
        let record = Record {
            metric: ("count".to_owned(), u32::MAX),
            payload: Payload::Bindings { values: bindings(&first.module) },
            proof: 42,
        };
        let report = serde_json::to_value(&record).unwrap();
        assert!(report.get("proof").is_none());
        let bytes = encode(&record, &ContextOrdinals::new([&first.module])).unwrap();
        assert_eq!(serde_json::to_value(&record).unwrap(), report);

        let second = js_ast::parse_js_module("second.js", source).unwrap();
        let expected_bindings = bindings(&second.module);
        assert_ne!(bindings(&first.module), expected_bindings);
        let decoded: Record = decode(&bytes, &ContextOrdinals::new([&second.module])).unwrap();
        assert_eq!(decoded.metric, record.metric);
        assert_eq!(decoded.payload, Payload::Bindings { values: expected_bindings });
        assert_eq!(decoded.proof, 42);
    });
}

#[derive(Artifact)]
struct Cause {
    label: String,
    parent: Option<Arc<Cause>>,
}

#[test]
fn shared_causes_keep_their_dag_sharing() {
    let shared = Arc::new(Cause {
        label: "shared".into(),
        parent: None,
    });
    let roots = vec![
        Arc::new(Cause {
            label: "left".into(),
            parent: Some(shared.clone()),
        }),
        Arc::new(Cause {
            label: "right".into(),
            parent: Some(shared),
        }),
    ];
    let ordinals = ContextOrdinals::new([]);
    let bytes = encode(&roots, &ordinals).unwrap();
    let decoded: Vec<Arc<Cause>> = decode(&bytes, &ordinals).unwrap();
    assert_eq!(decoded[0].label, "left");
    assert_eq!(decoded[1].label, "right");
    let left = decoded[0].parent.as_ref().unwrap();
    let right = decoded[1].parent.as_ref().unwrap();
    assert_eq!(left.label, "shared");
    assert!(Arc::ptr_eq(left, right));
}

#[test]
fn bindings_outside_the_analyzed_modules_are_refused() {
    GLOBALS.set(&Globals::new(), || {
        let parsed = js_ast::parse_js_module("outside.js", "const counter = 1;").unwrap();
        let values = bindings(&parsed.module);
        let empty = ContextOrdinals::new([]);
        assert!(encode(&values, &empty).is_err());
        let bytes = encode(&values, &ContextOrdinals::new([&parsed.module])).unwrap();
        assert!(decode::<Vec<Id>>(&bytes, &empty).is_err());
    });
}

#[test]
fn a_truncated_frame_cannot_supply_a_complete_value() {
    let ordinals = ContextOrdinals::new([]);
    let mut bytes = encode(&vec![1u32, 2, 3], &ordinals).unwrap();
    bytes.pop();
    assert!(decode::<Vec<u32>>(&bytes, &ordinals).is_err());
}
