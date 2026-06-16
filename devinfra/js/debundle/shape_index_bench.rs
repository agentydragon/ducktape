//! Size-sweep + OPT/read-off-depth distribution benchmark for the Layer-1
//! shape index (W1). Synthetic chunks only — never references real/Tana data.
//!
//! Measures, per chunk size: (i) index build time, (ii) total + per-item
//! read-off time, (iii) the distribution of read-off depth `d` (how often
//! `OPT=1` vs needs 2, 3, ... features). The depth distribution validates the
//! "Zipfian `OPT=1`-majority => near-linear" assumption that gates the full
//! migration: if `OPT=1` dominates, per-item read-off is O(d) and the whole
//! sweep is near-linear; if not, the greedy tail dominates and we revisit.
//!
//! Run: `bazelisk run //devinfra/js/debundle:shape_index_bench`.

use std::time::Instant;

use shape_index::ShapeIndex;
use swc_ecma_ast::Module;

/// Generate a synthetic chunk of `n` top-level items with mixed shapes: each
/// item is one of several archetypes (var-with-call, object literal, class,
/// function), parameterized so that a Zipfian-ish fraction carry a unique
/// semantic literal (=> `OPT=1`) and the rest share tokens with siblings
/// (=> tail). Deliberately synthetic; no real source.
fn synthetic_chunk_source(n: usize) -> String {
    let mut out = String::new();
    for i in 0..n {
        // ~70% get a unique magic literal (Zipfian-majority-unique); the rest
        // reuse one of a few shared tokens, forcing tail combinations.
        let unique = i % 10 < 7;
        let token = if unique {
            format!("magic-token-{i}")
        } else {
            format!("shared-token-{}", i % 3)
        };
        match i % 4 {
            0 => out.push_str(&format!(
                "const v{i} = makeWidget({token:?}, {{ role: \"button\", idx{i}: {i} }});\n"
            )),
            1 => out.push_str(&format!(
                "const o{i} = {{ kind{i}: {token:?}, shared: \"x\" }};\n"
            )),
            2 => out.push_str(&format!(
                "class C{i} {{ method{i}() {{ return {token:?}; }} shared() {{}} }}\n"
            )),
            _ => out.push_str(&format!(
                "function f{i}(a, b) {{ return makeWidget({token:?}); }}\n"
            )),
        }
    }
    out
}

fn parse(source: &str) -> Module {
    js_ast::with_swc_globals(|| js_ast::parse_js_module_ast("<bench>", source).unwrap())
}

struct SweepResult {
    n: usize,
    build_ms: f64,
    distinct_shapes: usize,
    posting_entries: usize,
    read_off_total_ms: f64,
    resolved: usize,
    unresolved: usize,
    /// depth_histogram[d-1] = number of items resolved with exactly `d` anchors.
    depth_histogram: Vec<usize>,
}

fn run_sweep(n: usize) -> SweepResult {
    let module = parse(&synthetic_chunk_source(n));

    let build_start = Instant::now();
    let index = ShapeIndex::new(&module);
    let build_ms = build_start.elapsed().as_secs_f64() * 1000.0;

    let mut depth_histogram = vec![0usize; 8];
    let mut resolved = 0;
    let mut unresolved = 0;
    let read_start = Instant::now();
    for body_idx in 0..index.len() {
        match index.minimal_anchor_set(body_idx) {
            Some(anchor) => {
                resolved += 1;
                let d = anchor.anchors.len().min(depth_histogram.len());
                if d >= 1 {
                    depth_histogram[d - 1] += 1;
                }
            }
            None => unresolved += 1,
        }
    }
    let read_off_total_ms = read_start.elapsed().as_secs_f64() * 1000.0;

    SweepResult {
        n,
        build_ms,
        distinct_shapes: index.distinct_shapes(),
        posting_entries: index.posting_entry_count(),
        read_off_total_ms,
        resolved,
        unresolved,
        depth_histogram,
    }
}

fn main() {
    println!("Layer-1 shape index size-sweep + OPT/read-off-depth distribution (synthetic)\n");
    for &n in &[200usize, 1000, 4000] {
        let r = run_sweep(n);
        let per_item_us = r.read_off_total_ms * 1000.0 / r.n as f64;
        println!("=== chunk size N = {} items ===", r.n);
        println!(
            "  build: {:.2} ms  ({:.4} ms/item)   distinct_shapes={}  posting_entries={}",
            r.build_ms,
            r.build_ms / r.n as f64,
            r.distinct_shapes,
            r.posting_entries
        );
        println!(
            "  read-off: {:.2} ms total  ({:.3} us/item)   resolved={} unresolved={}",
            r.read_off_total_ms, per_item_us, r.resolved, r.unresolved
        );
        let opt1 = r.depth_histogram.first().copied().unwrap_or(0);
        let opt1_pct = 100.0 * opt1 as f64 / r.resolved.max(1) as f64;
        print!("  read-off depth d: ");
        for (i, count) in r.depth_histogram.iter().enumerate() {
            if *count > 0 {
                print!("d={}:{} ", i + 1, count);
            }
        }
        println!("\n  OPT=1 share: {opt1}/{} ({opt1_pct:.1}%)\n", r.resolved);
    }
    println!(
        "Interpretation: an OPT=1-dominant depth distribution means per-item read-off is O(d) and\n\
         the whole-spec minimize is near-linear in chunk+spec size, consistent with the <=10s ideal\n\
         / <=30s hard target. A heavy tail (large d) would mean the greedy cover dominates."
    );
}
