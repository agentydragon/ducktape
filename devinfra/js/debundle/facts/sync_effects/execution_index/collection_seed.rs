//! Initial state and top-level declaration seeding for collection.

use super::{BundleGlobals, ExecutionIndex, GlobalNames};
use analysis::facts::global_object::{unshadowed_global_names, unshadowed_global_object_aliases};
use analysis::facts::local_effect_targets::metadata_decorate_call;
use analysis_domain::item_views::{TopLevelItemView, var_decl_of_item};
use analysis_vocabulary::inputs::AnalysisInputs;
use purity::LocalClassConstructionShapes;
use spec::ReentryFreeOperation;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Spanned;
use swc_ecma_ast::*;

impl ExecutionIndex {
    /// The index before any item is visited: per-owner tables sized to `body`
    /// and the author inputs the visitor consults.
    pub(super) fn collection_seed(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        plain_array_bindings: &BTreeSet<String>,
        construction_proof: &LocalClassConstructionShapes,
    ) -> Self {
        let host_operations = &hints.assumptions.reentry_free_operations;
        let global_guards_requested = !hints.assumptions.deferred_global_publications.is_empty()
            || !hints.assumptions.non_reentrant_global_membership.is_empty();
        // A standard host defines `global` only as Node's global object.
        let node_global = hints.assumptions.predefined_globals != spec::PredefinedGlobals::Unknown
            && unshadowed_global_names(body, &["global"]).contains("global");
        Self {
            source_chunk_id: hints.current_chunk_id.clone(),
            global_guards_requested,
            local_class_construction_shapes: construction_proof.clone(),
            top_calls: vec![Vec::new(); body.len()],
            original_statement_spans: body
                .iter()
                .map(|item| match item {
                    TopLevelItemView::Borrowed(item) => item.span(),
                    TopLevelItemView::Owned { provenance, .. } => provenance.parent_item.span(),
                })
                .collect(),
            owner_callable_prefix_effects: vec![None; body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
            top_dynamic_property_receivers: vec![Vec::new(); body.len()],
            top_destructuring_member_receivers: vec![Vec::new(); body.len()],
            top_assignments: vec![Vec::new(); body.len()],
            ordered_initializers: BTreeMap::new(),
            initialization_source_identities: BTreeMap::new(),
            initialization_coverage_refusals: vec![None; body.len()],
            top_definite_initializers: vec![BTreeSet::new(); body.len()],
            top_member_value_writes: vec![Vec::new(); body.len()],
            top_callback_result_writes: vec![Vec::new(); body.len()],
            top_exported_values: vec![Vec::new(); body.len()],
            top_metadata_decorations: body
                .iter()
                .map(|item| {
                    metadata_decorate_call(item.as_module_item(), &hints.assumptions.known_effects)
                })
                .collect(),
            deferred_global_candidates: vec![None; body.len()],
            discarded_sync_iifes: if !global_guards_requested {
                Vec::new()
            } else {
                vec![None; body.len()]
            },
            deferred_global_requested: !hints.assumptions.deferred_global_publications.is_empty(),
            global_object_names: unshadowed_global_object_aliases(body),
            census_globals: GlobalNames {
                aliases: unshadowed_global_object_aliases(body)
                    .into_iter()
                    .chain(node_global.then_some("global"))
                    .collect(),
                node_global,
                predefined: hints.assumptions.predefined_globals,
                bundle: BundleGlobals::default(),
            },
            plain_array_bindings: plain_array_bindings.clone(),
            no_sync_callback_members: hints.assumptions.no_sync_callback_members.clone(),
            retained_record_factories: hints.assumptions.retained_record_factories.clone(),
            no_reentry_statement_calls: hints.assumptions.no_reentry_statement_calls.clone(),
            host_promise_resolve: host_operations.contains(&ReentryFreeOperation::PromiseResolve),
            native_integrity_value_input_refs: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_value_inputs.clone())
                .unwrap_or_default(),
            native_integrity_host_behavior: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_host_behavior.clone())
                .unwrap_or_default(),
            ..Self::default()
        }
    }

    /// Seeds namespace-privacy roots, class and `const` bindings from every
    /// item before any item is visited. Returns the last source position.
    pub(super) fn seed_top_level_declarations(
        &mut self,
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
    ) -> u32 {
        let mut max_source_position = 0u32;
        for item in body {
            max_source_position = max_source_position.max(item.as_module_item().span().hi.0);
            // Seed all candidate roots before visiting any item, including imports
            // textually after a use. Other imported aliases remain an author audit.
            if (!hints.assumptions.namespace_independent_members.is_empty()
                || !hints.assumptions.no_reentry_members.is_empty()
                || !hints.assumptions.no_reentry_record_members.is_empty())
                && let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item()
            {
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    let name = local.sym.as_ref();
                    if hints
                        .assumptions
                        .callback_factory_members
                        .contains_key(name)
                        || hints
                            .assumptions
                            .retained_callback_factory_members
                            .contains_key(name)
                        || hints.assumptions.no_reentry_members.contains_key(name)
                        || hints
                            .assumptions
                            .no_reentry_record_members
                            .contains_key(name)
                        || hints
                            .assumptions
                            .namespace_independent_members
                            .contains_key(name)
                    {
                        self.namespace_privacy.members.insert(
                            local.to_id(),
                            hints
                                .assumptions
                                .namespace_independent_members
                                .get(name)
                                .cloned()
                                .unwrap_or_default(),
                        );
                        self.namespace_privacy.import_sites.insert(local.span.lo.0);
                    }
                }
            }
            let declaration = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration))) => Some(declaration),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Class(declaration) = &export.decl {
                        Some(declaration)
                    } else {
                        None
                    }
                }
                _ => None,
            };
            if let Some(declaration) = declaration {
                let id = declaration.ident.to_id();
                self.class_bindings
                    .insert(declaration.class.span.lo.0, id.clone());
                self.class_ids.insert(id);
            }
            if let Some(declaration) = var_decl_of_item(item.as_module_item())
                && declaration.kind == VarDeclKind::Const
            {
                for declarator in &declaration.decls {
                    self.top_const_bindings
                        .extend(binding_targets::binding_names(&declarator.name));
                }
            }
        }
        max_source_position
    }
}
