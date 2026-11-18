//! Behavioral regression for guarded computed-property snapshots.

use debundle_e2e_support::saved_analysis::{
    StagedInvocation, assert_same_invocation_trees, path, staged,
};
use debundle_e2e_support::*;
use serde_json::Value;

const CONDITION_SNAPSHOT_ROUTE: &str = r#"
function choosePayload(record) {
  let value;
  let chosen = null;
  for (const key in record) {
    if (Object.prototype.hasOwnProperty.call(record, key) &&
        ((value = record[key]), value != null)) {
      switch (key) {
        case "payload": chosen = value; break;
        default: break;
      }
    }
  }
  return chosen;
}
"#;

const SEPARATE_NULL_CHECK_ROUTE: &str = r#"
function choosePayload(record) {
  let chosen = null;
  for (let key in record) {
    if (Object.prototype.hasOwnProperty.call(record, key)) {
      var value = record[key];
      if (value != null) {
        switch (key) {
          case "payload": chosen = value; break;
          default: break;
        }
      }
    }
  }
  return chosen;
}
"#;

const MUTATED_KEY_ROUTE: &str = r#"
function choosePayload(record) {
  let value;
  let chosen = null;
  for (let key in record) {
    if (Object.prototype.hasOwnProperty.call(record, key)) {
      value = record[key];
      key = "payload";
      if (value != null) {
        switch (key) {
          case "payload": chosen = value; break;
          default: break;
        }
      }
    }
  }
  return chosen;
}
"#;

#[test]
fn keyed_payload_switch_does_not_treat_retained_callbacks_as_the_payload() {
    fn run_case(
        selection: &str,
        mutate: bool,
        route: &str,
        expected: &'static str,
    ) -> (StagedInvocation, StagedInvocation, &'static str) {
        let write = if mutate {
            "selected.state = \"ready\";"
        } else {
            "void selected;"
        };
        let source = r#"
import { factory } from './peer.js';
let later = 0;
const service = { state: "new" };
function callback() {
  const selected = __SELECTION__;
  __WRITE__
  return later;
}
const record = { payload: service, compare: callback };
__ROUTE__
const wrapped = factory(callback);
later = 42;
console.log(wrapped.invoke(), service.state);
"#
        .replace("__WRITE__", write)
        .replace("__SELECTION__", selection)
        .replace("__ROUTE__", route);
        let (stage, _) = staged(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "callbacks/selected",
                        &binding_members([
                            "service",
                            "callback",
                            "record",
                            "wrapped",
                            "choosePayload",
                        ]),
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_import_order(&["values/later", "callbacks/selected"])
            .with_extra_chunks(&[(
                "static/peer",
                "export function factory(callback) { return { invoke: callback }; }\n",
            )])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_retained_callback_factory("factory", 1, &[0])
                .build()])
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_node_output(&stage.root().join("snapshot/static/app.js"), expected, "");

        let fresh = stage.run(&["--emit-on-rejection"]);
        let saved = stage.run(&["--analysis", path(&stage.artifact), "--emit-on-rejection"]);
        (fresh, saved, expected)
    }

    let cases = [
        (
            "computed switch",
            "choosePayload(record)",
            true,
            CONDITION_SNAPSHOT_ROUTE,
            true,
            "42 ready\n",
        ),
        (
            "literal payload",
            "record.payload",
            true,
            CONDITION_SNAPSHOT_ROUTE,
            true,
            "42 ready\n",
        ),
        (
            "computed without write",
            "choosePayload(record)",
            false,
            CONDITION_SNAPSHOT_ROUTE,
            true,
            "42 new\n",
        ),
        (
            "callback payload",
            "choosePayload({payload: callback})",
            true,
            CONDITION_SNAPSHOT_ROUTE,
            false,
            "42 new\n",
        ),
        (
            "separate null check",
            "choosePayload(record)",
            true,
            SEPARATE_NULL_CHECK_ROUTE,
            true,
            "42 ready\n",
        ),
        (
            "separate null check callback payload",
            "choosePayload({payload: callback})",
            true,
            SEPARATE_NULL_CHECK_ROUTE,
            false,
            "42 new\n",
        ),
        (
            "separate null check after key mutation",
            "choosePayload(record)",
            true,
            MUTATED_KEY_ROUTE,
            false,
            "42 new\n",
        ),
    ];
    let mut runs = Vec::new();
    for (label, selection, mutate, route, accepted, expected) in cases {
        let (fresh, saved, expected) = run_case(selection, mutate, route, expected);
        runs.push((label, accepted, expected, fresh, saved));
    }

    let mut failures = Vec::new();
    for (label, accepted, expected, fresh, saved) in runs {
        for (kind, invocation) in [("fresh", &fresh), ("saved", &saved)] {
            if invocation.result.status.success() != accepted {
                failures.push(format!(
                    "{label} {kind} acceptance mismatch:\n{}",
                    invocation.result.stderr
                ));
                continue;
            }
            let emitted = invocation.output_root().join("app/static/app/entry.js");
            if accepted {
                assert_node_output(&emitted, expected, "");
            } else {
                let conflicts: Value = read_json(
                    &invocation
                        .output_root()
                        .join("reports/tree/static/app/atomic_group_conflicts.json"),
                );
                if !conflicts.as_array().is_some_and(|groups| {
                    groups.iter().any(|group| {
                        group["claims"].as_array().is_some_and(|claims| {
                            claims.iter().any(|claim| {
                                claim["binding_names"]
                                    .as_array()
                                    .is_some_and(|names| names.iter().any(|name| name == "later"))
                            })
                        })
                    })
                }) {
                    failures.push(format!(
                        "{label} {kind} refusal had no conflict witness involving `later`: {conflicts:#?}"
                    ));
                }
            }
        }
        assert_same_invocation_trees(&fresh, &saved);
    }
    assert!(
        failures.is_empty(),
        "case failures:\n{}",
        failures.join("\n")
    );
}
const PROVIDER: &str = "export function factory(callback) { return { invoke: callback }; }\n";

