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
}

/// Outcome of a phase-1 solve.
pub struct Resolution {
    /// binding name -> owners declaring it (categorical iff every value has len 1).
    pub name_to_owners: HashMap<String, Vec<u32>>,
    /// (owner, aliased binding) pairs from the `aliases` predicate.
    pub aliases: Vec<(u32, String)>,
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
    for n in &graph.nodes {
        let Some(o) = owner_id(&n.id) else { continue };
        prog.stmt_kind.push((o, n.statement_kind.clone()));
        for db in &n.declared_bindings {
            prog.declares.push((o, db.binding.clone()));
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
    prog.run();

    let mut name_to_owners: HashMap<String, Vec<u32>> = HashMap::new();
    for (b, o) in prog.name_owner {
        name_to_owners.entry(b).or_default().push(o);
    }
    Resolution {
        name_to_owners,
        aliases: prog.aliases,
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
