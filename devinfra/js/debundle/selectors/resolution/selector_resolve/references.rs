//! What a template's free identifiers name, and narrowing candidate rows by those references.

use std::collections::{BTreeMap, BTreeSet};

use selector_outcome::{Entity, EntityRef, InvalidSelectorError};

use crate::SpecModule;
use crate::collect::{Collected, CollectedRow, Rejection};

/// The spec entity a free template identifier names.
#[derive(Clone)]
pub(crate) enum Referent {
    /// A projected `source_match` entity, by its index in the collection.
    Projected(usize),
    /// A name pin: its binding is the pinned name.
    Pin {
        module_index: usize,
        member_index: usize,
        name: String,
    },
    /// A member pinned by a relational selector: its binding is known only
    /// in the solve.
    Relational {
        module_index: usize,
        member_index: usize,
    },
    /// A fixed member from the base assignment. It may constrain a free
    /// identifier by binding name, but is never a placement target.
    Frozen { binding: String },
    /// A `source_match` rejected before the solve. Its name stays a wildcard.
    Unprojected,
}

/// Names that, unless the chunk declares or imports them at top level, are
/// the runtime's globals. A free template identifier spelled like one
/// matches only that spelling.
const JS_GLOBALS: &[&str] = &[
    "AbortController",
    "Array",
    "ArrayBuffer",
    "Atomics",
    "BigInt",
    "Blob",
    "Boolean",
    "Buffer",
    "DataView",
    "Date",
    "Error",
    "EvalError",
    "Event",
    "EventTarget",
    "FinalizationRegistry",
    "Float32Array",
    "Float64Array",
    "FormData",
    "Function",
    "Headers",
    "Infinity",
    "Int16Array",
    "Int32Array",
    "Int8Array",
    "Intl",
    "JSON",
    "Map",
    "Math",
    "NaN",
    "Number",
    "Object",
    "Promise",
    "Proxy",
    "RangeError",
    "ReferenceError",
    "Reflect",
    "RegExp",
    "Request",
    "Response",
    "Set",
    "SharedArrayBuffer",
    "String",
    "Symbol",
    "SyntaxError",
    "TextDecoder",
    "TextEncoder",
    "TypeError",
    "URIError",
    "URL",
    "URLSearchParams",
    "Uint16Array",
    "Uint32Array",
    "Uint8Array",
    "Uint8ClampedArray",
    "WeakMap",
    "WeakRef",
    "WeakSet",
    "WebAssembly",
    "atob",
    "btoa",
    "cancelAnimationFrame",
    "clearInterval",
    "clearTimeout",
    "console",
    "crypto",
    "decodeURI",
    "decodeURIComponent",
    "document",
    "encodeURI",
    "encodeURIComponent",
    "fetch",
    "globalThis",
    "isFinite",
    "isNaN",
    "localStorage",
    "location",
    "navigator",
    "parseFloat",
    "parseInt",
    "performance",
    "process",
    "queueMicrotask",
    "requestAnimationFrame",
    "sessionStorage",
    "setInterval",
    "setTimeout",
    "structuredClone",
    "undefined",
    "window",
];

/// A collected entity's rows once its template's references narrowed them.
pub(crate) struct Narrowed {
    pub(crate) rows: Vec<CollectedRow>,
    /// Distinct rows before any spec-entity reference narrowed them.
    pub(crate) unreferenced_rows: usize,
    /// Each free identifier naming a spec entity: the name, the module that
    /// exports it, and what it names.
    pub(crate) references: Vec<(String, usize, Referent)>,
}

