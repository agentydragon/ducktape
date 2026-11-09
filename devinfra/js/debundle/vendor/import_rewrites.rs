//! Materialize a resolved vendor import action once for both lowered and pass-through files.
//! Each output file owns its own accumulator: shared namespace imports are deduplicated,
//! body replacements are applied by the caller, and direct replacements count immediately.
//!
//! This owns named-import construction, not every emitted directive. Imported-member
//! re-exports and moved `export ... from` directives in lowering do not all pass here.
//! Keep `validate_partial_swap_consumers` as the final fail-closed check for those
//! paths and unsupported namespace/export-star consumers; do not rewrite Module files
//! a second time with the pass-through rewriter (renames and counts are not idempotent).

use std::collections::BTreeMap;

use analysis::local_namespace_iife_target;
use artifact::reports::{CandidateVendorDestination, CandidateVendorPlanAction, OutputFileAddress};
use js_ast::{is_binding_identifier, member_property};
use source_identity::ChunkId;
use swc_common::{DUMMY_SP, Span, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use crate::VendorImportAction;
use crate::VendorResolutionPlan;

/// Build the typed correspondence premise from the same checked action and
/// plan entry that drive AST construction. A mismatch or missing plan row is
/// unsupported; callers must not reconstruct an action from emitted strings.
pub fn checked_vendor_replacement_action(
    plan: &VendorResolutionPlan,
    provider_chunk: ChunkId,
    source_export: &str,
    action: &VendorImportAction,
    emitted_request: &str,
) -> Option<(CandidateVendorPlanAction, CandidateVendorDestination)> {
    if let Some(partial) = plan.partial_swaps.get(&provider_chunk) {
        let symbol = partial.resolution.symbols.get(source_export)?;
        match action {
            VendorImportAction::PackageMember {
                package,
                namespace,
                upstream_export,
            } => {
                let coords = partial.resolution.packages.get(package)?;
                if symbol.package != *package
                    || symbol.kind != spec::PartialSwapKind::Member
                    || coords.namespace.as_deref() != Some(namespace.as_str())
                    || symbol.upstream_export.as_deref() != Some(upstream_export.as_str())
                    || emitted_request != package
                {
                    return None;
                }
                return Some((
                    CandidateVendorPlanAction::PartialPackageMember {
                        provider_chunk,
                        source_export: source_export.to_owned(),
                        package: package.clone(),
                        version: coords.version.clone(),
                        subpath: coords.subpath.clone(),
                        namespace: namespace.clone(),
                        upstream_export: upstream_export.clone(),
                    },
                    CandidateVendorDestination::Package {
                        request: emitted_request.to_owned(),
                        package: package.clone(),
                    },
                ));
            }
            VendorImportAction::PackageNamespace { package } => {
                let coords = partial.resolution.packages.get(package)?;
                if symbol.package != *package
                    || symbol.kind != spec::PartialSwapKind::Namespace
                    || emitted_request != package
                {
                    return None;
                }
                return Some((
                    CandidateVendorPlanAction::PartialPackageNamespace {
                        provider_chunk,
                        source_export: source_export.to_owned(),
                        package: package.clone(),
                        version: coords.version.clone(),
                        subpath: coords.subpath.clone(),
                    },
                    CandidateVendorDestination::Package {
                        request: emitted_request.to_owned(),
                        package: package.clone(),
                    },
                ));
            }
            VendorImportAction::PackageDefault { package } => {
                let coords = partial.resolution.packages.get(package)?;
                if symbol.package != *package
                    || symbol.kind != spec::PartialSwapKind::Default
                    || emitted_request != package
                {
                    return None;
                }
                return Some((
                    CandidateVendorPlanAction::PartialPackageDefault {
                        provider_chunk,
                        source_export: source_export.to_owned(),
                        package: package.clone(),
                        version: coords.version.clone(),
                        subpath: coords.subpath.clone(),
                    },
                    CandidateVendorDestination::Package {
                        request: emitted_request.to_owned(),
                        package: package.clone(),
                    },
                ));
            }
            VendorImportAction::PackageNamed {
                package,
                upstream_export,
            } => {
                let coords = partial.resolution.packages.get(package)?;
                if symbol.package != *package
                    || symbol.kind != spec::PartialSwapKind::Named
                    || symbol.upstream_export.as_deref() != Some(upstream_export.as_str())
                    || emitted_request != package
                {
                    return None;
                }
                return Some((
                    CandidateVendorPlanAction::PartialPackageNamed {
                        provider_chunk,
                        source_export: source_export.to_owned(),
                        package: package.clone(),
                        version: coords.version.clone(),
                        subpath: coords.subpath.clone(),
                        upstream_export: upstream_export.clone(),
                    },
                    CandidateVendorDestination::Package {
                        request: emitted_request.to_owned(),
                        package: package.clone(),
                    },
                ));
            }
            VendorImportAction::FacadeMember { .. } | VendorImportAction::FacadeDefault { .. } => {
                return None;
            }
        }
    }

    let bundled = plan.bundled_partial_swaps.get(&provider_chunk)?;
    let symbol = bundled.resolution.symbols.get(source_export)?;
    match action {
        VendorImportAction::FacadeMember {
            package,
            facade_app_path,
            namespace,
            property_path,
        } => {
            let coords = bundled.resolution.packages.get(package)?;
            if symbol.package != *package
                || !matches!(
                    symbol.kind,
                    spec::PartialSwapKind::Member | spec::PartialSwapKind::Named
                )
                || coords.generated_facade_path.as_deref() != Some(facade_app_path.as_str())
                || coords.namespace.as_deref() != Some(namespace.as_str())
                || crate::plan::upstream_property_path(
                    symbol.kind,
                    symbol.upstream_export.as_deref()?,
                ) != *property_path
            {
                return None;
            }
            return Some((
                CandidateVendorPlanAction::BundledFacadeMember {
                    provider_chunk,
                    source_export: source_export.to_owned(),
                    symbol_kind: symbol.kind,
                    package: package.clone(),
                    version: coords.version.clone(),
                    subpath: coords.subpath.clone(),
                    bundle_path: bundled.resolution.bundle.source_path.clone(),
                    bundle_export: coords.bundle_export.clone(),
                    facade_app_path: facade_app_path.clone(),
                    namespace: namespace.clone(),
                    property_path: property_path.clone(),
                },
                CandidateVendorDestination::Facade {
                    app_path: facade_app_path.clone(),
                    emitted_request: emitted_request.to_owned(),
                },
            ));
        }
        VendorImportAction::FacadeDefault { facade_app_path } => {
            let coords = bundled.resolution.packages.get(&symbol.package)?;
            if !matches!(
                symbol.kind,
                spec::PartialSwapKind::Namespace | spec::PartialSwapKind::Default
            ) || coords.generated_facade_path.as_deref() != Some(facade_app_path.as_str())
            {
                return None;
            }
            return Some((
                CandidateVendorPlanAction::BundledFacadeDefault {
                    provider_chunk,
                    source_export: source_export.to_owned(),
                    symbol_kind: symbol.kind,
                    package: symbol.package.clone(),
                    version: coords.version.clone(),
                    subpath: coords.subpath.clone(),
                    bundle_path: bundled.resolution.bundle.source_path.clone(),
                    bundle_export: coords.bundle_export.clone(),
                    facade_app_path: facade_app_path.clone(),
                },
                CandidateVendorDestination::Facade {
                    app_path: facade_app_path.clone(),
                    emitted_request: emitted_request.to_owned(),
                },
            ));
        }
        VendorImportAction::PackageMember { .. }
        | VendorImportAction::PackageNamespace { .. }
        | VendorImportAction::PackageDefault { .. }
        | VendorImportAction::PackageNamed { .. } => None,
    }
}

pub fn checked_boundary_rename_action(
    plan: &VendorResolutionPlan,
    provider_chunk: ChunkId,
    source_export: &str,
    public_export: &str,
    output: OutputFileAddress,
) -> Option<(CandidateVendorPlanAction, CandidateVendorDestination)> {
    if plan.boundary_public_export_name(provider_chunk, source_export)? != public_export {
        return None;
    }
    Some((
        CandidateVendorPlanAction::BoundaryRename {
            provider_chunk,
            source_export: source_export.to_owned(),
            public_export: public_export.to_owned(),
        },
        CandidateVendorDestination::BoundaryOutput {
            output,
            export: public_export.to_owned(),
        },
    ))
}

#[derive(Default)]
pub struct VendorImportRewrites {
    pub body_rewrites: BTreeMap<Id, IdentRewriteTarget>,
    pub references_rewritten: BTreeMap<(ChunkId, String), usize>,
    emitted_shared_import_for: BTreeMap<String, DeferredImportMarker>,
    pub(crate) materialized_request_markers: BTreeMap<Id, Vec<DeferredImportMarker>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DeferredImportMarker {
    pub source_span: Span,
    pub request: String,
    pub local: Id,
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
        self.materialize_at(
            action,
            local_id,
            chunk,
            chunk_export,
            DUMMY_SP,
            facade_source,
        )
    }

    /// Construct the same replacement while retaining the source specifier
    /// span and selected candidate-local syntax context. Emission uses this
    /// when it can tie a generated directive to one exact source binding.
    pub fn materialize_at(
        &mut self,
        action: VendorImportAction,
        local_id: Id,
        chunk: ChunkId,
        chunk_export: String,
        source_span: Span,
        facade_source: impl Fn(&str) -> String,
    ) -> Vec<DeferredImport> {
        let mut imports = Vec::new();
        let source_local = local_id.clone();
        let mut request_markers = Vec::new();
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
                let marker = if let Some(marker) = self
                    .emitted_shared_import_for
                    .get(&format!("package:{package}"))
                {
                    marker.clone()
                } else {
                    let marker = DeferredImportMarker {
                        source_span,
                        request: package.clone(),
                        local: Ident::new_no_ctxt(namespace.clone().into(), source_span).to_id(),
                    };
                    self.emitted_shared_import_for
                        .insert(format!("package:{package}"), marker.clone());
                    imports.push(DeferredImport::Namespace {
                        source: package.clone(),
                        local: namespace.clone(),
                        local_ctxt: SyntaxContext::empty(),
                        source_span,
                    });
                    marker
                };
                request_markers.push(marker);
            }
            VendorImportAction::PackageNamespace { package } => {
                request_markers.push(DeferredImportMarker {
                    source_span,
                    request: package.clone(),
                    local: local_id.clone(),
                });
                imports.push(DeferredImport::Namespace {
                    source: package,
                    local: local_id.0.to_string(),
                    local_ctxt: local_id.1,
                    source_span,
                });
                *self
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
            VendorImportAction::PackageDefault { package } => {
                request_markers.push(DeferredImportMarker {
                    source_span,
                    request: package.clone(),
                    local: local_id.clone(),
                });
                imports.push(DeferredImport::Default {
                    source: package,
                    local: local_id.0.to_string(),
                    local_ctxt: local_id.1,
                    source_span,
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
                let candidate_local = if local_id.0.as_ref() == local {
                    local_id.clone()
                } else {
                    Ident::new_no_ctxt(local.clone().into(), source_span).to_id()
                };
                request_markers.push(DeferredImportMarker {
                    source_span,
                    request: package.clone(),
                    local: candidate_local,
                });
                imports.push(DeferredImport::Named {
                    source: package,
                    local: local.clone(),
                    local_ctxt: if local_id.0.as_ref() == local {
                        local_id.1
                    } else {
                        SyntaxContext::empty()
                    },
                    upstream_export,
                    source_span,
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
                let marker = if let Some(marker) = self
                    .emitted_shared_import_for
                    .get(&format!("facade:{package}"))
                {
                    marker.clone()
                } else {
                    let marker = DeferredImportMarker {
                        source_span,
                        request: source.clone(),
                        local: Ident::new_no_ctxt(namespace.clone().into(), source_span).to_id(),
                    };
                    self.emitted_shared_import_for
                        .insert(format!("facade:{package}"), marker.clone());
                    imports.push(DeferredImport::Default {
                        source,
                        local: namespace.clone(),
                        local_ctxt: SyntaxContext::empty(),
                        source_span,
                    });
                    marker
                };
                request_markers.push(marker);
            }
            VendorImportAction::FacadeDefault { facade_app_path } => {
                let source = facade_source(&facade_app_path);
                request_markers.push(DeferredImportMarker {
                    source_span,
                    request: source.clone(),
                    local: local_id.clone(),
                });
                imports.push(DeferredImport::Default {
                    source,
                    local: local_id.0.to_string(),
                    local_ctxt: local_id.1,
                    source_span,
                });
                *self
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
        }
        self.materialized_request_markers
            .insert(source_local, request_markers);
        imports
    }
}

/// Replacement import decl shapes shared by the wave dispatchers and
/// lowering's construction-time vendor imports; one single-specifier
/// `ImportDecl` per value so both application sites emit identical AST.
pub enum DeferredImport {
    /// `import * as <local> from "<source>"`
    Namespace {
        source: String,
        local: String,
        local_ctxt: SyntaxContext,
        source_span: Span,
    },
    /// `import <local> from "<source>"`
    Default {
        source: String,
        local: String,
        local_ctxt: SyntaxContext,
        source_span: Span,
    },
    /// `import { <upstream_export> as <local> } from "<source>"`,
    /// or `import { <name> } from "<source>"` when local == upstream.
    Named {
        source: String,
        local: String,
        local_ctxt: SyntaxContext,
        upstream_export: String,
        source_span: Span,
    },
}

impl DeferredImport {
    pub fn into_module_item(self) -> ModuleItem {
        let (source, source_span, specifier) = match self {
            DeferredImport::Namespace {
                source,
                local,
                local_ctxt,
                source_span,
            } => (
                source,
                source_span,
                ImportSpecifier::Namespace(ImportStarAsSpecifier {
                    span: source_span,
                    local: Ident::new(local.into(), source_span, local_ctxt),
                }),
            ),
            DeferredImport::Default {
                source,
                local,
                local_ctxt,
                source_span,
            } => (
                source,
                source_span,
                ImportSpecifier::Default(ImportDefaultSpecifier {
                    span: source_span,
                    local: Ident::new(local.into(), source_span, local_ctxt),
                }),
            ),
            DeferredImport::Named {
                source,
                local,
                local_ctxt,
                upstream_export,
                source_span,
            } => (source, source_span, {
                let mut specifier = js_ast::named_import_specifier(
                    Ident::new(local.into(), source_span, local_ctxt),
                    &upstream_export,
                );
                if let ImportSpecifier::Named(named) = &mut specifier {
                    named.span = source_span;
                }
                specifier
            }),
        };
        let mut item = js_ast::import_decl_module_item(vec![specifier], &source);
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = &mut item {
            import.span = source_span;
        }
        item
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

/// One actual identifier replacement observed by the shared AST visitor.
/// Callers add source/output request identities and join `source_span` to
/// the canonical parsed import-use manifest; this observation alone grants
/// no source correspondence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VendorUseRewriteObservation {
    pub source_span: swc_common::Span,
    pub before_local: Id,
    pub after_span: swc_common::Span,
    pub result: VendorUseRewriteTarget,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VendorUseRewriteTarget {
    LocalIdentifier { local: Id },
    MemberPath { namespace: Id, path: Vec<String> },
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
    pub observations: Option<&'a mut Vec<VendorUseRewriteObservation>>,
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

    fn visit_mut_prop(&mut self, prop: &mut Prop) {
        let Prop::Shorthand(ident) = prop else {
            prop.visit_mut_children_with(self);
            return;
        };
        if !self.bindings.contains_key(&ident.to_id()) {
            return;
        }

        // A shorthand identifier is both its property key and its value
        // read. Keep the original key and hygiene-bearing value identifier,
        // then route the value through the same expression rewrite path.
        let ident = ident.clone();
        let key = if ident.sym.as_ref() == "__proto__" {
            // `{ __proto__ }` creates an own data property, but the
            // noncomputed `{ __proto__: value }` form sets the prototype.
            PropName::Computed(ComputedPropName {
                span: ident.span,
                expr: Box::new(Expr::Lit(Lit::Str(Str {
                    span: ident.span,
                    value: "__proto__".into(),
                    raw: None,
                }))),
            })
        } else {
            PropName::Ident(IdentName::new(ident.sym.clone(), ident.span))
        };
        let mut value = Expr::Ident(ident);
        self.visit_mut_expr(&mut value);
        *prop = Prop::KeyValue(KeyValueProp {
            key,
            value: Box::new(value),
        });
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
        let source_span = ident.span;
        let before_local = ident.to_id();
        let (chunk_id, chunk_export, result) = match target {
            IdentRewriteTarget::Member {
                namespace,
                property_path,
                chunk_id,
                chunk_export,
            } => {
                let root = Ident::new_no_ctxt(namespace.clone().into(), source_span);
                let namespace_id = root.to_id();
                *expr = property_path
                    .iter()
                    .fold(Expr::Ident(root), |obj, segment| {
                        Expr::Member(MemberExpr {
                            span: source_span,
                            obj: Box::new(obj),
                            prop: member_property(segment),
                        })
                    });
                (
                    *chunk_id,
                    chunk_export,
                    VendorUseRewriteTarget::MemberPath {
                        namespace: namespace_id,
                        path: property_path.clone(),
                    },
                )
            }
            IdentRewriteTarget::Rename {
                upstream_export,
                chunk_id,
                chunk_export,
            } => {
                let local = Ident::new_no_ctxt(upstream_export.clone().into(), source_span);
                let local_id = local.to_id();
                *expr = Expr::Ident(local);
                (
                    *chunk_id,
                    chunk_export,
                    VendorUseRewriteTarget::LocalIdentifier { local: local_id },
                )
            }
        };
        if let Some(observations) = self.observations.as_deref_mut() {
            observations.push(VendorUseRewriteObservation {
                source_span,
                before_local,
                after_span: source_span,
                result,
            });
        }
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
            matches!(&imports[..], [DeferredImport::Default { source, local, .. }] if source == "../facades/pkg.js" && local == "facade_ns")
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
                matches!(&imports[..], [DeferredImport::Named { source, local, upstream_export, .. }] if source == "pkg" && local == "value" && upstream_export == "value")
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
