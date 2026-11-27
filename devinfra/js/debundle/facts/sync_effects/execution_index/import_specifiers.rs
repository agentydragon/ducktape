//! Imported-binding recording.

use super::ExecutionIndex;
use analysis_vocabulary::inputs::AnalysisInputs;
use spec::ReentryFreeOperation;
use swc_ecma_ast::*;

impl ExecutionIndex {
    /// Records each imported binding of `import` with the summaries and author
    /// contracts that name it.
    pub(super) fn record_import_specifiers(&mut self, import: &ImportDecl, hints: &AnalysisInputs) {
        let host_operations = &hints.assumptions.reentry_free_operations;
        let host_async_local_storage = host_operations
            .contains(&ReentryFreeOperation::AsyncLocalStorageConstruct)
            && !import.type_only
            && matches!(&import.phase, ImportPhase::Evaluation)
            && import.with.is_none()
            && matches!(
                import.src.value.as_str(),
                Some("async_hooks" | "node:async_hooks")
            );
        for specifier in &import.specifiers {
            let local = specifier.local();
            self.imported_bindings.insert(local.to_id());
            if host_async_local_storage
                && let ImportSpecifier::Named(named) = specifier
                && !named.is_type_only
                && binding_targets::imported_export_name(named) == "AsyncLocalStorage"
            {
                self.host_async_local_storage_imports.insert(local.to_id());
            }
            if let Some(record) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.returned_record.as_ref())
            {
                self.returned_record_summaries
                    .insert(local.to_id(), record.clone());
            }
            if let Some(wrapper) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.namespace_wrapper.as_ref())
            {
                self.namespace_wrapper_summaries
                    .insert(local.to_id(), wrapper.clone());
            }
            // A narrower summary or an author contract keeps its own
            // consumer and use reports; the derived summary fills in
            // only where nothing else describes the call.
            if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                && let Some(call) = summary.foreign_call.as_ref()
                && summary.returned_record.is_none()
                && summary.namespace_wrapper.is_none()
                && summary.descriptor_copy_factory.is_none()
                && !hints
                    .assumptions
                    .names_imported_callable(local.sym.as_ref())
            {
                if summary.supports_callback_factory() {
                    // An author-asserted factory keeps its contract's
                    // consumers alone.
                    if summary.callback_factory_evidence().iter().all(|evidence| {
                        matches!(
                            evidence,
                            analysis_vocabulary::inputs::CallbackFactoryEvidence::InferredImmutableWrapper { .. }
                        )
                    }) {
                        self
                            .callback_factory_call_summaries
                            .insert(local.to_id(), call.clone());
                    }
                } else {
                    self.foreign_call_imports
                        .insert(local.to_id(), call.clone());
                }
            }
            if let Some(summary) = hints.imported_callables.get(local.sym.as_ref())
                && summary.supports_callback_factory()
            {
                self.callback_factory_imports.insert(local.to_id());
                self.callback_factory_summaries
                    .insert(local.to_id(), summary.clone());
            }
            if let Some(summary) = hints
                .imported_callables
                .get(local.sym.as_ref())
                .and_then(|summary| summary.descriptor_copy_factory.as_ref())
            {
                self.descriptor_copy_factories
                    .insert(local.to_id(), summary.clone());
            }
            if hints
                .assumptions
                .inline_callback_factories
                .contains(local.sym.as_ref())
            {
                self.inline_callback_factory_imports.insert(local.to_id());
            }
            if hints
                .assumptions
                .no_reentry_exports
                .contains(local.sym.as_ref())
            {
                self.no_reentry_exports.insert(local.to_id());
            }
            if let Some(signatures) = hints
                .assumptions
                .no_reentry_callback_exports
                .get(local.sym.as_ref())
            {
                self.no_reentry_callback_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(signatures) = hints
                .assumptions
                .retained_callback_factories
                .get(local.sym.as_ref())
            {
                self.retained_callback_factory_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(signatures) = hints
                .assumptions
                .retained_callback_constructors
                .get(local.sym.as_ref())
            {
                self.retained_callback_constructor_imports
                    .insert(local.to_id(), signatures.clone());
            }
            if let Some(members) = hints
                .assumptions
                .retained_callback_factory_members
                .get(local.sym.as_ref())
            {
                self.retained_callback_factory_members
                    .insert(local.to_id(), members.clone());
                self.audited_factory_members
                    .entry(local.to_id())
                    .or_default()
                    .extend(members.keys().cloned());
            }
            if let Some(members) = hints
                .assumptions
                .callback_factory_members
                .get(local.sym.as_ref())
            {
                self.callback_factory_members
                    .insert(local.to_id(), members.clone());
                self.audited_factory_members
                    .entry(local.to_id())
                    .or_default()
                    .extend(members.iter().cloned());
            }
            if let Some(members) = hints.assumptions.no_reentry_members.get(local.sym.as_ref()) {
                self.no_reentry_members
                    .insert(local.to_id(), members.clone());
            }
            if let Some(members) = hints
                .assumptions
                .no_reentry_record_members
                .get(local.sym.as_ref())
            {
                self.no_reentry_record_members
                    .insert(local.to_id(), members.clone());
            }
        }
    }
}
