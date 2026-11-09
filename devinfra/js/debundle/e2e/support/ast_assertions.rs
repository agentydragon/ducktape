//! Parse emitted JavaScript and assert on AST structure, not printer whitespace.

use binding_targets::binding_name_strings;
use std::fs;
use std::path::Path;
use swc_common::FileName;
use swc_common::sync::Lrc;
use swc_ecma_ast::{
    Decl, ExportSpecifier, Expr, Lit, Module, ModuleDecl, ModuleExportName, ModuleItem, Stmt,
    VarDeclKind,
};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum VariableDeclarationKind {
    Const,
    Let,
    Var,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum VariableInitializerKind {
    Array,
    Arrow,
    Binary,
    Call,
    Identifier,
    Number,
    Object,
    String,
    Other,
}

/// Assert the top-level variable declarators in an emitted module by AST.
/// Each expected entry gives a declarator's kind, bound names, and initializer
/// shape. Runtime assertions pin initializer values and effects.
pub fn assert_module_variable_declarators(
    out_root: &Path,
    module_path: &str,
    expected: &[(VariableDeclarationKind, &[&str], VariableInitializerKind)],
) {
    let source = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    let module = parse_module(&source);
    let mut actual = Vec::new();
    for item in &module.body {
        let declaration = match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => Some(declaration),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Var(declaration) => Some(declaration),
                _ => None,
            },
            _ => None,
        };
        let Some(declaration) = declaration else {
            continue;
        };
        let kind = match &declaration.kind {
            VarDeclKind::Const => VariableDeclarationKind::Const,
            VarDeclKind::Let => VariableDeclarationKind::Let,
            VarDeclKind::Var => VariableDeclarationKind::Var,
        };
        for declarator in &declaration.decls {
            let mut bindings = Vec::new();
            bindings.extend(binding_name_strings(&declarator.name));
            actual.push((
                kind,
                bindings,
                declarator.init.as_deref().map(variable_initializer_kind),
            ));
        }
    }
    let expected: Vec<_> = expected
        .iter()
        .map(|(kind, bindings, initializer)| {
            (
                *kind,
                bindings.iter().map(|name| (*name).to_string()).collect(),
                Some(*initializer),
            )
        })
        .collect();
    assert_eq!(
        actual, expected,
        "top-level variable declarators differed in {module_path}; source:\n{source}",
    );
}

fn variable_initializer_kind(expr: &Expr) -> VariableInitializerKind {
    match expr {
        Expr::Array(_) => VariableInitializerKind::Array,
        Expr::Arrow(_) => VariableInitializerKind::Arrow,
        Expr::Bin(_) => VariableInitializerKind::Binary,
        Expr::Call(_) => VariableInitializerKind::Call,
        Expr::Ident(_) => VariableInitializerKind::Identifier,
        Expr::Lit(Lit::Num(_)) => VariableInitializerKind::Number,
        Expr::Lit(Lit::Str(_)) => VariableInitializerKind::String,
        Expr::Object(_) => VariableInitializerKind::Object,
        _ => VariableInitializerKind::Other,
    }
}

/// Parse `source` as an ESM module and return the SWC AST. Tests use this
/// when the substring-on-emit checks aren't precise enough — e.g. when
/// they need to walk specifiers to disambiguate `aH$1 as aH` (correct)
/// from `aH$1 as aH$1` (corrupt).
pub fn parse_module(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("entry.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Typescript(TsSyntax {
            tsx: true,
            decorators: true,
            no_early_errors: true,
            ..Default::default()
        }),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer)
        .parse_module()
        .unwrap_or_else(|err| panic!("entry must parse, got {err:?}; source:\n{source}"))
}

pub(super) fn declared_bindings_in_source_match(source: &str) -> Vec<String> {
    let module = parse_module(source);
    let mut names = Vec::new();
    for item in &module.body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => {
                collect_declared_bindings_from_decl(decl, &mut names)
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                collect_declared_bindings_from_decl(&export.decl, &mut names)
            }
            _ => {}
        }
    }
    names
        .into_iter()
        .filter(|name| !is_declarator_list_hole_name(name))
        .collect()
}

fn is_declarator_list_hole_name(name: &str) -> bool {
    name == "DECLARATORS" || name.starts_with("DECLARATORS_")
}

fn collect_declared_bindings_from_decl(decl: &Decl, names: &mut Vec<String>) {
    match decl {
        Decl::Class(class) => names.push(class.ident.sym.to_string()),
        Decl::Fn(function) => names.push(function.ident.sym.to_string()),
        Decl::Var(var) => {
            for declarator in &var.decls {
                names.extend(binding_name_strings(&declarator.name));
            }
        }
        _ => {}
    }
}

/// Parse `source` and assert the named export specifiers for `expected_orig` match
/// the supplied set of exported names. `None` means the `as` clause is absent.
/// Walking the parsed specifier tree rejects near-matches such as
/// `export { aH$1 as aH$1 }` that substring assertions can accept.
pub fn assert_export_named_specifiers(
    source: &str,
    expected_orig: &str,
    expected_exported_as: &[Option<&str>],
) {
    let module = parse_module(source);
    let matched: Vec<_> = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => Some(named),
            _ => None,
        })
        .flat_map(|named| named.specifiers.iter())
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named) => Some(named),
            _ => None,
        })
        .filter(|spec| {
            let ModuleExportName::Ident(ident) = &spec.orig else {
                return false;
            };
            ident.sym.as_ref() == expected_orig
        })
        .collect();
    assert_eq!(
        matched.len(),
        expected_exported_as.len(),
        "expected {} `export {{ {expected_orig} ... }}` specifiers; got {} in:\n{source}",
        expected_exported_as.len(),
        matched.len(),
    );
    let mut actual: Vec<Option<String>> = matched
        .iter()
        .map(|spec| match &spec.exported {
            Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
            Some(ModuleExportName::Str(_)) => panic!("unexpected string export in:\n{source}"),
            None => None,
        })
        .collect();
    let mut expected: Vec<Option<String>> = expected_exported_as
        .iter()
        .map(|name| name.map(str::to_owned))
        .collect();
    actual.sort();
    expected.sort();
    assert_eq!(
        actual, expected,
        "export {{ {expected_orig} ... }} `as` clauses mismatch in:\n{source}",
    );
}
