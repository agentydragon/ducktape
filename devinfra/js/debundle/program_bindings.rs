//! Source linkage shared by program-level semantic analyses.
//!
//! Export references and local cells have separate identities. Two exports of
//! one local share its binding; an imported local links to its provider export.
//! A local initialized from an import remains a different cell. This graph
//! supplies identity, never stability, availability, purity or a value proof.

use std::collections::BTreeMap;

/// Source chunk name; destination module names belong to the output graph.
pub type SourceModuleKey = String;

/// A named/default import resolved by the artifact's existing path index.
/// Namespace members remain outside this representation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedImport {
    pub module: SourceModuleKey,
    pub export: String,
    pub source_specifier: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ExportId(usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct BindingId(usize);

/// Borrow the already prepared linkage; constructing the graph never walks ASTs.
pub struct ModuleBindings<'a> {
    pub module: &'a str,
    pub imports: &'a BTreeMap<String, ResolvedImport>,
    pub exports: &'a BTreeMap<String, String>,
}

struct Export {
    name: (SourceModuleKey, String),
    local: Option<BindingId>,
    importers: Vec<BindingId>,
}

struct Binding {
    name: (SourceModuleKey, String),
    imported: Option<ExportId>,
}

/// One finite graph for an analysis. IDs are private to this graph and are not
/// serialized; saved summaries retain their source names as before.
#[derive(Default)]
pub struct ProgramBindings {
    exports: Vec<Export>,
    export_ids: BTreeMap<(SourceModuleKey, String), ExportId>,
    bindings: Vec<Binding>,
    module_bindings: BTreeMap<SourceModuleKey, BTreeMap<String, BindingId>>,
    source_cells: Vec<Option<BindingId>>,
}

impl ProgramBindings {
    pub fn new<'a>(
        modules: impl IntoIterator<Item = ModuleBindings<'a>>,
        extra_exports: impl IntoIterator<Item = (SourceModuleKey, String)>,
    ) -> Self {
        let mut graph = Self::default();
        for module in modules {
            graph
                .module_bindings
                .entry(module.module.to_string())
                .or_default();
            for (local, target) in module.imports {
                let binding = graph.intern_binding(module.module, local);
                let export = graph.intern_export(&target.module, &target.export);
                graph.bindings[binding.0].imported = Some(export);
                graph.exports[export.0].importers.push(binding);
            }
            for (export, local) in module.exports {
                let binding = graph.intern_binding(module.module, local);
                let export = graph.intern_export(module.module, export);
                graph.exports[export.0].local = Some(binding);
            }
        }
        for (module, export) in extra_exports {
            graph.intern_export(&module, &export);
        }
        graph.resolve_source_cells();
        graph
    }

    /// Each binding has at most one import successor. Resolve shared tails
    /// once; a missing provider or a cycle without a local cell stays absent.
    fn resolve_source_cells(&mut self) {
        let mut visited = vec![false; self.bindings.len()];
        self.source_cells = vec![None; self.bindings.len()];
        for start in 0..self.bindings.len() {
            if visited[start] {
                continue;
            }
            let mut path = Vec::new();
            let mut current = BindingId(start);
            let terminal = loop {
                if visited[current.0] {
                    break self.source_cells[current.0];
                }
                visited[current.0] = true;
                path.push(current);
                let Some(export) = self.import_target(current) else {
                    break Some(current);
                };
                let Some(next) = self.local_export(export) else {
                    break None;
                };
                current = next;
            };
            for binding in path {
                self.source_cells[binding.0] = terminal;
            }
        }
    }

    fn intern_export(&mut self, module: &str, name: &str) -> ExportId {
        let key = (module.to_string(), name.to_string());
        if let Some(id) = self.export_ids.get(&key) {
            return *id;
        }
        let id = ExportId(self.exports.len());
        self.exports.push(Export {
            name: key.clone(),
            local: None,
            importers: Vec::new(),
        });
        self.export_ids.insert(key, id);
        id
    }

    fn intern_binding(&mut self, module: &str, name: &str) -> BindingId {
        let locals = self.module_bindings.entry(module.to_string()).or_default();
        if let Some(id) = locals.get(name) {
            return *id;
        }
        let id = BindingId(self.bindings.len());
        self.bindings.push(Binding {
            name: (module.to_string(), name.to_string()),
            imported: None,
        });
        locals.insert(name.to_string(), id);
        id
    }

    pub fn export_id(&self, module: &str, name: &str) -> Option<ExportId> {
        self.export_ids
            .get(&(module.to_string(), name.to_string()))
            .copied()
    }

    pub fn exports(&self) -> impl Iterator<Item = ExportId> + '_ {
        (0..self.exports.len()).map(ExportId)
    }

    pub fn export_name(&self, export: ExportId) -> &(SourceModuleKey, String) {
        &self.exports[export.0].name
    }

    pub fn local_export(&self, export: ExportId) -> Option<BindingId> {
        self.exports[export.0].local
    }

    pub fn binding_name(&self, binding: BindingId) -> &(SourceModuleKey, String) {
        &self.bindings[binding.0].name
    }

    /// Identity only: this is no initialization, stability or value proof.
    pub fn source_cell(&self, binding: BindingId) -> Option<BindingId> {
        self.source_cells[binding.0]
    }

    pub fn import_target(&self, binding: BindingId) -> Option<ExportId> {
        self.bindings[binding.0].imported
    }

    pub fn reexport_target(&self, export: ExportId) -> Option<ExportId> {
        self.import_target(self.local_export(export)?)
    }

    pub fn importers(&self, export: ExportId) -> &[BindingId] {
        &self.exports[export.0].importers
    }

    pub fn modules(&self) -> impl Iterator<Item = &SourceModuleKey> {
        self.module_bindings.keys()
    }

    pub fn imports(&self, module: &str) -> impl Iterator<Item = (BindingId, ExportId)> + '_ {
        self.module_bindings
            .get(module)
            .into_iter()
            .flat_map(|bindings| {
                bindings
                    .values()
                    .filter_map(|binding| Some((*binding, self.import_target(*binding)?)))
            })
    }
}