/// Classifies `entity`'s free identifiers and narrows its rows by them. A
/// name exported in the entity's own module names that export; otherwise a
/// name exported by exactly one module names it, and one exported by several
/// is an authoring error. A name no module exports that spells a runtime
/// global the chunk does not shadow matches only itself. Any other name
/// stays a wildcard.
pub(crate) fn narrow_by_references(
    index: usize,
    entity: &Collected,
    modules: &[SpecModule],
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Result<Narrowed, Rejection> {
    let mut globals = Vec::new();
    let mut references = Vec::new();
    for name in &entity.free {
        match classify(name, entity.module_index, exports, shadowing) {
            Meaning::Reference(_, Referent::Projected(referenced)) if referenced == index => {}
            Meaning::Reference(module_index, referent) => {
                references.push((name.clone(), module_index, referent));
            }
            Meaning::Ambiguous(exporters) => {
                return Err(Rejection::Invalid(
                    InvalidSelectorError::AmbiguousReference {
                        identifier: name.clone(),
                        modules: module_paths(modules, &exporters),
                    },
                ));
            }
            Meaning::Global => globals.push(name),
            Meaning::Wildcard => {}
        }
    }
    let candidates = entity
        .rows
        .iter()
        .filter(|row| {
            globals
                .iter()
                .all(|global| row.free_bindings.get(*global) == Some(*global))
        })
        .cloned()
        .collect::<Vec<_>>();
    let unreferenced_rows = candidates
        .iter()
        .map(|row| &row.places)
        .collect::<BTreeSet<_>>()
        .len();
    // A referenced name the row does not report bound different chunk
    // identifiers in different scopes of the match: no one entity.
    let rows = candidates
        .iter()
        .filter(|row| {
            references.iter().all(|(name, _, referent)| match referent {
                Referent::Unprojected => true,
                Referent::Pin { name: pinned, .. } => row.free_bindings.get(name) == Some(pinned),
                Referent::Frozen { binding } => row.free_bindings.get(name) == Some(binding),
                Referent::Projected(_) | Referent::Relational { .. } => {
                    row.free_bindings.contains_key(name)
                }
            })
        })
        .cloned()
        .collect::<Vec<_>>();
    if rows.is_empty() {
        let with = references
            .iter()
            .filter(|(name, _, referent)| {
                matches!(referent, Referent::Pin { name: pinned, .. }
                    if candidates.iter().all(|row| row.free_bindings.get(name) != Some(pinned)))
                    || matches!(referent, Referent::Frozen { binding }
                        if candidates.iter().all(|row| row.free_bindings.get(name) != Some(binding)))
            })
            .map(|(name, module_index, _)| reference_entity(modules, name, *module_index))
            .collect::<Vec<_>>();
        if !with.is_empty() {
            return Err(Rejection::Conflict(with));
        }
    }
    Ok(Narrowed {
        rows: Rejection::check_count(rows)?,
        unreferenced_rows,
        references,
    })
}

/// What a free template identifier of a template in module `module_index`
/// means (SPEC.md § Matching).
pub(crate) enum Meaning {
    /// The spec entity exported under that name, and the module exporting it.
    Reference(usize, Referent),
    /// Exported by these modules, none of them the template's own.
    Ambiguous(Vec<usize>),
    Global,
    Wildcard,
}

pub(crate) fn classify(
    name: &str,
    module_index: usize,
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Meaning {
    classify_with_local_preference(name, Some(module_index), exports, shadowing)
}

/// Classify with no same-module export preference. This is the canonical
/// resolver view a source-match reference will use after its current module
/// no longer supplies the local preference.
pub(crate) fn classify_without_local_preference(
    name: &str,
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Meaning {
    classify_with_local_preference(name, None, exports, shadowing)
}

fn classify_with_local_preference(
    name: &str,
    preferred_module: Option<usize>,
    exports: &BTreeMap<&str, Vec<(usize, Referent)>>,
    shadowing: &BTreeSet<String>,
) -> Meaning {
    match exports.get(name).map(Vec::as_slice) {
        Some(named) => match (
            preferred_module.and_then(|module_index| {
                named.iter().find(|(exporter, _)| *exporter == module_index)
            }),
            named,
        ) {
            (Some((exporter, referent)), _) | (None, [(exporter, referent)]) => {
                Meaning::Reference(*exporter, referent.clone())
            }
            (None, several) => {
                Meaning::Ambiguous(several.iter().map(|(exporter, _)| *exporter).collect())
            }
        },
        None if JS_GLOBALS.contains(&name) && !shadowing.contains(name) => Meaning::Global,
        None => Meaning::Wildcard,
    }
}

pub(crate) fn module_paths(modules: &[SpecModule], indices: &[usize]) -> Vec<String> {
    indices
        .iter()
        .map(|index| modules[*index].path.clone())
        .collect()
}

/// Narrows every entity's rows by the references whose referent every one of
/// its own rows already places at one binding, until nothing changes, and
/// returns those settled bindings by (collected entity, export name). Only
/// references to entities still open after this reach the solver, so
/// entities referencing a settled one stay in small groups.
pub(crate) fn settle_references(
    collected: &[Collected],
    narrowed: &mut [Result<Narrowed, Rejection>],
    modules: &[SpecModule],
) -> BTreeMap<(usize, String), String> {
    loop {
        let mut settled = BTreeMap::new();
        for (index, (entity, narrowed)) in collected.iter().zip(narrowed.iter()).enumerate() {
            let Ok(narrowed) = narrowed else {
                continue;
            };
            for (position, export_name) in entity.export_names(modules).into_iter().enumerate() {
                let bindings = narrowed
                    .rows
                    .iter()
                    .map(|row| &row.places[position].binding)
                    .collect::<BTreeSet<_>>();
                if let [Some(binding)] = bindings.into_iter().collect::<Vec<_>>().as_slice() {
                    settled.insert((index, export_name), binding.clone());
                }
            }
        }
        let mut changed = false;
        for entry in narrowed.iter_mut() {
            let Ok(narrowed) = entry else {
                continue;
            };
            let Narrowed {
                rows, references, ..
            } = narrowed;
            let agrees = |row: &CollectedRow, name: &String, referent: &Referent| match referent {
                Referent::Projected(referenced) => settled
                    .get(&(*referenced, name.clone()))
                    .is_none_or(|binding| row.free_bindings.get(name) == Some(binding)),
                _ => true,
            };
            let with = references
                .iter()
                .filter(|(name, _, referent)| !rows.iter().any(|row| agrees(row, name, referent)))
                .map(|(name, module_index, _)| reference_entity(modules, name, *module_index))
                .collect::<Vec<_>>();
            let before = rows.len();
            rows.retain(|row| {
                references
                    .iter()
                    .all(|(name, _, referent)| agrees(row, name, referent))
            });
            if rows.len() != before {
                changed = true;
                if rows.is_empty() {
                    *entry = Err(if with.is_empty() {
                        Rejection::NoCandidates
                    } else {
                        Rejection::Conflict(with)
                    });
                }
            }
        }
        if !changed {
            return settled;
        }
    }
}

/// The spec entity `name` names in `modules[module_index]`.
pub(crate) fn reference_entity(
    modules: &[SpecModule],
    name: &str,
    module_index: usize,
) -> EntityRef {
    EntityRef {
        logical_module: modules[module_index].path.clone(),
        entity: Some(Entity::Export(name.to_string())),
    }
}
