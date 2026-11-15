use summary_effects::publication::{Refusal, SourceIndex, positions};

fn check(source: &str, binding: &str, order: &[usize], expected: Option<Refusal>, satisfied: bool) {
    js_ast::with_swc_globals(|| {
        let parsed = js_ast::parse_js_module("fixture.js", source).unwrap();
        let index = SourceIndex::new(&parsed.module);
        let candidates = index.certificates();
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.binding == binding)
            .unwrap();
        assert_eq!(candidate.refusal, expected);
        let positions = positions(order, index.unit_count()).unwrap();
        assert_eq!(
            candidate.refusal.is_none() && candidate.violations(&positions).is_empty(),
            satisfied
        );
    });
}

#[test]
fn allocation_can_move_but_its_copy_cannot_cross_observer() {
    let source = "var Holder = class { static get() { return alias; } }; inspect(Holder); var Hidden = class {}; var alias = Hidden; console.log(alias === Hidden);";
    check(source, "Hidden", &[2, 0, 1, 3, 4], None, true);
    check(source, "Hidden", &[2, 3, 0, 1, 4], None, false);
    let lexical = source.replace("var alias", "let alias");
    check(&lexical, "Hidden", &[2, 0, 1, 3, 4], None, true);
    check(&lexical, "Hidden", &[2, 3, 0, 1, 4], None, false);
}

#[test]
fn hoisted_and_already_captured_readers_are_unavailable_for_certificate() {
    check(
        "inspect(readHidden); var Hidden = class {}; function readHidden(){ return Hidden; }",
        "Hidden",
        &[1, 0, 2],
        Some(Refusal::HoistedReader),
        false,
    );
    check(
        "var readHidden = () => Hidden; inspect(readHidden); var Hidden = class {};",
        "Hidden",
        &[2, 0, 1],
        Some(Refusal::AlreadyAvailableReader),
        false,
    );
}

#[test]
fn captures_created_after_init_are_publication_owners() {
    let source = "inspect(); var Hidden = class {}; var reader = () => Hidden; publish(reader);";
    check(source, "Hidden", &[1, 0, 2, 3], None, true);
    check(source, "Hidden", &[1, 2, 0, 3], None, false);
}

#[test]
fn exported_cells_including_future_values_are_never_certified() {
    check(
        "inspect(); export var Hidden = class {};",
        "Hidden",
        &[1, 0],
        Some(Refusal::ExportedCell),
        false,
    );
    check(
        "inspect(); var Hidden = class {}; export {Hidden as publicValue};",
        "Hidden",
        &[1, 0, 2],
        Some(Refusal::ExportedCell),
        false,
    );
    // An export label with the same spelling is not a reference to Hidden.
    check(
        "var other = 1; inspect(); var Hidden = class {}; var alias = Hidden; export {other as Hidden};",
        "Hidden",
        &[2, 0, 1, 3, 4],
        None,
        true,
    );
}

#[test]
fn only_noninvoking_class_allocation_is_certified() {
    for class in [
        "class extends Base {}",
        "class { [key()](){} }",
        "class { static field = effect(); }",
        "class { field = 1; }",
        "class { static { effect(); } }",
        "class { get value(){ return 1; } }",
    ] {
        let source = format!("inspect(); var Hidden = {class}; var alias = Hidden;");
        check(
            &source,
            "Hidden",
            &[1, 0, 2],
            Some(Refusal::EagerClassEvaluation),
            false,
        );
    }
    check(
        "inspect(); var Hidden = class { constructor(){ effect(); } static value(){ effect(); } value(){ effect(); } }; var alias = Hidden;",
        "Hidden",
        &[1, 0, 2],
        None,
        true,
    );
}

#[test]
fn writes_anywhere_and_incomplete_census_refuse() {
    for write in [
        "Hidden = 1",
        "Hidden++",
        "({value: Hidden} = object)",
        "for (Hidden of values) {}",
        "function change(){ Hidden = 1; }",
    ] {
        let source = format!("inspect(); var Hidden = class {{}}; {write};");
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module("fixture.js", &source).unwrap();
            let index = SourceIndex::new(&parsed.module);
            let candidate = index
                .certificates()
                .into_iter()
                .find(|candidate| candidate.binding == "Hidden")
                .unwrap();
            assert_eq!(
                candidate.refusal,
                Some(Refusal::RedeclaredOrRebound),
                "{write}"
            );
        });
    }
    check(
        "inspect(); var Hidden = class {}; eval('Hidden');",
        "Hidden",
        &[1, 0, 2],
        Some(Refusal::IncompleteCensus),
        false,
    );
    check(
        "var Hidden; inspect(); var Hidden = class {};",
        "Hidden",
        &[2, 0, 1],
        Some(Refusal::RedeclaredOrRebound),
        false,
    );
}

