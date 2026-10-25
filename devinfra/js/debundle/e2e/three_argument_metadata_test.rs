//! Three-argument legacy decorators preserve descriptors and deferred getters.

use debundle_e2e_support::*;

const SOURCE: &str = r#"import { annotate, inheritAnnotation, annotationsOf } from './peer.js';
function decorate(decorators, target, key) {
  let descriptor = Object.getOwnPropertyDescriptor(target, key);
  for (const decorator of decorators) descriptor = decorator(target, key, descriptor) || descriptor;
  if (descriptor) Object.defineProperty(target, key, descriptor);
}
class Base { get read() { return 1; } }
decorate([annotate], Base.prototype, "read");
class Child extends Base {
  get read() { return later; }
  get own() { return 2; }
}
decorate([inheritAnnotation], Child.prototype, "read");
decorate([annotate], Child.prototype, "own");
const later = 42;
console.log(new Child().read, annotationsOf(Child.prototype), new Base().read, annotationsOf(Base.prototype));
export { Base, Child, later };
"#;

const PROVIDER: &str = r#"const metadata = Symbol();
function ownMetadata(target) {
  if (!Object.prototype.hasOwnProperty.call(target, metadata)) {
    Object.defineProperty(target, metadata, { value: { ...target[metadata] } });
  }
  return target[metadata];
}
export function annotate(target, key) { ownMetadata(target)[key] = true; }
export function inheritAnnotation(target, key) { ownMetadata(target); }
export function annotationsOf(target) { return Object.keys(target[metadata]).join(','); }
"#;

fn modules(effect: MemberEffect) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "controllers/base",
            &[Member::new("Base")],
            &["decorate([annotate], Base.prototype, \"read\");"],
        ),
        logical_module_with_anon(
            "controllers/child",
            &[Member::new("Child")],
            &[
                "decorate([inheritAnnotation], Child.prototype, \"read\");",
                "decorate([annotate], Child.prototype, \"own\");",
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
fn three_argument_metadata_keeps_getters_deferred_and_inheritance_intact() {
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            modules(MemberEffect::TypescriptMetadataDecorateHelperThreeArgs),
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)]),
    );
    assert_preserved_entry_output(&fixture, "42 read,own 1 read\n");
}

#[test]
fn earlier_effects_do_not_assert_three_argument_metadata_timing() {
    for effect in [
        MemberEffect::Default,
        MemberEffect::TypescriptDecorateHelper,
        MemberEffect::TypescriptMetadataDecorateHelper,
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(SOURCE, modules(effect))
                .with_extra_chunks(&[("static/peer", PROVIDER)]),
            &["controllers/child", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn three_argument_metadata_mutations_stay_with_the_target() {
    let mut claimed = modules(MemberEffect::TypescriptMetadataDecorateHelperThreeArgs);
    claimed[1] = logical_module("controllers/child", &[Member::new("Child")]);
    claimed.push(logical_module_with_anon(
        "metadata/install",
        &[],
        &[
            "decorate([inheritAnnotation], Child.prototype, \"read\");",
            "decorate([annotate], Child.prototype, \"own\");",
        ],
    ));
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, claimed).with_extra_chunks(&[("static/peer", PROVIDER)]),
        &["controllers/child", "metadata/install"],
        &["local_effect"],
    );
}

#[test]
fn unsupported_three_argument_metadata_shapes_remain_opaque() {
    let original = "decorate([inheritAnnotation], Child.prototype, \"read\");";
    for unsupported in [
        "decorate([inheritAnnotation], Child.prototype, \"read\", 1);",
        "decorate([... [inheritAnnotation]], Child.prototype, \"read\");",
        "decorate([({ inheritAnnotation }).inheritAnnotation], Child.prototype, \"read\");",
        "decorate([inheritAnnotation], Child.prototype, [\"read\"][0]);",
    ] {
        let source = SOURCE.replace(original, unsupported);
        let mut claimed = modules(MemberEffect::TypescriptMetadataDecorateHelperThreeArgs);
        claimed[1] = logical_module_with_anon(
            "controllers/child",
            &[Member::new("Child")],
            &[
                unsupported,
                "decorate([annotate], Child.prototype, \"own\");",
            ],
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, claimed).with_extra_chunks(&[("static/peer", PROVIDER)]),
            &["controllers/child", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}
