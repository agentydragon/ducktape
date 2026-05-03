//! Scrambled-identifier frequency queue: a priority list of still-
//! scrambled top-level symbols, keyed by stable selector identity, ranked
//! by how much reference surface they occupy in the bundle.
//!
//! This is the **side output** every pipeline run emits to drive the
//! reverse-engineering workflow: which still-scrambled symbols should
//! the next rename / module-extraction wave attack to buy the most
//! readability per unit of effort?
//!
//! ## Heuristic
//!
//! [`is_scrambled_name`] decides whether a top-level binding name still
//! looks like a production minifier's letter-pair output (the shape
//! Vite/esbuild/terser produce on `minify: true`). The heuristic:
//!
//! - Length ≤ 4: scrambled (covers 1-3 letters + optional `$N` digits;
//!   includes `aH`, `aH$1`, `a`, `_x`, `__t`).
//! - All-caps acronym: NOT scrambled (`URL`, `API`).
//! - camelCase ≥ 5 chars: NOT scrambled (`getUserId`, `parseQuery`).
//! - underscore-or-dollar in a short ≤ 6 name: scrambled.
//! - mixed-case ≤ 5 chars without natural shape: scrambled.
//!
//! The heuristic is intentionally conservative on the side of "scrambled"
//! — false positives only mean a well-named symbol shows up in the queue
//! and the RE'er skips it. False negatives lose RE coverage, so the bias
//! is to flag.
//!
//! ## Stable selector
//!
//! The selector encodes a symbol identity that survives upstream
//! version bumps even when the scrambled letter pair regenerates:
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

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::SystemTime;

use anyhow::{Context, Result};
use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use swc_ecma_ast::{
    BindingIdent, ClassDecl, ExportDecl, ExportDefaultDecl, ExportSpecifier, FnDecl, GetterProp,
    Ident, MemberExpr, MemberProp, ModuleDecl, ModuleExportName, ModuleItem, NamedExport, Pat,
    Prop, PropName, SetterProp, Stmt, VarDeclarator,
};
use swc_ecma_visit::{Visit, VisitWith};

use artifact::JsPipelineArtifact;
use js_ast::ParsedJsModule;

/// The schema version of the emitted JSON. Bump whenever the on-disk
/// shape changes; existing readers MUST validate this field before
/// trying to decode entries.
pub const SCHEMA_VERSION: u32 = 1;

/// Suggested filename for the side-output JSON.
pub const OUTPUT_FILENAME: &str = "scrambled-identifier-frequencies.json";

/// `kind` discriminator for downstream tools that mux multiple manifest
/// shapes.
pub const KIND: &str = "js.scrambled_identifier_frequencies";

/// Decide whether `name` is a developer-readable identifier or the kind
/// of scrambled letter-pair a production minifier produces (Vite,
/// esbuild, terser, swc-minify on `minify: true`).
///
/// See module-level docs for the heuristic rationale; the test module at
/// the bottom of this file exercises every documented edge case.
pub fn is_scrambled_name(name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    if name.chars().any(|c| !is_identifier_char(c)) {
        return false;
    }
    // All-caps acronyms (URL, API, ID, SVG, JSON) are NOT scrambled even
    // when short — they're conventional developer names.
    if name.len() >= 2 && name.chars().all(|c| !c.is_ascii_lowercase()) && has_letter(name) {
        return false;
    }
    let len = name.chars().count();
    // Length ≤ 4 covers production-minifier letter pairs (`aH`, `aB$1`)
    // and short developer-internal names (`__t`, `_x`, `a`).
    if len <= 4 {
        return true;
    }
    // `__name`, `__defProp`, ECMAScript pollyfills internal markers — by
    // convention these are compiler/runtime internals and generally not
    // RE-targets. Flag them as scrambled so they're surfaced for review.
    if name.starts_with("__") {
        return true;
    }
    let has_dollar_or_underscore = name.contains('$') || name.contains('_');
    if len <= 6 && has_dollar_or_underscore {
        return true;
    }
    if len <= 5 && name.chars().any(|c| c.is_ascii_digit()) {
        return true;
    }
    if len <= 5 && has_lowercase(name) && has_uppercase(name) {
        return true;
    }
    false
}

fn is_identifier_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '$'
}

fn has_letter(name: &str) -> bool {
    name.chars().any(|c| c.is_ascii_alphabetic())
}

fn has_lowercase(name: &str) -> bool {
    name.chars().any(|c| c.is_ascii_lowercase())
}

fn has_uppercase(name: &str) -> bool {
    name.chars().any(|c| c.is_ascii_uppercase())
}

