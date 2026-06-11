//! Identifier rename priority queue: a priority list of top-level
//! bundle symbols that still have their input-bundle names, keyed by
//! stable selector identity and ranked by how much reference surface
//! they occupy in the output tree.
//!
//! This is the **side output** every pipeline run emits to drive the
//! reverse-engineering workflow: which still-unrenamed symbols should
//! the next rename / module-extraction wave address to buy the most
//! readability per unit of effort?
//!
//! ## Candidate predicate
//!
//! The queue does not inspect spelling. A symbol is included when the
//! final output binding name is still one of the names recorded from the
//! input bundle for the owning chunk. Once a spec/module extraction pass
//! gives that binding a new readable name, its final name no longer
//! matches the bundle-origin name and it disappears from this queue.
//!
//! ## Stable selector
//!
//! The selector encodes a symbol identity that survives upstream
//! version bumps even when minified names regenerate:
//!
//! - `chunk_id`: the chunk source path (e.g. `static/index-DI2GynTv`).
//!   Vite-emitted chunk filenames carry a content hash; the unhashed
//!   prefix is the stable part the spec generator uses, but we keep the
//!   full chunk_id because Vite-generated hashes are stable across
//!   patch builds.
//! - `owner_file`: the chunk-relative file path the declaration lands in
//!   *after* pipeline execution (post-rename, post-materialize). For a
//!   pure entry chunk this is `entry.js`; after `materialize_logical_modules`
//!   it might be `modules/foo/bar.js`.
//! - `owner_ordinal`: the zero-based ordinal of the top-level declaration
//!   in `owner_file`'s module body. Stable across patch builds; can shift
//!   when upstream source adds/removes top-level declarations, but
//!   shifts are local — the ordering of the queue's top entries is what
//!   matters, and a shift of one symbol doesn't perturb the rest.
//!
//! Rendered as `"<chunk_id>:<owner_file>:<ordinal>"` for human-readable
//! diffing across runs.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use anyhow::Result;
use serde::Serialize;
use swc_ecma_ast::{
    BindingIdent, ClassDecl, ExportDecl, ExportDefaultDecl, ExportSpecifier, FnDecl, GetterProp,
    Ident, MemberExpr, MemberProp, ModuleDecl, ModuleExportName, ModuleItem, NamedExport, Pat,
    Prop, PropName, SetterProp, Stmt, VarDeclarator,
};
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{ChunkBundle, ChunkDecompositionOutput, ChunkId};
use js_ast::ParsedJsModule;

/// One entry in the priority queue: a still-unrenamed top-level symbol
/// ranked by how much reference surface it occupies.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct RenameQueueEntry {
    /// Stable selector — see module-level docs.
    pub selector: String,
    /// Current output binding name. Queue membership guarantees this is
    /// also a name that came from the input bundle for this chunk.
    pub name: String,
    /// Total reference count across the bundle.
    pub ref_count: usize,
    /// Number of distinct `<chunk_id>/<file>` modules that reference
    /// this symbol.
    pub fanout_modules: usize,
    /// The chunk owning the declaration.
    pub owner_chunk: String,
    /// Chunk-relative file the declaration lives in, in `<chunk_id>/<file>`
    /// form for full-bundle navigation.
    pub owner_file: String,
}

/// Top-level shape of the emitted JSON.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct IdentifierRenameQueue {
    pub total_references: usize,
    pub entries: Vec<RenameQueueEntry>,
}

