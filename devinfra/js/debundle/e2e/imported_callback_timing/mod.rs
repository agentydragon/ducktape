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

const API_MAKE_PEER: &str =
    "export const api = { make(callback) { return { invoke: () => callback() }; } };\n";

/// One named `callbacks/wrapped` / `values/later` split of `source`.
#[derive(Default)]
struct SplitCase<'a> {
    name: &'a str,
    source: &'a str,
    /// `callbacks/wrapped` bindings and anonymous statements.
    wrapped: &'a [&'static str],
    wrapped_statements: &'a [&'a str],
    /// `values/later` also owns the anonymous `later = 42;`.
    later_write: bool,
    /// Leave `later` to the residual instead of authoring `values/later`.
    later_unassigned: bool,
    import_order: Option<&'a [&'a str]>,
    peers: &'a [(&'a str, &'a str)],
    contracts: &'a [(&'a str, ChunkExportPurity)],
}

impl<'a> SplitCase<'a> {
    fn members(names: &[&'static str]) -> Vec<Member> {
        names.iter().map(|name| Member::new(name)).collect()
    }

    fn with_inputs(&self, opts: FixtureOpts<'a>) -> FixtureOpts<'a> {
        opts.with_extra_chunks(self.peers)
            .with_chunk_export_purity(self.contracts)
    }

    fn split_opts(&self) -> FixtureOpts<'a> {
        let mut modules = vec![logical_module_with_anon(
            "callbacks/wrapped",
            &Self::members(self.wrapped),
            self.wrapped_statements,
        )];
        if !self.later_unassigned {
            modules.push(logical_module_with_anon(
                "values/later",
                &[Member::new("later")],
                if self.later_write {
                    &["later = 42;"]
                } else {
                    &[]
                },
            ));
        }
        let opts = self.with_inputs(FixtureOpts::new(self.source, modules));
        match self.import_order {
            Some(order) => opts.with_import_order(order),
            None => opts,
        }
    }

    /// The split is accepted and the emitted entry prints `output`.
    fn assert_split_runs(&self, output: &str) {
        eprintln!("case: {}", self.name);
        assert_preserved_entry_output(&run_fixture(self.split_opts()), output);
    }

    /// The split is rejected as one atomic conflict with `causes`.
    fn assert_split_rejected(&self, causes: &[&str]) {
        eprintln!("case: {}", self.name);
        let modules: &[&str] = if self.later_unassigned {
            &["callbacks/wrapped"]
        } else {
            &["callbacks/wrapped", "values/later"]
        };
        expect_atomic_conflict_rejection(self.split_opts(), modules, causes);
    }

    /// Everything in one `all` module (wrapped bindings, then `later`) prints `output`.
    fn assert_colocated_runs(&self, output: &str) {
        eprintln!("case: {}", self.name);
        let mut members = Self::members(self.wrapped);
        members.push(Member::new("later"));
        let mut statements = self.wrapped_statements.to_vec();
        if self.later_write {
            statements.push("later = 42;");
        }
        let all = vec![logical_module_with_anon("all", &members, &statements)];
        assert_preserved_entry_output(
            &run_fixture(self.with_inputs(FixtureOpts::new(self.source, all))),
            output,
        );
    }

    /// Everything left in the residual prints `output`.
    fn assert_residual_runs(&self, output: &str) {
        eprintln!("case: {}", self.name);
        let opts = self
            .with_inputs(FixtureOpts::new(self.source, vec![]))
            .with_import_order(&["residual"]);
        assert_preserved_entry_output(&run_fixture(opts), output);
    }
}

fn expect_namespace_factory_rejection(opts: FixtureOpts<'_>, causes: &[&str]) {
    expect_atomic_conflict_rejection(opts, &["callbacks/wrapped", "values/later"], causes);
}
