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
    /// Member accesses (`obj.X`) this owner's body performs — the EDB for the
    /// `reads_member` primitive. Each entry is one syntactic member-access of a
    /// non-computed property within this owner's statement subtree. Derived from
    /// the chunk's AST facts (`chunk_facts`), not from the lean owner graph's
    /// reference edges (a property name like `.X` off an arbitrary object is not
    /// an owner→binding reference), so absent in fixtures that don't supply it.
    #[serde(default)]
    pub member_reads: Vec<MemberRead>,
    /// Module-member uses (`mod.X`, where `mod` is a chunk-top **imported**
    /// binding) this owner's body performs — the EDB for the `member_of_module`
    /// **use-site** primitive. Each entry pairs the import **source module**
    /// (`mod` resolved through the import table to the specifier string, e.g.
    /// `"./codegen"`) with the member name `X`. Unlike `member_reads`, the object
    /// is not a minified local but the re-minify-invariant module identity, so a
    /// `member_of_module` selector pins "the entity that consumes `mod.X`" by two
    /// labels neither of which churns. Derived from the chunk AST joined to the
    /// import/owner graph (`chunk_facts::module_member_uses_by_ordinal`), so
    /// absent in fixtures that don't supply it.
    #[serde(default)]
    pub module_member_uses: Vec<ModuleMemberUse>,
}

/// One `mod.X` use-site where `mod` is a chunk-top imported binding. `module` is
/// the import **source specifier** the local binding resolves to (`"./codegen"`,
/// `"react"`), `member` is the property name `X` consumed off it. The invariant
/// pair a `member_of_module` selector rides — "consumed as `mod.X`" — neither of
/// which a re-minification rewrites (module specifiers and export names are the
/// public API).
#[derive(Deserialize)]
pub struct ModuleMemberUse {
    pub module: String,
    pub member: String,
}

