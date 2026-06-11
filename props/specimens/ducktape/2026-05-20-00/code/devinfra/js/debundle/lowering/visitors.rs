//! AST visitors used by the lowering pipeline.
//!
//! - `IdentifierRenamer` rewrites local identifiers per a rename map.
//! - `RenameAndShorthandNaturalizer` combines that with shorthand collapse.
//! - `ShorthandNaturalizer` is the standalone shorthand-only pass.
//!
//! The `naturalize_object_*_shorthand` helpers are shared between them.

use super::*;

pub(super) struct IdentifierRenamer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
}

impl VisitMut for IdentifierRenamer<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(to) = self.renames.get(ident.sym.as_ref()) {
            ident.sym = to.clone().into();
        }
    }

    fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
        let original_local = spec.local.sym.clone();
        let Some(to) = self.renames.get(original_local.as_ref()) else {
            return;
        };
        if spec.imported.is_none() {
            spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                original_local,
                DUMMY_SP,
            )));
        }
        spec.local.sym = to.clone().into();
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

    fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
        // Re-export specifiers' orig field (`export { x } from "./mod"`) is
        // the imported name in the source module, not a local binding here,
        // so don't touch it. Without `from`, orig is a local binding —
        // recurse into specifiers so visit_mut_export_named_specifier can
        // narrow which fields to rewrite.
        if named.src.is_none() {
            named.specifiers.visit_mut_with(self);
        }
    }

    fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
        // The `exported` field is a public-API name, not a local binding,
        // so it must not be rewritten when a colliding local is renamed.
        spec.orig.visit_mut_with(self);
    }
}

pub(super) struct RenameAndShorthandNaturalizer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
}

impl VisitMut for RenameAndShorthandNaturalizer<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(to) = self.renames.get(ident.sym.as_ref()) {
            ident.sym = to.clone().into();
        }
    }

    fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
        let original_local = spec.local.sym.clone();
        let Some(to) = self.renames.get(original_local.as_ref()) else {
            return;
        };
        if spec.imported.is_none() {
            spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                original_local,
                DUMMY_SP,
            )));
        }
        spec.local.sym = to.clone().into();
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

    fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
        if named.src.is_none() {
            named.specifiers.visit_mut_with(self);
        }
    }

    fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
        spec.orig.visit_mut_with(self);
    }

    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

pub(super) struct ShorthandNaturalizer;

impl VisitMut for ShorthandNaturalizer {
    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

pub(super) fn naturalize_object_pattern_shorthand(object: &mut ObjectPat) {
    for prop in &mut object.props {
        if let ObjectPatProp::KeyValue(key_value) = prop
            && let PropName::Ident(key) = &key_value.key
            && let Pat::Ident(value) = &*key_value.value
            && key.sym == value.id.sym
        {
            *prop = ObjectPatProp::Assign(AssignPatProp {
                span: DUMMY_SP,
                key: value.clone(),
                value: None,
            });
        }
    }
}

pub(super) fn naturalize_object_literal_shorthand(object: &mut ObjectLit) {
    for prop in &mut object.props {
        if let PropOrSpread::Prop(prop_box) = prop
            && let Prop::KeyValue(key_value) = &**prop_box
            && let PropName::Ident(key) = &key_value.key
            && let Expr::Ident(value) = &*key_value.value
            && key.sym == value.sym
        {
            *prop = PropOrSpread::Prop(Box::new(Prop::Shorthand(value.clone())));
        }
    }
}
