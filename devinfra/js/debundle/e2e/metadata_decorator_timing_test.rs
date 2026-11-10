//! Metadata decoration preserves mutation without running decorated methods.

use debundle_e2e_support::*;

const SOURCE: &str = r#"import { annotate, annotationsOf } from './peer.js';
function decorate(decorators, target, key, kind) {
  const descriptor = kind === 1 ? Object.getOwnPropertyDescriptor(target, key) : undefined;
  for (const decorator of decorators) decorator(target, key, descriptor);
  if (descriptor) Object.defineProperty(target, key, descriptor);
}
class Controller {
  read() { return later; }
}
decorate([annotate], Controller.prototype, "read", 1);
decorate([annotate], Controller.prototype, "record", 2);
const later = 42;
console.log(new Controller().read(), annotationsOf(Controller.prototype));
export { Controller, later };
"#;

const PROVIDER: &str = r#"const metadata = Symbol();
export function annotate(target, key, descriptor) {
  if (!Object.prototype.hasOwnProperty.call(target, metadata)) {
    Object.defineProperty(target, metadata, { value: [] });
  }
  target[metadata].push(key);
}
export function annotationsOf(target) { return target[metadata].join(','); }
"#;

fn modules(effect: MemberEffect) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "controllers/controller",
            &[Member::new("Controller")],
            &[
                "decorate([annotate], Controller.prototype, \"read\", 1);",
                "decorate([annotate], Controller.prototype, \"record\", 2);",
            ],
        ),
        logical_module(
            "runtime/decorate",
            &[Member::new("decorate").with_effect(effect)],
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

#[test]
fn metadata_only_applications_keep_methods_deferred_and_metadata_visible() {
    let peers = [("static/peer", PROVIDER)];
    let arrow = SOURCE.replace(
        "function decorate(decorators, target, key, kind) {",
        "const decorate = (decorators, target, key, kind) => {",
    );
    for source in [SOURCE, &arrow] {
        let fixture = run_fixture(
            FixtureOpts::new(
                source,
                modules(MemberEffect::TypescriptMetadataDecorateHelper),
            )
            .with_extra_chunks(&peers),
        );
        assert_preserved_entry_output(&fixture, "42 read,record\n");
    }
}

#[test]
fn unpublished_metadata_target_stays_deferred_after_opaque_call() {
    let source = source_with_opaque_write();
    let mut claimed = modules(MemberEffect::TypescriptMetadataDecorateHelper);
    claimed.push(logical_module(
        "values/dynamic",
        &[Member::new("dynamic"), Member::new("prefix")],
    ));
    let fixture = run_fixture(
        FixtureOpts::new(&source, claimed).with_extra_chunks(&[("static/peer", PROVIDER)]),
    );
    assert_preserved_entry_output(&fixture, "42 read,record\n");
    // These are internal modules of the designated entry, not alternative ESM
    // roots. Probe their export boundaries after that entry initializes them.
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const controller = await import('./static/app/modules/controllers/controller.js');
const dynamic = await import('./static/app/modules/values/dynamic.js');
const later = await import('./static/app/modules/values/later.js');
console.log('Controller' in controller, 'later' in controller,
  'dynamic' in dynamic, 'later' in dynamic, 'later' in later, 'dynamic' in later);
"#,
        "true false true false true false\n",
    );
}

#[test]
fn published_metadata_target_keeps_early_method_read() {
    let source = source_with_opaque_write().replace(
        "const prefix = new globalThis.Date();",
        "globalThis.saved = Controller;\nconst prefix = new globalThis.Date();",
    );
    let mut claimed = modules(MemberEffect::TypescriptMetadataDecorateHelper);
    claimed.push(logical_module(
        "values/dynamic",
        &[Member::new("dynamic"), Member::new("prefix")],
    ));
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, claimed).with_extra_chunks(&[("static/peer", PROVIDER)]),
        &["values/dynamic", "values/later"],
        &[],
    );
}

fn source_with_opaque_write() -> String {
    SOURCE
        .replace(
            "const later = 42;",
            r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 42;
})({});
const later = dynamic;"#,
        )
        .replace("export { Controller, later };\n", "")
}

