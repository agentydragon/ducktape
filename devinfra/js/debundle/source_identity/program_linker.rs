//! Canonical identities and resolution for bindings that cross source chunks.
//!
//! The artifact layer converts manifest import/export records into these
//! normalized links. Resolution here is independent of source paths, ASTs,
//! and the artifact container, so analysis and the reconstruction gate can
//! share one immutable view of program-level identities.

use super::ChunkId;
use std::collections::{BTreeMap, BTreeSet};

/// The canonical identity of one local binding in the configured program.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct ProgramBindingId {
    pub chunk: ChunkId,
    pub local: String,
}

impl ProgramBindingId {
    pub fn new(chunk: ChunkId, local: impl Into<String>) -> Self {
        Self {
            chunk,
            local: local.into(),
        }
    }
}

/// The canonical identity of one named export in the configured program.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct ProgramExportId {
    pub chunk: ChunkId,
    pub name: String,
}

impl ProgramExportId {
    pub fn new(chunk: ChunkId, name: impl Into<String>) -> Self {
        Self {
            chunk,
            name: name.into(),
        }
    }
}

/// A failure to resolve is retained as data so consumers can conservatively
/// decline a fact without inventing a provider.
#[derive(Debug, Clone, Eq, PartialEq)]
pub enum ProgramLinkResolution<T> {
    Resolved(T),
    Missing,
    Cyclic,
    Ambiguous,
    Unsupported,
}

impl<T> ProgramLinkResolution<T> {
    pub fn map<U>(self, f: impl FnOnce(T) -> U) -> ProgramLinkResolution<U> {
        match self {
            Self::Resolved(value) => ProgramLinkResolution::Resolved(f(value)),
            Self::Missing => ProgramLinkResolution::Missing,
            Self::Cyclic => ProgramLinkResolution::Cyclic,
            Self::Ambiguous => ProgramLinkResolution::Ambiguous,
            Self::Unsupported => ProgramLinkResolution::Unsupported,
        }
    }

    pub fn as_ref(&self) -> ProgramLinkResolution<&T> {
        match self {
            Self::Resolved(value) => ProgramLinkResolution::Resolved(value),
            Self::Missing => ProgramLinkResolution::Missing,
            Self::Cyclic => ProgramLinkResolution::Cyclic,
            Self::Ambiguous => ProgramLinkResolution::Ambiguous,
            Self::Unsupported => ProgramLinkResolution::Unsupported,
        }
    }

    pub fn failure<U>(&self) -> Option<ProgramLinkResolution<U>> {
        match self {
            Self::Resolved(_) => None,
            Self::Missing => Some(ProgramLinkResolution::Missing),
            Self::Cyclic => Some(ProgramLinkResolution::Cyclic),
            Self::Ambiguous => Some(ProgramLinkResolution::Ambiguous),
            Self::Unsupported => Some(ProgramLinkResolution::Unsupported),
        }
    }
}

/// The import form written in a source chunk.
#[derive(Debug, Clone, Eq, PartialEq)]
pub enum ProgramImportKind {
    Named { imported: String },
    Default,
    Namespace,
}

impl ProgramImportKind {
    pub fn imported_name(&self) -> Option<&str> {
        match self {
            Self::Named { imported } => Some(imported),
            Self::Default => Some("default"),
            Self::Namespace => None,
        }
    }
}

/// A normalized import edge. `provider` is resolved by the artifact layer
/// from the manifest's source specifier and entry-file indexes.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProgramImportLink {
    pub local: ProgramBindingId,
    pub source_specifier: String,
    pub kind: ProgramImportKind,
    pub provider: ProgramLinkResolution<ChunkId>,
}

/// A normalized export edge. Explicit exports take precedence over star
/// exports; multiple explicit declarations or distinct star origins remain
/// ambiguous.
#[derive(Debug, Clone, Eq, PartialEq)]
pub enum ProgramExportLink {
    Local {
        export: ProgramExportId,
        binding: ProgramBindingId,
    },
    DefaultValue {
        export: ProgramExportId,
        body_index: usize,
    },
    ReExport {
        export: ProgramExportId,
        provider: ProgramLinkResolution<ChunkId>,
        imported: String,
    },
    NamespaceReExport {
        export: ProgramExportId,
        provider: ProgramLinkResolution<ChunkId>,
    },
    StarReExport {
        provider: ProgramLinkResolution<ChunkId>,
    },
}

