//! `spec lint` decides `host_behavior` loader keys and the loader
//! `program_premises` key from the export-reentry loader closures' recorded
//! uses (docs/premise_provenance.md): a key no closure consumed is `inferable`;
//! a consumed one is `inconclusive` until `--probe-analysis` reruns without it.

use analysis::analysis_inputs::HostBehaviorKey;
use analysis::reports::spec_lint::{
    InconclusiveReason, InferableReason, InferenceVerdict, LintFinding, LintedAnnotation,
};
use debundle_e2e_support::*;

const PROVIDER_CHUNK: &str = "static/provider";

/// An opaque call precedes later exported state; `loadHelper` is the program's
/// one loader site, a literal CommonJS request.
const PROVIDER: &str = r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
export function loadHelper() { return import.meta.require("./helper.js"); }
"#;
const HELPER: &str = "exports.suffix = \"!\";\n";
const CONSUMER: &str = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
const EXTRA_CHUNKS: [(&str, &str); 2] = [("static/helper", HELPER), (PROVIDER_CHUNK, PROVIDER)];

/// The two keys the closure needs, plus two it never reads here: the program
/// has no suspended loader site and no embedded-file request.
fn host() -> HostBehavior {
    HostBehavior {
        loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Throws),
        synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
        synchronous_load_drains_microtasks: Some(SynchronousLoadDrainsMicrotasks::Never),
        reentry_free_embedded_loaders: [EmbeddedLoader::Text].into_iter().collect(),
        ..HostBehavior::default()
    }
}

fn provider_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/later",
            &[
                Member::new("later"),
                Member::new("Registry"),
                Member::new("readLater"),
                Member::new("loadHelper"),
            ],
        ),
    ]
}

fn loader_verdicts(args: &[&str]) -> (Vec<(HostBehaviorKey, InferenceVerdict)>, serde_json::Value) {
    let provider_modules = [(PROVIDER_CHUNK, provider_modules())];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&EXTRA_CHUNKS)
            .with_extra_chunk_logical_modules(&provider_modules)
            .with_host_behavior(host()),
    );
    let report = spec_lint_json(&fixture.spec_path, args);
    let findings: Vec<LintFinding> = serde_json::from_value(report["findings"].clone())
        .unwrap_or_else(|error| panic!("{error}: {report:#}"));
    let verdicts = findings
        .into_iter()
        .filter_map(|finding| match finding.annotation {
            LintedAnnotation::LoaderHostBehavior { key } => {
                assert_eq!(finding.chunk, None, "{report:#}");
                Some((key, finding.verdict))
            }
            _ => None,
        })
        .collect();
    (verdicts, report)
}

const CONSUMED: InferenceVerdict = InferenceVerdict::Inconclusive {
    reason: InconclusiveReason::Consumed,
};
const NEVER_CONSUMED: InferenceVerdict = InferenceVerdict::Inferable {
    reason: InferableReason::NeverConsumed,
};

#[test]
fn keys_the_closed_closure_did_not_read_are_inferable() {
    let (verdicts, report) = loader_verdicts(&[]);
    assert_eq!(
        verdicts,
        vec![
            (HostBehaviorKey::LoaderResultForEvaluatingModule, CONSUMED),
            (HostBehaviorKey::SynchronousLoadRunsAppCallbacks, CONSUMED),
            (
                HostBehaviorKey::SynchronousLoadDrainsMicrotasks,
                NEVER_CONSUMED
            ),
            (
                HostBehaviorKey::ReentryFreeEmbeddedLoader(EmbeddedLoader::Text),
                NEVER_CONSUMED
            ),
        ],
        "{report:#}"
    );
}

/// Withholding either consumed key leaves the closure undeclared, so the
/// policy turns conservative and the split is rejected: the probe reports
/// nothing for them.
#[test]
fn probe_drops_keys_the_closure_needs() {
    let (verdicts, report) = loader_verdicts(&["--probe-analysis"]);
    assert!(
        verdicts.iter().all(|(key, _)| !matches!(
            key,
            HostBehaviorKey::LoaderResultForEvaluatingModule
                | HostBehaviorKey::SynchronousLoadRunsAppCallbacks
        )),
        "{report:#}"
    );
    let probes = report["probes"].as_array().expect("probe rows");
    assert_eq!(probes.len(), 2, "{report:#}");
    assert!(
        probes.iter().all(|probe| probe["outcome"] == "changed"),
        "{report:#}"
    );
}
