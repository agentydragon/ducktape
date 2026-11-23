//! Automatic Object own-data reads preserve getter-free lookup and later call effects.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::saved_analysis::{
    assert_ok, assert_same_invocation_trees, graph_node_for_binding, path, staged,
};
use debundle_e2e_support::*;
use serde_json::Value;

fn input(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(source_match_selector(
            declaration.trim(),
            &["alias"],
        )),
        operation: NativeOperationKind::Symbol,
        audited_source_sha256: source_sha256(source),
        note: Some("Only this direct Symbol call is audited.".to_owned()),
    }
}

fn decorator_trio(suffix: &str, class: &str, role: &str) -> String {
    format!(
        "var g{suffix} = Object.getOwnPropertyDescriptor;\nvar p{suffix} = Object.defineProperty;\n\
         var d{suffix} = (decorators, target, key) => {{\n  const desc = g{suffix}(target, key);\n\
           for (const decorator of decorators) decorator(target, key, desc);\n\
           p{suffix}(target, key, desc);\n}};\n\
         class {class} {{ m() {{ return '{role}'; }} }}\n\
         d{suffix}([], {class}.prototype, 'm', 1);\n"
    )
}

fn intrinsic_alias_opts(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "alpha",
                &[
                    Member::source_alpha("Alpha", "class Alpha { m() { return 'alpha role'; } }"),
                    Member::makes_decorate_call("decorate", "Alpha", None, None),
                    Member::intrinsic_alias("alphaDefine", "defineProperty", "decorate"),
                    Member::intrinsic_alias("alphaGetter", "getOwnPropertyDescriptor", "decorate"),
                ],
            ),
            logical_module(
                "beta",
                &[
                    Member::source_alpha("Beta", "class Beta { m() { return 'beta role'; } }"),
                    Member::makes_decorate_call("decorate", "Beta", None, None),
                    Member::intrinsic_alias("betaDefine", "defineProperty", "decorate"),
                ],
            ),
        ],
    )
    .with_import_order(&["alpha", "beta", "residual"])
}

fn resolved_member_input(source: &str, module: &str, name: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::ResolvedMember(ResolvedMemberSelector {
            module: module.to_owned(),
            name: name.to_owned(),
        }),
        operation: NativeOperationKind::Symbol,
        audited_source_sha256: source_sha256(source),
        note: Some("One resolved Symbol call is audited.".to_owned()),
    }
}

fn project_analysis(opts: FixtureOpts<'_>) -> CommandResult {
    let fixture = write_validate_fixture_spec(opts);
    let output = debundle_e2e_support::process::debundle_output(
        std::process::Command::new(debundler_path())
            .args(["spec", "project-analysis", "--spec"])
            .arg(&fixture.spec_path)
            .arg("--out")
            .arg(fixture.spec_path.with_extension("analysis.projection")),
    )
    .expect("spawn debundle spec project-analysis");
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

fn assert_project_analysis_rejected(opts: FixtureOpts<'_>, tokens: &[&str]) {
    let result = project_analysis(opts);
    assert_eq!(
        result.status.code(),
        Some(1),
        "expected project-analysis to reject the premise\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    for token in tokens {
        assert!(
            result.stderr.contains(token),
            "project-analysis stderr missing {token:?}\nstderr:\n{}",
            result.stderr,
        );
    }
}

#[test]
fn intrinsic_alias_relations_resolve_without_native_operation_inputs() {
    const EXPECTED: &str = "[[\"alpha role\",true,false,true,\"alpha role\"],[\"beta role\",true,false,true,\"beta role\"]]\n";
    let source = format!(
        "{}{}\
         const observe = Type => {{\
           const instance = new Type();\
           const descriptor = Object.getOwnPropertyDescriptor(Type.prototype, 'm');\
           return [descriptor.value.call(instance), descriptor.writable, descriptor.enumerable, descriptor.configurable, instance.m()];\
         }};\
         console.log(JSON.stringify([observe(Alpha), observe(Beta)]));\n",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let (staged, _fixture) = staged(intrinsic_alias_opts(&source));
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct relational-alias run");
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&saved.result, "saved-analysis relational-alias run");
    assert_same_invocation_trees(&direct, &saved);

    for invocation in [&direct, &saved] {
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            EXPECTED,
            "",
        );
    }

    let out_root = direct.output_root();
    let app_root = out_root.join("app");
    assert_module_exports(
        &app_root,
        "static/app/modules/alpha.js",
        &["Alpha", "decorate"],
        &["Beta"],
    );
    assert_module_exports(
        &app_root,
        "static/app/modules/beta.js",
        &["Beta", "decorate"],
        &["Alpha"],
    );
    assert_module_source(
        &app_root,
        "static/app/modules/alpha.js",
        &[
            "var alphaDefine = Object.defineProperty",
            "var alphaGetter = Object.getOwnPropertyDescriptor",
            "alphaGetter(target, key)",
            "alphaDefine(target, key, desc)",
            "class Alpha",
        ],
        &["class Beta", "betaDefine", "betaGetter"],
    );
    assert_module_source(
        &app_root,
        "static/app/modules/beta.js",
        &[
            "import { gb } from \"./residual/unhandled.js\";",
            "var betaDefine = Object.defineProperty",
            "gb(target, key)",
            "betaDefine(target, key, desc)",
            "class Beta",
        ],
        &["class Alpha", "alphaDefine", "alphaGetter"],
    );
}

