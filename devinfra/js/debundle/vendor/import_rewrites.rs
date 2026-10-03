//! Materialize a resolved vendor import action once for both lowered and pass-through files.
//! Each output file owns its own accumulator: shared namespace imports are deduplicated,
//! body replacements are applied by the caller, and direct replacements count immediately.
//!
//! This owns named-import construction, not every emitted directive. Imported-member
//! re-exports and moved `export ... from` directives in lowering do not all pass here.
//! Keep `validate_partial_swap_consumers` as the final fail-closed check for those
//! paths and unsupported namespace/export-star consumers; do not rewrite Module files
//! a second time with the pass-through rewriter (renames and counts are not idempotent).

use std::collections::{BTreeMap, BTreeSet};

use analysis::local_namespace_iife_target;
use artifact::ChunkId;
use js_ast::{is_binding_identifier, member_property};
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use crate::VendorImportAction;

#[derive(Default)]
pub struct VendorImportRewrites {
    pub body_rewrites: BTreeMap<Id, IdentRewriteTarget>,
    pub references_rewritten: BTreeMap<(ChunkId, String), usize>,
    emitted_shared_import_for: BTreeSet<String>,
}

impl VendorImportRewrites {
    /// Construct replacement directives without choosing their position in the file.
    /// Facade paths are resolved in the caller's output-file coordinate system.
    pub fn materialize(
        &mut self,
        action: VendorImportAction,
        local_id: Id,
        chunk: ChunkId,
        chunk_export: String,
        facade_source: impl Fn(&str) -> String,
    ) -> Vec<DeferredImport> {
        let mut imports = Vec::new();
        match action {
            VendorImportAction::PackageMember {
                package,
                namespace,
                upstream_export,
            } => {
                self.body_rewrites.insert(
                    local_id,
                    IdentRewriteTarget::Member {
                        namespace: namespace.clone(),
                        property_path: upstream_export.split('.').map(str::to_owned).collect(),
                        chunk_id: chunk,
                        chunk_export,
                    },
                );
                if self
                    .emitted_shared_import_for
                    .insert(format!("package:{package}"))
                {
                    imports.push(DeferredImport::Namespace {
                        source: package,
                        local: namespace,
                    });
                }
            }
            VendorImportAction::PackageNamespace { package } => {
                imports.push(DeferredImport::Namespace {
                    source: package,
                    local: local_id.0.to_string(),
                });
                *self
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
            VendorImportAction::PackageDefault { package } => {
                imports.push(DeferredImport::Default {
                    source: package,
                    local: local_id.0.to_string(),
                });
                *self
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
            VendorImportAction::PackageNamed {
                package,
                upstream_export,
            } => {
                // An external name need not be a legal local binding. Keep the
                // existing, hygienic local for reserved or string export names.
                let local = if is_binding_identifier(&upstream_export) {
                    upstream_export.clone()
                } else {
                    local_id.0.to_string()
                };
                imports.push(DeferredImport::Named {
                    source: package,
                    local: local.clone(),
                    upstream_export,
                });
                if local_id.0.as_ref() != local {
                    self.body_rewrites.insert(
                        local_id,
                        IdentRewriteTarget::Rename {
                            upstream_export: local,
                            chunk_id: chunk,
                            chunk_export,
                        },
                    );
                } else {
                    *self
                        .references_rewritten
                        .entry((chunk, chunk_export))
                        .or_insert(0) += 1;
                }
            }
            VendorImportAction::FacadeMember {
                package,
                facade_app_path,
                namespace,
                property_path,
            } => {
                let source = facade_source(&facade_app_path);
                self.body_rewrites.insert(
                    local_id,
                    IdentRewriteTarget::Member {
                        namespace: namespace.clone(),
                        property_path,
                        chunk_id: chunk,
                        chunk_export,
                    },
                );
                if self
                    .emitted_shared_import_for
                    .insert(format!("facade:{package}"))
                {
                    imports.push(DeferredImport::Default {
                        source,
                        local: namespace,
                    });
                }
            }
            VendorImportAction::FacadeDefault { facade_app_path } => {
                let source = facade_source(&facade_app_path);
                imports.push(DeferredImport::Default {
                    source,
                    local: local_id.0.to_string(),
                });
                *self
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
        }
        imports
    }
}

/// Replacement import decl shapes shared by the wave dispatchers and
/// lowering's construction-time vendor imports; one single-specifier
/// `ImportDecl` per value so both application sites emit identical AST.
pub enum DeferredImport {
    /// `import * as <local> from "<source>"`
    Namespace { source: String, local: String },
    /// `import <local> from "<source>"`
    Default { source: String, local: String },
    /// `import { <upstream_export> as <local> } from "<source>"`,
    /// or `import { <name> } from "<source>"` when local == upstream.
    Named {
        source: String,
        local: String,
        upstream_export: String,
    },
}

impl DeferredImport {
    pub fn into_module_item(self) -> ModuleItem {
        let (source, specifier) = match self {
            DeferredImport::Namespace { source, local } => (
                source,
                ImportSpecifier::Namespace(ImportStarAsSpecifier {
                    span: DUMMY_SP,
                    local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
                }),
            ),
            DeferredImport::Default { source, local } => (
                source,
                ImportSpecifier::Default(ImportDefaultSpecifier {
                    span: DUMMY_SP,
                    local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
                }),
            ),
            DeferredImport::Named {
                source,
                local,
                upstream_export,
            } => (
                source,
                js_ast::named_import_specifier(
                    Ident::new_no_ctxt(local.into(), DUMMY_SP),
                    &upstream_export,
                ),
            ),
        };
        js_ast::import_decl_module_item(vec![specifier], &source)
    }
}

#[derive(Debug, Clone)]
pub enum IdentRewriteTarget {
    /// Rewrite `<local>` references to property access rooted at `<namespace>`.
    Member {
        namespace: String,
        /// Member paths have one segment per dot; named exports have one literal segment.
        property_path: Vec<String>,
        chunk_id: ChunkId,
        chunk_export: String,
    },
    /// kind=named auto-rename: rewrite `<local>` references to a bare
    /// `<upstream_export>` identifier (matching the new no-alias import).
    Rename {
        upstream_export: String,
        chunk_id: ChunkId,
        chunk_export: String,
    },
}

/// Rewrites references to a partial-swap import local into the
/// replacement facade access. `bindings` is keyed by the import
/// local's hygiene-preserving `Id` (`(atom, SyntaxContext)`), captured
/// from the actual binding `Ident` at construction. The module here has
/// been through SWC's `resolver` pass (see
/// `js_ast::parse_and_resolve`), so `ident.to_id()` is the canonical
/// binding identity. Keying on `Id` (rather than the bare textual
/// symbol) ensures a same-named binding in a nested scope — a function
/// parameter, a shadowing `const`/`let`, a `catch` binding — is left
/// untouched, since it carries a different `SyntaxContext`.
pub struct PartialSwapIdentRewriter<'a> {
    pub bindings: &'a BTreeMap<Id, IdentRewriteTarget>,
    pub references_by_symbol: &'a mut BTreeMap<(ChunkId, String), usize>,
}

impl VisitMut for PartialSwapIdentRewriter<'_> {
    fn visit_mut_call_expr(&mut self, call: &mut CallExpr) {
        if local_namespace_iife_target(call)
            .is_some_and(|target| self.bindings.contains_key(&target))
        {
            // Preserve TS namespace/enum initializer arguments such as
            // `Sa || (Sa = {})`. The strip pass recognizes that shape as
            // a local mutation island and can then drop the old vendor
            // implementation. Rewriting the read side first would turn it
            // into `<facade>.Enum || (Sa = {})`, making it look like a hard
            // residual side effect and potentially mutating the replacement
            // facade.
            if let Callee::Expr(callee) = &mut call.callee {
                callee.visit_mut_with(self);
            }
            for arg in call.args.iter_mut().skip(1) {
                arg.visit_mut_with(self);
            }
            return;
        }

        call.visit_mut_children_with(self);
    }

    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        // Recurse first so nested matches (e.g., the obj of a member
        // expression, the callee of a call) are rewritten before we
        // inspect this node.
        expr.visit_mut_children_with(self);
        let Expr::Ident(ident) = expr else {
            return;
        };
        let Some(target) = self.bindings.get(&ident.to_id()) else {
            return;
        };
        let (chunk_id, chunk_export) = match target {
            IdentRewriteTarget::Member {
                namespace,
                property_path,
                chunk_id,
                chunk_export,
            } => {
                *expr = property_path.iter().fold(
                    Expr::Ident(Ident::new_no_ctxt(namespace.clone().into(), DUMMY_SP)),
                    |obj, segment| {
                        Expr::Member(MemberExpr {
                            span: DUMMY_SP,
                            obj: Box::new(obj),
                            prop: member_property(segment),
                        })
                    },
                );
                (*chunk_id, chunk_export)
            }
            IdentRewriteTarget::Rename {
                upstream_export,
                chunk_id,
                chunk_export,
            } => {
                *expr = Expr::Ident(Ident::new_no_ctxt(upstream_export.clone().into(), DUMMY_SP));
                (*chunk_id, chunk_export)
            }
        };
        *self
            .references_by_symbol
            .entry((chunk_id, chunk_export.clone()))
            .or_insert(0) += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::SyntaxContext;

    fn local(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    #[test]
    fn shared_imports_are_deduplicated_per_file_and_source_family() {
        let package = || VendorImportAction::PackageMember {
            package: "pkg".into(),
            namespace: "pkg_ns".into(),
            upstream_export: "value".into(),
        };
        let facade = || VendorImportAction::FacadeMember {
            package: "pkg".into(),
            namespace: "facade_ns".into(),
            property_path: vec!["value".into()],
            facade_app_path: "facades/pkg.js".into(),
        };
        let mut rewrites = VendorImportRewrites::default();
        let resolve = |path: &str| format!("../{path}");
        assert_eq!(
            rewrites
                .materialize(package(), local("a"), ChunkId(0), "a".into(), resolve)
                .len(),
            1
        );
        assert!(
            rewrites
                .materialize(package(), local("b"), ChunkId(0), "b".into(), resolve)
                .is_empty()
        );
        let imports = rewrites.materialize(facade(), local("c"), ChunkId(1), "c".into(), resolve);
        assert!(
            matches!(&imports[..], [DeferredImport::Default { source, local }] if source == "../facades/pkg.js" && local == "facade_ns")
        );
        assert!(
            rewrites
                .materialize(facade(), local("d"), ChunkId(1), "d".into(), resolve)
                .is_empty()
        );
        assert_eq!(rewrites.body_rewrites.len(), 4);
        assert!(rewrites.references_rewritten.is_empty());
        assert_eq!(
            VendorImportRewrites::default()
                .materialize(package(), local("a"), ChunkId(0), "a".into(), resolve)
                .len(),
            1
        );
    }

    #[test]
    fn named_imports_count_direct_replacements_but_defer_renamed_references() {
        let action = || VendorImportAction::PackageNamed {
            package: "pkg".into(),
            upstream_export: "value".into(),
        };
        let mut rewrites = VendorImportRewrites::default();
        for name in ["value", "alias"] {
            let imports = rewrites.materialize(
                action(),
                local(name),
                ChunkId(0),
                "original".into(),
                |_| unreachable!(),
            );
            assert!(
                matches!(&imports[..], [DeferredImport::Named { source, local, upstream_export }] if source == "pkg" && local == "value" && upstream_export == "value")
            );
        }
        assert_eq!(
            rewrites.references_rewritten[&(ChunkId(0), "original".into())],
            1
        );
        assert_eq!(rewrites.body_rewrites.len(), 1);
        assert!(
            matches!(&rewrites.body_rewrites[&local("alias")], IdentRewriteTarget::Rename { upstream_export, .. } if upstream_export == "value")
        );
    }
}
