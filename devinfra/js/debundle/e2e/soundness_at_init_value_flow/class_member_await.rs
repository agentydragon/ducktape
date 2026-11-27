//! Opaque class members keep reads before their first `await` and skip reads after it.

use super::in_case;
use debundle_e2e_support::*;

const CLASS_MEMBER_PEER_CHUNKS: &[(&str, &str)] = &[(
    "static/peer",
    r#"export function inspect(Reader) {
  const instance = new Reader();
  void instance.read();
  return "ok";
}
"#,
)];

const CLASS_MEMBER_PRE_AWAIT_SOURCE: &str = r#"import { inspect } from "./peer.js";
let later = 0;
class Reader { async read() { globalThis.observed = later; await 0; return later; } }
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#;

fn opaque_class_member_opts(source: &'static str, assign_later: bool) -> FixtureOpts<'static> {
    let later = if assign_later {
        logical_module_with_anon("value/later", &[Member::new("later")], &["later = 7;"])
    } else {
        logical_module("value/later", &[Member::new("later")])
    };
    FixtureOpts::new(
        source,
        vec![
            logical_module("value/reader", &binding_members(["Reader", "result"])),
            later,
        ],
    )
    .with_chunk_id("static/provider")
    .with_extra_chunks(CLASS_MEMBER_PEER_CHUNKS)
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn opaque_class_member_skips_post_await_read() {
    let fixture = run_fixture(opaque_class_member_opts(
        r#"import { inspect } from "./peer.js";
class Reader { async read() { await 0; return later; } }
const result = inspect(Reader);
const later = 7;
console.log(result, later);
export { Reader, result, later };
"#,
        false,
    ));
    assert_preserved_entry_output(&fixture, "ok 7\n");
}

/// The peer may construct `Reader` and call `read`: reads that run before
/// its first `await` (in the method, a field or a getter) stay ordered.
#[test]
fn opaque_class_member_keeps_reads_before_await() {
    for (name, source) in [
        ("method body", CLASS_MEMBER_PRE_AWAIT_SOURCE),
        (
            "instance field",
            r#"import { inspect } from "./peer.js";
let later = 0;
class Reader {
  value = (globalThis.observed = later);
  async read() { await 0; return this.value; }
}
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#,
        ),
        (
            "getter read before the await",
            r#"import { inspect } from "./peer.js";
let later = 0;
class Reader {
  get value() { return (globalThis.observed = later); }
  async read() { const value = this.value; await 0; return value; }
}
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#,
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                opaque_class_member_opts(source, true),
                &["value/reader", "value/later"],
                &[],
            );
        });
    }
}

#[test]
fn opaque_class_member_pre_await_read_runs_when_colocated() {
    let fixture = run_fixture(
        FixtureOpts::new(
            CLASS_MEMBER_PRE_AWAIT_SOURCE,
            vec![logical_module_with_anon(
                "value/all",
                &binding_members(["Reader", "result", "later"]),
                &["later = 7;"],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(CLASS_MEMBER_PEER_CHUNKS)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ok 7 0\n");
}

// ─── bounded callback fallback ──────────────────────────────────────────
