//! Shared application and peer for the namespace native-integrity fixtures.

use crate::{
    FixtureOpts, Member, binding_members, expect_atomic_conflict_rejection, logical_module,
};

pub const PEER: (&str, &str) = (
    "static/peer",
    r#"import { readLater } from './app.js';
export function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }
"#,
);

/// A peer constructs a Map before Later is initialized. Each case supplies the namespace
/// code to check whether that construction can synchronously reenter the application.
pub fn opts<'a>(chunks: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        r#"import { factory } from './peer.js';
import './namespace.js';
export function readLater() { return Later; }
const queue = factory();
class Later {}
console.log(queue.size, readLater().name);
export { queue, Later };
"#,
        vec![
            logical_module("queue", &binding_members(["queue", "readLater"])),
            logical_module("later", &[Member::new("Later")]),
        ],
    )
    .with_extra_chunks(chunks)
}

pub fn expect_split_conflict(chunks: &[(&str, &str)]) {
    expect_atomic_conflict_rejection(opts(chunks), &["queue", "later"], &[]);
}