/// One `obj.X` member-access in an owner's body. `object` is the **minified
/// identifier** the property is read off when the object is a bare identifier
/// (`ctx.X` ⟹ `object = Some("ctx")`); `None` when the object is any other
/// expression (`foo().X`, `this.X`, `a.b.X`). `member` is the property name `X`.
/// The object handle is what the `reads_member` selector's optional `object:
/// @Anchor` constraint rides — pinning "the owner that reads `.X` **off the
/// codegen context**", not merely "off something".
#[derive(Deserialize)]
pub struct MemberRead {
    #[serde(default)]
    pub object: Option<String>,
    pub member: String,
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

/// The unique owner in `owners` whose statement kind is `kind`, `None` if zero
/// or several. The `kind`-disambiguation a relational selector carries ("the
/// *function* that …") narrows the case where several owners stand in a relation;
/// shared by `referencer_of_kind` and the `reads_member` kind resolvers.
fn unique_of_kind(owners: &[u32], owner_kind: &HashMap<u32, String>, kind: &str) -> Option<u32> {
    let mut of_kind = owners
        .iter()
        .filter(|o| owner_kind.get(o).map(String::as_str) == Some(kind));
    match (of_kind.next(), of_kind.next()) {
        (Some(o), None) => Some(*o),
        _ => None,
    }
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

    // ---- the `reads_member` primitive: an owner whose body reads member `.X`.
    // This is the stable identity of the ~72 TS codegen helpers — "the function
    // that reads `.X` off the codegen context" — currently pinned by minified
    // name. Unlike `references`, the property name `X` is not an owner→binding
    // edge (a member of an arbitrary object is not a top-level binding), so the
    // fact is derived from the chunk's AST member-access expressions and joined
    // to the owner. The `declares(o, _d)` conjunct mirrors `references`: only a
    // *declaring* owner has an identity a selector can name as the target, so an
    // anonymous side-effect statement that happens to read `.X` is correctly not
    // a candidate (and would otherwise spoil categoricity). ----
    relation member_read(u32, String); // owner reads member `.X` (off any object)
    relation member_read_from(u32, String, String); // owner reads `obj.X`, obj a bare ident

    relation reads_member(u32, String);
    reads_member(*o, m.clone()) <-- member_read(o, m), declares(o, _d);

    relation reads_member_from(u32, String, String);
    reads_member_from(*o, obj.clone(), m.clone()) <--
        member_read_from(o, obj, m), declares(o, _d);

    // ---- the `member_of_module` primitive: the first **use-site** edge. A
    // *declaring* owner whose body consumes `mod.X`, where `mod` is a chunk-top
    // imported binding resolved (in the lowering bridge) to its import source
    // module. This pulls use-site references into the relational model: the
    // identity is *how the entity is consumed*, by two re-minify-invariant labels
    // — the import source `module` and the export `member` — rather than the
    // target's own minified name or a fragile adjacency. It is the consumer-side
    // analogue of `reads_member`: there the object is a (minified) local binding,
    // here it is the module the binding is imported from, so the whole edge
    // survives re-minification. The `declares(o, _d)` conjunct mirrors
    // `reads_member`/`references`: only a declaring owner has an identity a
    // selector can name, so an anonymous side-effect statement that uses `mod.X`
    // is correctly not a candidate (and would otherwise spoil categoricity). ----
    relation module_member_use(u32, String, String); // owner consumes `<module>.X`

    relation consumes_module_member(u32, String, String);
    consumes_module_member(*o, module.clone(), m.clone()) <--
        module_member_use(o, module, m), declares(o, _d);
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
    /// member name `X` -> declaring owners whose body reads `.X` (off any
    /// object). The `reads_member` primitive indexed for selector resolution.
    pub member_readers: HashMap<String, Vec<u32>>,
    /// (object minified binding, member name) -> declaring owners whose body
    /// reads `<object>.X`. The object-constrained `reads_member` primitive ("the
    /// owner that reads `.X` off `@object`").
    pub member_readers_from: HashMap<(String, String), Vec<u32>>,
    /// (import source module, member name) -> declaring owners whose body
    /// consumes `<module>.X`. The `member_of_module` **use-site** primitive,
    /// indexed for selector resolution ("the entity consumed as `mod.X`").
    pub module_member_consumers: HashMap<(String, String), Vec<u32>>,
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
        unique_of_kind(self.referencers.get(anchor)?, &self.owner_kind, kind)
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

    /// Resolve a `reads_member` selector: the unique declaring owner whose body
    /// reads member `.member` (off any object), `None` if zero or several
    /// (per-target categoricity). Pins a codegen helper by the member it reads —
    /// `function ls(c){ return c.uniqueId }` as "the owner that reads `.uniqueId`"
    /// — without riding the minified `ls`.
    pub fn reads_member_owner(&self, member: &str) -> Option<u32> {
        match self.member_readers.get(member)?.as_slice() {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// Resolve a `reads_member` selector disambiguated by the target's statement
    /// `kind` (a raw owner-graph kind like `fn_decl`): the unique owner of that
    /// kind whose body reads `.member`, `None` if zero or several. Narrows the
    /// case where several declaring owners read one member on a full bundle.
    pub fn reads_member_owner_of_kind(&self, member: &str, kind: &str) -> Option<u32> {
        unique_of_kind(self.member_readers.get(member)?, &self.owner_kind, kind)
    }

    /// Resolve an object-constrained `reads_member` selector: the unique declaring
    /// owner whose body reads `<object>.member` (the object being the minified
    /// binding the selector's `@object` anchor resolved to), `None` if zero or
    /// several. The canonical codegen-helper shape — "the owner that reads
    /// `.member` **off the codegen context**" — narrowing past every owner that
    /// reads `.member` off some unrelated object.
    pub fn reads_member_from_owner(&self, object: &str, member: &str) -> Option<u32> {
        match self
            .member_readers_from
            .get(&(object.to_string(), member.to_string()))?
            .as_slice()
        {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// Object-constrained `reads_member` resolution further narrowed by the
    /// target's statement `kind`: the unique owner of that kind reading
    /// `<object>.member`, `None` if zero or several.
    pub fn reads_member_from_owner_of_kind(
        &self,
        object: &str,
        member: &str,
        kind: &str,
    ) -> Option<u32> {
        unique_of_kind(
            self.member_readers_from
                .get(&(object.to_string(), member.to_string()))?,
            &self.owner_kind,
            kind,
        )
    }

    /// Resolve a `member_of_module` **use-site** selector: the unique declaring
    /// owner whose body consumes `<module>.member` (the module being the import
    /// source the local binding resolves to, the member the export name), `None`
    /// if zero or several (per-target categoricity). Pins an entity by *how it is
    /// consumed at a use site* — "the class consumed as `codegen.NodeAccessor`" —
    /// by two re-minify-invariant labels, never the target's minified name.
    pub fn consumes_module_member_owner(&self, module: &str, member: &str) -> Option<u32> {
        match self
            .module_member_consumers
            .get(&(module.to_string(), member.to_string()))?
            .as_slice()
        {
            [o] => Some(*o),
            _ => None,
        }
    }

    /// `member_of_module` resolution narrowed by the target's statement `kind`
    /// (a raw owner-graph kind like `class_decl`): the unique owner of that kind
    /// consuming `<module>.member`, `None` if zero or several. The kind a real
    /// selector supplies ("the *class* consumed as `mod.X`") narrows the case
    /// where several declaring owners consume one module member on a full bundle.
    pub fn consumes_module_member_owner_of_kind(
        &self,
        module: &str,
        member: &str,
        kind: &str,
    ) -> Option<u32> {
        unique_of_kind(
            self.module_member_consumers
                .get(&(module.to_string(), member.to_string()))?,
            &self.owner_kind,
            kind,
        )
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
        for read in &n.member_reads {
            prog.member_read.push((o, read.member.clone()));
            if let Some(object) = &read.object {
                prog.member_read_from
                    .push((o, object.clone(), read.member.clone()));
            }
        }
        for use_site in &n.module_member_uses {
            prog.module_member_use
                .push((o, use_site.module.clone(), use_site.member.clone()));
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
    let mut member_readers: HashMap<String, Vec<u32>> = HashMap::new();
    for (o, m) in prog.reads_member {
        member_readers.entry(m).or_default().push(o);
    }
    let mut member_readers_from: HashMap<(String, String), Vec<u32>> = HashMap::new();
    for (o, obj, m) in prog.reads_member_from {
        member_readers_from.entry((obj, m)).or_default().push(o);
    }
    let mut module_member_consumers: HashMap<(String, String), Vec<u32>> = HashMap::new();
    for (o, module, m) in prog.consumes_module_member {
        module_member_consumers
            .entry((module, m))
            .or_default()
            .push(o);
    }
    Resolution {
        name_to_owners,
        aliases: prog.aliases,
        referencers,
        owner_kind,
        owner_bindings,
        export_to_owners,
        member_readers,
        member_readers_from,
        module_member_consumers,
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
    fn reads_member_resolves_to_the_reading_owner() {
        // The codegen-helper shape: `function ls(c){ return c.uniqueId }` — a
        // helper whose stable identity is that it reads member `.uniqueId`. Pinned
        // as "the owner that reads `.uniqueId`", not by the minified `ls`. A second
        // owner reads an unrelated member, so it must not interfere.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:3","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"ls"}],
                 "member_reads":[{"object":"c","member":"uniqueId"}]},
                {"id":"owner:7","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"other"}],
                 "member_reads":[{"object":"c","member":"label"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.reads_member_owner("uniqueId"), Some(3));
        assert_eq!(r.binding_for_owner(3), Some("ls"));
        assert_eq!(r.reads_member_owner("label"), Some(7));
        assert_eq!(r.reads_member_owner("absent"), None);
    }

    #[test]
    fn reads_member_is_categorical() {
        // Two declaring owners read `.shared` -> ambiguous -> no resolution.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"a"}],
                 "member_reads":[{"member":"shared"}]},
                {"id":"owner:1","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"b"}],
                 "member_reads":[{"member":"shared"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.reads_member_owner("shared"), None);
    }

    #[test]
    fn reads_member_excludes_non_declaring_owners() {
        // A bare side-effect statement (`ctx.flush()`) reads `.flush` but declares
        // nothing, so it is not a `reads_member` candidate — only the declaring
        // helper is. This is the `declares` conjunct: the side-effect owner would
        // otherwise be a second reader and spoil categoricity.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"flushHelper"}],
                 "member_reads":[{"object":"c","member":"flush"}]},
                {"id":"owner:1","statement_kind":"side_effect",
                 "declared_bindings":[],
                 "member_reads":[{"object":"ctx","member":"flush"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.reads_member_owner("flush"), Some(0));
        assert_eq!(r.binding_for_owner(0), Some("flushHelper"));
    }

    #[test]
    fn reads_member_disambiguates_by_kind() {
        // Two declaring owners read `.render` — a function and a var-decl.
        // `reads_member_owner` is ambiguous; the `kind` constraint narrows to one.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"f"}],
                 "member_reads":[{"member":"render"}]},
                {"id":"owner:1","statement_kind":"var_decl",
                 "declared_bindings":[{"binding":"v"}],
                 "member_reads":[{"member":"render"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.reads_member_owner("render"), None);
        assert_eq!(r.reads_member_owner_of_kind("render", "fn_decl"), Some(0));
        assert_eq!(r.reads_member_owner_of_kind("render", "var_decl"), Some(1));
        assert_eq!(r.reads_member_owner_of_kind("render", "class_decl"), None);
    }

    #[test]
    fn reads_member_from_constrains_by_object() {
        // Two helpers read `.id`, but off different objects: one off `ctx` (the
        // codegen context), one off `node`. The bare `reads_member` is ambiguous;
        // constraining the object to `ctx` picks out exactly the context helper —
        // "the owner that reads `.id` off the codegen context".
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"ctxIdHelper"}],
                 "member_reads":[{"object":"ctx","member":"id"}]},
                {"id":"owner:1","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"nodeIdHelper"}],
                 "member_reads":[{"object":"node","member":"id"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.reads_member_owner("id"), None); // ambiguous off any object
        assert_eq!(r.reads_member_from_owner("ctx", "id"), Some(0));
        assert_eq!(r.reads_member_from_owner("node", "id"), Some(1));
        assert_eq!(r.reads_member_from_owner("absent", "id"), None);
        // Object + kind, the most specific narrowing.
        assert_eq!(
            r.reads_member_from_owner_of_kind("ctx", "id", "fn_decl"),
            Some(0)
        );
        assert_eq!(
            r.reads_member_from_owner_of_kind("ctx", "id", "class_decl"),
            None
        );
    }

    #[test]
    fn member_of_module_resolves_to_the_consuming_owner() {
        // The use-site shape: a delegator `function f(x){ return codegen.emit(x) }`
        // whose stable identity is that it consumes `codegen.emit` — `codegen`
        // imported from `"./codegen"`, `emit` the export. Pinned by the invariant
        // (module, member) pair, never by the minified `f`. A second owner consumes
        // an unrelated module member and must not interfere.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:3","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"f"}],
                 "module_member_uses":[{"module":"./codegen","member":"emit"}]},
                {"id":"owner:7","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"other"}],
                 "module_member_uses":[{"module":"./codegen","member":"flush"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.consumes_module_member_owner("./codegen", "emit"), Some(3));
        assert_eq!(r.binding_for_owner(3), Some("f"));
        assert_eq!(
            r.consumes_module_member_owner("./codegen", "flush"),
            Some(7)
        );
        assert_eq!(r.consumes_module_member_owner("./codegen", "absent"), None);
        // The member alone is not the key — a different module's `emit` is distinct.
        assert_eq!(r.consumes_module_member_owner("./other", "emit"), None);
    }

    #[test]
    fn member_of_module_is_categorical() {
        // Two declaring owners consume `./m.shared` -> ambiguous -> no resolution.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"a"}],
                 "module_member_uses":[{"module":"./m","member":"shared"}]},
                {"id":"owner:1","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"b"}],
                 "module_member_uses":[{"module":"./m","member":"shared"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.consumes_module_member_owner("./m", "shared"), None);
    }

    #[test]
    fn member_of_module_excludes_non_declaring_owners() {
        // A bare side-effect statement (`codegen.register()`) consumes
        // `codegen.register` but declares nothing, so it is not a candidate — only
        // the declaring helper is. The `declares` conjunct: the side-effect owner
        // would otherwise be a second consumer and spoil categoricity.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"registerHelper"}],
                 "module_member_uses":[{"module":"./codegen","member":"register"}]},
                {"id":"owner:1","statement_kind":"side_effect",
                 "declared_bindings":[],
                 "module_member_uses":[{"module":"./codegen","member":"register"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(
            r.consumes_module_member_owner("./codegen", "register"),
            Some(0)
        );
        assert_eq!(r.binding_for_owner(0), Some("registerHelper"));
    }

    #[test]
    fn member_of_module_disambiguates_empty_subclasses_by_use_site() {
        // The empty-class/superclass cluster (`CardsViewAccessor`): two empty
        // subclasses `class Uee extends Ye {}` and `class pN extends Ye {}` are
        // byte-identical templates — no internal anchor distinguishes them. Only
        // their **use sites** do: `Uee` is consumed as `accessors.CardsView`, `pN`
        // as `accessors.Tree`. The use-site edge picks each out categorically; the
        // bare member would too here, but the (module, member) pair is what makes
        // it survive re-minification. This is the cluster X3 unlocks.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"class_decl",
                 "declared_bindings":[{"binding":"Uee"}],
                 "module_member_uses":[{"module":"./accessors","member":"CardsView"}]},
                {"id":"owner:1","statement_kind":"class_decl",
                 "declared_bindings":[{"binding":"pN"}],
                 "module_member_uses":[{"module":"./accessors","member":"Tree"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(
            r.consumes_module_member_owner("./accessors", "CardsView"),
            Some(0)
        );
        assert_eq!(r.binding_for_owner(0), Some("Uee"));
        assert_eq!(
            r.consumes_module_member_owner("./accessors", "Tree"),
            Some(1)
        );
    }

    #[test]
    fn member_of_module_disambiguates_by_kind() {
        // Two declaring owners consume `./m.build` — a function and a class. The
        // bare `member_of_module` is ambiguous; the `kind` constraint a real
        // selector carries ("the *class* consumed as `m.build`") narrows to one.
        let r = solve_str(
            r#"{
              "nodes": [
                {"id":"owner:0","statement_kind":"fn_decl",
                 "declared_bindings":[{"binding":"f"}],
                 "module_member_uses":[{"module":"./m","member":"build"}]},
                {"id":"owner:1","statement_kind":"class_decl",
                 "declared_bindings":[{"binding":"C"}],
                 "module_member_uses":[{"module":"./m","member":"build"}]}
              ],
              "edges": []
            }"#,
        )
        .unwrap();
        assert_eq!(r.consumes_module_member_owner("./m", "build"), None);
        assert_eq!(
            r.consumes_module_member_owner_of_kind("./m", "build", "fn_decl"),
            Some(0)
        );
        assert_eq!(
            r.consumes_module_member_owner_of_kind("./m", "build", "class_decl"),
            Some(1)
        );
        assert_eq!(
            r.consumes_module_member_owner_of_kind("./m", "build", "var_decl"),
            None
        );
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
