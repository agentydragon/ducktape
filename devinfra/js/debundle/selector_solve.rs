//! In-process Datalog (Ascent) resolution over the owner-graph EDB.
//!
//! Phase-1 shadow of the selector resolution layer (see
//! `plans/selector_constraint_model.md`). The EDB is the owner graph: `owner:N`
//! top-level statements, the bindings each declares, and owner->owner reference
//! edges carrying the binding and edge kind. The bootstrap rule resolves each
//! binding name to its declaring owner — this reproduces today's name-pin
//! resolution and is the equivalence-gate primitive the shadow asserts against
//! the current matcher. `aliases` is a first derived relational predicate,
//! demonstrating the cross-reference machinery the relational model needs.

use ascent::ascent;
use serde::Deserialize;
use std::collections::HashMap;

/// Subset of `owner_graph.json` consumed as EDB facts.
#[derive(Deserialize)]
pub struct OwnerGraph {
    pub nodes: Vec<OwnerNode>,
    pub edges: Vec<OwnerEdge>,
}

#[derive(Deserialize)]
pub struct OwnerNode {
    pub id: String,
    pub statement_kind: String,
    #[serde(default)]
    pub declared_bindings: Vec<DeclaredBinding>,
}

#[derive(Deserialize)]
pub struct DeclaredBinding {
    pub binding: String,
    /// The readable name this binding is exported under — the spec's member
    /// name. The stable, re-minify-proof handle a `@Name` anchor names (the
    /// minified `binding` churns; this does not). Absent in lean test fixtures.
    #[serde(default)]
    pub export_name: Option<String>,
}

#[derive(Deserialize)]
pub struct OwnerEdge {
    pub source: String,
    #[serde(default)]
    pub binding: Option<String>,
    pub edge_kind: String,
}

fn owner_id(s: &str) -> Option<u32> {
    s.strip_prefix("owner:")?.parse().ok()
}

ascent! {
    // ---- EDB ----
    relation declares(u32, String); // owner declares binding
    relation stmt_kind(u32, String); // owner -> statement kind
    relation uses(u32, String, String); // owner references binding, with edge_kind

    // ---- bootstrap: name-pin resolution (reproduces current resolution) ----
    relation name_owner(String, u32);
    name_owner(b.clone(), *o) <-- declares(o, b);

    // ---- a derived relational predicate: var-decl alias via an eager_use edge.
    // Generic (not tied to any minified name); shows the cross-ref machinery. ----
    relation aliases(u32, String);
    aliases(*o, b.clone()) <--
        stmt_kind(o, sk), uses(o, b, k),
        if sk.as_str() == "var_decl", if k.as_str() == "eager_use";

    // ---- the cross-reference primitive: a *declaring* owner references a binding.
    // This is `resolves_to` projected to owner granularity — the join a `@Name`
    // anchor rides ("the entity that references @Name"). The `declares(o, _d)`
    // conjunct is load-bearing on real pipeline output: an `export { X }` or a
    // `console.log(X)` statement is modelled as an owner with a `uses` edge to
    // every binding it touches but no declared binding of its own, so without it
    // every exported binding is referenced by the export owner and nothing
    // resolves categorically. A `@Name` target has identity — it declares
    // something — so an anonymous consumer is correctly not a referencer. An
    // AST-level `calls` (reference that is specifically a call) is a later
    // refinement over the parsed chunk; at owner granularity this is the honest
    // primitive. ----
    relation references(u32, String);
    references(*o, b.clone()) <-- uses(o, b, _k), declares(o, _d);
}