/// One entry in the priority queue: a still-scrambled top-level symbol
/// ranked by how much reference surface it occupies.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct FrequencyEntry {
    /// Stable selector — see module-level docs.
    pub selector: String,
    /// The current scrambled name (changes across version bumps).
    pub scrambled_name: String,
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
pub struct FrequencyQueue {
    pub schema_version: u32,
    pub kind: &'static str,
    pub generated_at_iso: String,
    pub total_scrambled_symbols: usize,
    pub total_references: usize,
    pub entries: Vec<FrequencyEntry>,
}

/// Compute the queue from the *final* artifact state — post-rename,
/// post-materialize. Pure function over the artifact; does not touch
/// the filesystem.
pub fn compute_scrambled_identifier_frequencies(
    artifact: &JsPipelineArtifact,
) -> Result<FrequencyQueue> {
    compute_with_clock(artifact, SystemTime::now())
}

/// Same as [`compute_scrambled_identifier_frequencies`] but with an
/// injected wall clock for deterministic testing.
pub fn compute_with_clock(
    artifact: &JsPipelineArtifact,
    now: SystemTime,
) -> Result<FrequencyQueue> {
    // Per-chunk, per-file: walk the AST to identify top-level scrambled
    // declarations (the candidates), then tally references across the
    // whole bundle.

    // Map each scrambled name -> list of declaration sites that bind it.
    // Most scrambled names are unique across the bundle, but in principle
    // two chunks can independently mint the same letter pair for separate
    // symbols, so we keep a Vec and resolve references chunk-locally.
    let mut sites_by_name: BTreeMap<String, Vec<DeclSite>> = BTreeMap::new();

    for chunk_id in artifact.list_chunk_ids() {
        let chunk = artifact
            .chunks
            .get(&chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
        for (file_path, file) in &chunk.files {
            let Some(parsed) = file.ast.as_ref() else {
                continue;
            };
            for site in scrambled_top_level_sites(parsed, &chunk_id, file_path) {
                sites_by_name
                    .entry(site.scrambled_name.clone())
                    .or_default()
                    .push(site);
            }
        }
    }

    // Tally references: visit every file, count ident references that
    // resolve to one of our scrambled names declared *in the same chunk*.
    //
    // Cross-chunk reference resolution would require following each
    // file's import bindings back to the declaring chunk; we don't do
    // that here because the same scrambled letter pair (`e`, `t`, `n`)
    // is reused across many independently-minified chunks, so a naive
    // cross-chunk attribution attaches the same reference to every
    // chunk's `e` symbol. Counting only within-chunk references gives
    // an honest per-symbol weight: the surface a renamer would touch by
    // renaming THIS declaration. Cross-chunk reference attribution is
    // tracked as a follow-up.
    //
    // TODO(scrambled-cross-chunk): once `materialize_logical_modules`
    // resolves imports onto a stable cross-chunk binding identity, fold
    // that in here so a "names everywhere" symbol shows its true fanout.
    for chunk_id in artifact.list_chunk_ids() {
        let chunk = artifact
            .chunks
            .get(&chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
        for (file_path, file) in &chunk.files {
            let Some(parsed) = file.ast.as_ref() else {
                continue;
            };
            let mut counter = ReferenceCounter {
                of_interest: &sites_by_name,
                local_counts: BTreeMap::new(),
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
                // per-file scrambled imports (`m` shadowing each
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

    let mut entries: Vec<FrequencyEntry> = sites_by_name
        .into_iter()
        .flat_map(|(_, sites)| sites.into_iter())
        .map(|site| FrequencyEntry {
            selector: format!(
                "{}:{}:{}",
                site.owner_chunk, site.owner_file, site.owner_ordinal
            ),
            owner_file: format!("{}/{}", site.owner_chunk, site.owner_file),
            scrambled_name: site.scrambled_name,
            ref_count: site.ref_count,
            fanout_modules: site.referencing_modules.len(),
            owner_chunk: site.owner_chunk,
        })
        .collect();

    entries.sort_by(|a, b| {
        b.ref_count
            .cmp(&a.ref_count)
            .then_with(|| b.fanout_modules.cmp(&a.fanout_modules))
            // Final tiebreak on selector keeps ordering byte-stable across
            // identical re-runs even when ref_count + fanout collide.
            .then_with(|| a.selector.cmp(&b.selector))
    });

    let total_references = entries.iter().map(|entry| entry.ref_count).sum();

    Ok(FrequencyQueue {
        schema_version: SCHEMA_VERSION,
        kind: KIND,
        generated_at_iso: format_iso(now),
        total_scrambled_symbols: entries.len(),
        total_references,
        entries,
    })
}

fn format_iso(now: SystemTime) -> String {
    chrono::DateTime::<Utc>::from(now).to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// Write the queue to `<dir>/scrambled-identifier-frequencies.json` and
/// return the path written. Idempotent: caller is free to re-emit on
/// every run.
pub fn write_queue(dir: &Path, queue: &FrequencyQueue) -> Result<std::path::PathBuf> {
    let path = dir.join(OUTPUT_FILENAME);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(queue)? + "\n")?;
    Ok(path)
}

// ---------------------------------------------------------------------------
// AST walking
// ---------------------------------------------------------------------------

/// Top-level declaration site we tally references for. The fields here
/// mirror [`FrequencyEntry`]'s identity portion; ref/fanout counts are
/// kept separately in `compute_with_clock` since this struct is used as
/// a per-walk record only.
struct DeclSite {
    scrambled_name: String,
    owner_chunk: String,
    owner_file: String,
    owner_ordinal: usize,
    ref_count: usize,
    referencing_modules: BTreeSet<String>,
}

/// Walk a parsed module's body, returning one [`DeclSite`] per top-level
/// scrambled binding. Bindings inside non-top-level scopes (function
/// bodies, class members) are skipped — those are local letters that
/// don't belong in the bundle-scope priority queue.
fn scrambled_top_level_sites(
    parsed: &ParsedJsModule,
    chunk_id: &str,
    file_path: &str,
) -> Vec<DeclSite> {
    let mut out = Vec::new();
    for (ordinal, item) in parsed.module.body.iter().enumerate() {
        for name in top_level_binding_names(item) {
            if !is_scrambled_name(&name) {
                continue;
            }
            out.push(DeclSite {
                scrambled_name: name,
                owner_chunk: chunk_id.to_string(),
                owner_file: file_path.to_string(),
                owner_ordinal: ordinal,
                ref_count: 0,
                referencing_modules: BTreeSet::new(),
            });
        }
    }
    out
}

/// Names bound at module top-level by `item`. Mirrors the surface
/// `program_analysis::analyze_program_shallow` already classifies as
/// "owners" — function/class/var declarations, including the same
/// shapes wrapped in `export`. Import locals also count: a scrambled
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
    of_interest: &'a BTreeMap<String, Vec<DeclSite>>,
    local_counts: BTreeMap<String, usize>,
}

impl Visit for ReferenceCounter<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        let name = node.sym.as_ref();
        if self.of_interest.contains_key(name) {
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
    fn is_scrambled_name_handles_documented_edge_cases() {
        // Letter pairs (production minifier output): scrambled.
        assert!(is_scrambled_name("aH"));
        assert!(is_scrambled_name("aH$1"));
        // Length-1 letter: scrambled.
        assert!(is_scrambled_name("a"));
        // Leading underscore is a compile-internal name; flag.
        assert!(is_scrambled_name("__t"));
        // Short underscore-prefixed is also scrambled.
        assert!(is_scrambled_name("_x"));

        // Conventional all-caps acronyms: NOT scrambled.
        assert!(!is_scrambled_name("URL"));
        assert!(!is_scrambled_name("API"));
        // Real camelCase identifier: NOT scrambled.
        assert!(!is_scrambled_name("getUserId"));
        // Empty string is not an identifier at all.
        assert!(!is_scrambled_name(""));
    }

    #[test]
    fn is_scrambled_name_long_developer_names_pass_through() {
        for name in [
            "buildTaskContextPrompt",
            "useContextProvider",
            "validateInputAndDispatch",
            "registerAllExtensions",
        ] {
            assert!(
                !is_scrambled_name(name),
                "{name} should not be classified as scrambled"
            );
        }
    }

    #[test]
    fn is_scrambled_name_classifies_minifier_shapes_as_scrambled() {
        for name in [
            "aB",     // pure letter pair
            "aB$1",   // letter pair with collision suffix
            "x9",     // mixed-case-with-digit short
            "_a",     // underscore-prefixed letter
            "$id$2",  // dollar-laden short
            "abcd",   // all-lower 4-char short
            "Abc1",   // mixed-case digit, length 4
            "ab$cd",  // length-5 with $
            "ab_cd",  // length-5 with _
            "__t",    // length-3 leading-underscore
            "__name", // length-6 starts with __
        ] {
            assert!(
                is_scrambled_name(name),
                "{name} should be classified as scrambled"
            );
        }
    }

    #[test]
    fn iso_timestamp_is_utc_seconds() {
        let formatted = format_iso(SystemTime::UNIX_EPOCH);
        assert_eq!(formatted, "1970-01-01T00:00:00Z");
    }
}
