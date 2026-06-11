//! Program-level cross-module function-purity oracle.
//!
//! The per-chunk purity classifier ([`crate::purity::ChunkCodeGraph`])
//! treats a call to an imported binding as `unknown_call` — it cannot see
//! into the other module's body. On a real app bundle that is the dominant
//! impurity (every `memo(fn)` / `forwardRef(fn)` factory wrap), and via the
//! dataflow-aware S-chain it over-merges atomic factor units.
//!
//! This oracle closes that gap. Given every module's entry body plus its
//! resolved imports and exports, it computes, per module, the purity verdict
//! for each imported function binding — the map fed to that module's
//! [`ChunkCodeGraph::build_full`] (`imported_purities`).
//!
//! ## Algorithm — greatest fixpoint
//!
//! Purity forms a two-point lattice `Pure ⊐ NotPure`. Every function export
//! starts optimistic (`Pure`); each round rebuilds every module's
//! `ChunkCodeGraph` with the imported purities resolved from the *current*
//! export verdicts, reads each export's local function purity, and **demotes**
//! any that turned out impure. Demotion is monotone (a verdict only ever moves
//! `Pure → NotPure`) over a finite domain, so the iteration converges; the
//! result is the greatest (most-pure) sound fixpoint. Starting optimistic is
//! what lets a function that is pure except for calling a same-cycle peer stay
//! `Pure`.
//!
//! Soundness is conservative by construction: an import whose target module or
//! export is absent (external/vendor module not analyzed, a non-function
//! export, an unresolved re-export) simply gets no entry, so the call stays
//! `unknown_call` exactly as before. The oracle only ever *removes*
//! false-impurity; it never asserts purity it did not derive from a body.

use std::collections::{BTreeMap, BTreeSet};

use swc_ecma_ast::ModuleItem;

use crate::facts::{compute_shadowed_globals, top_level_item_views};
use crate::purity::{ChunkCodeGraph, Purity};

/// Stable key identifying a module in the program (the chunk name).
pub type ModuleKey = String;

/// An import of a single named/default binding, already resolved to the
/// module that defines it and the export read. Namespace imports
/// (`import * as ns`) are intentionally not represented: their call sites are
/// member calls (`ns.foo()`), not the bare-`Ident` callee the imported-purity
/// arm handles.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedImport {
    /// The module that defines the imported binding.
    pub module: ModuleKey,
    /// The export name read from that module.
    pub export: String,
}

/// One module's purity-relevant surface for the oracle.
pub struct ModulePurityFacts<'a> {
    /// The module entry's top-level items (caller-owned parsed AST).
    pub body: &'a [ModuleItem],
    /// Local import binding name → the export it resolves to.
    pub imports: BTreeMap<String, ResolvedImport>,
    /// Export name → the local binding this module re-exports under it.
    pub exports: BTreeMap<String, String>,
}

impl ModulePurityFacts<'_> {
    /// Build this module's `ChunkCodeGraph` with the supplied imported-binding
    /// purities. Only the cross-module map varies across fixpoint rounds; the
    /// declared-purity inputs are not consulted here (the oracle reasons about
    /// inferred body purity, and declared annotations only ever make a binding
    /// *more* pure, so omitting them keeps the verdict conservative).
    fn build_graph(&self, imported: &BTreeMap<String, Purity>) -> ChunkCodeGraph {
        let views = top_level_item_views(self.body);
        let shadowed = compute_shadowed_globals(&views);
        ChunkCodeGraph::build_full(
            &views,
            &shadowed,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &BTreeMap::new(),
            imported,
        )
    }

    /// The imported-binding purities for this module under the current
    /// program-wide export verdicts: each local import bound to a *function*
    /// export of an analyzed module gets that export's verdict; anything
    /// unresolved is omitted (stays `unknown_call`).
    fn imported_purities(
        &self,
        export_purity: &BTreeMap<(ModuleKey, String), Purity>,
    ) -> BTreeMap<String, Purity> {
        self.imports
            .iter()
            .filter_map(|(local, target)| {
                let key = (target.module.clone(), target.export.clone());
                export_purity
                    .get(&key)
                    .map(|purity| (local.clone(), purity.clone()))
            })
            .collect()
    }
}

