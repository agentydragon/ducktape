//! Math.round only avoids synchronous callbacks after primitive argument evaluation.

use debundle_e2e_support::*;

const PRIMITIVE_ROUND_SOURCE: &str = r#"globalThis.saved = () => later;
const seconds = 138240;
const rounded = Math.round(seconds / 86400);
const later = 42;
console.log(rounded, later);
"#;

fn round_opts<'a>(source: &'a str, members: &[Member], anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/rounded", members, anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

#[test]
fn unknown_host_setter_observes_publication_before_later_initializes() {
    let fixture = run_fixture(round_opts(
        PRIMITIVE_ROUND_SOURCE,
        &[Member::new("seconds"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    ));
    assert_preserved_entry_output_with_host_setup(
        &fixture,
        r#"Object.defineProperty(globalThis, 'saved', { configurable: true, set(callback) {
  try { console.log(callback()); } catch (error) { console.log(error.name); }
} });"#,
        "ReferenceError\n2 42\n",
        "global-publication-host-setter",
    );
}

#[test]
fn standard_host_names_do_not_prove_ordinary_prototype_set_behavior() {
    let fixture = run_fixture(
        round_opts(
            PRIMITIVE_ROUND_SOURCE,
            &[Member::new("seconds"), Member::new("rounded")],
            &["globalThis.saved = () => later;"],
        )
        .with_host_behavior(HostBehavior {
            predefined_globals: PredefinedGlobals::Standard,
            ..Default::default()
        }),
    );
    assert_preserved_entry_output_with_host_setup(
        &fixture,
        r#"const originalPrototype = Object.getPrototypeOf(globalThis);
const hostPrototype = new Proxy(Object.create(originalPrototype), {
  set(target, property, value, receiver) {
    if (property === 'saved') {
      try { console.log(value()); } catch (error) { console.log(error.name); }
    }
    return Reflect.set(target, property, value, receiver);
  }
});
if (Reflect.ownKeys(hostPrototype).length !== 0 || 'saved' in globalThis) {
  throw new Error('the host control must introduce no property definitions');
}
Object.setPrototypeOf(globalThis, hostPrototype);"#,
        "ReferenceError\n2 42\n",
        "global-publication-standard-prototype-set",
    );
}

#[test]
fn primitive_arithmetic_preserves_a_standard_host_publication() {
    let host_behavior = HostBehavior {
        predefined_globals: PredefinedGlobals::Standard,
        ..Default::default()
    };
    let baseline = run_fixture(
        FixtureOpts::new(PRIMITIVE_ROUND_SOURCE, vec![]).with_host_behavior(host_behavior.clone()),
    );
    assert_preserved_entry_output(&baseline, "2 42\n");
    save_relocation_reports(
        &baseline.report_root,
        &baseline.chunk_id,
        "global-publication-standard-host-baseline",
    );
    let fixture = run_fixture(
        round_opts(
            PRIMITIVE_ROUND_SOURCE,
            &[Member::new("seconds"), Member::new("rounded")],
            &["globalThis.saved = () => later;"],
        )
        .with_host_behavior(host_behavior),
    );
    save_relocation_reports(
        &fixture.report_root,
        &fixture.chunk_id,
        "global-publication-standard-host",
    );
    assert_preserved_entry_output(&fixture, "2 42\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 0, "{report}");
    assert_eq!(report["emitted_order"], serde_json::json!([0, 1, 2, 3, 4]));
    assert!(
        report["coverage"]["unknown_coverage"]["host_effects"]
            .as_u64()
            .unwrap()
            > 0,
        "source-order preservation does not prove global-store effects: {report}"
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const rounded = await import('./static/app/modules/values/rounded.js');
const later = await import('./static/app/modules/values/later.js');
console.log(Object.keys(rounded).sort().join(','), Object.keys(later).sort().join(','));"#,
        "rounded later\n",
    );
}

fn assert_round_reentry(case: &str, source: &str, members: &[Member], anonymous: &[&str]) {
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 2\n");
    match run_fixture_observing_gate(round_opts(source, members, anonymous)) {
        FixtureRunOutcome::Accepted(fixture) => {
            save_relocation_reports(&fixture.report_root, &fixture.chunk_id, case);
            assert_preserved_entry_output(&fixture, "ReferenceError 2\n");
        }
        FixtureRunOutcome::Rejected(rejected) => {
            save_relocation_reports(&rejected.report_root, &rejected.chunk_id, case);
            assert_eq!(rejected.status, Some(1), "{}", rejected.stderr);
            assert_ne!(
                rejected.candidate_relocation_assessment()["status"],
                "accepted_within_checked_scope"
            );
        }
    }
}

#[test]
fn object_argument_coercion_can_reenter() {
    assert_round_reentry(
        "math-object-coercion",
        r#"globalThis.saved = () => later;
const argument = { valueOf() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 1.6;
} };
const rounded = Math.round(argument);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("argument"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn primitive_argument_evaluation_can_reenter() {
    assert_round_reentry(
        "math-argument-evaluation",
        r#"globalThis.saved = () => later;
function argument() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 1.6;
}
const rounded = Math.round((argument(), 1.6));
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("argument"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn shadowed_math_can_reenter() {
    assert_round_reentry(
        "math-shadowed-receiver",
        r#"globalThis.saved = () => later;
const Math = { round(value) {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 2;
} };
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("Math"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn aliased_math_method_mutation_can_reenter() {
    assert_round_reentry(
        "math-aliased-method",
        r#"globalThis.saved = () => later;
const math = Math;
math.round = () => {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 2;
};
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("math"), Member::new("rounded")],
        &[
            "globalThis.saved = () => later;",
            "math.round = () => { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return 2; };",
        ],
    );
}

#[test]
fn math_method_getter_can_reenter() {
    assert_round_reentry(
        "math-method-getter",
        r#"globalThis.saved = () => later;
const originalRound = Math.round;
Object.defineProperty(Math, "round", { get() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return originalRound;
} });
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("originalRound"), Member::new("rounded")],
        &[
            "globalThis.saved = () => later;",
            "Object.defineProperty(Math, 'round', { get() { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return originalRound; } });",
        ],
    );
}
