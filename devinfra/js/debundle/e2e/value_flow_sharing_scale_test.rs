//! Wide, repeated property writes exercise value-set sharing in the call oracle.

use debundle_e2e_support::*;
use std::fmt::Write as _;

fn provider() -> String {
    let mut source = String::from("globalThis.key = 'value';\n");
    for index in 0..4096 {
        writeln!(source, "const object{index} = {{ value: () => {index} }};").unwrap();
    }
    source.push_str("function choose() {\n");
    for index in 0..4096 {
        writeln!(source, "  return object{index};").unwrap();
    }
    source.push_str("}\nfunction assign(target, value) {\n");
    for index in 0..1408 {
        writeln!(source, "  target.property{index} = value;").unwrap();
    }
    source.push_str("}\nconst pool = choose();\n");
    for _ in 0..512 {
        source.push_str("assign(pool, pool[globalThis.key]);\n");
    }
    source.push_str("export const result = 7;\n");
    source
}

#[test]
fn repeated_wide_value_sets_preserve_cross_chunk_output() {
    let provider = provider();
    let peers = [
        ("static/peer", provider.as_str()),
        ("static/peer1", provider.as_str()),
        ("static/peer2", provider.as_str()),
        ("static/peer3", provider.as_str()),
    ];
    let source = "import { result as a } from './peer.js';\n\
                  import { result as b } from './peer1.js';\n\
                  import { result as c } from './peer2.js';\n\
                  import { result as d } from './peer3.js';\n\
                  console.log(a + b + c + d);\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "28\n");
}
