//! Imported wrappers keep callback-body reads deferred until invocation.

mod direct_export;
mod factory_results;
mod namespace_capability_privacy;
mod namespace_contract;
mod namespace_factory;
mod primitive_operators;
mod source_record_factory;

use debundle_e2e_support::*;

const SOURCE: &str = r#"import { factory } from './peer.js';
const wrapped = factory(() => later);
const later = 42;
console.log(wrapped());
export { wrapped, later };
"#;

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn peer_api_callback_factories(members: &[&str]) -> ChunkExportPurityBuilder {
    ChunkExportPurityBuilder::new("static/peer").with_callback_factory_members("api", members)
}

const NAMESPACE_FACTORY: &str = "export const api = {\n\
    make(callback) { return { invoke: () => callback() }; },\n\
    wrap(inner) { return { invoke: () => inner.invoke() }; },\n\
};\n";

fn expect_namespace_factory_rejection(opts: FixtureOpts<'_>, causes: &[&str]) {
    expect_atomic_conflict_rejection(opts, &["callbacks/wrapped", "values/later"], causes);
}

fn expect_namespace_factory_contract_rejection<'a>(
    opts: FixtureOpts<'a>,
    peer_source: &'a str,
    factory_members: &[&str],
    causes: &[&str],
) {
    let peer = [("static/peer", peer_source)];
    let contracts = [peer_api_callback_factories(factory_members).build()];
    expect_namespace_factory_rejection(
        opts.with_extra_chunks(&peer)
            .with_chunk_export_purity(&contracts),
        causes,
    );
}