/// One source-order module request, including requests with no imported
/// bindings and requests introduced by re-exports. Binding resolution alone
/// cannot reconstruct the ESM evaluation graph.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProgramModuleRequest {
    pub source_specifier: String,
    pub provider: ProgramLinkResolution<ChunkId>,
}

/// Whether this module's source supports synchronous ESM evaluation. An
/// unsupported loader or top-level await cannot silently become a ready body.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum ProgramEvaluationKind {
    SynchronousEsm,
    Unsupported,
}

/// All normalized import and export links for one source chunk.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProgramChunkLinks {
    pub chunk: ChunkId,
    pub evaluation: ProgramEvaluationKind,
    pub requests: Vec<ProgramModuleRequest>,
    pub imports: Vec<ProgramImportLink>,
    pub exports: Vec<ProgramExportLink>,
}

/// The identity reached after resolving an export or import.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum ProgramExportOrigin {
    Binding(ProgramBindingId),
    /// An anonymous default export is a real module export cell but has no
    /// source binding spelling. `body_index` keeps its parser origin local to
    /// its owning chunk without carrying an SWC span across chunks.
    DefaultValue {
        chunk: ChunkId,
        body_index: usize,
    },
    Namespace {
        chunk: ChunkId,
    },
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedProgramExport {
    pub requested: ProgramExportId,
    /// The deepest named export that supplied the value. Namespace and
    /// anonymous-default values can have no named defining export.
    pub defining_export: Option<ProgramExportId>,
    pub origin: ProgramExportOrigin,
    /// Export names traversed from the request to the defining export.
    pub route: Vec<ProgramExportId>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedProgramImport {
    pub local: ProgramBindingId,
    pub source_specifier: String,
    pub kind: ProgramImportKind,
    pub requested_export: Option<ProgramExportId>,
    pub defining_export: Option<ProgramExportId>,
    pub origin: ProgramExportOrigin,
    pub route: Vec<ProgramExportId>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
enum ExplicitExport {
    Local(ProgramBindingId),
    DefaultValue {
        chunk: ChunkId,
        body_index: usize,
    },
    ReExport {
        provider: ProgramLinkResolution<ChunkId>,
        imported: String,
    },
    NamespaceReExport(ProgramLinkResolution<ChunkId>),
    Conflict,
}

/// Immutable program-wide resolution facts, built from one configured input
/// program. This index contains no parsed AST and does not merge chunk bodies.
#[derive(Debug, Clone, Default)]
pub struct ProgramLinkIndex {
    evaluation: BTreeMap<ChunkId, ProgramEvaluationKind>,
    requests: BTreeMap<ChunkId, Vec<ProgramModuleRequest>>,
    imports: BTreeMap<ProgramBindingId, ProgramImportLink>,
    duplicate_imports: BTreeSet<ProgramBindingId>,
    explicit_exports: BTreeMap<ProgramExportId, ExplicitExport>,
    stars: BTreeMap<ChunkId, Vec<ProgramLinkResolution<ChunkId>>>,
    chunks: BTreeSet<ChunkId>,
}

#[derive(Debug, Clone)]
struct InternalResolution {
    defining_export: Option<ProgramExportId>,
    origin: ProgramExportOrigin,
    route: Vec<ProgramExportId>,
}

impl ProgramLinkIndex {
    pub fn new(chunks: impl IntoIterator<Item = ProgramChunkLinks>) -> Self {
        let mut index = Self::default();
        for chunk in chunks {
            index.chunks.insert(chunk.chunk);
            index.evaluation.insert(chunk.chunk, chunk.evaluation);
            index.requests.insert(chunk.chunk, chunk.requests);
            for import in chunk.imports {
                let local = import.local.clone();
                if index.imports.insert(local.clone(), import).is_some() {
                    index.duplicate_imports.insert(local);
                }
            }
            for export in chunk.exports {
                match export {
                    ProgramExportLink::Local { export, binding } => {
                        index.insert_explicit(export, ExplicitExport::Local(binding));
                    }
                    ProgramExportLink::DefaultValue { export, body_index } => {
                        index.insert_explicit(
                            export.clone(),
                            ExplicitExport::DefaultValue {
                                chunk: export.chunk,
                                body_index,
                            },
                        );
                    }
                    ProgramExportLink::ReExport {
                        export,
                        provider,
                        imported,
                    } => {
                        index.insert_explicit(
                            export,
                            ExplicitExport::ReExport { provider, imported },
                        );
                    }
                    ProgramExportLink::NamespaceReExport { export, provider } => {
                        index.insert_explicit(export, ExplicitExport::NamespaceReExport(provider));
                    }
                    ProgramExportLink::StarReExport { provider } => {
                        index.stars.entry(chunk.chunk).or_default().push(provider);
                    }
                }
            }
        }
        index
    }

    fn insert_explicit(&mut self, export: ProgramExportId, target: ExplicitExport) {
        match self.explicit_exports.entry(export) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(target);
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                entry.insert(ExplicitExport::Conflict);
            }
        }
    }

    pub fn chunks(&self) -> impl Iterator<Item = ChunkId> + '_ {
        self.chunks.iter().copied()
    }

    /// Source-order dependencies used by module evaluation. These are not
    /// terminal binding origins: a re-export barrel itself still evaluates.
    pub fn module_requests(&self, chunk: ChunkId) -> Option<&[ProgramModuleRequest]> {
        self.requests.get(&chunk).map(Vec::as_slice)
    }

    pub fn evaluation_kind(&self, chunk: ChunkId) -> Option<ProgramEvaluationKind> {
        self.evaluation.get(&chunk).copied()
    }

    /// The source ESM evaluation order for one closed synchronous entry.
    /// Back-edges are instantiated before evaluation and do not re-enter a
    /// running body. Unknown requests prevent a closed schedule; no partial
    /// order is returned as evidence that a provider has initialized.
    pub fn evaluation_order(&self, entry: ChunkId) -> ProgramLinkResolution<Vec<ChunkId>> {
        let mut active = BTreeSet::new();
        let mut evaluated = BTreeSet::new();
        let mut order = Vec::new();
        let mut stack = vec![(entry, false)];
        while let Some((chunk, finish)) = stack.pop() {
            if finish {
                active.remove(&chunk);
                if evaluated.insert(chunk) {
                    order.push(chunk);
                }
                continue;
            }
            if evaluated.contains(&chunk) || active.contains(&chunk) {
                continue;
            }
            match self.evaluation_kind(chunk) {
                None => return ProgramLinkResolution::Missing,
                Some(ProgramEvaluationKind::Unsupported) => {
                    return ProgramLinkResolution::Unsupported;
                }
                Some(ProgramEvaluationKind::SynchronousEsm) => {}
            }
            active.insert(chunk);
            stack.push((chunk, true));
            for request in self.module_requests(chunk).unwrap_or_default().iter().rev() {
                match &request.provider {
                    ProgramLinkResolution::Resolved(provider) => stack.push((*provider, false)),
                    failure => return failure.failure().expect("an unresolved module request"),
                }
            }
        }
        ProgramLinkResolution::Resolved(order)
    }

    pub fn import_links(&self, chunk: ChunkId) -> impl Iterator<Item = &ProgramImportLink> + '_ {
        self.imports.values().filter(move |edge| {
            edge.local.chunk == chunk && !self.duplicate_imports.contains(&edge.local)
        })
    }

    pub fn import_link(&self, importer: ChunkId, local: &str) -> Option<&ProgramImportLink> {
        let binding = ProgramBindingId::new(importer, local);
        (!self.duplicate_imports.contains(&binding))
            .then(|| self.imports.get(&binding))
            .flatten()
    }

    /// Resolve one named/default import to its canonical origin. Re-export
    /// chains are followed through local aliases and `export ... from` edges.
    pub fn resolve_import(
        &self,
        importer: ChunkId,
        local: &str,
    ) -> ProgramLinkResolution<ResolvedProgramImport> {
        let binding = ProgramBindingId::new(importer, local);
        let mut active_exports = BTreeSet::new();
        let mut active_imports = BTreeSet::new();
        self.resolve_import_inner(&binding, &mut active_exports, &mut active_imports)
    }

    /// Resolve a statically named member of an imported namespace, including
    /// namespaces that were re-exported under a named/default alias.
    pub fn resolve_namespace_member(
        &self,
        importer: ChunkId,
        namespace_local: &str,
        member: &str,
    ) -> ProgramLinkResolution<ResolvedProgramExport> {
        let imported = self.resolve_import(importer, namespace_local);
        let imported = match imported {
            ProgramLinkResolution::Resolved(imported) => imported,
            failure => {
                return failure
                    .failure()
                    .unwrap_or(ProgramLinkResolution::Unsupported);
            }
        };
        let ProgramExportOrigin::Namespace { chunk } = imported.origin else {
            return ProgramLinkResolution::Unsupported;
        };
        self.resolve_export(chunk, member)
    }

    pub fn resolve_export(
        &self,
        chunk: ChunkId,
        export: &str,
    ) -> ProgramLinkResolution<ResolvedProgramExport> {
        let mut active_exports = BTreeSet::new();
        let mut active_imports = BTreeSet::new();
        self.resolve_export_inner(
            ProgramExportId::new(chunk, export),
            &mut active_exports,
            &mut active_imports,
        )
        .map(|resolved| ResolvedProgramExport {
            requested: ProgramExportId::new(chunk, export),
            defining_export: resolved.defining_export,
            origin: resolved.origin,
            route: resolved.route,
        })
    }

    /// Return the statically enumerable names visible through a module
    /// namespace. A missing, cyclic, or unsupported star edge makes the
    /// namespace surface incomplete, so consumers must not treat a partial
    /// list as exhaustive.
    pub fn export_names(&self, chunk: ChunkId) -> ProgramLinkResolution<Vec<String>> {
        let mut active = BTreeSet::new();
        self.export_names_inner(chunk, &mut active)
            .map(|names| names.into_iter().collect())
    }

    fn export_names_inner(
        &self,
        chunk: ChunkId,
        active: &mut BTreeSet<ChunkId>,
    ) -> ProgramLinkResolution<BTreeSet<String>> {
        if !self.chunks.contains(&chunk) {
            return ProgramLinkResolution::Unsupported;
        }
        if !active.insert(chunk) {
            return ProgramLinkResolution::Cyclic;
        }
        let mut names: BTreeSet<_> = self
            .explicit_exports
            .keys()
            .filter(|export| export.chunk == chunk)
            .map(|export| export.name.clone())
            .collect();
        let mut failure: Option<ProgramLinkResolution<BTreeSet<String>>> = None;
        for provider in self.stars.get(&chunk).into_iter().flatten() {
            let provider = match provider {
                ProgramLinkResolution::Resolved(provider) => *provider,
                other => {
                    failure = Some(combine_link_failures(failure, other));
                    continue;
                }
            };
            match self.export_names_inner(provider, active) {
                ProgramLinkResolution::Resolved(provider_names) => {
                    names.extend(provider_names.into_iter().filter(|name| name != "default"));
                }
                other => failure = Some(combine_link_failures(failure, &other)),
            }
        }
        active.remove(&chunk);
        match failure {
            Some(failure) => failure
                .failure()
                .unwrap_or(ProgramLinkResolution::Unsupported),
            None => ProgramLinkResolution::Resolved(names),
        }
    }

    fn resolve_import_inner(
        &self,
        binding: &ProgramBindingId,
        active_exports: &mut BTreeSet<ProgramExportId>,
        active_imports: &mut BTreeSet<ProgramBindingId>,
    ) -> ProgramLinkResolution<ResolvedProgramImport> {
        if self.duplicate_imports.contains(binding) {
            return ProgramLinkResolution::Ambiguous;
        }
        let Some(edge) = self.imports.get(binding) else {
            return ProgramLinkResolution::Missing;
        };
        let provider = match &edge.provider {
            ProgramLinkResolution::Resolved(provider) => *provider,
            failure => {
                return failure
                    .failure()
                    .unwrap_or(ProgramLinkResolution::Unsupported);
            }
        };
        if !self.chunks.contains(&provider) {
            return ProgramLinkResolution::Unsupported;
        }
        if !active_imports.insert(binding.clone()) {
            return ProgramLinkResolution::Cyclic;
        }
        let result = match &edge.kind {
            ProgramImportKind::Namespace => {
                ProgramLinkResolution::Resolved(ResolvedProgramImport {
                    local: binding.clone(),
                    source_specifier: edge.source_specifier.clone(),
                    kind: edge.kind.clone(),
                    requested_export: None,
                    defining_export: None,
                    origin: ProgramExportOrigin::Namespace { chunk: provider },
                    route: Vec::new(),
                })
            }
            ProgramImportKind::Named { imported } => self
                .resolve_export_inner(
                    ProgramExportId::new(provider, imported),
                    active_exports,
                    active_imports,
                )
                .map(|resolved| ResolvedProgramImport {
                    local: binding.clone(),
                    source_specifier: edge.source_specifier.clone(),
                    kind: edge.kind.clone(),
                    requested_export: Some(ProgramExportId::new(provider, imported)),
                    defining_export: resolved.defining_export,
                    origin: resolved.origin,
                    route: resolved.route,
                }),
            ProgramImportKind::Default => self
                .resolve_export_inner(
                    ProgramExportId::new(provider, "default"),
                    active_exports,
                    active_imports,
                )
                .map(|resolved| ResolvedProgramImport {
                    local: binding.clone(),
                    source_specifier: edge.source_specifier.clone(),
                    kind: edge.kind.clone(),
                    requested_export: Some(ProgramExportId::new(provider, "default")),
                    defining_export: resolved.defining_export,
                    origin: resolved.origin,
                    route: resolved.route,
                }),
        };
        active_imports.remove(binding);
        result
    }

    fn resolve_export_inner(
        &self,
        requested: ProgramExportId,
        active_exports: &mut BTreeSet<ProgramExportId>,
        active_imports: &mut BTreeSet<ProgramBindingId>,
    ) -> ProgramLinkResolution<InternalResolution> {
        if !self.chunks.contains(&requested.chunk) {
            return ProgramLinkResolution::Unsupported;
        }
        if !active_exports.insert(requested.clone()) {
            return ProgramLinkResolution::Cyclic;
        }
        let result = if let Some(explicit) = self.explicit_exports.get(&requested) {
            self.resolve_explicit_export(&requested, explicit, active_exports, active_imports)
        } else if requested.name == "default" {
            ProgramLinkResolution::Missing
        } else {
            self.resolve_star_export(&requested, active_exports, active_imports)
        };
        active_exports.remove(&requested);
        result
    }

    fn resolve_explicit_export(
        &self,
        requested: &ProgramExportId,
        explicit: &ExplicitExport,
        active_exports: &mut BTreeSet<ProgramExportId>,
        active_imports: &mut BTreeSet<ProgramBindingId>,
    ) -> ProgramLinkResolution<InternalResolution> {
        match explicit {
            ExplicitExport::Local(binding) => self
                .resolve_binding(binding, active_exports, active_imports)
                .map(|mut resolved| {
                    if resolved.defining_export.is_none() {
                        resolved.defining_export = Some(requested.clone());
                    }
                    resolved.route.insert(0, requested.clone());
                    resolved
                }),
            ExplicitExport::DefaultValue { chunk, body_index } => {
                ProgramLinkResolution::Resolved(InternalResolution {
                    defining_export: Some(requested.clone()),
                    origin: ProgramExportOrigin::DefaultValue {
                        chunk: *chunk,
                        body_index: *body_index,
                    },
                    route: vec![requested.clone()],
                })
            }
            ExplicitExport::ReExport { provider, imported } => {
                let provider = match provider {
                    ProgramLinkResolution::Resolved(provider) => *provider,
                    failure => {
                        return failure
                            .failure()
                            .unwrap_or(ProgramLinkResolution::Unsupported);
                    }
                };
                if !self.chunks.contains(&provider) {
                    return ProgramLinkResolution::Unsupported;
                }
                self.resolve_export_inner(
                    ProgramExportId::new(provider, imported),
                    active_exports,
                    active_imports,
                )
                .map(|mut resolved| {
                    resolved.route.insert(0, requested.clone());
                    resolved
                })
            }
            ExplicitExport::NamespaceReExport(provider) => {
                let provider = match provider {
                    ProgramLinkResolution::Resolved(provider) => *provider,
                    failure => {
                        return failure
                            .failure()
                            .unwrap_or(ProgramLinkResolution::Unsupported);
                    }
                };
                if !self.chunks.contains(&provider) {
                    return ProgramLinkResolution::Unsupported;
                }
                ProgramLinkResolution::Resolved(InternalResolution {
                    defining_export: Some(requested.clone()),
                    origin: ProgramExportOrigin::Namespace { chunk: provider },
                    route: vec![requested.clone()],
                })
            }
            ExplicitExport::Conflict => ProgramLinkResolution::Ambiguous,
        }
    }

    fn resolve_binding(
        &self,
        binding: &ProgramBindingId,
        active_exports: &mut BTreeSet<ProgramExportId>,
        active_imports: &mut BTreeSet<ProgramBindingId>,
    ) -> ProgramLinkResolution<InternalResolution> {
        if self.duplicate_imports.contains(binding) {
            return ProgramLinkResolution::Ambiguous;
        }
        if self.imports.contains_key(binding) {
            if !active_imports.insert(binding.clone()) {
                return ProgramLinkResolution::Cyclic;
            }
            let edge = &self.imports[binding];
            let result = match &edge.provider {
                ProgramLinkResolution::Resolved(provider) if self.chunks.contains(provider) => {
                    match &edge.kind {
                        ProgramImportKind::Namespace => {
                            ProgramLinkResolution::Resolved(InternalResolution {
                                defining_export: None,
                                origin: ProgramExportOrigin::Namespace { chunk: *provider },
                                route: Vec::new(),
                            })
                        }
                        ProgramImportKind::Named { imported } => self.resolve_export_inner(
                            ProgramExportId::new(*provider, imported),
                            active_exports,
                            active_imports,
                        ),
                        ProgramImportKind::Default => self.resolve_export_inner(
                            ProgramExportId::new(*provider, "default"),
                            active_exports,
                            active_imports,
                        ),
                    }
                }
                ProgramLinkResolution::Resolved(_) => ProgramLinkResolution::Unsupported,
                ProgramLinkResolution::Missing => ProgramLinkResolution::Missing,
                ProgramLinkResolution::Cyclic => ProgramLinkResolution::Cyclic,
                ProgramLinkResolution::Ambiguous => ProgramLinkResolution::Ambiguous,
                ProgramLinkResolution::Unsupported => ProgramLinkResolution::Unsupported,
            };
            active_imports.remove(binding);
            return result;
        }
        if !self.chunks.contains(&binding.chunk) {
            return ProgramLinkResolution::Unsupported;
        }
        ProgramLinkResolution::Resolved(InternalResolution {
            defining_export: None,
            origin: ProgramExportOrigin::Binding(binding.clone()),
            route: Vec::new(),
        })
    }

    fn resolve_star_export(
        &self,
        requested: &ProgramExportId,
        active_exports: &mut BTreeSet<ProgramExportId>,
        active_imports: &mut BTreeSet<ProgramBindingId>,
    ) -> ProgramLinkResolution<InternalResolution> {
        let mut resolved = Vec::<InternalResolution>::new();
        let mut unresolved = Vec::new();
        for provider in self.stars.get(&requested.chunk).into_iter().flatten() {
            let ProgramLinkResolution::Resolved(provider) = provider else {
                unresolved.push(provider.clone().map(|_| ()));
                continue;
            };
            if !self.chunks.contains(provider) {
                unresolved.push(ProgramLinkResolution::Unsupported);
                continue;
            }
            match self.resolve_export_inner(
                ProgramExportId::new(*provider, &requested.name),
                active_exports,
                active_imports,
            ) {
                ProgramLinkResolution::Resolved(mut candidate) => {
                    candidate.route.insert(0, requested.clone());
                    resolved.push(candidate);
                }
                other => unresolved.push(other.map(|_| ())),
            }
        }

        if let Some(first) = resolved.first() {
            if resolved
                .iter()
                .any(|candidate| candidate.origin != first.origin)
            {
                return ProgramLinkResolution::Ambiguous;
            }
            // A cyclic, unsupported, or ambiguous sibling can hide another
            // distinct provider. Do not select the visible candidate.
            if unresolved.iter().any(|result| {
                matches!(
                    result,
                    ProgramLinkResolution::Cyclic
                        | ProgramLinkResolution::Ambiguous
                        | ProgramLinkResolution::Unsupported
                )
            }) {
                return conservative_unresolved(&unresolved);
            }
            return ProgramLinkResolution::Resolved(first.clone());
        }
        conservative_unresolved(&unresolved)
    }
}

