use debundle_e2e_support::{
    FixtureOpts, Member, assert_entry_output, assert_module_exports, assert_module_source,
    logical_module, run_fixture,
};

const ROUTE_COUNT: usize = 12;
const SPECIFIC_ROUTE_EXPORTS: [&str; ROUTE_COUNT - 1] = [
    "Route00", "Route01", "Route02", "Route03", "Route04", "Route05", "Route06", "Route07",
    "Route08", "Route09", "Route10",
];

#[test]
fn individual_selectors_match_plain_exported_and_destructured_declarators() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
const a = { label: "first" }, b = (value) => value + "-second";
export const { label: c } = { label: "third" }, d = ["fourth"];
console.log(a.label, b("value"), c, d[0]);
"#,
        vec![
            logical_module(
                "plain",
                &[
                    Member::source_alpha("First", r#"const selected = { label: "first" };"#),
                    Member::source_alpha(
                        "Second",
                        r#"const selected = (input) => input + "-second";"#,
                    ),
                ],
            ),
            logical_module(
                "exported",
                &[
                    Member::source_alpha_target(
                        "Third",
                        "selected",
                        r#"export const { label: selected } = { label: "third" };"#,
                    ),
                    Member::source_alpha("Fourth", r#"export const selected = ["fourth"];"#),
                ],
            ),
        ],
    ));

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/plain.js",
        &["First", "Second"],
        &["Third", "Fourth"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/exported.js",
        &["Third", "Fourth"],
        &["First", "Second"],
    );
    assert_entry_output(&fixture, "first value-second third fourth\n");
}

#[test]
fn broad_alpha_all_selector_resolves_to_remaining_global_target() {
    let source = route_source();
    let specific_members: Vec<Member> = SPECIFIC_ROUTE_EXPORTS
        .iter()
        .enumerate()
        .map(|(slot, &export_name)| {
            Member::source_alpha_target(export_name, "targetRoute", route_selector(Some(slot)))
        })
        .collect();
    let remainder_members = vec![Member::source_alpha_target(
        "RouteRemainder",
        "targetRoute",
        route_selector(None),
    )];

    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![
            logical_module("routes/specific", &specific_members),
            logical_module("routes/remainder", &remainder_members),
        ],
    ));

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/routes/remainder.js",
        &["RouteRemainder"],
        &["Route00", "Route10"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/routes/remainder.js",
        &["slot-11"],
        &["slot-00", "slot-10"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/routes/specific.js",
        &["slot-00", "slot-10"],
        &["slot-11"],
    );
    assert_entry_output(&fixture, "slot-11\n");
}

fn route_source() -> String {
    let mut source = r#"const sharedRuntime = {
  normalize(value) {
    return String(value).trim();
  },
  emit(kind, wrapped, slot) {
    return { kind, wrapped, slot, marker: slot };
  },
};

function sharedRead(input, key) {
  return input[key] ?? "";
}

function commonWrap(label, value) {
  return `${label}:${value}`;
}
"#
    .to_string();
    for slot in 0..ROUTE_COUNT {
        source.push_str(&format!(
            r#"
function route{slot:02}(input) {{
  const local{slot:02}A = sharedRead(input, "user");
  const local{slot:02}B = sharedRuntime.normalize(local{slot:02}A);
  return sharedRuntime.emit("route", commonWrap("common", local{slot:02}B), "slot-{slot:02}");
}}
"#
        ));
    }
    source.push_str(
        r#"
console.log(route11({ user: " ok " }).marker);
export {
"#,
    );
    for slot in 0..ROUTE_COUNT {
        source.push_str(&format!("  route{slot:02},\n"));
    }
    source.push_str("};\n");
    source
}

fn route_selector(slot: Option<usize>) -> String {
    let slot_arg = slot
        .map(|slot| format!(r#""slot-{slot:02}""#))
        .unwrap_or_else(|| "ANYTHING".to_string());
    format!(
        r#"function targetRoute(input) {{
  const first = sharedRead(input, "user");
  const second = sharedRuntime.normalize(first);
  return sharedRuntime.emit("route", commonWrap("common", second), {slot_arg});
}}"#
    )
}
