//! Emit-side pass that consolidates multiple `import` declarations
//! pointing at the same source into a single declaration.
//!
//! Why this is needed: production bundlers (esbuild in particular)
//! often emit one `import { x as L } from "<src>"` per binding when
//! they chunk-split; the debundler preserves the input structure all
//! the way to emission, so without this pass the emitted output keeps
//! one statement per binding (e.g. 13 separate lines re-importing from
//! the same vendor chunk). Coalescing them into a single statement
//! matches what a human author would write and what the upstream
//! source — before chunk-splitting — looked like.
//!
//! Pass shape and ordering rules:
//!
//! - **Side-effect-only** imports (`import "src";`, zero specifiers)
//!   are NEVER merged with named/default-bearing imports. They have a
//!   distinct ESM meaning (forced module evaluation without binding
//!   anything) and may sit elsewhere in the import block for
//!   ordering reasons; keep them as their own statements.
//! - **Default** and **named** specifiers from the same source DO
//!   coalesce: `import D from "src"` + `import { a } from "src"`
//!   becomes `import D, { a } from "src"`.
//! - **Namespace** imports (`import * as ns from "src"`) cannot share a
//!   statement with named specifiers per ESM grammar. They stay on
//!   their own line (one namespace-only statement remains; named
//!   bindings coalesce into a separate statement next to it).
//! - First-occurrence order is preserved both for the source itself
//!   (the coalesced statement sits where the first import of that
//!   source originally sat) and for bindings within the statement
//!   (first-seen specifier comes first).
//! - `with` / `phase` attributes must match across all imports of the
//!   same source for coalescing to fire; mismatched attributes leave
//!   the imports untouched.

use anyhow::Result;
use rayon::prelude::*;
use swc_common::{DUMMY_SP, EqIgnoreSpan};
use swc_ecma_ast::{ImportDecl, ImportSpecifier, ModuleDecl, ModuleItem, ObjectLit};

use artifact::{JsFile, JsFileAstParts, JsPipelineArtifact};
use js_ast::{ParsedJsModule, str_value};

pub fn coalesce_imports(mut artifact: JsPipelineArtifact) -> Result<JsPipelineArtifact> {
    let mut jobs: Vec<CoalesceJob> = Vec::new();
    for (chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
        let file_paths: Vec<String> = chunk_artifact
            .js
            .file_paths()
            .map(|s| s.to_string())
            .collect();
        for file_path in file_paths {
            let Some(file) = chunk_artifact.js.get_file(&file_path) else {
                continue;
            };
            if !file.is_ast() {
                continue;
            }
            let file = chunk_artifact.js.remove_file(&file_path).expect("file present");
            let Some((parts, ast)) = file.into_ast_parts() else {
                continue;
            };
            jobs.push(CoalesceJob {
                chunk_index,
                parts,
                ast,
            });
        }
    }
    let results: Vec<CoalesceJob> = jobs
        .into_par_iter()
        .map(|mut job| {
            coalesce_imports_in_module(&mut job.ast);
            job
        })
        .collect();
    for result in results {
        artifact.chunks[result.chunk_index]
            .js
            .insert_file(JsFile::from_ast_parts(result.parts, result.ast));
    }
    Ok(artifact)
}

struct CoalesceJob {
    chunk_index: usize,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

/// Public entry point that walks one module body and consolidates
/// imports in place. Exposed (crate-public) so unit tests can drive it
/// without spinning up a full pipeline; the pipeline driver calls
/// `coalesce_imports` instead.
pub fn coalesce_imports_in_module(parsed: &mut ParsedJsModule) {
    let body = std::mem::take(&mut parsed.module.body);
    parsed.module.body = coalesce_import_items(body);
}

fn coalesce_import_items(body: Vec<ModuleItem>) -> Vec<ModuleItem> {
    // Walk the import prelude (the leading run of `import` ModuleItems).
    // Once we hit a non-import, everything that follows is preserved
    // verbatim — coalescing must not reach past statements, since later
    // imports might be intentionally ordered after side-effecting code
    // (rare, but ESM allows it). The runtime payload of the chunks this
    // pass targets always has its imports at the top, so the prelude
    // covers every case in practice.
    let prelude_end = body
        .iter()
        .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
        .count();
    if prelude_end < 2 {
        return body;
    }

    let mut prelude: Vec<ImportDecl> = Vec::with_capacity(prelude_end);
    let mut tail: Vec<ModuleItem> = Vec::with_capacity(body.len() - prelude_end);
    for (index, item) in body.into_iter().enumerate() {
        if index < prelude_end {
            let ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) = item else {
                unreachable!("prelude_end miscounted");
            };
            prelude.push(decl);
        } else {
            tail.push(item);
        }
    }

