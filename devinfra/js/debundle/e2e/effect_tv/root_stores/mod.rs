use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

fn source_path(rejected: &RejectedFixture) -> std::path::PathBuf {
    rejected
        .source_root
        .join(format!("{}.js", rejected.chunk_id))
}

fn assert_owner_coverage_refusal(report: &serde_json::Value, reason: &str, case: &str) {
    assert!(
        report["coverage"]["owner_coverage_refusals"][reason]
            .as_u64()
            .is_some_and(|owners| owners > 0),
        "{case}: no owner refused with {reason}: {report:#}"
    );
}

/// Both owners of the swapped pair are covered and conflict on the named
/// binding or own-property cell: a modeled conflict, not uncovered havoc.
fn assert_covered_cell_conflict(
    effects: &serde_json::Value,
    kind: &str,
    discriminator: &str,
    name: &str,
) {
    assert_eq!(effects["decision"]["status"], "conflict", "{effects:#}");
    assert_eq!(effects["decision"]["havoc"], false, "{effects:#}");
    assert!(
        examples(effects).iter().any(|row| {
            row["havoc"] == false
                && row["cells"].as_array().is_some_and(|cells| {
                    cells.iter().any(|cell| {
                        cell["cell"]["kind"] == kind && cell["cell"][discriminator] == name
                    })
                })
        }),
        "{effects:#}"
    );
}

mod binding_stores;
mod own_property_stores;
