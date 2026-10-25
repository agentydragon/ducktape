//! Opaque calls may invoke methods reached through a class prototype.

use debundle_e2e_support::*;

#[test]
fn decorator_can_call_a_method_before_its_read_is_initialized() {
    let source = r#"import { invokeMethod } from './peer.js';
function decorate(decorators, target, key, kind) {
  const descriptor = kind === 1 ? Object.getOwnPropertyDescriptor(target, key) : undefined;
  for (const decorator of decorators) decorator(target, key, descriptor);
  if (descriptor) Object.defineProperty(target, key, descriptor);
}

class Controller {
  read() { return later; }
}
decorate([invokeMethod], Controller.prototype, "read", 1);
const later = 42;
export { Controller, later };
"#;
    let peer = [(
        "static/peer",
        "export function invokeMethod(target, key, descriptor) { descriptor.value.call(target); }",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "controllers/controller",
                    &[Member::new("Controller")],
                    &["decorate([invokeMethod], Controller.prototype, \"read\", 1);"],
                ),
                logical_module(
                    "runtime/decorate",
                    &[Member::new("decorate").with_effect(MemberEffect::TypescriptDecorateHelper)],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peer),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn opaque_call_can_invoke_a_method_through_a_prototype_alias() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "class Controller { read() { return later; } }\n\
             const prototype = Controller.prototype;\n\
             const outcome = host(prototype);\n\
             const later = 42;\n\
             export { Controller, prototype, outcome, later };",
            vec![
                logical_module(
                    "controllers/controller",
                    &[
                        Member::new("Controller"),
                        Member::new("prototype"),
                        Member::new("outcome"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn escaped_prototype_methods_remain_reachable_at_a_later_opaque_call() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "class Controller { read() { return later; } }\n\
             globalThis.saved = Controller.prototype;\n\
             const result = host();\n\
             const later = 42;\n\
             export { Controller, result, later };",
            vec![
                logical_module("controllers/controller", &[Member::new("Controller")]),
                logical_module("runtime/call", &[Member::new("result")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["runtime/call", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn opaque_call_can_invoke_a_static_method_from_the_class_value() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "class Controller { static read() { return later; } }\n\
             const outcome = host(Controller);\n\
             const later = 42;\n\
             export { Controller, outcome, later };",
            vec![
                logical_module(
                    "controllers/controller",
                    &[Member::new("Controller"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn opaque_call_can_read_a_getter_from_the_prototype() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "class Controller { get value() { return later; } }\n\
             const outcome = host(Controller.prototype);\n\
             const later = 42;\n\
             export { Controller, outcome, later };",
            vec![
                logical_module(
                    "controllers/controller",
                    &[Member::new("Controller"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        ),
        &["controllers/controller", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn class_creation_keeps_an_unused_method_deferred() {
    let fixture = run_fixture(FixtureOpts::new(
        "class Controller { read() { return later; } }\n\
         const later = 42;\n\
         console.log(new Controller().read());\n\
         export { Controller, later };",
        vec![
            logical_module("controllers/controller", &[Member::new("Controller")]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn class_method_rebind_reaches_later_imported_callback_invocation() {
    let source = r#"import { install, invoke } from './peer.js';
var receiver = () => 0;
class Controller { flip() { receiver = invoke; } }
install(Controller.prototype);
const result = receiver(() => later);
const later = 42;
export { receiver, Controller, result, later };
"#;
    let peer = [(
        "static/peer",
        "export function install(target) { target.flip(); }\n\
         export function invoke(callback) { return callback(); }",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "controllers/controller",
                    &[Member::new("Controller"), Member::new("receiver")],
                    &["install(Controller.prototype);"],
                ),
                logical_module("runtime/call", &[Member::new("result")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peer),
        &["runtime/call", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn symbol_key_does_not_defer_an_actual_early_method_call() {
    let source = r#"let later = 0;
const brand = Symbol('brand');
class Controller { [brand]() { return later; } }
const observed = new Controller()[brand]();
later = 42;
console.log(observed, later);
export { Controller, observed, later };
"#;
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "controllers/controller",
            &[
                Member::new("brand"),
                Member::new("Controller"),
                Member::new("observed"),
                Member::new("later"),
            ],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "controllers/controller",
                    &[
                        Member::new("brand"),
                        Member::new("Controller"),
                        Member::new("observed"),
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["controllers/controller", "values/later"],
        &[],
    );
}