    // Group named/default imports by source. Side-effect-only imports
    // (zero specifiers) and imports carrying a namespace specifier
    // stay on their own line (see module docs); track them as
    // "untouched" slots that hold their original prelude position.
    //
    // For coalescable imports, the first occurrence of each source
    // gets its slot recorded; subsequent occurrences are folded into
    // that slot. Slots are stored as `Option<ImportDecl>` so empties
    // can be skipped when reassembling the prelude.
    let mut slots: Vec<Option<ImportDecl>> = Vec::with_capacity(prelude.len());
    // Map source-string -> slot index for the consolidated statement.
    let mut consolidation_index: std::collections::HashMap<String, usize> =
        std::collections::HashMap::with_capacity(prelude.len());

    for mut decl in prelude {
        if !is_coalescable(&decl) {
            slots.push(Some(decl));
            continue;
        }
        let src = str_value(&decl.src);
        if let Some(&slot_index) = consolidation_index.get(&src) {
            let head = slots[slot_index]
                .as_mut()
                .expect("consolidation slot must be occupied");
            if !import_attributes_match(head, &decl) {
                // Mismatched `with` / `phase`: bail on this one and
                // leave it as its own statement. Future imports of the
                // same source still try to coalesce with the head.
                slots.push(Some(decl));
                continue;
            }
            // Append the new specifiers, deduplicating by local-binding
            // name. Two imports with the same source and the same local
            // are redundant by construction; keeping both would emit a
            // duplicate-binding error from Node anyway.
            for specifier in std::mem::take(&mut decl.specifiers) {
                if head
                    .specifiers
                    .iter()
                    .any(|existing| same_local_binding(existing, &specifier))
                {
                    continue;
                }
                head.specifiers.push(specifier);
            }
        } else {
            consolidation_index.insert(src, slots.len());
            slots.push(Some(decl));
        }
    }

    let mut out: Vec<ModuleItem> = Vec::with_capacity(slots.len() + tail.len());
    for slot in slots.into_iter().flatten() {
        out.push(ModuleItem::ModuleDecl(ModuleDecl::Import(slot)));
    }
    out.extend(tail);
    // SWC's emitter doesn't care about per-decl spans for coalesced
    // statements; reset the span to DUMMY_SP on every kept decl so
    // emit is deterministic regardless of which input statement we
    // ended up using as the head.
    for item in &mut out {
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) = item {
            decl.span = DUMMY_SP;
        }
    }
    out
}

fn is_coalescable(decl: &ImportDecl) -> bool {
    // Side-effect-only imports never coalesce.
    if decl.specifiers.is_empty() {
        return false;
    }
    // Namespace imports (`import * as ns from "src"`) cannot live in a
    // statement that also has named or default specifiers per ESM
    // grammar (ImportNamedSpecifier and NameSpaceImport are
    // mutually exclusive within a single ImportClause). Leave them on
    // their own line; the rest of the bindings still coalesce around
    // them.
    if decl
        .specifiers
        .iter()
        .any(|s| matches!(s, ImportSpecifier::Namespace(_)))
    {
        return false;
    }
    true
}

fn same_local_binding(left: &ImportSpecifier, right: &ImportSpecifier) -> bool {
    let left_local = import_specifier_local(left);
    let right_local = import_specifier_local(right);
    left_local == right_local
}

fn import_specifier_local(specifier: &ImportSpecifier) -> &str {
    match specifier {
        ImportSpecifier::Named(named) => named.local.sym.as_str(),
        ImportSpecifier::Default(default) => default.local.sym.as_str(),
        ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_str(),
    }
}

fn import_attributes_match(left: &ImportDecl, right: &ImportDecl) -> bool {
    if left.phase != right.phase {
        return false;
    }
    if left.type_only != right.type_only {
        return false;
    }
    match (&left.with, &right.with) {
        (None, None) => true,
        (Some(left_with), Some(right_with)) => object_lits_equal(left_with, right_with),
        _ => false,
    }
}

