//! Phase-1 shadow runner: resolve an owner-graph EDB with the in-process Datalog
//! solver and print name-pin categoricity + the derived `aliases` count. Mirrors
//! the feasibility spike, now in-tree. See `plans/selector_constraint_model.md`.
//!
//! Usage: selector_solve_bin <owner_graph.json>

use std::fs;

fn main() {
    let path = std::env::args()
        .nth(1)
        .expect("usage: selector_solve_bin <owner_graph.json>");
    let json = fs::read_to_string(&path).expect("read owner_graph.json");
    let r = selector_solve::solve_str(&json).expect("parse owner_graph.json");
    println!("EDB: declares={} uses={}", r.edb_declares, r.edb_uses);
    println!(
        "name-pin: bindings={} unique={} ambiguous={}",
        r.total(),
        r.unique(),
        r.ambiguous()
    );
    println!("aliases (var-decl eager_use): {}", r.aliases.len());
}