fn conservative_unresolved<T>(
    unresolved: &[ProgramLinkResolution<T>],
) -> ProgramLinkResolution<InternalResolution> {
    if unresolved
        .iter()
        .any(|result| matches!(result, ProgramLinkResolution::Ambiguous))
    {
        ProgramLinkResolution::Ambiguous
    } else if unresolved
        .iter()
        .any(|result| matches!(result, ProgramLinkResolution::Cyclic))
    {
        ProgramLinkResolution::Cyclic
    } else if unresolved
        .iter()
        .any(|result| matches!(result, ProgramLinkResolution::Unsupported))
    {
        ProgramLinkResolution::Unsupported
    } else {
        ProgramLinkResolution::Missing
    }
}

fn combine_link_failures<T, U>(
    previous: Option<ProgramLinkResolution<T>>,
    next: &ProgramLinkResolution<U>,
) -> ProgramLinkResolution<T> {
    fn priority<T>(failure: &ProgramLinkResolution<T>) -> u8 {
        match failure {
            ProgramLinkResolution::Resolved(_) => 0,
            ProgramLinkResolution::Missing => 1,
            ProgramLinkResolution::Unsupported => 2,
            ProgramLinkResolution::Cyclic => 3,
            ProgramLinkResolution::Ambiguous => 4,
        }
    }

    match previous {
        Some(previous) if priority(&previous) >= priority(next) => previous,
        _ => next.failure().unwrap_or(ProgramLinkResolution::Unsupported),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn local(chunk: usize, name: &str) -> ProgramBindingId {
        ProgramBindingId::new(ChunkId(chunk), name)
    }

    fn export(chunk: usize, name: &str) -> ProgramExportId {
        ProgramExportId::new(ChunkId(chunk), name)
    }

    fn provider(chunk: usize) -> ProgramLinkResolution<ChunkId> {
        ProgramLinkResolution::Resolved(ChunkId(chunk))
    }

    fn requested_module(chunk: usize, providers: &[usize]) -> ProgramChunkLinks {
        ProgramChunkLinks {
            chunk: ChunkId(chunk),
            evaluation: ProgramEvaluationKind::SynchronousEsm,
            requests: providers
                .iter()
                .map(|provider| ProgramModuleRequest {
                    source_specifier: format!("./{provider}.js"),
                    provider: ProgramLinkResolution::Resolved(ChunkId(*provider)),
                })
                .collect(),
            imports: Vec::new(),
            exports: Vec::new(),
        }
    }

    #[test]
    fn source_schedule_preserves_request_order_and_evaluates_shared_providers_once() {
        let index = ProgramLinkIndex::new([
            requested_module(0, &[2, 1]),
            requested_module(1, &[3]),
            requested_module(2, &[3]),
            requested_module(3, &[]),
        ]);
        assert_eq!(
            index.evaluation_order(ChunkId(0)),
            ProgramLinkResolution::Resolved(vec![ChunkId(3), ChunkId(2), ChunkId(1), ChunkId(0)])
        );
        // Re-rooting the same ESM graph yields its own evaluation schedule.
        assert_eq!(
            index.evaluation_order(ChunkId(1)),
            ProgramLinkResolution::Resolved(vec![ChunkId(3), ChunkId(1)])
        );
    }

    #[test]
    fn source_schedule_distinguishes_a_back_edge_from_an_initialized_module() {
        let index = ProgramLinkIndex::new([requested_module(0, &[1]), requested_module(1, &[0])]);
        assert_eq!(
            index.evaluation_order(ChunkId(0)),
            ProgramLinkResolution::Resolved(vec![ChunkId(1), ChunkId(0)])
        );
        assert_eq!(
            index.evaluation_order(ChunkId(1)),
            ProgramLinkResolution::Resolved(vec![ChunkId(0), ChunkId(1)])
        );
    }

    #[test]
    fn source_schedule_never_uses_an_unknown_or_async_provider_as_a_ready_prefix() {
        let mut module = requested_module(0, &[]);
        module.requests.push(ProgramModuleRequest {
            source_specifier: "external".into(),
            provider: ProgramLinkResolution::Unsupported,
        });
        assert_eq!(
            ProgramLinkIndex::new([module]).evaluation_order(ChunkId(0)),
            ProgramLinkResolution::Unsupported
        );
        let mut module = requested_module(0, &[]);
        module.evaluation = ProgramEvaluationKind::Unsupported;
        assert_eq!(
            ProgramLinkIndex::new([module]).evaluation_order(ChunkId(0)),
            ProgramLinkResolution::Unsupported
        );
        assert_eq!(
            ProgramLinkIndex::new([requested_module(0, &[1])]).evaluation_order(ChunkId(0)),
            ProgramLinkResolution::Missing
        );
    }

    #[test]
    fn resolves_import_alias_reexport_chain_and_namespace_members() {
        let index = ProgramLinkIndex::new([
            ProgramChunkLinks {
                chunk: ChunkId(0),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: Vec::new(),
                exports: vec![
                    ProgramExportLink::Local {
                        export: export(0, "value"),
                        binding: local(0, "value"),
                    },
                    ProgramExportLink::DefaultValue {
                        export: export(0, "default"),
                        body_index: 3,
                    },
                ],
            },
            ProgramChunkLinks {
                chunk: ChunkId(1),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: vec![
                    ProgramImportLink {
                        local: local(1, "alias"),
                        source_specifier: "./a".into(),
                        kind: ProgramImportKind::Named {
                            imported: "value".into(),
                        },
                        provider: provider(0),
                    },
                    ProgramImportLink {
                        local: local(1, "default_alias"),
                        source_specifier: "./a".into(),
                        kind: ProgramImportKind::Default,
                        provider: provider(0),
                    },
                    ProgramImportLink {
                        local: local(1, "namespace"),
                        source_specifier: "./a".into(),
                        kind: ProgramImportKind::Namespace,
                        provider: provider(0),
                    },
                ],
                exports: vec![
                    ProgramExportLink::Local {
                        export: export(1, "renamed"),
                        binding: local(1, "alias"),
                    },
                    ProgramExportLink::ReExport {
                        export: export(1, "default_alias"),
                        provider: provider(0),
                        imported: "default".into(),
                    },
                    ProgramExportLink::NamespaceReExport {
                        export: export(1, "all_a"),
                        provider: provider(0),
                    },
                ],
            },
            ProgramChunkLinks {
                chunk: ChunkId(2),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: vec![ProgramImportLink {
                    local: local(2, "through_reexport"),
                    source_specifier: "./b".into(),
                    kind: ProgramImportKind::Named {
                        imported: "renamed".into(),
                    },
                    provider: provider(1),
                }],
                exports: Vec::new(),
            },
        ]);

        let ProgramLinkResolution::Resolved(import) =
            index.resolve_import(ChunkId(2), "through_reexport")
        else {
            panic!("reexport chain should resolve");
        };
        assert_eq!(
            import.origin,
            ProgramExportOrigin::Binding(local(0, "value"))
        );
        assert_eq!(import.defining_export, Some(export(0, "value")));
        assert_eq!(import.route, vec![export(1, "renamed"), export(0, "value")]);

        let ProgramLinkResolution::Resolved(default) =
            index.resolve_export(ChunkId(1), "default_alias")
        else {
            panic!("default reexport should resolve");
        };
        assert_eq!(
            default.origin,
            ProgramExportOrigin::DefaultValue {
                chunk: ChunkId(0),
                body_index: 3,
            }
        );

        let ProgramLinkResolution::Resolved(member) =
            index.resolve_namespace_member(ChunkId(1), "namespace", "value")
        else {
            panic!("namespace member should resolve");
        };
        assert_eq!(
            member.origin,
            ProgramExportOrigin::Binding(local(0, "value"))
        );
        assert_eq!(
            index.export_names(ChunkId(1)),
            ProgramLinkResolution::Resolved(vec![
                "all_a".into(),
                "default_alias".into(),
                "renamed".into(),
            ])
        );
    }

    #[test]
    fn retains_missing_cycle_and_ambiguous_outcomes() {
        let index = ProgramLinkIndex::new([
            ProgramChunkLinks {
                chunk: ChunkId(0),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: vec![ProgramImportLink {
                    local: local(0, "missing"),
                    source_specifier: "./b".into(),
                    kind: ProgramImportKind::Named {
                        imported: "absent".into(),
                    },
                    provider: provider(1),
                }],
                exports: vec![ProgramExportLink::ReExport {
                    export: export(0, "cycle"),
                    provider: provider(1),
                    imported: "cycle".into(),
                }],
            },
            ProgramChunkLinks {
                chunk: ChunkId(1),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: Vec::new(),
                exports: vec![ProgramExportLink::ReExport {
                    export: export(1, "cycle"),
                    provider: provider(0),
                    imported: "cycle".into(),
                }],
            },
            ProgramChunkLinks {
                chunk: ChunkId(2),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: Vec::new(),
                exports: vec![ProgramExportLink::Local {
                    export: export(2, "same"),
                    binding: local(2, "same"),
                }],
            },
            ProgramChunkLinks {
                chunk: ChunkId(3),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: Vec::new(),
                exports: vec![ProgramExportLink::Local {
                    export: export(3, "same"),
                    binding: local(3, "same"),
                }],
            },
            ProgramChunkLinks {
                chunk: ChunkId(4),
                evaluation: ProgramEvaluationKind::SynchronousEsm,
                requests: Vec::new(),
                imports: Vec::new(),
                exports: vec![
                    ProgramExportLink::StarReExport {
                        provider: provider(2),
                    },
                    ProgramExportLink::StarReExport {
                        provider: provider(3),
                    },
                ],
            },
        ]);

        assert_eq!(
            index.resolve_import(ChunkId(0), "missing"),
            ProgramLinkResolution::Missing
        );
        assert_eq!(
            index.resolve_export(ChunkId(0), "cycle"),
            ProgramLinkResolution::Cyclic
        );
        assert_eq!(
            index.resolve_export(ChunkId(4), "same"),
            ProgramLinkResolution::Ambiguous
        );
    }
}