fn fixture(
    selection: &str,
    record: &str,
    write: &str,
    before_later: &str,
    after_later: &str,
) -> debundle_e2e_support::saved_analysis::Staged {
    let route = if selection == "record.payload" {
        ""
    } else {
        r#"
function route(record) {
  let chosen = null;
  for (const key in record) {
    let snapshot;
    if (((snapshot = record[key]), snapshot != null)) {
      switch (key) {
        case "payload": chosen = snapshot; break;
        default: break;
      }
    }
  }
  return chosen;
}
"#
    };
    let source = r#"
import { factory } from './peer.js';
let later = 0;
const service = { state: "new" };
function callback() {
  const selected = __SELECTION__;
  __WRITE__
  return 0;
}
const record = __RECORD__;
__ROUTE__
const wrapped = factory(callback);
__BEFORE_LATER__
later = 42;
__AFTER_LATER__
__LOG__
"#
    .replace("__SELECTION__", selection)
    .replace("__WRITE__", write)
    .replace("__RECORD__", record)
    .replace("__ROUTE__", route)
    .replace("__BEFORE_LATER__", before_later)
    .replace("__AFTER_LATER__", after_later)
    .replace(
        "__LOG__",
        if before_later.contains("observed") || after_later.contains("observed") {
            "console.log(observed, later);"
        } else {
            "console.log(service.state, later);"
        },
    );
    let mut members = ["service", "callback", "record", "wrapped"]
        .into_iter()
        .map(Member::new)
        .collect::<Vec<_>>();
    if !route.is_empty() {
        members.push(Member::new("route"));
    }
    if before_later.contains("observed") || after_later.contains("observed") {
        members.push(Member::new("observed"));
    }
    let (invocation, _) = staged(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon("callbacks/selected", &members, &["wrapped.invoke();"]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "callbacks/selected"])
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    invocation
}

fn assert_unsafe_cut_rejected(
    stage: &debundle_e2e_support::saved_analysis::Staged,
    original: &str,
    expected_error: &str,
) {
    assert_node_output(&stage.root().join("snapshot/static/app.js"), original, "");
    let fresh = stage.run(&["--emit-on-rejection"]);
    let saved = stage.run(&["--emit-on-rejection", "--analysis", path(&stage.artifact)]);
    for (kind, invocation) in [("fresh", &fresh), ("saved", &saved)] {
        assert_eq!(
            invocation.result.status.code(),
            Some(3),
            "{kind} unsafe member-projection cut was accepted:\n{}",
            invocation.result.stderr
        );
        assert_node_error(
            &invocation.output_root().join("app/static/app/entry.js"),
            expected_error,
        );
    }
    assert_same_invocation_trees(&fresh, &saved);
}

#[test]
fn getter_payload_that_returns_the_callback_keeps_the_unsafe_alias() {
    let getter = fixture(
        "route(record)",
        "{ get payload() { return callback; }, compare: callback }",
        "selected.extra = () => later;",
        "wrapped.invoke();\nconst observed = wrapped.invoke.extra();",
        "",
    );
    assert_unsafe_cut_rejected(
        &getter,
        "0 42\n",
        "Cannot access 'later' before initialization",
    );
}
