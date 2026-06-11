//! End-to-end coverage for `binding_groups[].adopt_names`.

use debundle_e2e_support::*;

#[test]
fn binding_group_adopt_names_true_exports_selector_local_names() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var a = "Ada", b = "Lovelace";
console.log(a + " " + b);
export { a, b };
"#,
        vec![logical_module_with_binding_groups(
            "person",
            &[],
            &[BindingGroup::source_alpha_adopt_all(
                r#"var FirstName = "Ada", LastName = "Lovelace";"#,
            )],
        )],
    ));

    assert_entry_output(&fixture, "Ada Lovelace\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/person.js",
        &["FirstName", "LastName"],
        &["a", "b"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/person.js",
        &[r#"var FirstName = "Ada""#, r#"var LastName = "Lovelace""#],
        &[],
    );
}

#[test]
fn binding_group_adopt_names_list_exports_only_requested_names() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1, b = 2, c = 3;
console.log(a + b + c);
export { a, b, c };
"#,
        vec![logical_module_with_binding_groups(
            "subset",
            &[],
            &[BindingGroup::source_alpha_adopt_names(
                r#"const ReadableA = 1, ReadableB = 2, ReadableC = 3;"#,
                &["ReadableB"],
            )],
        )],
    ));

    assert_entry_output(&fixture, "6\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/subset.js",
        &["ReadableB"],
        &["b"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/subset.js",
        &["const ReadableB = 2"],
        &["ReadableA", "ReadableC"],
    );
}

#[test]
fn binding_group_exports_override_adopted_public_names() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 10, b = 20;
console.log(a + b);
export { a, b };
"#,
        vec![logical_module_with_binding_groups(
            "aliased",
            &[],
            &[BindingGroup::source_alpha_adopt_all_with_exports(
                r#"const ReadableA = 10, ReadableB = 20;"#,
                &[("ReadableB", "renamedB")],
            )],
        )],
    ));

    assert_entry_output(&fixture, "30\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/aliased.js",
        &["ReadableA", "renamedB"],
        &["a", "b"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/aliased.js",
        &["const ReadableA = 10", "const renamedB = 20"],
        &["ReadableB = 20"],
    );
}

#[test]
fn binding_group_adopt_names_list_rejects_missing_selector_binding() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a = 1;
console.log(a);
export { a };
"#,
            vec![logical_module_with_binding_groups(
                "bad",
                &[],
                &[BindingGroup::source_alpha_adopt_names(
                    r#"const Present = 1;"#,
                    &["Missing"],
                )],
            )],
        ),
        &["adopt_names", "Missing", "not declared"],
    );
}

#[test]
fn binding_group_adopt_names_true_rejects_duplicate_selector_bindings() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"var a = 1, b = 2;
console.log(a + b);
export { a, b };
"#,
            vec![logical_module_with_binding_groups(
                "bad",
                &[],
                &[BindingGroup::source_alpha_adopt_all(
                    r#"var Same = 1, Same = 2;"#,
                )],
            )],
        ),
        &["duplicate", "selector-local", "Same"],
    );
}
