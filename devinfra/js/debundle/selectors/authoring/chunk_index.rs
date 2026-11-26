//! Per-chunk declaration and use-site index that selector synthesis queries.

use std::collections::BTreeMap;

use binding_targets::{DeclShape, decl_shape, declaration_name_strings};
use js_ast::item_decl;
use shape_index::ShapeIndex;
use swc_ecma_ast::*;

/// Indexed source facts for selector synthesis.
///
/// The target architecture is a trie/lattice of stable AST discriminants:
/// declaration kind, wrapper shape, initializer kind, callee/member paths,
/// object keys, literal atoms, class/function names, and declarator slots.
/// Synthesis can then ask for the smallest feature path whose candidate set is
/// singleton and render everything else as selector holes.
pub(crate) struct ChunkSelectorIndex<'m> {
    pub(crate) module: &'m Module,
    pub(crate) chunk: selector_resolve::Chunk<'m>,
    pub(crate) decls: Vec<IndexedDeclaration>,
    pub(crate) binding_to_decl: BTreeMap<String, Vec<usize>>,
    /// Top-level object literals with a named property directly referencing a
    /// binding. Built once so use-site fallback does not scan the chunk per pin.
    pub(crate) named_object_use_sites: BTreeMap<String, Vec<usize>>,
    /// Top-level calls that pass a binding beside a stable string argument.
    pub(crate) call_use_sites: BTreeMap<String, Vec<usize>>,
    /// Quality overrides for tokens whose AST context changes their likely
    /// rebuild stability. Built once per chunk, not rediscovered per target.
    pub(crate) anchor_quality: BTreeMap<selector_render::AnchorSpan, u8>,
    /// Layer-1 read-off shape index (W2). Built once per chunk; the migrated
    /// forms (single-target function and var) read their minimal anchor set off
    /// it instead of running the cover search. The resolve
    /// ([`prove_synthesized_selector`]) stays the proof gate.
    pub(crate) shape_index: ShapeIndex,
}

#[derive(Debug)]
pub(crate) struct IndexedDeclaration {
    pub(crate) body_idx: usize,
    pub(crate) kind: DeclShape,
}

impl<'m> ChunkSelectorIndex<'m> {
    pub(crate) fn new(module: &'m Module) -> Self {
        let mut decls = Vec::new();
        let mut binding_to_decl: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        let mut named_object_use_sites: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        let mut call_use_sites: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for (body_idx, item) in module.body.iter().enumerate() {
            for binding in crate::minimize::named_object_use_bindings(item) {
                named_object_use_sites
                    .entry(binding)
                    .or_default()
                    .push(body_idx);
            }
            for binding in crate::minimize::call_use_bindings(item) {
                call_use_sites.entry(binding).or_default().push(body_idx);
            }
            let Some(decl) = item_decl(item) else {
                continue;
            };
            let Some(kind) = decl_shape(decl) else {
                continue;
            };
            let names = declaration_name_strings(decl);
            if names.is_empty() {
                continue;
            }
            let decl_idx = decls.len();
            for name in names {
                binding_to_decl.entry(name).or_default().push(decl_idx);
            }
            decls.push(IndexedDeclaration { body_idx, kind });
        }
        let shape_index = ShapeIndex::new(module);
        let anchor_quality = crate::minimize::anchor_quality_overrides(module);
        Self {
            module,
            chunk: selector_resolve::Chunk::analyze(SYNTHESIS_MODULE, module),
            decls,
            binding_to_decl,
            named_object_use_sites,
            call_use_sites,
            anchor_quality,
            shape_index,
        }
    }
}

/// The module name the synthesis probes resolve under.
pub(crate) const SYNTHESIS_MODULE: &str = "<synthesize-selectors>";
