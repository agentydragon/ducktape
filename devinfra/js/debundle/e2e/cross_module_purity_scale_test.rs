//! Namespace-only providers cannot supply the bare-call verdicts inferred by
//! the purity oracle. Their potentially expensive value flow is irrelevant.

use debundle_e2e_support::*;
use std::fmt::Write as _;

#[test]
fn namespace_only_provider_skips_unused_purity_inference() {
    let mut provider = String::from("globalThis.key = 'value';\n");
    for index in 0..4096 {
        writeln!(provider, "const object{index} = {{value: () => {index}}};").unwrap();
    }
    provider.push_str("function choose() {");
    for index in 0..4096 {
        write!(provider, "return object{index};").unwrap();
    }
    provider.push_str("}\nfunction assign(target, value) {");
    for index in 0..2048 {
        write!(provider, "target.property{index} = value;").unwrap();
    }
    provider.push_str("}\nconst pool = choose();\n");
    for _ in 0..512 {
        provider.push_str("assign(pool, pool[globalThis.key]);\n");
    }
    provider.push_str("export const result = 7;\n");
    let peers = [
        ("static/peer", provider.as_str()),
        ("static/peer1", provider.as_str()),
        ("static/peer2", provider.as_str()),
        ("static/peer3", provider.as_str()),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import * as a from './peer.js';\n\
             import * as b from './peer1.js';\n\
             import * as c from './peer2.js';\n\
             import * as d from './peer3.js';\n\
             console.log(a.result + b.result + c.result + d.result);\n",
            vec![],
        )
        .with_extra_chunks(&peers)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "28\n");
}