#[test]
fn source_match_and_resolved_member_keep_the_same_site_and_dependencies() {
    const SOURCE: &str =
        "var alias = Symbol('alias');\nconst value = alias;\nexport { alias, value };\n";
    let modules = vec![logical_module(
        "alias_module",
        &binding_members(["alias", "value"]),
    )];
    let source_match = run_fixture_with_graphs(
        FixtureOpts::new(SOURCE, modules.clone()).with_native_operation_inputs(
            "static/app",
            vec![input(SOURCE, "var alias = Symbol('alias');")],
        ),
    );
    let resolved_member = run_fixture_with_graphs(
        FixtureOpts::new(SOURCE, modules).with_native_operation_inputs(
            "static/app",
            vec![resolved_member_input(SOURCE, "alias_module", "alias")],
        ),
    );

    let source_graph = serde_json::to_value(source_match.dependency_graph()).unwrap();
    let member_graph = serde_json::to_value(resolved_member.dependency_graph()).unwrap();
    let source_alias = graph_node_for_binding(&source_graph, "alias");
    let member_alias = graph_node_for_binding(&member_graph, "alias");
    let source_use = &source_alias["synchronous_execution"]["native_operation_input_uses"][0];
    let member_use = &member_alias["synchronous_execution"]["native_operation_input_uses"][0];
    for field in ["reference", "audited_source_sha256"] {
        assert_eq!(
            source_use["evidence"][field], member_use["evidence"][field],
            "selector form changed the selected site in {field}"
        );
    }
    let spec_claim_location = |use_site: &Value| -> String {
        use_site["evidence"]["spec_location"]
            .as_str()
            .unwrap()
            .split_once(":chunk_analysis_options.")
            .unwrap()
            .1
            .to_owned()
    };
    assert_eq!(
        spec_claim_location(source_use),
        spec_claim_location(member_use),
        "selector form changed the authored claim location"
    );
    assert_eq!(
        source_use["evidence"]["operation"],
        member_use["evidence"]["operation"]
    );
    assert_eq!(
        source_alias["inference_uses"],
        member_alias["inference_uses"]
    );
    assert_eq!(source_graph["edges"], member_graph["edges"]);
}

