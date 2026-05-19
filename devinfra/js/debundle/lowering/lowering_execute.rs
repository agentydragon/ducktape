//! Apply a sealed [`CheckedPlan`] to a JS AST. One visitor walk:
//! renames idents by hygiene-preserving `Id`, preserves public
//! export names, synthesizes `as` aliases on renamed import
//! specifiers, leaves property labels alone, and optionally
//! collapses object literal/pattern shorthand.

use std::collections::HashMap;

use swc_atoms::Atom;
use swc_common::DUMMY_SP;
use swc_ecma_ast::{
    ExportNamedSpecifier, ExportSpecifier, Id, Ident, ImportNamedSpecifier, MemberProp, ModuleDecl,
    ModuleExportName, ModuleItem, NamedExport, ObjectLit, ObjectPat, PropName,
};
use swc_ecma_visit::{VisitMut, VisitMutWith};

use super::lowering_plan::{CheckedPlan, Scope};
use super::visitors::{naturalize_object_literal_shorthand, naturalize_object_pattern_shorthand};

/// Renames only; no shorthand collapse.
pub fn apply_chunk_renames_to_items(items: &mut [ModuleItem], plan: &CheckedPlan) {
    apply(items, plan, false);
}

/// Renames + object literal/pattern shorthand collapse in one
/// walk. Used for moved-module bodies.
pub fn apply_plan_renames_and_naturalize(items: &mut [ModuleItem], plan: &CheckedPlan) {
    apply(items, plan, true);
}

fn apply(items: &mut [ModuleItem], plan: &CheckedPlan, naturalize_shorthand: bool) {
    let renames = chunk_rename_map(plan);
    preserve_export_specifier_names_for_renamed(items, &renames);
    let mut v = PlanRenameVisitor {
        renames: &renames,
        naturalize_shorthand,
    };
    for item in items.iter_mut() {
        item.visit_mut_with(&mut v);
    }
}

fn chunk_rename_map(plan: &CheckedPlan) -> HashMap<Id, Atom> {
    plan.rename_index
        .iter()
        .filter_map(|((scope, id), atom)| match scope {
            Scope::Chunk => Some((id.clone(), atom.clone())),
            _ => None,
        })
        .collect()
}

/// Pre-fill `exported` on `export { local }` re-export specifiers
/// whose `local` is about to be renamed, so the public export
/// name survives.
fn preserve_export_specifier_names_for_renamed(
    items: &mut [ModuleItem],
    renames: &HashMap<Id, Atom>,
) {
    for item in items.iter_mut() {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            continue;
        };
        if named.src.is_some() {
            continue;
        }
        for specifier in &mut named.specifiers {
            let ExportSpecifier::Named(spec) = specifier else {
                continue;
            };
            if spec.exported.is_some() {
                continue;
            }
            let ModuleExportName::Ident(orig) = &spec.orig else {
                continue;
            };
            if !renames.contains_key(&orig.to_id()) {
                continue;
            }
            spec.exported = Some(spec.orig.clone());
        }
    }
}

struct PlanRenameVisitor<'a> {
    renames: &'a HashMap<Id, Atom>,
    naturalize_shorthand: bool,
}

impl VisitMut for PlanRenameVisitor<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(new_name) = self.renames.get(&ident.to_id()) {
            ident.sym = new_name.clone();
        }
    }

    fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
        let original = spec.local.to_id();
        let Some(new_name) = self.renames.get(&original).cloned() else {
            return;
        };
        if new_name == original.0 {
            return;
        }
        if spec.imported.is_none() {
            spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                original.0.clone(),
                DUMMY_SP,
            )));
        }
        spec.local.sym = new_name;
    }

    fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
        if named.src.is_none() {
            named.specifiers.visit_mut_with(self);
        }
    }

    fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
        spec.orig.visit_mut_with(self);
    }

    fn visit_mut_prop_name(&mut self, prop_name: &mut PropName) {
        if let PropName::Computed(computed) = prop_name {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_member_prop(&mut self, member_prop: &mut MemberProp) {
        if let MemberProp::Computed(computed) = member_prop {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        if self.naturalize_shorthand {
            naturalize_object_literal_shorthand(object);
        }
    }

    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        if self.naturalize_shorthand {
            naturalize_object_pattern_shorthand(object);
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
    use swc_ecma_ast::{Ident, Module};
    use swc_ecma_transforms_base::resolver;
    use swc_ecma_visit::{Visit, VisitMutWith, VisitWith};

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

    fn first_ident_id(module: &Module, name: &str) -> Id {
        struct Finder<'a> {
            needle: &'a str,
            found: Option<Id>,
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
            apply_chunk_renames_to_items(&mut module.body, &checked);
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
            apply_chunk_renames_to_items(&mut module.body, &checked);
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
            apply_chunk_renames_to_items(&mut module.body, &checked);
            assert_eq!(idents(&module), before);
        });
    }

    #[test]
    fn function_local_binding_with_same_atom_is_distinct() {
        with_swc_globals(|| {
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
            apply_chunk_renames_to_items(&mut module.body, &checked);
            assert_eq!(
                idents(&module),
                vec!["renamed", "f", "x", "x", "f", "renamed"]
            );
        });
    }
}
