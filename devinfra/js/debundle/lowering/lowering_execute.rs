//! Apply a sealed [`CheckedPlan`] to a module's AST.
//!
//! Phase 4a deliverable: rename application. Walks a `Module`
//! and rewrites every `Ident` whose `(Scope::Chunk, to_id())` is
//! in `plan.rename_index` to the resolved final name. Per-module
//! and per-function scope renames are not applied here yet —
//! Phase 6 (naturalizer migration) extends this visitor with
//! scope tracking. Move application (Phase 4b) lives in a
//! sibling pass that consumes `plan.move_index`.

use swc_ecma_ast::{Ident, Module};
use swc_ecma_visit::{VisitMut, VisitMutWith};

use super::lowering_plan::{CheckedPlan, Scope};

/// Rewrite every `Ident` in `module` whose `(Scope::Chunk, to_id())`
/// is in `plan.rename_index` to its resolved final name.
///
/// Walks the whole module tree — declarations and references
/// alike. swc's hygiene means a binding's declaration and its
/// references share the same `Id`, so this visitor handles both
/// in one walk.
pub fn apply_chunk_renames(module: &mut Module, plan: &CheckedPlan) {
    let mut v = ChunkRenameVisitor { plan };
    module.visit_mut_with(&mut v);
}

struct ChunkRenameVisitor<'a> {
    plan: &'a CheckedPlan,
}

impl VisitMut for ChunkRenameVisitor<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(new_name) = self.plan.rename_index.get(&(Scope::Chunk, ident.to_id())) {
            ident.sym = new_name.clone();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::lowering_plan::{
        LoweringOp, LoweringPlan, NamePolicy, Priority, SubmitPolicy,
    };
    use super::*;
    use analysis::ModuleId;
    use js_ast::{parse_js_module_ast, with_swc_globals};
    use std::collections::HashMap;
    use swc_atoms::Atom;
    use swc_common::Mark;
    use swc_ecma_ast::Ident;
    use swc_ecma_transforms_base::resolver;
    use swc_ecma_visit::{Visit, VisitMutWith, VisitWith};

    /// Collect every `Ident` atom that appears in the module, in
    /// source order. Helper for `assert_idents_eq` below.
    struct IdentCollector {
        atoms: Vec<String>,
    }

    impl Visit for IdentCollector {
        fn visit_ident(&mut self, ident: &Ident) {
            self.atoms.push(ident.sym.to_string());
        }
    }

    fn idents(module: &Module) -> Vec<String> {
        let mut c = IdentCollector { atoms: Vec::new() };
        module.visit_with(&mut c);
        c.atoms
    }

    fn first_ident_id(module: &Module, name: &str) -> swc_ecma_ast::Id {
        struct Finder<'a> {
            needle: &'a str,
            found: Option<swc_ecma_ast::Id>,
        }
        impl Visit for Finder<'_> {
            fn visit_ident(&mut self, ident: &Ident) {
                if self.found.is_none() && ident.sym.as_str() == self.needle {
                    self.found = Some(ident.to_id());
                }
            }
        }
        let mut f = Finder {
            needle: name,
            found: None,
        };
        module.visit_with(&mut f);
        f.found
            .unwrap_or_else(|| panic!("identifier {name} not found in module"))
    }

    /// Parse + resolve a module so every Ident carries its
    /// canonical `SyntaxContext`.
    fn parse_and_resolve(src: &str) -> Module {
        let mut module = parse_js_module_ast("test.js", src).unwrap();
        let unresolved_mark = Mark::new();
        let top_level_mark = Mark::new();
        module.visit_mut_with(&mut resolver(unresolved_mark, top_level_mark, true));
        module
    }

    #[test]
    fn renames_declaration_and_reference() {
        with_swc_globals(|| {
            let mut module = parse_and_resolve("var x = 1; foo(x);");
            let x_id = first_ident_id(&module, "x");
            let mut plan = LoweringPlan::new(
                ModuleId::logical(0),
                vec![ModuleId::logical(0)],
                HashMap::new(),
            );
            plan.submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: x_id,
                    name: NamePolicy::Required(Atom::from("renamed")),
                    reason: "test",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
            let checked = plan.seal().unwrap();
            apply_chunk_renames(&mut module, &checked);
            // `foo` is a free variable (unresolved mark), so it
            // stays put even if its atom matches a renamed binding
            // — distinct `Id`s.
            assert_eq!(idents(&module), vec!["renamed", "foo", "renamed"]);
        });
    }

    #[test]
    fn unrelated_idents_left_alone() {
        with_swc_globals(|| {
            let mut module = parse_and_resolve("var x = 1; var y = 2;");
            let x_id = first_ident_id(&module, "x");
            let mut plan = LoweringPlan::new(
                ModuleId::logical(0),
                vec![ModuleId::logical(0)],
                HashMap::new(),
            );
            plan.submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: x_id,
                    name: NamePolicy::Required(Atom::from("xx")),
                    reason: "test",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
            let checked = plan.seal().unwrap();
            apply_chunk_renames(&mut module, &checked);
            assert_eq!(idents(&module), vec!["xx", "y"]);
        });
    }

    #[test]
    fn empty_plan_is_noop() {
        with_swc_globals(|| {
            let mut module = parse_and_resolve("var x = 1; foo(x);");
            let before = idents(&module);
            let plan = LoweringPlan::new(
                ModuleId::logical(0),
                vec![ModuleId::logical(0)],
                HashMap::new(),
            );
            let checked = plan.seal().unwrap();
            apply_chunk_renames(&mut module, &checked);
            assert_eq!(idents(&module), before);
        });
    }

    #[test]
    fn function_local_binding_with_same_atom_is_distinct() {
        with_swc_globals(|| {
            // Top-level `x` and function-local `x` get different
            // SyntaxContexts after resolver. Renaming the top-level
            // one should NOT touch the function-local — distinct Ids.
            let mut module =
                parse_and_resolve("var x = 1; function f() { var x = 2; return x; } f(x);");
            let top_level_x = first_ident_id(&module, "x");
            let mut plan = LoweringPlan::new(
                ModuleId::logical(0),
                vec![ModuleId::logical(0)],
                HashMap::new(),
            );
            plan.submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: top_level_x,
                    name: NamePolicy::Required(Atom::from("renamed")),
                    reason: "test",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
            let checked = plan.seal().unwrap();
            apply_chunk_renames(&mut module, &checked);
            // Identifiers in order: `renamed` (decl), `f` (decl),
            // `x` (function-local decl), `x` (function-local
            // return), `f` (call), `renamed` (call arg).
            assert_eq!(
                idents(&module),
                vec!["renamed", "f", "x", "x", "f", "renamed"]
            );
        });
    }
}