/// Outcome of a phase-1 solve.
pub struct Resolution {
    /// binding name -> owners declaring it (categorical iff every value has len 1).
    pub name_to_owners: HashMap<String, Vec<u32>>,
    /// (owner, aliased binding) pairs from the `aliases` predicate.
    pub aliases: Vec<(u32, String)>,
    /// binding name -> owners that reference it (the `references` cross-ref
    /// primitive, indexed for `@Name`-anchor resolution).
    pub referencers: HashMap<String, Vec<u32>>,
    /// owner -> its statement kind (e.g. `fn_decl`, `class_decl`, `var_decl`),
    /// for disambiguating a cross-ref target by kind.
    pub owner_kind: HashMap<u32, String>,
    /// owner -> the binding(s) it declares (the minified name(s)).
    pub owner_bindings: HashMap<u32, Vec<String>>,
    /// readable export name (the spec member name) -> owners declaring a binding
    /// under it. The `@Name` anchor's stable handle into the owner graph.
    pub export_to_owners: HashMap<String, Vec<u32>>,
    pub edb_declares: usize,
    pub edb_uses: usize,
}

impl Resolution {
    pub fn total(&self) -> usize {
        self.name_to_owners.len()
    }
    pub fn unique(&self) -> usize {
        self.name_to_owners
            .values()
            .filter(|v| v.len() == 1)
            .count()
    }
    pub fn ambiguous(&self) -> usize {
        self.total() - self.unique()
    }
    /// Resolve a binding name to its single declaring owner; `None` if absent or
    /// ambiguous (the categoricity check, per target).
    pub fn owner_for(&self, name: &str) -> Option<u32> {
        match self.name_to_owners.get(name)?.as_slice() {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// Resolve a `@Name` **cross-reference** anchor: the unique owner that
    /// references `anchor`, `None` if zero or several (per-target categoricity).
    /// This is the relational anchor the model is built for — it pins a target by
    /// an invariant edge (`resolves_to`) to a separately-identified entity, so a
    /// shapeless delegator like `function UBt(x){ return EBt(x) }` is pinned as
    /// "the owner that references @EBt" without riding the minified name `UBt`.
    pub fn referencer_for(&self, anchor: &str) -> Option<u32> {
        match self.referencers.get(anchor)?.as_slice() {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// Resolve a `@Name` cross-reference anchor disambiguated by the target's
    /// statement `kind` (a raw owner-graph kind like `fn_decl` / `class_decl` /
    /// `var_decl`): the unique owner of that kind that references `anchor`,
    /// `None` if zero or several. The kind is what a real selector supplies —
    /// "the *function* that calls @EBt" — and narrows the case where several
    /// declaring owners reference one anchor on a full bundle.
    pub fn referencer_of_kind(&self, anchor: &str, kind: &str) -> Option<u32> {
        let mut of_kind = self
            .referencers
            .get(anchor)?
            .iter()
            .filter(|o| self.owner_kind.get(o).map(String::as_str) == Some(kind));
        match (of_kind.next(), of_kind.next()) {
            (Some(o), None) => Some(*o),
            _ => None,
        }
    }

    /// Resolve a `@Name` **alias** anchor: the unique var-decl owner aliasing
    /// `anchor` (`const X = @anchor`), `None` if zero or several. Pins a
    /// re-export/alias by the class it aliases, not by its own minified name.
    pub fn alias_owner_for(&self, anchor: &str) -> Option<u32> {
        let owners: Vec<u32> = self
            .aliases
            .iter()
            .filter(|(_, b)| b == anchor)
            .map(|(o, _)| *o)
            .collect();
        match owners.as_slice() {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// The owner whose declared binding is exported under the readable name
    /// `export_name` (the spec's member name) — the stable handle a `@Name`
    /// anchor names. `None` if zero or several.
    pub fn owner_for_export(&self, export_name: &str) -> Option<u32> {
        match self.export_to_owners.get(export_name)?.as_slice() {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// The single binding an owner declares (the minified name), `None` if it
    /// declares zero or several — used to turn a resolved cross-ref *owner* back
    /// into the target's binding.
    pub fn binding_for_owner(&self, owner: u32) -> Option<&str> {
        match self.owner_bindings.get(&owner)?.as_slice() {
            [b] => Some(b),
            _ => None,
        }
    }

    /// Bootstrap-precondition gate: the solver's name-pin path can faithfully
    /// reproduce the matcher only if every binding name resolves to exactly one
    /// owner. Reports any binding declared by more than one owner.
    pub fn shadow_check(&self) -> ShadowReport {
        let mut ambiguous: Vec<(String, usize)> = self
            .name_to_owners
            .iter()
            .filter(|(_, owners)| owners.len() > 1)
            .map(|(name, owners)| (name.clone(), owners.len()))
            .collect();
        ambiguous.sort();
        ShadowReport {
            total: self.name_to_owners.len(),
            ambiguous,
        }
    }
}

/// Result of the shadow-precondition gate. `ok()` iff name-pin resolution is
/// total and categorical over the whole chunk.
pub struct ShadowReport {
    pub total: usize,
    /// Binding names declared by more than one owner, with the owner count.
    pub ambiguous: Vec<(String, usize)>,
}

impl ShadowReport {
    pub fn ok(&self) -> bool {
        self.ambiguous.is_empty()
    }
}

/// Build the EDB from an owner graph and run the phase-1 solve.
pub fn solve(graph: &OwnerGraph) -> Resolution {
    let mut prog = AscentProgram::default();
    let mut owner_bindings: HashMap<u32, Vec<String>> = HashMap::new();
    let mut export_to_owners: HashMap<String, Vec<u32>> = HashMap::new();
    for n in &graph.nodes {
        let Some(o) = owner_id(&n.id) else { continue };
        prog.stmt_kind.push((o, n.statement_kind.clone()));
        for db in &n.declared_bindings {
            prog.declares.push((o, db.binding.clone()));
            owner_bindings
                .entry(o)
                .or_default()
                .push(db.binding.clone());
            if let Some(export) = &db.export_name {
                export_to_owners.entry(export.clone()).or_default().push(o);
            }
        }
    }
    for e in &graph.edges {
        let Some(src) = owner_id(&e.source) else {
            continue;
        };
        if let Some(b) = &e.binding {
            prog.uses.push((src, b.clone(), e.edge_kind.clone()));
        }
    }
    let edb_declares = prog.declares.len();
    let edb_uses = prog.uses.len();
    let owner_kind: HashMap<u32, String> = prog.stmt_kind.iter().cloned().collect();
    prog.run();

    let mut name_to_owners: HashMap<String, Vec<u32>> = HashMap::new();
    for (b, o) in prog.name_owner {
        name_to_owners.entry(b).or_default().push(o);
    }
    let mut referencers: HashMap<String, Vec<u32>> = HashMap::new();
    for (o, b) in prog.references {
        referencers.entry(b).or_default().push(o);
    }
    Resolution {
        name_to_owners,
        aliases: prog.aliases,
        referencers,
        owner_kind,
        owner_bindings,
        export_to_owners,
        edb_declares,
        edb_uses,
    }
}

/// Parse an owner-graph JSON document and solve it.
pub fn solve_str(json: &str) -> serde_json::Result<Resolution> {
    Ok(solve(&serde_json::from_str(json)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_pin_is_categorical_and_resolves() {
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"a"}]},
                {"id":"owner:1","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"b"}]},
                {"id":"owner:2","statement_kind":"var_decl",
                 "declared_bindings":[{"binding":"c"}]}
              ],
              "edges": [
                {"source":"owner:2","binding":"b","edge_kind":"eager_use"}
              ]
            }"#,
        )
        .unwrap();
        assert_eq!((r.total(), r.unique(), r.ambiguous()), (3, 3, 0));
        assert_eq!(r.owner_for("a"), Some(0));
        assert_eq!(r.owner_for("b"), Some(1));
        // c is the var_decl aliasing b via eager_use -> cross-ref predicate fires.
        assert_eq!(r.aliases, vec![(2, "b".to_string())]);
    }

    #[test]
    fn cross_reference_anchor_resolves_to_the_referencer() {
        // The `isMeetingTranscriptionProvider` shape from the metaNode pass: a
        // shapeless delegator `function UBt(x){ return EBt(x) }` whose only stable
        // identity is that it references EBt. `@EBt` is pinned by name; the target
        // is "the owner that references @EBt" — no reliance on the minified `UBt`.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:5","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"UBt"}]},
                {"id":"owner:9","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"EBt"}]}
              ],
              "edges": [
                {"source":"owner:5","binding":"EBt","edge_kind":"eager_use"}
              ]
            }"#,
        )
        .unwrap();
        assert_eq!(r.owner_for("EBt"), Some(9)); // the anchor pins by name
        assert_eq!(r.referencer_for("EBt"), Some(5)); // the target pins by edge
        assert_eq!(r.referencer_for("absent"), None);
    }

    #[test]
    fn cross_reference_anchor_is_categorical() {
        // Two owners reference the anchor -> ambiguous -> no resolution.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"a"}]},
                {"id":"owner:1","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"b"}]},
                {"id":"owner:2","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"shared"}]}
              ],
              "edges": [
                {"source":"owner:0","binding":"shared","edge_kind":"eager_use"},
                {"source":"owner:1","binding":"shared","edge_kind":"eager_use"}
              ]
            }"#,
        )
        .unwrap();
        assert_eq!(r.referencer_for("shared"), None);
    }

    #[test]
    fn alias_anchor_resolves_to_the_aliasing_owner() {
        // `let HI = UJ` — a re-export alias whose identity is the class it aliases.
        // Pinned as "the var-decl aliasing @UJ", not by the minified `HI`.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:1","statement_kind":"class_declaration",
                 "declared_bindings":[{"binding":"UJ"}]},
                {"id":"owner:2","statement_kind":"var_decl",
                 "declared_bindings":[{"binding":"HI"}]}
              ],
              "edges": [
                {"source":"owner:2","binding":"UJ","edge_kind":"eager_use"}
              ]
            }"#,
        )
        .unwrap();
        assert_eq!(r.alias_owner_for("UJ"), Some(2));
        assert_eq!(r.alias_owner_for("absent"), None);
    }

    #[test]
    fn cross_reference_anchor_disambiguates_by_kind() {
        // Two declaring owners reference `shared` — a function and a var-decl.
        // `referencer_for` is ambiguous; the kind constraint a real selector
        // carries ("the *function* that calls @shared") narrows to exactly one.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"f"}]},
                {"id":"owner:1","statement_kind":"var_decl",
                 "declared_bindings":[{"binding":"v"}]},
                {"id":"owner:2","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"shared"}]}
              ],
              "edges": [
                {"source":"owner:0","binding":"shared","edge_kind":"eager_use"},
                {"source":"owner:1","binding":"shared","edge_kind":"eager_use"}
              ]
            }"#,
        )
        .unwrap();
        assert_eq!(r.referencer_for("shared"), None); // two declaring referencers
        assert_eq!(r.referencer_of_kind("shared", "fn_decl"), Some(0));
        assert_eq!(r.referencer_of_kind("shared", "var_decl"), Some(1));
        assert_eq!(r.referencer_of_kind("shared", "class_decl"), None);
    }

    #[test]
    fn ambiguous_name_does_not_resolve() {
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"dup"}]},
                {"id":"owner:1","statement_kind":"function_declaration",
                 "declared_bindings":[{"binding":"dup"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.ambiguous(), 1);
        assert_eq!(r.owner_for("dup"), None);
    }

    #[test]
    fn shadow_gate_passes_when_categorical_and_flags_ambiguous() {
        let categorical = solve_str(
            r#"{"nodes":[{"id":"owner:0","statement_kind":"x",
                "declared_bindings":[{"binding":"a"}]}],"edges":[]}"#,
        )
        .unwrap();
        assert!(categorical.shadow_check().ok());

        let ambiguous = solve_str(
            r#"{"nodes":[
                {"id":"owner:0","statement_kind":"x","declared_bindings":[{"binding":"dup"}]},
                {"id":"owner:1","statement_kind":"x","declared_bindings":[{"binding":"dup"}]}
               ],"edges":[]}"#,
        )
        .unwrap();
        let rep = ambiguous.shadow_check();
        assert!(!rep.ok());
        assert_eq!(rep.ambiguous, vec![("dup".to_string(), 2)]);
    }
}
