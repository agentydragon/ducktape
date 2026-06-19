//! Feasibility spike: the six capability-delta examples from
//! `plans/selector_constraint_model.md` ("What the matcher cannot do that the
//! query model can"), each encoded as Ascent rules and proven to resolve.
//!
//! The point is to answer "can these actually be implemented in the engine we
//! picked?" with a runnable program rather than a promise. The EDB here is
//! hand-built synthetic facts standing in for the real phase-1 owner-graph EDB
//! plus the phase-2 AST facts (`class_decl`, `has_method`, `subclass_of`,
//! `method_returns_str`, …) the lowering will project from a parsed chunk.
//!
//! Each example shows which Datalog feature it leans on:
//!   1. cross-reference anchor — positive join
//!   2. shared variable + `all_different` — positive join + disequality guard
//!   3. negation / absence — **stratified negation** (`!rel`)
//!   4. transitive closure — recursion
//!   5. uniqueness / "the only" — **aggregation** (`count`) + a guard
//!   6. identity ∧ relation — a join across AST-literal facts and graph edges
//!
//! Owner ids are grouped by example (1x, 2x, … 7x) so the synthetic graph reads
//! as one chunk while keeping each scenario legible.

use ascent::aggregators::count;
use ascent::ascent;

ascent! {
    // ---- EDB: owners + the relations a lowering would project ----
    relation owner(u32);
    relation calls(u32, u32); // caller, callee
    relation reads_member(u32, u32, String); // reader, target, member name
    relation dep(u32, u32, String); // src, dst, edge kind (init-order graph)
    relation class_decl(u32);
    relation has_method(u32, String);
    relation subclass_of(u32, u32); // subclass, base
    relation exported(u32);
    relation imports(u32, u32); // importer, imported
    relation method_returns_str(u32, String, String); // owner, method, returned literal

    // Resolved cross-reference anchors (`@Name`) — in the real engine these are
    // other selectors' distinguished variables; here, given facts.
    relation registry_anchor(u32);
    relation registry2_anchor(u32);
    relation root_anchor(u32);
    relation base_a(u32);
    relation base_b(u32);
    relation settings_anchor(u32);

    // ---- 1. cross-reference anchor: the owner that calls @registry ----
    relation provider_call(u32);
    provider_call(c) <-- calls(c, r), registry_anchor(r);

    // ---- 2. shared variable + all_different ----
    // @registry2 is the *same* owner in both consumer rules (a join on r); the
    // two consumers are then forced distinct without either naming the other.
    relation add_provider(u32);
    add_provider(o) <-- registry2_anchor(r), calls(o, r), reads_member(o, r, m), if m.as_str() == "set";
    relation get_provider(u32);
    get_provider(o) <-- registry2_anchor(r), calls(o, r), reads_member(o, r, m), if m.as_str() == "get";
    relation distinct_consumers(u32, u32);
    distinct_consumers(a, b) <-- add_provider(a), get_provider(b), if a != b;

    // ---- 3. negation / absence (stratified `!`) ----
    relation has_dispose(u32);
    has_dispose(c) <-- has_method(c, m), if m.as_str() == "dispose";
    relation getname_class(u32);
    getname_class(c) <-- class_decl(c), has_method(c, m), if m.as_str() == "getName";
    // "the class with getName but NOT dispose":
    relation disposable_accessor(u32);
    disposable_accessor(c) <-- getname_class(c), !has_dispose(c);

    // "the owner nothing references" (a root): negate the derived in-edge set.
    relation referenced(u32);
    referenced(o) <-- calls(_, o);
    referenced(o) <-- reads_member(_, o, _);
    referenced(o) <-- subclass_of(_, o);
    referenced(o) <-- imports(_, o);
    relation root(u32);
    root(o) <-- owner(o), !referenced(o);

    // ---- 4. transitive closure (recursion) ----
    // everything reachable from @root following only eager_use edges.
    relation reach(u32);
    reach(o) <-- root_anchor(o);
    reach(d) <-- reach(s), dep(s, d, k), if k.as_str() == "eager_use";

    // ---- 5. uniqueness / "the only" (aggregation + guard) ----
    relation sub_of_a(u32);
    sub_of_a(c) <-- class_decl(c), exported(c), subclass_of(c, b), base_a(b);
    relation sub_of_b(u32);
    sub_of_b(c) <-- class_decl(c), exported(c), subclass_of(c, b), base_b(b);
    relation count_a(usize);
    count_a(n) <-- agg n = count() in sub_of_a(_);
    relation count_b(usize);
    count_b(n) <-- agg n = count() in sub_of_b(_);
    // "the ONLY exported subclass" — emitted only when the count is exactly one.
    relation only_sub_a(u32);
    only_sub_a(c) <-- sub_of_a(c), count_a(n), if *n == 1;
    relation only_sub_b(u32);
    only_sub_b(c) <-- sub_of_b(c), count_b(n), if *n == 1;

    // ---- 6. identity (AST literal) joined with a graph edge ----
    // class whose getName() returns "DocumentAccessorFactory" AND is imported by
    // @settings — a single solve over an AST-literal fact and an import edge.
    relation document_accessor_factory(u32);
    document_accessor_factory(c) <--
        method_returns_str(c, meth, lit),
        if meth.as_str() == "getName",
        if lit.as_str() == "DocumentAccessorFactory",
        settings_anchor(s),
        imports(s, c);
}

