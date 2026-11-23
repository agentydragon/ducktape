//! Object.fromEntries over a fresh mapped pair array stores values without
//! invoking functions nested in the mapped entries.

use debundle_e2e_support::*;

/// Binding names and anonymous statements assigned to one module.
type ModuleContents<'a> = (&'a [&'static str], &'a [&'a str]);

fn module(path: &str, (bindings, anonymous): ModuleContents<'_>) -> LogicalModuleEntry {
    let members: Vec<_> = bindings.iter().copied().map(Member::new).collect();
    logical_module_with_anon(path, &members, anonymous)
}

/// Rejects placing `registry` and `later` in separate modules, then runs the same
/// bindings and statements together in `values/all` and expects `expected_stdout`.
fn expect_split_rejected_and_colocated_run(
    source: &str,
    registry: ModuleContents<'_>,
    later: ModuleContents<'_>,
    expected_stdout: &str,
) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                module("values/registry", registry),
                module("values/later", later),
            ],
        ),
        &["values/registry", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![module(
                "values/all",
                (
                    &[registry.0, later.0].concat(),
                    &[registry.1, later.1].concat(),
                ),
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, expected_stdout);
    assert_all_emitted_js_checks(&colocated);
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
    expect_split_rejected_and_colocated_run(
        source,
        (&["entries", "registry", "early", "invocations"], &[]),
        (
            &["later"],
            &["later = 42;", "console.log(early, invocations, later);"],
        ),
        "0 1 42\n",
    );
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
    expect_split_rejected_and_colocated_run(
        source,
        (&["entries", "early", "invocations"], &[]),
        (
            &["later"],
            &["later = 42;", "console.log(early, invocations, later);"],
        ),
        "0 1 42\n",
    );
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
    expect_split_rejected_and_colocated_run(
        source,
        (
            &["entries", "registry", "early", "invocations"],
            &["globalThis.consumeRegistry = value => value.read.run();"],
        ),
        (
            &["later"],
            &["later = 42;", "console.log(early, invocations, later);"],
        ),
        "0 1 42\n",
    );
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
    expect_split_rejected_and_colocated_run(
        source,
        (
            &[
                "entries",
                "registry",
                "oldKey",
                "key",
                "conversions",
                "mutateEntry",
            ],
            &["mutateEntry(entries[0]);"],
        ),
        (
            &["later", "result"],
            &["later = 42;", "console.log(conversions, result);"],
        ),
        "1 42\n",
    );
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
    expect_split_rejected_and_colocated_run(
        source,
        (
            &["entries", "registry", "observed", "SpeciesArray", "alias"],
            &["alias.constructor = { [Symbol.species]: SpeciesArray };"],
        ),
        (&["later"], &["later = 42;", "console.log(observed);"]),
        "0\n",
    );
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
    expect_split_rejected_and_colocated_run(
        source,
        (
            &[
                "entries",
                "firstRegistry",
                "secondRegistry",
                "observed",
                "mutate",
            ],
            &["mutate(firstRegistry);"],
        ),
        (
            &["later"],
            &[
                "later = 42;",
                "console.log(observed, secondRegistry.read.value);",
            ],
        ),
        "0 0\n",
    );
}