fn object_lits_equal(left: &ObjectLit, right: &ObjectLit) -> bool {
    // `with` clauses are small ObjectLits of literal key/value pairs.
    // `EqIgnoreSpan` is the right comparison here: it walks the AST
    // structurally, ignoring source-position metadata that always
    // differs between two import statements. A mismatch yields a
    // no-op coalescing decision — the conservative direction.
    left.eq_ignore_span(right)
}

#[cfg(test)]
mod tests {
    use super::*;
    use js_ast::parse_js_module;
    use swc_ecma_ast::ModuleDecl;

    fn ast(source: &str) -> ParsedJsModule {
        parse_js_module("test.js", source).expect("parse")
    }

    fn import_decls(parsed: &ParsedJsModule) -> Vec<&ImportDecl> {
        parsed
            .module
            .body
            .iter()
            .filter_map(|item| match item {
                ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) => Some(decl),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn coalesces_three_named_imports_into_one() {
        let mut parsed = ast(
            "import { a as x } from \"vendor.js\";\n\
             import { b as y } from \"vendor.js\";\n\
             import { c as z } from \"vendor.js\";\n\
             use(x, y, z);\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 1, "expected one ImportDecl");
        assert_eq!(decls[0].specifiers.len(), 3);
        assert_eq!(str_value(&decls[0].src), "vendor.js");
    }

    #[test]
    fn preserves_side_effect_only_import_as_own_statement() {
        let mut parsed = ast(
            "import \"vendor.js\";\n\
             import { a } from \"vendor.js\";\n\
             import { b } from \"vendor.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 2);
        // First is side-effect-only.
        assert!(decls[0].specifiers.is_empty());
        // Second is coalesced named.
        assert_eq!(decls[1].specifiers.len(), 2);
    }

    #[test]
    fn merges_default_and_named_from_same_source() {
        let mut parsed = ast(
            "import D from \"vendor.js\";\n\
             import { a } from \"vendor.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 1);
        assert_eq!(decls[0].specifiers.len(), 2);
        let kinds: Vec<&'static str> = decls[0]
            .specifiers
            .iter()
            .map(|s| match s {
                ImportSpecifier::Default(_) => "default",
                ImportSpecifier::Named(_) => "named",
                ImportSpecifier::Namespace(_) => "namespace",
            })
            .collect();
        assert_eq!(kinds, vec!["default", "named"]);
    }

    #[test]
    fn keeps_namespace_import_separate() {
        let mut parsed = ast(
            "import * as ns from \"vendor.js\";\n\
             import { a } from \"vendor.js\";\n\
             import { b } from \"vendor.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 2);
        let mut saw_namespace = false;
        let mut saw_named_count = 0usize;
        for decl in &decls {
            for spec in &decl.specifiers {
                match spec {
                    ImportSpecifier::Namespace(_) => saw_namespace = true,
                    ImportSpecifier::Named(_) => saw_named_count += 1,
                    ImportSpecifier::Default(_) => {}
                }
            }
        }
        assert!(saw_namespace);
        assert_eq!(saw_named_count, 2);
    }

    #[test]
    fn preserves_first_occurrence_order() {
        let mut parsed = ast(
            "import { a } from \"first.js\";\n\
             import { b } from \"second.js\";\n\
             import { c } from \"first.js\";\n\
             import { d } from \"second.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 2);
        // first.js consolidated statement sits at original first.js position.
        assert_eq!(str_value(&decls[0].src), "first.js");
        assert_eq!(decls[0].specifiers.len(), 2);
        assert_eq!(str_value(&decls[1].src), "second.js");
        assert_eq!(decls[1].specifiers.len(), 2);
    }

    #[test]
    fn deduplicates_redundant_specifiers_with_same_local() {
        let mut parsed = ast(
            "import { a as x } from \"vendor.js\";\n\
             import { a as x } from \"vendor.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        assert_eq!(decls.len(), 1);
        assert_eq!(decls[0].specifiers.len(), 1);
    }

    #[test]
    fn does_not_coalesce_across_non_import_statement() {
        let mut parsed = ast(
            "import { a } from \"vendor.js\";\n\
             const noise = 1;\n\
             import { b } from \"vendor.js\";\n",
        );
        coalesce_imports_in_module(&mut parsed);
        let decls = import_decls(&parsed);
        // Late import after a statement is rare but legal; conservative
        // emit leaves it intact.
        assert_eq!(decls.len(), 2);
    }
}
