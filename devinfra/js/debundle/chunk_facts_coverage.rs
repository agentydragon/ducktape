//! Measure `chunk_facts` coverage on a real chunk: how many top-level
//! statements fully extract vs. hit a fail-closed `Unsupported`, with the
//! first-blocker histogram. The instrument that drives P1 growth by real-corpus
//! frequency (see `plans/selector_constraint_model.md`).
//!
//! Run locally against a chunk: `bb run //devinfra/js/debundle:chunk_facts_coverage -- <chunk.js>`.

use std::fs;

fn main() {
    let path = std::env::args()
        .nth(1)
        .expect("usage: chunk_facts_coverage <chunk.js>");
    let source = fs::read_to_string(&path).expect("read chunk source");
    let report = js_ast::with_swc_globals(|| {
        let module = js_ast::parse_js_module_ast(&path, &source).expect("parse chunk");
        chunk_facts::coverage_report(&module)
    });

    let percent = 100.0 * report.covered as f64 / report.total.max(1) as f64;
    println!("{path}");
    println!("top-level statements: {}", report.total);
    println!("fully extracted:      {} ({percent:.1}%)", report.covered);
    println!("unsupported (first blocker per statement, by frequency):");
    let mut rows: Vec<(&&str, &usize)> = report.unsupported.iter().collect();
    rows.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
    for (context, count) in rows {
        println!("  {count:>6}  {context}");
    }
}