/// Compute, per module, the imported-binding purity map to feed that module's
/// `ChunkCodeGraph::build_full`. See the module docs for the fixpoint.
///
/// `asserted_pure` carries author-asserted pure exports keyed by defining
/// module (`TransformSpec::chunk_export_purity`). Assertions are trusted
/// axioms: they enter the verdict map as `Pure` even when the export's
/// binding is not a classifiable chunk-top function (interop wrappers,
/// re-exported callables), and the fixpoint never demotes them. An assertion
/// naming a module or export the program doesn't have is reported to stderr
/// as a dangling assertion (likely a typo or a stale entry) and ignored.
pub fn resolve_imported_purities(
    modules: &BTreeMap<ModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<ModuleKey, BTreeSet<String>>,
) -> BTreeMap<ModuleKey, BTreeMap<String, Purity>> {
    // The fixpoint state: the verdict for each *function* export. A
    // `(module, export)` pair is in this map iff `export`'s local binding is a
    // chunk-top function (so calling the import is meaningful); we seed those
    // optimistically with `Pure`. Non-function exports never enter the map, so
    // imports bound to them stay unresolved (`unknown_call`).
    let mut export_purity: BTreeMap<(ModuleKey, String), Purity> = BTreeMap::new();
    for (key, facts) in modules {
        // Build once with no resolved imports to discover which exports are
        // functions; their concrete verdict is refined by the loop below.
        let graph = facts.build_graph(&BTreeMap::new());
        for (export_name, local) in &facts.exports {
            if graph.function_purity(local).is_some() {
                export_purity.insert((key.clone(), export_name.clone()), Purity::Pure);
            }
        }
    }

    // Author-asserted axioms: pinned Pure, exempt from demotion below.
    let mut pinned: BTreeSet<(ModuleKey, String)> = BTreeSet::new();
    for (module, export_names) in asserted_pure {
        for export_name in export_names {
            let known_export = modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export_name));
            if !known_export {
                eprintln!(
                    "cross_module_purity: dangling pure_exports assertion \
                     {module}:{export_name} — no such analyzed module export; ignoring"
                );
                continue;
            }
            export_purity.insert((module.clone(), export_name.clone()), Purity::Pure);
            pinned.insert((module.clone(), export_name.clone()));
        }
    }

    // Greatest-fixpoint: rebuild every module with the current cross-module
    // verdicts and demote any function export that is impure under them. A
    // verdict only moves Pure → NotPure, so this terminates. Modules whose
    // resolved imported map did not change since their last build cannot
    // produce new demotions, so they are skipped — this bounds the total
    // rebuild work to (initial pass) + (one rebuild per demotion-affected
    // module per round) instead of (all modules × rounds).
    let mut last_inputs: BTreeMap<&ModuleKey, BTreeMap<String, Purity>> = BTreeMap::new();
    loop {
        let mut changed = false;
        for (key, facts) in modules {
            let imported = facts.imported_purities(&export_purity);
            if last_inputs.get(key) == Some(&imported) {
                continue;
            }
            let graph = facts.build_graph(&imported);
            last_inputs.insert(key, imported);
            for (export_name, local) in &facts.exports {
                let slot_key = (key.clone(), export_name.clone());
                if !export_purity.contains_key(&slot_key) || pinned.contains(&slot_key) {
                    continue;
                }
                let Some(purity) = graph.function_purity(local) else {
                    continue;
                };
                if export_purity[&slot_key].is_pure() && !purity.is_pure() {
                    export_purity.insert(slot_key, purity);
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }

    modules
        .iter()
        .map(|(key, facts)| (key.clone(), facts.imported_purities(&export_purity)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{FileName, sync::Lrc};
    use swc_ecma_ast::Module;
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn parse(source: &str) -> Module {
        let cm: Lrc<swc_common::SourceMap> = Default::default();
        let fm = cm.new_source_file(FileName::Custom("m.js".into()).into(), source.to_string());
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer).parse_module().unwrap()
    }

    fn facts<'a>(
        module: &'a Module,
        imports: &[(&str, &str, &str)],
        exports: &[(&str, &str)],
    ) -> ModulePurityFacts<'a> {
        ModulePurityFacts {
            body: &module.body,
            imports: imports
                .iter()
                .map(|(local, source, export)| {
                    (
                        (*local).to_string(),
                        ResolvedImport {
                            module: (*source).to_string(),
                            export: (*export).to_string(),
                        },
                    )
                })
                .collect(),
            exports: exports
                .iter()
                .map(|(name, local)| ((*name).to_string(), (*local).to_string()))
                .collect(),
        }
    }

    #[test]
    fn pure_function_import_is_resolved_pure() {
        let app = parse("const C = memo(x);");
        let react = parse("export function memo(t) { return { type: t }; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("memo", "react", "memo")], &[]),
            ),
            ("react".to_string(), facts(&react, &[], &[("memo", "memo")])),
        ]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(resolved["app"]["memo"].is_pure());
    }

    #[test]
    fn impure_function_import_is_resolved_impure() {
        let app = parse("const C = boot(x);");
        let lib = parse("export function boot() { globalSink(); }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("boot", "lib", "boot")], &[]),
            ),
            ("lib".to_string(), facts(&lib, &[], &[("boot", "boot")])),
        ]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(!resolved["app"]["boot"].is_pure());
    }

    #[test]
    fn purity_propagates_across_a_module_chain() {
        // c.base (pure) → b.wrap (calls base) → a imports wrap.
        let a = parse("const W = wrap();");
        let b = parse("import { base } from \"c\";\nexport function wrap() { return base(); }");
        let c = parse("export function base() { return 1; }");
        let modules = BTreeMap::from([
            ("a".to_string(), facts(&a, &[("wrap", "b", "wrap")], &[])),
            (
                "b".to_string(),
                facts(&b, &[("base", "c", "base")], &[("wrap", "wrap")]),
            ),
            ("c".to_string(), facts(&c, &[], &[("base", "base")])),
        ]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(resolved["a"]["wrap"].is_pure());
    }

    #[test]
    fn impurity_propagates_back_across_a_module_chain() {
        // c.base (impure) demotes b.wrap, which demotes a's `wrap` import.
        let a = parse("const W = wrap();");
        let b = parse("import { base } from \"c\";\nexport function wrap() { return base(); }");
        let c = parse("export function base() { globalSink(); }");
        let modules = BTreeMap::from([
            ("a".to_string(), facts(&a, &[("wrap", "b", "wrap")], &[])),
            (
                "b".to_string(),
                facts(&b, &[("base", "c", "base")], &[("wrap", "wrap")]),
            ),
            ("c".to_string(), facts(&c, &[], &[("base", "base")])),
        ]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(!resolved["a"]["wrap"].is_pure());
    }

    #[test]
    fn import_of_an_unanalyzed_module_stays_unknown() {
        // `vendor` is not in the module set (external/opaque) → no entry.
        let app = parse("const C = ext(x);");
        let modules = BTreeMap::from([(
            "app".to_string(),
            facts(&app, &[("ext", "vendor", "ext")], &[]),
        )]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(!resolved["app"].contains_key("ext"));
    }

    #[test]
    fn import_of_a_non_function_export_stays_unknown() {
        // A data export is not callable → no purity entry, stays unknown_call.
        let app = parse("const C = table(x);");
        let lib = parse("export const table = 42;");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("table", "lib", "table")], &[]),
            ),
            ("lib".to_string(), facts(&lib, &[], &[("table", "table")])),
        ]);
        let resolved = resolve_imported_purities(&modules, &BTreeMap::new());
        assert!(!resolved["app"].contains_key("table"));
    }

    #[test]
    fn asserted_pure_export_overrides_inferred_impurity() {
        // `observer` is genuinely impure to the classifier (writes a global),
        // so inference demotes it. An author assertion pins it Pure, and the
        // verdict propagates to the importing chunk.
        let app = parse("const C = observer(x);");
        let lib = parse("export function observer(c) { globalThis.__warned = 1; return c; }");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("observer", "lib", "observer")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("observer", "observer")]),
            ),
        ]);
        // Without the assertion: impure.
        assert!(
            !resolve_imported_purities(&modules, &BTreeMap::new())["app"]["observer"].is_pure()
        );
        // With the assertion on the defining chunk: pure, at every importer.
        let asserted =
            BTreeMap::from([("lib".to_string(), BTreeSet::from(["observer".to_string()]))]);
        assert!(resolve_imported_purities(&modules, &asserted)["app"]["observer"].is_pure());
    }

    #[test]
    fn asserted_pure_export_applies_to_a_non_function_callable() {
        // The export is a re-exported interop value the classifier does not see
        // as a chunk-top function, so inference yields no verdict. The
        // assertion still admits it (author trust), reaching the importer.
        let app = parse("const C = forwardRef(x);");
        let lib = parse("const inner = makeRef();\nexport { inner as forwardRef };");
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                facts(&app, &[("forwardRef", "lib", "forwardRef")], &[]),
            ),
            (
                "lib".to_string(),
                facts(&lib, &[], &[("forwardRef", "inner")]),
            ),
        ]);
        assert!(
            !resolve_imported_purities(&modules, &BTreeMap::new())["app"]
                .contains_key("forwardRef")
        );
        let asserted = BTreeMap::from([(
            "lib".to_string(),
            BTreeSet::from(["forwardRef".to_string()]),
        )]);
        assert!(resolve_imported_purities(&modules, &asserted)["app"]["forwardRef"].is_pure());
    }
}