#[test]
fn target_local_mutation_alone_does_not_assert_callback_timing() {
    let peers = [("static/peer", PROVIDER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules(MemberEffect::TypescriptDecorateHelper))
            .with_extra_chunks(&peers),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn invoking_decorator_keeps_the_early_method_read() {
    let peers = [(
        "static/peer",
        "export function annotate(target, key, descriptor) { descriptor.value.call(target); }\n\
         export function annotationsOf() { return ''; }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules(MemberEffect::TypescriptDecorateHelper))
            .with_extra_chunks(&peers),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn metadata_mutation_must_stay_with_the_target_class() {
    let peers = [("static/peer", PROVIDER)];
    let mut split = modules(MemberEffect::TypescriptMetadataDecorateHelper);
    split[0] = logical_module("controllers/controller", &[Member::new("Controller")]);
    split.push(logical_module_with_anon(
        "metadata/install",
        &[],
        &[
            "decorate([annotate], Controller.prototype, \"read\", 1);",
            "decorate([annotate], Controller.prototype, \"record\", 2);",
        ],
    ));
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, split).with_extra_chunks(&peers),
        &["controllers/controller", "metadata/install"],
        &["local_effect"],
    );
}

#[test]
fn unsupported_metadata_application_shapes_remain_conservative() {
    let original = "decorate([annotate], Controller.prototype, \"read\", 1);";
    for unsupported in [
        "decorate([... [annotate]], Controller.prototype, \"read\", 1);",
        "decorate([({ annotate }).annotate], Controller.prototype, \"read\", 1);",
        "decorate([annotate], Controller.prototype, \"read\", 1 + 0);",
        "decorate([annotate], Controller.prototype, \"__proto__\", 1);",
    ] {
        let source = SOURCE.replace(original, unsupported);
        let mut claims = modules(MemberEffect::TypescriptMetadataDecorateHelper);
        claims[0] = logical_module_with_anon(
            "controllers/controller",
            &[Member::new("Controller")],
            &[
                unsupported,
                "decorate([annotate], Controller.prototype, \"record\", 2);",
            ],
        );
        let peers = [("static/peer", PROVIDER)];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, claims).with_extra_chunks(&peers),
            &["controllers/controller", "values/later"],
            &[],
        );
    }
}

#[test]
fn local_decorator_binding_reads_are_preserved() {
    let source = SOURCE.replace(
        "import { annotate, annotationsOf } from './peer.js';",
        "import { annotationsOf } from './peer.js';\n\
         const metadata = Symbol();\n\
         function value() { return later; }\n\
         function annotate(target) {\n\
           Object.defineProperty(target, metadata, { value: value() });\n\
         }",
    );
    let peers = [("static/peer", PROVIDER)];
    for effect in [
        MemberEffect::TypescriptMetadataDecorateHelper,
        MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, modules(effect)).with_extra_chunks(&peers),
            &["controllers/controller", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn a_reassigned_helper_does_not_keep_metadata_precision() {
    let source = SOURCE.replace(
        "class Controller {",
        "decorate = (decorators, target) => target.read();\nclass Controller {",
    );
    let peers = [("static/peer", PROVIDER)];
    for effect in [
        MemberEffect::TypescriptMetadataDecorateHelper,
        MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, modules(effect)).with_extra_chunks(&peers),
            &["controllers/controller", "values/later"],
            &[],
        );
    }
}

#[test]
fn imported_static_member_decorator_keeps_method_deferred() {
    let source = member_source();
    let provider = member_provider();
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            member_modules(
                MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
                "api.annotate",
            ),
        )
        .with_extra_chunks(&[("static/peer", provider.as_str())]),
    );
    assert_preserved_entry_output(&fixture, "42 read,record\n");
}

fn member_source() -> String {
    SOURCE
        .replace(
            "import { annotate, annotationsOf } from './peer.js';",
            "import { api, annotationsOf } from './peer.js';",
        )
        .replace("decorate([annotate]", "decorate([api.annotate]")
}

fn member_provider() -> String {
    PROVIDER.replace(
        "export function annotate(target, key, descriptor) {",
        "function annotate(target, key, descriptor) {",
    ) + "\nexport { annotate };\nexport const api = Object.assign(function api() {}, { annotate, inner: { annotate } });\n"
}

