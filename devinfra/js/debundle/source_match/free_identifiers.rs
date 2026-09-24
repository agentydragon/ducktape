//! A template's free identifiers: names it references but never declares in any
//! of its scopes. The fact matcher binds them in its root frame (one free name,
//! one subject name, across the whole template) and reports what each bound to.
//!
//! Classified over the matcher's own fact projection, so a name counts as
//! declared exactly where the matcher treats it as one: a binding pattern
//! (`var`/`let`/`const`, params, catch params, destructuring), a
//! function/class declaration or expression name, an import local. Hole
//! keywords, the regex-predicate callee and nodes a run hole absorbs are not
//! identifiers the matcher compares, so they are never free.

use super::*;
use chunk_facts::NodeKind;
use selector_match::Index;

/// The free identifiers of a template given as its statements' match indices.
pub fn free_identifiers<'a>(template: impl IntoIterator<Item = &'a Index>) -> BTreeSet<String> {
    let mut declared = BTreeSet::new();
    let mut referenced = BTreeSet::new();
    for index in template {
        let consumed = selector_match::consumed_nodes(index);
        let mut declared_names = BTreeSet::new();
        for node in 0..index.node_count() as chunk_facts::NodeId {
            // A function/class declaration's name is child 0; an expression's is
            // child 0 only when named (`[name?, function-or-class]`).
            let named = match index.node_kind(node) {
                NodeKind::FnDecl | NodeKind::ClassDecl => true,
                NodeKind::FnExpr | NodeKind::ClassExpr => index.children(node).len() == 2,
                _ => false,
            };
            if named {
                declared_names.insert(index.children(node)[0]);
            }
        }
        for node in 0..index.node_count() as chunk_facts::NodeId {
            let Some(name) = index.ident(node) else {
                continue;
            };
            if consumed.contains(&node)
                || selector_match::is_hole_keyword(name)
                || name == STRING_LITERAL_REGEX_PREDICATE
            {
                continue;
            }
            let is_declaration = declared_names.contains(&node)
                || matches!(
                    index.node_kind(node),
                    NodeKind::BindingIdent | NodeKind::ImportSpecifier | NodeKind::PatAssign
                );
            if is_declaration {
                declared.insert(name);
            } else {
                referenced.insert(name);
            }
        }
    }
    referenced
        .difference(&declared)
        .map(|name| name.to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn free(source: &str) -> BTreeSet<String> {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast("<t>", source).unwrap();
            let indices: Vec<Index> = module
                .body
                .iter()
                .map(|item| {
                    Index::build(
                        &chunk_facts::extract_facts_items(std::slice::from_ref(item)).unwrap(),
                    )
                })
                .collect();
            free_identifiers(&indices)
        })
    }

    fn names(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn declarations_in_every_scope_are_not_free() {
        assert_eq!(
            free(
                "import def, { named as local } from \"m\";\n\
                 function f(param, { key, other: renamed = 1 }, ...rest) {\n\
                   var v; let l; const c = 1;\n\
                   try {} catch (error) { error; }\n\
                   return [param, key, renamed, rest, v, l, c, def, local, f];\n\
                 }\n\
                 class K extends Base {}\n\
                 const g = function named() { return named; };\n\
                 const h = class Named {};"
            ),
            names(&["Base"]),
        );
    }

    #[test]
    fn a_name_declared_by_another_statement_is_not_free() {
        assert_eq!(free("use(later);\nconst later = 1;"), names(&["use"]));
    }

    #[test]
    fn a_name_declared_in_any_scope_is_not_free_anywhere() {
        assert_eq!(
            free("function f() { let inner; }\ninner(outer);"),
            names(&["outer"]),
        );
    }

    #[test]
    fn references_in_every_position_are_free() {
        assert_eq!(
            free(
                "const a = helper(x, { shorthand, key: value }, y.z);\n\
                 target = other;"
            ),
            names(&["helper", "other", "shorthand", "target", "value", "x", "y"]),
        );
    }

    #[test]
    fn holes_and_the_regex_predicate_are_not_free() {
        assert_eq!(
            free(
                "const a = f(ANYTHING, EXPR_label, ARGS);\n\
                 const DECLARATORS = absorbed, b = STR_LITERAL_MATCHING_RE(\"^x\");\n\
                 STMT_LIST;"
            ),
            names(&["f"]),
        );
    }
}