#[test]
fn resolved_member_selects_only_the_selected_symbol_site() {
    const SOURCE: &str = "var first = Symbol(7);\nvar second = Symbol(8);\nconsole.log(first.description, second.description);\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module("chosen", &[Member::new("first")]),
                logical_module("sibling", &[Member::new("second")]),
            ],
        )
        .with_native_operation_inputs(
            "static/app",
            vec![resolved_member_input(SOURCE, "chosen", "first")],
        ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let selected = graph_node_for_binding(&graph, "first");
    let sibling = graph_node_for_binding(&graph, "second");
    let evidence = &selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"];
    assert_eq!(evidence["reference"]["defining_chunk"], "static/app");
    assert_eq!(evidence["reference"]["input_index"], 0);
    assert!(
        evidence["spec_location"]
            .as_str()
            .unwrap()
            .contains("native_operation_inputs[0]")
    );
    assert_eq!(
        evidence["audited_source_sha256"],
        resolved_member_input(SOURCE, "chosen", "first").audited_source_sha256
    );
    assert_eq!(evidence["selector"]["resolved_member"]["module"], "chosen");
    assert_eq!(evidence["selector"]["resolved_member"]["name"], "first");
    assert!(
        sibling["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
    assert_preserved_entry_output(&fixture, "7 8\n");
}

#[test]
fn native_own_data_read_composes_exact_root_effect_coverage() {
    const SOURCE: &str = "const marker = 1;\nvar alias = Object.defineProperty;\nconst after = alias;\nconsole.log(marker, typeof after);\n";
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        SOURCE,
        vec![
            logical_module("alias_module", &binding_members(["alias", "after"])),
            logical_module_with_anon(
                "observe",
                &[Member::new("marker")],
                &["console.log(marker, typeof after);"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 function\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let execution = &graph_node_for_binding(&graph, "alias")["synchronous_execution"];
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
    let report = fixture.effect_translation_validation();
    assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
}

#[test]
fn project_analysis_rejects_missing_and_foreign_resolved_members() {
    const SOURCE: &str = "var alias = Symbol('alias');\nexport { alias };\n";
    let modules = vec![logical_module("alpha", &[Member::new("alias")])];
    let valid = project_analysis(
        FixtureOpts::new(SOURCE, modules.clone()).with_native_operation_inputs(
            "static/app",
            vec![resolved_member_input(SOURCE, "alpha", "alias")],
        ),
    );
    assert!(
        valid.status.success(),
        "valid resolved member failed project-analysis\nstdout:\n{}\nstderr:\n{}",
        valid.stdout,
        valid.stderr,
    );

    for (module, name) in [("missing", "alias"), ("alpha", "missing")] {
        assert_project_analysis_rejected(
            FixtureOpts::new(SOURCE, modules.clone()).with_native_operation_inputs(
                "static/app",
                vec![resolved_member_input(SOURCE, module, name)],
            ),
            &[
                "chunk_analysis_options.static/app.native_operation_inputs[0]",
                &format!("module {module}"),
                &format!("member {name}"),
                "resolved_member names no logical module member",
            ],
        );
    }

    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = Symbol('alias');\nexport { alias };\n";
    assert_project_analysis_rejected(
        FixtureOpts::new(CONSUMER, vec![])
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("peer", &[Member::new("alias")])],
            )])
            .with_native_operation_inputs(
                "static/app",
                vec![resolved_member_input(CONSUMER, "peer", "alias")],
            ),
        &[
            "chunk_analysis_options.static/app.native_operation_inputs[0]",
            "module peer",
            "member alias",
            "static/peer",
            "resolved_member belongs to another chunk",
        ],
    );
}

#[test]
fn resolved_member_rejects_stale_hash_and_wrong_reference_or_shape() {
    const SOURCE: &str = "var alias = Symbol(1);\nvar other = Math.random();\n";
    let modules = vec![logical_module(
        "alpha",
        &binding_members(["alias", "other"]),
    )];
    let mut stale = resolved_member_input(SOURCE, "alpha", "alias");
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        FixtureOpts::new(SOURCE, modules.clone())
            .with_native_operation_inputs("static/app", vec![stale]),
        &["stale audited_source_sha256"],
    );
    expect_rejection_containing_all(
        FixtureOpts::new(SOURCE, modules.clone()).with_native_operation_inputs(
            "static/app",
            vec![resolved_member_input(SOURCE, "alpha", "missing")],
        ),
        &["resolved_member names no logical module member"],
    );
    expect_rejection_containing_all(
        FixtureOpts::new(SOURCE, modules).with_native_operation_inputs(
            "static/app",
            vec![resolved_member_input(SOURCE, "alpha", "other")],
        ),
        &["must select a direct uncomputed Symbol identifier call"],
    );
}

#[test]
fn resolved_member_cannot_reference_another_provider_chunk() {
    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = Symbol(1);\nexport { alias };\n";
    expect_rejection_containing_all(
        FixtureOpts::new(CONSUMER, vec![])
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("peer", &[Member::new("alias")])],
            )])
            .with_native_operation_inputs(
                "static/app",
                vec![resolved_member_input(CONSUMER, "peer", "alias")],
            ),
        &["resolved_member belongs to another chunk"],
    );
}

#[test]
fn native_input_requires_exactly_one_selector_variant() {
    let common = "operation: symbol\naudited_source_sha256: ";
    let hash = "0".repeat(64);
    let none = format!("{common}{hash}\n");
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&none)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let both = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Symbol(1);'\n  bindings: [alias]\nresolved_member:\n  module: alpha\n  name: alias\n"
    );
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&both)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let source_match = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Symbol(1);'\n  bindings: [alias]\n"
    );
    assert!(matches!(
        serde_yaml::from_str::<NativeOperationInputContract>(&source_match)
            .unwrap()
            .selector,
        SourceUnitSelector::SourceMatch(_)
    ));
}

#[test]
fn intact_object_read_has_an_independent_source_certificate() {
    const SOURCE: &str = "const sentinel = [1];\nconst before = Math.random();\nvar alias = Object.defineProperty;\nconst after = alias;\nconsole.log(typeof after);\n";
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        SOURCE,
        vec![
            logical_module("alias", &binding_members(["sentinel", "alias"])),
            logical_module("consumer", &binding_members(["before", "after"])),
        ],
    ));
    assert_preserved_entry_output(&fixture, "function\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alias.js",
        &["var alias = Object.defineProperty"],
        &[],
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let alias = graph_node_for_binding(&graph, "alias");
    assert_eq!(
        alias["synchronous_execution"]["inference_uses"][0]["conclusion"],
        "native_own_data_read"
    );
}

#[test]
fn invoking_an_automatically_recognized_alias_keeps_call_heap_effects() {
    let source = "const sentinel = [1];\nvar alias = Object.defineProperty;\nconst after = alias(sentinel, 'x', { value: 2 });\nconsole.log(sentinel.x);\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_preserved_entry_output(&fixture, "2\n");
    assert_ne!(
        graph_node_for_binding(&graph, "after")["purity"]["kind"],
        "pure"
    );
}