/// Compute the queue from the *final* artifact state — post-rename,
/// post-materialize. Pure function over the artifact; does not touch
/// the filesystem.
pub fn compute_identifier_rename_queue(
    artifact: &ChunkBundle,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> Result<IdentifierRenameQueue> {
    // Per-chunk, per-file: walk the final AST to identify top-level
    // declarations whose names still match input-bundle names, then
    // tally references across the whole bundle.
    let input_names_by_chunk = input_bundle_names_by_chunk(artifact, decomposition_by_chunk);

    // Map each current name -> list of declaration sites that bind it.
    // Most minified names are unique across the bundle, but in principle
    // two chunks can independently mint the same letter pair for separate
    // symbols, so we keep a Vec and resolve references chunk-locally.
    let mut sites_by_name: HashMap<String, Vec<DeclSite>> = HashMap::new();
    let mut interest_names_by_chunk = HashMap::<String, HashSet<String>>::new();

    for chunk_id_interned in artifact.list_chunk_ids() {
        let chunk_id = artifact.chunk_table.name(chunk_id_interned);
        let chunk = artifact.js_chunk(chunk_id_interned)?;
        let input_names = input_names_by_chunk
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        for file in &chunk.files {
            let file_path = file.path.as_str();
            let Some(parsed) = file.ast() else {
                continue;
            };
            for site in unrenamed_top_level_sites(parsed, chunk_id, file_path, &input_names) {
                interest_names_by_chunk
                    .entry(site.owner_chunk.clone())
                    .or_default()
                    .insert(site.name.clone());
                sites_by_name
                    .entry(site.name.clone())
                    .or_default()
                    .push(site);
            }
        }
    }

    // Tally references: visit every file, count ident references that
    // resolve to one of our queued names declared *in the same chunk*.
    //
    // Cross-chunk reference resolution would require following each
    // file's import bindings back to the declaring chunk; we don't do
    // that here because the same minified letter pair (`e`, `t`, `n`)
    // is reused across many independently-minified chunks, so a naive
    // cross-chunk attribution attaches the same reference to every
    // chunk's `e` symbol. Counting only within-chunk references gives
    // an honest per-symbol weight: the surface a renamer would touch by
    // renaming THIS declaration. Cross-chunk reference attribution is
    // tracked as a follow-up.
    //
    // TODO(rename-queue-cross-chunk): once `materialize_logical_modules`
    // resolves imports onto a stable cross-chunk binding identity, fold
    // that in here so a "names everywhere" symbol shows its true fanout.
    for chunk_id_interned in artifact.list_chunk_ids() {
        let chunk_id = artifact.chunk_table.name(chunk_id_interned);
        let chunk = artifact.js_chunk(chunk_id_interned)?;
        let Some(of_interest) = interest_names_by_chunk.get(chunk_id) else {
            continue;
        };
        for file in &chunk.files {
            let file_path = file.path.as_str();
            let Some(parsed) = file.ast() else {
                continue;
            };
            let mut counter = ReferenceCounter {
                of_interest,
                local_counts: HashMap::new(),
            };
            parsed.module.visit_with(&mut counter);
            let module_key = format!("{chunk_id}/{file_path}");
            for (name, count) in counter.local_counts {
                let Some(sites) = sites_by_name.get_mut(&name) else {
                    continue;
                };
                // Attribution rule: prefer the site declared in the
                // *same file* (the local binding shadows everything
                // else); fall back to same-chunk sites if the scanning
                // file doesn't declare this name itself. This keeps
                // per-file minified imports (`m` shadowing each
                // file's own `m`) from each receiving every other
                // file's reference count.
                let in_same_file = sites
                    .iter()
                    .any(|site| site.owner_chunk == chunk_id && site.owner_file == *file_path);
                if in_same_file {
                    for site in sites.iter_mut().filter(|site| {
                        site.owner_chunk == chunk_id && site.owner_file == *file_path
                    }) {
                        site.ref_count += count;
                        site.referencing_modules.insert(module_key.clone());
                    }
                } else {
                    // No same-file declaration; the reference is to a
                    // chunk-level export. Attribute to all same-chunk
                    // sites with this name. This still over-counts when
                    // multiple files in the same chunk declare the same
                    // name without one of them shadowing in the scanned
                    // file, but that case is rare in practice.
                    for site in sites.iter_mut().filter(|site| site.owner_chunk == chunk_id) {
                        site.ref_count += count;
                        site.referencing_modules.insert(module_key.clone());
                    }
                }
            }
        }
    }

    let mut entries: Vec<RenameQueueEntry> = sites_by_name
        .into_iter()
        .flat_map(|(_, sites)| sites.into_iter())
        .map(|site| RenameQueueEntry {
            selector: format!(
                "{}:{}:{}",
                site.owner_chunk, site.owner_file, site.owner_ordinal
            ),
            owner_file: format!("{}/{}", site.owner_chunk, site.owner_file),
            name: site.name,
            ref_count: site.ref_count,
            fanout_modules: site.referencing_modules.len(),
            owner_chunk: site.owner_chunk,
        })
        .collect();

    entries.sort_by(|a, b| {
        b.ref_count
            .cmp(&a.ref_count)
            .then_with(|| b.fanout_modules.cmp(&a.fanout_modules))
            .then_with(|| a.selector.cmp(&b.selector))
            // Final tiebreak on name keeps ordering byte-stable across
            // identical re-runs when a single declaration row binds
            // multiple names and therefore shares a selector.
            .then_with(|| a.name.cmp(&b.name))
    });

    let total_references = entries.iter().map(|entry| entry.ref_count).sum();

    Ok(IdentifierRenameQueue {
        total_references,
        entries,
    })
}

fn input_bundle_names_by_chunk(
    artifact: &ChunkBundle,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> HashMap<String, HashSet<String>> {
    let mut names_by_chunk = HashMap::<String, HashSet<String>>::new();
    for chunk in &artifact.chunks {
        let chunk_name = artifact.chunk_table.name(chunk.chunk_id).to_string();
        let names = names_by_chunk.entry(chunk_name).or_default();
        for declaration in &chunk.analysis.kept_top_level_declarations {
            names.extend(declaration.names.iter().cloned());
        }
        for import in &chunk.analysis.imports {
            names.extend(
                import
                    .specifiers
                    .iter()
                    .map(|specifier| specifier.local.clone()),
            );
        }
        if let Some(decomp) = decomposition_by_chunk.get(&chunk.chunk_id) {
            for lowering in &decomp.selected_module_lowerings {
                names.extend(lowering.binding_names.iter().cloned());
            }
        }
    }
    names_by_chunk
}

fn is_input_bundle_name(name: &str, input_names: &HashSet<String>) -> bool {
    input_names.contains(name)
}

/// Write the queue to `path` and
/// return the path written. Idempotent: caller is free to re-emit on
/// every run.
pub fn write_queue(path: &Path, queue: &IdentifierRenameQueue) -> Result<std::path::PathBuf> {
    let path = path.to_path_buf();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    serde_json::to_writer_pretty(&std::fs::File::create(&path)?, queue)?;
    Ok(path)
}

// ---------------------------------------------------------------------------
// AST walking
// ---------------------------------------------------------------------------

/// Top-level declaration site we tally references for. The fields here
/// mirror [`RenameQueueEntry`]'s identity portion; ref/fanout counts are
/// kept separately in the queue computation since this struct is used as
/// a per-walk record only.
struct DeclSite {
    name: String,
    owner_chunk: String,
    owner_file: String,
    owner_ordinal: usize,
    ref_count: usize,
    referencing_modules: HashSet<String>,
}

/// Walk a parsed module's body, returning one [`DeclSite`] per top-level
/// still-unrenamed binding. Bindings inside non-top-level scopes (function
/// bodies, class members) are skipped — those are local letters that
/// don't belong in the bundle-scope priority queue.
fn unrenamed_top_level_sites(
    parsed: &ParsedJsModule,
    chunk_id: &str,
    file_path: &str,
    input_names: &HashSet<String>,
) -> Vec<DeclSite> {
    let mut out = Vec::new();
    for (ordinal, item) in parsed.module.body.iter().enumerate() {
        for name in top_level_binding_names(item) {
            if !is_input_bundle_name(&name, input_names) {
                continue;
            }
            out.push(DeclSite {
                name,
                owner_chunk: chunk_id.to_string(),
                owner_file: file_path.to_string(),
                owner_ordinal: ordinal,
                ref_count: 0,
                referencing_modules: HashSet::new(),
            });
        }
    }
    out
}

/// Names bound at module top-level by `item`. Mirrors the surface
/// `program_analysis::analyze_program_shallow` already classifies as
/// "owners" — function/class/var declarations, including the same
/// shapes wrapped in `export`. Import locals also count: a minified
/// import alias is a top-level binding that other modules see.
fn top_level_binding_names(item: &ModuleItem) -> Vec<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl { decl, .. })) => decl_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(ExportDefaultDecl {
            decl, ..
        })) => match decl {
            swc_ecma_ast::DefaultDecl::Class(class) => class
                .ident
                .as_ref()
                .map(|ident| vec![ident.sym.to_string()])
                .unwrap_or_default(),
            swc_ecma_ast::DefaultDecl::Fn(function) => function
                .ident
                .as_ref()
                .map(|ident| vec![ident.sym.to_string()])
                .unwrap_or_default(),
            swc_ecma_ast::DefaultDecl::TsInterfaceDecl(_) => Vec::new(),
        },
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                swc_ecma_ast::ImportSpecifier::Default(default) => default.local.sym.to_string(),
                swc_ecma_ast::ImportSpecifier::Namespace(ns) => ns.local.sym.to_string(),
                swc_ecma_ast::ImportSpecifier::Named(named) => named.local.sym.to_string(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn decl_names(decl: &swc_ecma_ast::Decl) -> Vec<String> {
    match decl {
        swc_ecma_ast::Decl::Fn(FnDecl { ident, .. }) => vec![ident.sym.to_string()],
        swc_ecma_ast::Decl::Class(ClassDecl { ident, .. }) => vec![ident.sym.to_string()],
        swc_ecma_ast::Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|VarDeclarator { name, .. }| pat_names(name))
            .collect(),
        _ => Vec::new(),
    }
}

