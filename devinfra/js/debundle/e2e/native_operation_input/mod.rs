//! A narrowly selected native call can retain the existing call model while
//! unknown receiver writes elsewhere in the provider remain visible.

mod encoder_input;
mod set_frontier;
mod set_protocol;
mod symbol_input;

use debundle_e2e_support::*;

const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst marker = Symbol('marker');\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst made = factory();\nglobalThis.unrelatedKey = 'safe';\nconsole.log(marker.description, made.name, globalThis.unrelatedKey);\nexport { marker, factory, LaterError, reflect };\n";

fn selected_native_input(
    source: &str,
    declaration: &str,
    binding: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    native_operation_input(
        source,
        declared_source_unit(declaration.trim(), binding),
        operation,
        "Neutral fixture audits this exact native operation.",
    )
}

fn rejected_opts<'a>(
    source: &'a str,
    selected_binding: &'static str,
    input: NativeOperationInputContract,
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module("selected", &[Member::new(selected_binding)])],
    )
    .with_native_operation_inputs("static/app", vec![input])
}
