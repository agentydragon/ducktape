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
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            modules(MemberEffect::TypescriptMetadataDecorateHelper),
        )
        .with_extra_chunks(&peers),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn a_reassigned_helper_does_not_keep_metadata_precision() {
    let source = SOURCE.replace(
        "class Controller {",
        "decorate = (decorators, target) => target.read();\nclass Controller {",
    );
    let peers = [("static/peer", PROVIDER)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            modules(MemberEffect::TypescriptMetadataDecorateHelper),
        )
        .with_extra_chunks(&peers),
        &["controllers/controller", "values/later"],
        &[],
    );
}