fn pat_names(pat: &Pat) -> Vec<String> {
    match pat {
        Pat::Ident(BindingIdent { id, .. }) => vec![id.sym.to_string()],
        Pat::Rest(rest) => pat_names(&rest.arg),
        Pat::Assign(assign) => pat_names(&assign.left),
        Pat::Array(array) => array.elems.iter().flatten().flat_map(pat_names).collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                swc_ecma_ast::ObjectPatProp::KeyValue(kv) => pat_names(&kv.value),
                swc_ecma_ast::ObjectPatProp::Assign(assign) => vec![assign.key.id.sym.to_string()],
                swc_ecma_ast::ObjectPatProp::Rest(rest) => pat_names(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// Visitor that counts references to identifiers in `of_interest`.
///
/// "Reference" here means an `Ident` node read in an expression position
/// — declaration sites are explicitly skipped, and member-access keys
/// (`foo.bar` — `bar` is a property name, not an ident reference) are
/// skipped because those don't bind to the same symbol. Export-from
/// specifiers (`export { x } from "./y"`) and named-export `exported`
/// names are skipped for the same reason.
///
/// This is intentionally close to babel's `isReferencedIdentifier` —
/// see the deleted JS implementation's `identifierRole`.
struct ReferenceCounter<'a> {
    of_interest: &'a HashSet<String>,
    local_counts: HashMap<String, usize>,
}

impl Visit for ReferenceCounter<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        let name = node.sym.as_ref();
        if self.of_interest.contains(name) {
            *self.local_counts.entry(name.to_string()).or_default() += 1;
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {
        // BindingIdent is the *binding* form (left-hand side of var,
        // function/class id, import local). Skip — those are the
        // declarations the queue is about, not references to them.
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        // Visit obj (which IS a reference) but skip the property name.
        node.obj.visit_with(self);
        // Computed keys (`obj[expr]`) are real reads of `expr` — visit
        // those. Static keys (`obj.foo`) are property strings, not idents
        // bound at top level — skip.
        if let MemberProp::Computed(computed) = &node.prop {
            computed.expr.visit_with(self);
        }
    }

    fn visit_prop(&mut self, node: &Prop) {
        // Object-literal shorthand (`{ foo }` ≡ `{ foo: foo }`) IS a
        // reference to `foo`; SWC represents it as `Prop::Shorthand(Ident)`
        // and the default visitor reaches the Ident — we want the count.
        // KeyValue/Method/etc. property names are not references (object
        // literal `{ foo: bar }` — `foo` is a key string).
        match node {
            Prop::Shorthand(ident) => ident.visit_with(self),
            Prop::KeyValue(kv) => {
                self.visit_prop_name_computed(&kv.key);
                kv.value.visit_with(self);
            }
            Prop::Assign(assign) => {
                // Prop::Assign is `{ foo = 1 }` shorthand reachable in
                // permissive parsing — `foo` is the binding key (not a
                // reference). Only visit the default expression.
                assign.value.visit_with(self);
            }
            Prop::Getter(GetterProp { key, body, .. }) => {
                self.visit_prop_name_computed(key);
                body.visit_with(self);
            }
            Prop::Setter(SetterProp {
                key, param, body, ..
            }) => {
                self.visit_prop_name_computed(key);
                param.visit_with(self);
                body.visit_with(self);
            }
            Prop::Method(method) => {
                self.visit_prop_name_computed(&method.key);
                method.function.visit_with(self);
            }
        }
    }

    fn visit_named_export(&mut self, node: &NamedExport) {
        // `export { foo }` (without `from`) re-exports a top-level
        // binding — `foo` is a real reference to the binding.
        // `export { foo } from "./bar"` does NOT reference the local
        // `foo`; it just names a re-export source.
        if node.src.is_some() {
            return;
        }
        for specifier in &node.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(ident) = &named.orig
            {
                ident.visit_with(self);
            }
        }
    }
}

impl ReferenceCounter<'_> {
    fn visit_prop_name_computed(&mut self, key: &PropName) {
        if let PropName::Computed(computed) = key {
            computed.expr.visit_with(self);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn input_bundle_name_detection_uses_origin_not_spelling() {
        let input_names = HashSet::from([
            "aH".to_string(),
            "getUserData".to_string(),
            "__vite__mapDeps".to_string(),
        ]);
        assert!(is_input_bundle_name("aH", &input_names));
        assert!(is_input_bundle_name("getUserData", &input_names));
        assert!(is_input_bundle_name("__vite__mapDeps", &input_names));
        assert!(!is_input_bundle_name("renamedUsefulThing", &input_names));
        assert!(!is_input_bundle_name("bC", &input_names));
    }
}
