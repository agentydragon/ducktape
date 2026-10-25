//! Object.fromEntries over a fresh mapped pair array stores values without
//! invoking functions nested in the mapped entries.

use debundle_e2e_support::*;

#[test]
fn from_entries_map_does_not_run_stored_callback_during_construction() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let invocations = 0;
let later = 0;
const read = () => { invocations++; return later; };
globalThis.saved = read;
const entries = [{ name: "read", run: read }];
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
const duringConstruction = invocations;
later = 42;
const result = registry.read.run();
console.log(duringConstruction, invocations, result);
export { entries, registry, duringConstruction, later, result };
"#,
        vec![
            logical_module_with_anon(
                "values/registry",
                &[
                    Member::new("entries"),
                    Member::new("registry"),
                    Member::new("duringConstruction"),
                    Member::new("invocations"),
                    Member::new("read"),
                ],
                &["globalThis.saved = read;"],
            ),
            logical_module_with_anon(
                "values/later",
                &[Member::new("later"), Member::new("result")],
                &[
                    "later = 42;",
                    "console.log(duringConstruction, invocations, result);",
                ],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 1 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn returned_value_callback_remains_visible_to_an_early_member_call() {
    let source = r#"let invocations = 0;
let later = 0;
const entries = [{ name: "read", run: () => { invocations++; return later; } }];
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
const early = registry.read.run();
later = 42;
console.log(early, invocations, later);
export { entries, registry, early, later };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("registry"),
                        Member::new("early"),
                        Member::new("invocations"),
                    ],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(early, invocations, later);"],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("registry"),
                    Member::new("early"),
                    Member::new("later"),
                    Member::new("invocations"),
                ],
                &["later = 42;", "console.log(early, invocations, later);"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 1 42\n");
    assert_all_emitted_js_checks(&colocated);
}

#[test]
fn direct_chained_call_does_not_match_the_from_entries_constructor_site() {
    let source = r#"let invocations = 0;
let later = 0;
const entries = [{ name: "read", run: () => { invocations++; return later; } }];
const early = Object.fromEntries(entries.map(entry => [entry.name, entry])).read.run();
later = 42;
console.log(early, invocations, later);
export { entries, early, later, invocations };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("early"),
                        Member::new("invocations"),
                    ],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(early, invocations, later);"],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("early"),
                    Member::new("later"),
                    Member::new("invocations"),
                ],
                &["later = 42;", "console.log(early, invocations, later);"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 1 42\n");
    assert_all_emitted_js_checks(&colocated);
}

#[test]
fn returned_value_callback_remains_visible_to_an_opaque_consumer() {
    let source = r#"let invocations = 0;
let later = 0;
const entries = [{ name: "read", run: () => { invocations++; return later; } }];
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
globalThis.consumeRegistry = value => value.read.run();
const early = globalThis.consumeRegistry(registry);
later = 42;
console.log(early, invocations, later);
export { entries, registry, early, later };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("registry"),
                        Member::new("early"),
                        Member::new("invocations"),
                    ],
                    &["globalThis.consumeRegistry = value => value.read.run();"],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(early, invocations, later);"],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("registry"),
                    Member::new("early"),
                    Member::new("later"),
                    Member::new("invocations"),
                ],
                &[
                    "globalThis.consumeRegistry = value => value.read.run();",
                    "later = 42;",
                    "console.log(early, invocations, later);",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 1 42\n");
    assert_all_emitted_js_checks(&colocated);
}

#[test]
fn mutated_entry_key_is_not_treated_as_a_primitive_data_property() {
    let source = r#"let later = 0;
let conversions = 0;
const entries = [{ name: "read", run: () => later }];
const key = { toString() { conversions++; return later; } };
function mutateEntry(entry) { entry.name = key; }
mutateEntry(entries[0]);
const oldKey = "0";
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
later = 42;
const result = registry[oldKey].run();
console.log(conversions, result);
export { entries, registry, oldKey, later, result };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("registry"),
                        Member::new("oldKey"),
                        Member::new("key"),
                        Member::new("conversions"),
                        Member::new("mutateEntry"),
                    ],
                    &["mutateEntry(entries[0]);"],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later"), Member::new("result")],
                    &["later = 42;", "console.log(conversions, result);"],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("registry"),
                    Member::new("oldKey"),
                    Member::new("later"),
                    Member::new("result"),
                    Member::new("key"),
                    Member::new("conversions"),
                    Member::new("mutateEntry"),
                ],
                &[
                    "mutateEntry(entries[0]);",
                    "later = 42;",
                    "console.log(conversions, result);",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "1 42\n");
    assert_all_emitted_js_checks(&colocated);
}

#[test]
fn aliased_array_species_mutation_keeps_map_construction_opaque() {
    let source = r#"let later = 0;
let observed = -1;
const entries = [{ name: "read", run: () => later }];
const alias = entries;
class SpeciesArray extends Array {
  constructor(length) { super(length); observed = later; }
}
alias.constructor = { [Symbol.species]: SpeciesArray };
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
later = 42;
console.log(observed);
export { entries, registry, later, observed, SpeciesArray };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("registry"),
                        Member::new("observed"),
                        Member::new("SpeciesArray"),
                        Member::new("alias"),
                    ],
                    &["alias.constructor = { [Symbol.species]: SpeciesArray };"],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(observed);"],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("registry"),
                    Member::new("later"),
                    Member::new("observed"),
                    Member::new("SpeciesArray"),
                    Member::new("alias"),
                ],
                &[
                    "alias.constructor = { [Symbol.species]: SpeciesArray };",
                    "later = 42;",
                    "console.log(observed);",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0\n");
    assert_all_emitted_js_checks(&colocated);
}

#[test]
fn mutations_through_from_entries_result_reach_source_entries() {
    let source = r#"let later = 0;
let observed = -1;
const entries = [{ name: "read", value: 0 }];
const firstRegistry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
function mutate(result) {
  result.read.name = { toString() { observed = later; return "read"; } };
}
mutate(firstRegistry);
const secondRegistry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
later = 42;
console.log(observed, secondRegistry.read.value);
export { entries, firstRegistry, secondRegistry, later, observed, mutate };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/registry",
                    &[
                        Member::new("entries"),
                        Member::new("firstRegistry"),
                        Member::new("secondRegistry"),
                        Member::new("observed"),
                        Member::new("mutate"),
                    ],
                    &["mutate(firstRegistry);"],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later")],
                    &[
                        "later = 42;",
                        "console.log(observed, secondRegistry.read.value);",
                    ],
                ),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values/all",
                &[
                    Member::new("entries"),
                    Member::new("firstRegistry"),
                    Member::new("secondRegistry"),
                    Member::new("later"),
                    Member::new("observed"),
                    Member::new("mutate"),
                ],
                &[
                    "mutate(firstRegistry);",
                    "later = 42;",
                    "console.log(observed, secondRegistry.read.value);",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 0\n");
    assert_all_emitted_js_checks(&colocated);
}