/// The derived relations of interest, each collected and sorted for assertion.
pub struct ExampleResults {
    pub provider_call: Vec<u32>,
    pub add_provider: Vec<u32>,
    pub get_provider: Vec<u32>,
    pub distinct_consumers: Vec<(u32, u32)>,
    pub disposable_accessor: Vec<u32>,
    pub root: Vec<u32>,
    pub reach: Vec<u32>,
    pub count_a: Vec<usize>,
    pub count_b: Vec<usize>,
    pub only_sub_a: Vec<u32>,
    pub only_sub_b: Vec<u32>,
    pub document_accessor_factory: Vec<u32>,
}

fn col1(rows: Vec<(u32,)>) -> Vec<u32> {
    let mut out: Vec<u32> = rows.into_iter().map(|(x,)| x).collect();
    out.sort_unstable();
    out
}

/// Build the synthetic chunk EDB, run the program, and return the derived
/// relations. The EDB is fixed so the tests pin exact answers.
pub fn solve_examples() -> ExampleResults {
    let s = |x: &str| x.to_string();

    let mut prog = AscentProgram {
        owner: [
            10, 11, 12, 20, 21, 22, 30, 31, 34, 35, 40, 41, 42, 43, 44, 50, 51, 52, 60, 61, 62, 70,
            71, 72, 73,
        ]
        .into_iter()
        .map(|o| (o,))
        .collect(),
        // 1: owner 11 is the sole caller of registry 10; 12 calls 11 (a decoy).
        registry_anchor: vec![(10,)],
        // 2: registry2 = 20; addProvider 21 (.set), getProvider 22 (.get).
        registry2_anchor: vec![(20,)],
        calls: vec![(11, 10), (12, 11), (21, 20), (22, 20), (34, 35)],
        reads_member: vec![(21, 20, s("set")), (22, 20, s("get"))],
        // 3: class 30 has getName only; 31 has getName + dispose. 34 is a root
        //    (nothing references it); 35 is referenced (34 calls it).
        class_decl: vec![
            (30,),
            (31,),
            (51,),
            (52,),
            (61,),
            (62,),
            (71,),
            (72,),
            (73,),
        ],
        has_method: vec![(30, s("getName")), (31, s("getName")), (31, s("dispose"))],
        // 4: eager chain 40->41->42; lazy edges 42->43 and 40->44 are not followed.
        root_anchor: vec![(40,)],
        dep: vec![
            (40, 41, s("eager_use")),
            (41, 42, s("eager_use")),
            (42, 43, s("lazy_use")),
            (40, 44, s("lazy_use")),
        ],
        // 5: base_a (50) has exactly one exported subclass (51; 52 is non-exported).
        //    base_b (60) has two exported subclasses (61, 62) — ambiguous.
        base_a: vec![(50,)],
        base_b: vec![(60,)],
        subclass_of: vec![(51, 50), (52, 50), (61, 60), (62, 60)],
        exported: vec![(51,), (61,), (62,)],
        // 6: 71 returns the literal AND is imported by settings (70); 72 returns the
        //    literal but is not imported; 73 is imported but returns another literal.
        settings_anchor: vec![(70,)],
        imports: vec![(70, 71), (70, 73)],
        method_returns_str: vec![
            (71, s("getName"), s("DocumentAccessorFactory")),
            (72, s("getName"), s("DocumentAccessorFactory")),
            (73, s("getName"), s("SomethingElse")),
        ],
        ..Default::default()
    };

    prog.run();

    let mut distinct_consumers: Vec<(u32, u32)> = prog.distinct_consumers.clone();
    distinct_consumers.sort_unstable();
    let mut count_a: Vec<usize> = prog.count_a.iter().map(|(n,)| *n).collect();
    count_a.sort_unstable();
    let mut count_b: Vec<usize> = prog.count_b.iter().map(|(n,)| *n).collect();
    count_b.sort_unstable();

    ExampleResults {
        provider_call: col1(prog.provider_call),
        add_provider: col1(prog.add_provider),
        get_provider: col1(prog.get_provider),
        distinct_consumers,
        disposable_accessor: col1(prog.disposable_accessor),
        root: col1(prog.root),
        reach: col1(prog.reach),
        count_a,
        count_b,
        only_sub_a: col1(prog.only_sub_a),
        only_sub_b: col1(prog.only_sub_b),
        document_accessor_factory: col1(prog.document_accessor_factory),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ex1_cross_reference_anchor() {
        // The owner whose call target is @registry — a positive join. The bare
        // delegator has no shape to match; this anchors on the relationship.
        assert_eq!(solve_examples().provider_call, vec![11]);
    }

    #[test]
    fn ex2_shared_variable_and_all_different() {
        let r = solve_examples();
        assert_eq!(r.add_provider, vec![21]);
        assert_eq!(r.get_provider, vec![22]);
        // Both consumers pinned relative to the *same* @registry2, and forced
        // distinct via the disequality guard — neither names the other.
        assert_eq!(r.distinct_consumers, vec![(21, 22)]);
    }

    #[test]
    fn ex3_negation_absence() {
        let r = solve_examples();
        // getName but not dispose: 30 qualifies, 31 (has dispose) is excluded.
        assert_eq!(r.disposable_accessor, vec![30]);
        // "nothing references it": 34 is a root; 35 (called) and 10 (called) are not.
        assert!(
            r.root.contains(&34),
            "34 has no in-edges → root: {:?}",
            r.root
        );
        assert!(!r.root.contains(&35), "35 is referenced → not root");
        assert!(!r.root.contains(&10), "10 is referenced → not root");
    }

    #[test]
    fn ex4_transitive_closure() {
        // Reachable from @root following only eager_use: {40,41,42}. The lazy
        // edges into 43 and 44 are not followed.
        assert_eq!(solve_examples().reach, vec![40, 41, 42]);
    }

    #[test]
    fn ex5_uniqueness_counting() {
        let r = solve_examples();
        // base_a has exactly one exported subclass → "the only" resolves to 51.
        assert_eq!(r.count_a, vec![1]);
        assert_eq!(r.only_sub_a, vec![51]);
        // base_b has two → the uniqueness guard correctly yields nothing.
        assert_eq!(r.count_b, vec![2]);
        assert_eq!(r.only_sub_b, Vec::<u32>::new());
    }

    #[test]
    fn ex6_identity_joined_with_graph_edge() {
        // getName() returns "DocumentAccessorFactory" AND imported by @settings.
        // 72 returns the literal but isn't imported; 73 is imported but returns
        // another literal — only 71 satisfies both atoms in one solve.
        assert_eq!(solve_examples().document_accessor_factory, vec![71]);
    }
}