#[test]
fn resolved_identity_separates_shadowed_names() {
    check(
        "function unrelated(Hidden){ return Hidden; } inspect(); var Hidden = class {}; var alias = Hidden;",
        "Hidden",
        &[2, 0, 1, 3],
        None,
        true,
    );
    check(
        "inspect(); var Hidden = class Named { method(){ return Named; } }; var alias = Hidden;",
        "Hidden",
        &[1, 0, 2],
        None,
        true,
    );
}

#[test]
fn node_observes_publication_fence_and_tdz_boundary() {
    use runfiles::{Runfiles, rlocation};
    let runfiles = Runfiles::create().unwrap();
    let node = rlocation!(runfiles, "nodejs_linux_amd64/bin/node").unwrap();
    for (declaration, expected) in [
        ("var", "undefined\ntrue\n"),
        ("let", "ReferenceError\ntrue\n"),
    ] {
        let source = format!(
            "var Holder = class {{ static get() {{ return alias; }} }}; inspect(Holder); var Hidden = class {{}}; {declaration} alias = Hidden; console.log(alias === Hidden); function inspect(value) {{ try {{ console.log(typeof value.get()); }} catch(error) {{ console.log(error.name); }} }}"
        );
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module("fixture.js", &source).unwrap();
            let index = SourceIndex::new(&parsed.module);
            let candidates = index.certificates();
            let certificate = candidates
                .iter()
                .find(|candidate| candidate.binding == "Hidden")
                .unwrap();
            assert_eq!(
                certificate.refusal, None,
                "positive selects typed certificate"
            );
            for (order, output, accepted) in [
                (vec![0, 1, 2, 3, 4, 5], expected, true),
                (vec![2, 0, 1, 3, 4, 5], expected, true),
                (vec![2, 3, 0, 1, 4, 5], "function\ntrue\n", false),
            ] {
                let mut emitted = parsed.module.clone();
                emitted.body = order
                    .iter()
                    .map(|unit| parsed.module.body[*unit].clone())
                    .collect();
                let code = js_ast::emit_module_source(&emitted).unwrap();
                let result = std::process::Command::new(&node)
                    .args(["--input-type=module", "--eval", &code])
                    .output()
                    .unwrap();
                assert!(
                    result.status.success(),
                    "{}",
                    String::from_utf8_lossy(&result.stderr)
                );
                assert_eq!(String::from_utf8(result.stdout).unwrap(), output);
                let positions = positions(&order, index.unit_count()).unwrap();
                assert_eq!(certificate.violations(&positions).is_empty(), accepted);
            }
        });
    }
}

#[test]
fn ordinary_construction_remains_a_publication_barrier() {
    use runfiles::{Runfiles, rlocation};
    let runfiles = Runfiles::create().unwrap();
    let node = rlocation!(runfiles, "nodejs_linux_amd64/bin/node").unwrap();
    let source = "var Factory = class { constructor(){ console.log(typeof alias); } }; var Hidden = class {}; new Factory(); var alias = Hidden; console.log(alias === Hidden);";
    js_ast::with_swc_globals(|| {
        let parsed = js_ast::parse_js_module("fixture.js", source).unwrap();
        let index = SourceIndex::new(&parsed.module);
        let candidates = index.certificates();
        let certificate = candidates
            .iter()
            .find(|candidate| candidate.binding == "Hidden")
            .unwrap();
        assert_eq!(certificate.refusal, None);
        for (order, expected, accepted) in [
            (vec![0, 1, 2, 3, 4], "undefined\ntrue\n", true),
            (vec![1, 0, 2, 3, 4], "undefined\ntrue\n", true),
            (vec![0, 1, 3, 2, 4], "function\ntrue\n", false),
        ] {
            let mut emitted = parsed.module.clone();
            emitted.body = order
                .iter()
                .map(|unit| parsed.module.body[*unit].clone())
                .collect();
            let code = js_ast::emit_module_source(&emitted).unwrap();
            let result = std::process::Command::new(&node)
                .args(["--input-type=module", "--eval", &code])
                .output()
                .unwrap();
            assert!(result.status.success());
            assert_eq!(String::from_utf8(result.stdout).unwrap(), expected);
            let positions = positions(&order, index.unit_count()).unwrap();
            assert_eq!(certificate.violations(&positions).is_empty(), accepted);
        }
    });
}