fn member_modules(effect: MemberEffect, decorator: &str) -> Vec<LogicalModuleEntry> {
    let mut claimed = modules(effect);
    let read = format!("decorate([{decorator}], Controller.prototype, \"read\", 1);");
    let record = format!("decorate([{decorator}], Controller.prototype, \"record\", 2);");
    claimed[0] = logical_module_with_anon(
        "controllers/controller",
        &[Member::new("Controller")],
        &[&read, &record],
    );
    claimed
}

#[test]
fn original_metadata_contract_keeps_member_lookup_opaque() {
    let source = member_source();
    let provider = member_provider();
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            member_modules(
                MemberEffect::TypescriptMetadataDecorateHelper,
                "api.annotate",
            ),
        )
        .with_extra_chunks(&[("static/peer", provider.as_str())]),
        &["controllers/controller", "values/later"],
        &[],
    );
}

#[test]
fn member_contract_rejects_computed_chained_and_spread_decorators() {
    let provider = member_provider();
    for decorator in [
        "api['annotate']",
        "api.inner.annotate",
        "... [api.annotate]",
    ] {
        let source = member_source().replace("[api.annotate]", &format!("[{decorator}]"));
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                member_modules(
                    MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
                    decorator,
                ),
            )
            .with_extra_chunks(&[("static/peer", provider.as_str())]),
            &["controllers/controller", "values/later"],
            &[],
        );
    }
}

#[test]
fn member_contract_rejects_local_getter_and_proxy_providers() {
    let provider = member_provider();
    for local in [
        "const api = { get annotate() { return annotate; } };",
        "const api = new Proxy({ annotate }, { get(target, key) { return target[key]; } });",
    ] {
        let source = member_source().replace(
            "import { api, annotationsOf } from './peer.js';",
            &format!("import {{ annotate, annotationsOf }} from './peer.js';\n{local}"),
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                member_modules(
                    MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
                    "api.annotate",
                ),
            )
            .with_extra_chunks(&[("static/peer", provider.as_str())]),
            &["controllers/controller", "values/later"],
            &[],
        );
    }
}

#[test]
fn member_contract_rejects_a_direct_provider_member_write() {
    let source = member_source()
        .replace(
            "import { api, annotationsOf } from './peer.js';",
            "import { api, annotate, annotationsOf } from './peer.js';",
        )
        .replace(
            "class Controller {",
            "let later = 0;\napi.annotate = (target, key, descriptor) => { if (descriptor?.value) globalThis.observed = descriptor.value.call(target); annotate(target, key, descriptor); };\nclass Controller {",
        )
        .replace("const later = 42;", "later = 42;")
        .replace(
            "console.log(new Controller().read(), annotationsOf(Controller.prototype));",
            "console.log(globalThis.observed, new Controller().read(), annotationsOf(Controller.prototype));",
        );
    let provider = member_provider();
    let mut claimed = member_modules(
        MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators,
        "api.annotate",
    );
    claimed[0] = logical_module_with_anon(
        "controllers/controller",
        &[Member::new("Controller")],
        &[
            "api.annotate = (target, key, descriptor) => { if (descriptor?.value) globalThis.observed = descriptor.value.call(target); annotate(target, key, descriptor); };",
            "decorate([api.annotate], Controller.prototype, \"read\", 1);",
            "decorate([api.annotate], Controller.prototype, \"record\", 2);",
        ],
    );
    claimed[2] =
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]);
    let colocated = vec![
        logical_module_with_anon(
            "controllers/controller",
            &[Member::new("Controller"), Member::new("later")],
            &[
                "api.annotate = (target, key, descriptor) => { if (descriptor?.value) globalThis.observed = descriptor.value.call(target); annotate(target, key, descriptor); };",
                "decorate([api.annotate], Controller.prototype, \"read\", 1);",
                "decorate([api.annotate], Controller.prototype, \"record\", 2);",
                "later = 42;",
            ],
        ),
        logical_module(
            "runtime/decorate",
            &[Member::new("decorate")
                .with_effect(MemberEffect::TypescriptMetadataDecorateHelperWithMemberDecorators)],
        ),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(&source, colocated)
            .with_extra_chunks(&[("static/peer", provider.as_str())])
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0 42 read,record\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, claimed).with_extra_chunks(&[("static/peer", provider.as_str())]),
        &["controllers/controller", "values/later"],
        &[],
    );
}
