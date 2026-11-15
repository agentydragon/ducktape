use debundle_e2e_support::*;

const SOURCE: &str = r#"import { host } from './peer.js';
const value = 7;
function read() { return value; }
function outer() { return read(); }
function driver() { outer(); outer(); return outer(); }
host(driver, outer, read);
console.log(host(driver, outer, read));
"#;
const PEER: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(...callbacks) { return callbacks[0](); }\n",
)];

#[test]
fn nested_tainted_reader_preserves_colocated_and_split_output() {
    for modules in [
        Vec::new(),
        vec![logical_module(
            "reader",
            &[Member::new("value"), Member::new("read")],
        )],
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(SOURCE, modules)
                .with_extra_chunks(&PEER)
                .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "7\n");
        let nested_hits: Vec<_> = fixture
            .stderr
            .lines()
            .filter(|line| {
                line.split_whitespace().any(|field| {
                    field
                        .strip_prefix("nested_would_hit=")
                        .and_then(|n| n.parse::<usize>().ok())
                        .is_some_and(|n| n > 0)
                })
            })
            .collect();
        assert!(
            !nested_hits.is_empty(),
            "no genuine nested would-hit: {}",
            fixture.stderr
        );
        eprintln!("{}", nested_hits.join("\n"));
    }
}

#[test]
fn branch_cells_and_factory_activations_remain_distinct() {
    let source = r#"import { choose } from './peer.js';
function factory() {
  let value = 0;
  function read() { return value; }
  function write(next) { value = next; }
  return {read, write};
}
function driver() {
  const first = factory();
  const second = factory();
  let selected = () => first.read();
  if (choose()) { first.write(3); selected = () => second.read(); }
  else { second.write(5); }
  console.log(first.read(), second.read(), selected());
}
driver();
"#;
    for (choice, expected) in [("true", "3 0 0\n"), ("false", "0 5 0\n")] {
        let peer = format!("export function choose() {{ return {choice}; }}\n");
        let chunks = [("static/peer", peer.as_str())];
        let fixture = run_fixture(
            FixtureOpts::new(source, Vec::new())
                .with_extra_chunks(&chunks)
                .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, expected);
    }
}
