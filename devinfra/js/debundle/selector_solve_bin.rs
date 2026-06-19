//! Phase-1 shadow runner: resolve an owner-graph EDB with the in-process Datalog
//! solver and print name-pin categoricity + the derived `aliases` count. With
//! `--check` it acts as the bootstrap-precondition gate (exit 1 if name-pin
//! resolution is not total + categorical). See `plans/selector_constraint_model.md`.
//!
//! Usage: selector_solve_bin [--check] <owner_graph.json>

use std::fs;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let check = args.iter().any(|a| a == "--check");
    let path = args
        .iter()
        .skip(1)
        .find(|a| !a.starts_with("--"))
        .expect("usage: selector_solve_bin [--check] <owner_graph.json>");
    let json = fs::read_to_string(path).expect("read owner_graph.json");
    let r = selector_solve::solve_str(&json).expect("parse owner_graph.json");
    println!("EDB: declares={} uses={}", r.edb_declares, r.edb_uses);
    println!(
        "name-pin: bindings={} unique={} ambiguous={}",
        r.total(),
        r.unique(),
        r.ambiguous()
    );
    println!("aliases (var-decl eager_use): {}", r.aliases.len());

    if check {
        let rep = r.shadow_check();
        if rep.ok() {
            println!(
                "shadow: OK — name-pin resolution total + categorical ({} bindings)",
                rep.total
            );
        } else {
            let shown = &rep.ambiguous[..rep.ambiguous.len().min(10)];
            eprintln!(
                "shadow: FAIL — {} ambiguous binding name(s) block the bootstrap: {:?}",
                rep.ambiguous.len(),
                shown
            );
            std::process::exit(1);
        }
    }
}
