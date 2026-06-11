use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{ChunkBundle, JsFile, get_chunk_entry_path};
use spec::{PartialSwapSymbol, VendorLevel, VendorMark};

pub struct StripSwappedVendorExportsResult {
    pub artifact: ChunkBundle,
    pub manifest: StripSwappedVendorExportsManifest,
}

#[derive(Debug, Clone, Serialize)]
pub struct StripSwappedVendorExportsManifest {
    pub per_chunk: BTreeMap<String, ChunkStripStats>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkStripStats {
    pub chunk_path: String,
    pub stripped_export_specifiers: usize,
    pub dropped_top_level_items: usize,
    pub retained_top_level_items: usize,
}

/// Per-chunk pass that drops swapped names from the vendor entry's
/// trailing `export { … }` block (Phase 1) and sweeps top-level
/// bindings that are no longer reachable from the residual export
/// surface plus retained side-effect statements (Phase 2).
///
/// Runs after `apply_partial_vendor_swaps` — the consumer side has
/// already been rewritten to import each swapped name from upstream,
/// so the chunk's residual `export { … }` entries for those names
/// are dead weight. Without this pass the on-disk vendor blob stays
/// byte-identical to pre-swap.
pub fn strip_swapped_vendor_exports(
    mut artifact: ChunkBundle,
    vendor: &BTreeMap<String, VendorMark>,
) -> Result<StripSwappedVendorExportsResult> {
    let chunk_table = artifact.chunk_table.clone();
    let mut per_chunk = BTreeMap::new();

    for (chunk_path, mark) in vendor {
        let symbols = match &mark.level {
            VendorLevel::PartialSwap(partial) => &partial.symbols,
            VendorLevel::BundledPartialSwap(partial) => &partial.symbols,
            _ => continue,
        };

        let chunk_name = chunk_id_from_chunk_path(chunk_path)?;
        let chunk_id = chunk_table.get(&chunk_name).with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path} targets unknown chunk: {chunk_name}"
            )
        })?;
        let entry_relative_file = get_chunk_entry_path(&artifact, chunk_id).with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path} targets missing chunk (chunk_id={chunk_name})"
            )
        })?;

        let js_chunk = artifact.js_chunk_mut(chunk_id)?;
        let file = js_chunk.remove_file(&entry_relative_file).with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: entry file {entry_relative_file} missing from chunk {chunk_name}"
            )
        })?;
        let (parts, mut ast) = file.into_ast_parts().with_context(|| {
            format!(
                "strip_swapped_vendor_exports vendor entry {chunk_path}: chunk {chunk_name} entry has no AST"
            )
        })?;

        let stats = strip_one_chunk(&mut ast.module, symbols, chunk_path)?;
        per_chunk.insert(chunk_path.clone(), stats);

        js_chunk.insert_file(JsFile::from_ast_parts(parts, ast));
    }

    Ok(StripSwappedVendorExportsResult {
        artifact,
        manifest: StripSwappedVendorExportsManifest { per_chunk },
    })
}

fn strip_one_chunk(
    module: &mut Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    chunk_path: &str,
) -> Result<ChunkStripStats> {
    let swapped: BTreeSet<String> = symbols.keys().cloned().collect();

    let stripped_export_specifiers = strip_export_specifiers(module, &swapped, chunk_path)?;
    let post_strip_exports = collect_exported_names(module);

    let dropped_total_before = module.body.len();
    sweep_unreachable_top_level(module, &post_strip_exports, chunk_path)?;
    let retained = module.body.len();
    let dropped = dropped_total_before - retained;

    // Phase 2 must not change the export surface relative to Phase 1.
    let post_dce_exports = collect_exported_names(module);
    if post_dce_exports != post_strip_exports {
        let removed: Vec<_> = post_strip_exports
            .difference(&post_dce_exports)
            .cloned()
            .collect();
        let added: Vec<_> = post_dce_exports
            .difference(&post_strip_exports)
            .cloned()
            .collect();
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: DCE pass changed the export surface (removed=[{}], added=[{}])",
            removed.join(","),
            added.join(","),
        );
    }

    // Sanity: stripped names should not appear in pre or post export set.
    let leaked: Vec<_> = swapped.intersection(&post_strip_exports).cloned().collect();
    if !leaked.is_empty() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: swapped names still exported after strip: [{}]",
            leaked.join(","),
        );
    }

    Ok(ChunkStripStats {
        chunk_path: chunk_path.to_string(),
        stripped_export_specifiers,
        dropped_top_level_items: dropped,
        retained_top_level_items: retained,
    })
}

fn chunk_id_from_chunk_path(chunk_path: &str) -> Result<String> {
    if chunk_path.is_empty() {
        bail!("strip_swapped_vendor_exports: empty chunk path");
    }
    let chunk_id = chunk_path.strip_suffix(".js").with_context(|| {
        format!("strip_swapped_vendor_exports: chunk path must end in .js: {chunk_path}")
    })?;
    Ok(chunk_id.to_string())
}

/// Walk `module.body` once and strip the chunk's *local* re-exports of
/// every name in `swapped`. Two shapes are handled:
///
/// - `export { x, y as z }` (`ExportNamed` with `src.is_none()`): the
///   matching specifier is dropped from the list; an empty list collapses
///   the statement.
/// - `export const x = …` / `export function x() {}` / `export class x {}`
///   (`ExportDecl`): the `export` prefix is dropped — the declaration
///   itself stays, becoming a chunk-local binding the DCE pass can
///   collect if no live item references it.
///
/// `export { x } from "./y"` (`ExportNamed` with `src.is_some()`) is left
/// alone — those forward upstream names through a side import, not from
/// a chunk-local binding.
fn strip_export_specifiers(
    module: &mut Module,
    swapped: &BTreeSet<String>,
    chunk_path: &str,
) -> Result<usize> {
    let mut found: BTreeSet<String> = BTreeSet::new();
    let mut stripped = 0usize;
    let mut new_body = Vec::with_capacity(module.body.len());

    for item in std::mem::take(&mut module.body) {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(mut named)) => {
                if named.src.is_some() {
                    new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
                    continue;
                }
                let mut kept = Vec::with_capacity(named.specifiers.len());
                for spec in std::mem::take(&mut named.specifiers) {
                    let ExportSpecifier::Named(ref named_spec) = spec else {
                        kept.push(spec);
                        continue;
                    };
                    let exported = named_spec
                        .exported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| module_export_name(&named_spec.orig));
                    if swapped.contains(&exported) {
                        found.insert(exported);
                        stripped += 1;
                    } else {
                        kept.push(spec);
                    }
                }
                if kept.is_empty() {
                    continue;
                }
                named.specifiers = kept;
                new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                let inline_names = export_decl_declared_names(&export_decl.decl);
                // For an `ExportDecl`, every declared name is exported
                // under that same name. Drop the `export` prefix only
                // if *all* names declared by the statement are swapped;
                // otherwise we'd silently un-export a non-swapped
                // sibling (legal but surprising for a multi-declarator
                // `export const a = …, b = …`).
                if !inline_names.is_empty() && inline_names.iter().all(|n| swapped.contains(n)) {
                    for n in &inline_names {
                        found.insert(n.clone());
                        stripped += 1;
                    }
                    new_body.push(ModuleItem::Stmt(Stmt::Decl(export_decl.decl)));
                } else {
                    new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)));
                }
            }
            other => new_body.push(other),
        }
    }
    module.body = new_body;

    let missing: Vec<String> = swapped.difference(&found).cloned().collect();
    if !missing.is_empty() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: swapped symbols not found in any chunk-local export: [{}]",
            missing.join(","),
        );
    }
    Ok(stripped)
}

fn export_decl_declared_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(f) => vec![f.ident.sym.to_string()],
        Decl::Class(c) => vec![c.ident.sym.to_string()],
        Decl::Var(v) => {
            let mut out = Vec::new();
            for d in &v.decls {
                collect_pat_names(&d.name, &mut out);
            }
            out
        }
        _ => Vec::new(),
    }
}

/// Conservative top-level dead-code sweep. Each `module.body[i]` is
/// either a **side-effect** anchor (must stay), or a **declaration**
/// whose retention depends on whether anything live reads its names.
///
/// Algorithm:
/// 1. Classify each `body[i]` into `ItemClass::Decl { names, reads }`
///    or `ItemClass::SideEffect { reads }`. Hoistable, side-effect-free
///    shapes (`function X`, `class X`, `var/let/const X = <pure_init>`,
///    `export const X = <pure_init>`, etc.) go to `Decl`; everything
///    else (top-level expressions, `Object.defineProperty(...)` calls,
///    imports, side-effecting var inits) goes to `SideEffect`.
/// 2. Seed the live set with all `SideEffect` items, plus any `Decl`
///    that introduces a name in `live_exports`.
/// 3. Fixpoint: while there's a `Decl` not yet live whose declared
///    names are referenced by a live item, mark it live.
/// 4. Filter `module.body` to keep only live items in source order.
///
/// Reads are over-approximated to all free identifier names appearing
/// anywhere in the item — no scope analysis. This is safe (keeps more
/// code than strictly necessary) and avoids re-implementing lexical
/// scoping.
fn sweep_unreachable_top_level(
    module: &mut Module,
    live_exports: &BTreeSet<String>,
    chunk_path: &str,
) -> Result<()> {
    let analyses: Vec<ItemAnalysis> = module.body.iter().map(classify_item).collect();

    // BindingName -> index that declares it. If two items declare the
    // same name (legal for `var`), prefer the last declaration; later
    // writes shadow earlier ones for reachability purposes.
    let mut declarer: BTreeMap<&str, usize> = BTreeMap::new();
    for (i, an) in analyses.iter().enumerate() {
        for name in &an.declared {
            declarer.insert(name.as_str(), i);
        }
    }

    let mut live = vec![false; analyses.len()];
    for (i, an) in analyses.iter().enumerate() {
        if an.is_side_effect || an.declared.iter().any(|n| live_exports.contains(n)) {
            live[i] = true;
        }
    }

    let mut queue: Vec<usize> = (0..analyses.len()).filter(|&i| live[i]).collect();
    while let Some(i) = queue.pop() {
        for name in &analyses[i].reads {
            let Some(&decl_idx) = declarer.get(name.as_str()) else {
                continue;
            };
            if !live[decl_idx] {
                live[decl_idx] = true;
                queue.push(decl_idx);
            }
        }
    }

    // Soundness gate: if any *kept* item reads a name declared only by
    // a *dropped* item, the classification missed a side-effect or the
    // fixpoint didn't converge. Bail with the offending pair.
    for (i, is_live) in live.iter().enumerate() {
        if !is_live {
            continue;
        }
        for name in &analyses[i].reads {
            if let Some(&decl_idx) = declarer.get(name.as_str())
                && !live[decl_idx]
            {
                bail!(
                    "strip_swapped_vendor_exports vendor entry {chunk_path}: live item {i} reads `{name}` declared by dropped item {decl_idx}",
                );
            }
        }
    }

    let mut original = std::mem::take(&mut module.body);
    for (i, is_live) in live.iter().enumerate().rev() {
        if !*is_live {
            original.remove(i);
        }
    }
    module.body = original;
    Ok(())
}

struct ItemAnalysis {
    declared: Vec<String>,
    reads: BTreeSet<String>,
    is_side_effect: bool,
}

fn classify_item(item: &ModuleItem) -> ItemAnalysis {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => classify_decl(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            classify_decl(&export_decl.decl)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
            // `export { a, b as c };` — declares nothing locally;
            // reads `a` and `b`. Liveness pulled in through the
            // export-root seed (the export names appear in
            // `live_exports`), and that, in turn, keeps the bindings
            // they reference live via the fixpoint.
            let mut reads = BTreeSet::new();
            for spec in &named.specifiers {
                if let ExportSpecifier::Named(named_spec) = spec
                    && let ModuleExportName::Ident(ident) = &named_spec.orig
                {
                    reads.insert(ident.sym.to_string());
                }
            }
            // Marked side-effect so we never drop a residual export
            // block (even if its names somehow aren't in
            // `live_exports` — defensive against a mismatch).
            ItemAnalysis {
                declared: Vec::new(),
                reads,
                is_side_effect: true,
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export_default)) => {
            let mut reads = BTreeSet::new();
            collect_idents(export_default, &mut reads);
            ItemAnalysis {
                declared: vec!["default".to_string()],
                reads,
                is_side_effect: false,
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export_default)) => {
            let mut reads = BTreeSet::new();
            collect_idents(&*export_default.expr, &mut reads);
            // `export default <expr>` evaluates expr at module init;
            // if expr is impure (e.g. `export default sideEffect()`)
            // we must keep it. For a pure expr (`export default X`),
            // the expression itself is inert — the export is the
            // anchor of the read chain.
            let is_side_effect = !is_pure_expr(&export_default.expr);
            ItemAnalysis {
                declared: vec!["default".to_string()],
                reads,
                is_side_effect,
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            let mut reads = BTreeSet::new();
            collect_idents(import, &mut reads);
            ItemAnalysis {
                declared: Vec::new(),
                reads,
                is_side_effect: true,
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => ItemAnalysis {
            declared: Vec::new(),
            reads: BTreeSet::new(),
            is_side_effect: true,
        },
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(_)) => ItemAnalysis {
            declared: Vec::new(),
            reads: BTreeSet::new(),
            is_side_effect: true,
        },
        ModuleItem::ModuleDecl(_) => {
            let mut reads = BTreeSet::new();
            collect_idents(item, &mut reads);
            ItemAnalysis {
                declared: Vec::new(),
                reads,
                is_side_effect: true,
            }
        }
        ModuleItem::Stmt(_) => {
            let mut reads = BTreeSet::new();
            collect_idents(item, &mut reads);
            ItemAnalysis {
                declared: Vec::new(),
                reads,
                is_side_effect: true,
            }
        }
    }
}

fn classify_decl(decl: &Decl) -> ItemAnalysis {
    let mut reads = BTreeSet::new();
    match decl {
        Decl::Fn(fn_decl) => {
            let declared = vec![fn_decl.ident.sym.to_string()];
            collect_idents(&fn_decl.function, &mut reads);
            reads.remove(fn_decl.ident.sym.as_ref());
            ItemAnalysis {
                declared,
                reads,
                is_side_effect: false,
            }
        }
        Decl::Class(class_decl) => {
            let declared = vec![class_decl.ident.sym.to_string()];
            collect_idents(&class_decl.class, &mut reads);
            reads.remove(class_decl.ident.sym.as_ref());
            ItemAnalysis {
                declared,
                reads,
                is_side_effect: false,
            }
        }
        Decl::Var(var) => {
            let mut declared = Vec::new();
            let mut has_side_effect_init = false;
            for d in &var.decls {
                collect_pat_names(&d.name, &mut declared);
                if let Some(init) = &d.init {
                    if !is_pure_expr(init) {
                        has_side_effect_init = true;
                    }
                    collect_idents(&**init, &mut reads);
                }
            }
            for name in &declared {
                reads.remove(name.as_str());
            }
            ItemAnalysis {
                declared,
                reads,
                is_side_effect: has_side_effect_init,
            }
        }
        Decl::Using(_)
        | Decl::TsInterface(_)
        | Decl::TsTypeAlias(_)
        | Decl::TsEnum(_)
        | Decl::TsModule(_) => {
            collect_idents(decl, &mut reads);
            ItemAnalysis {
                declared: Vec::new(),
                reads,
                is_side_effect: true,
            }
        }
    }
}

fn collect_pat_names(pat: &Pat, out: &mut Vec<String>) {
    match pat {
        Pat::Ident(b) => out.push(b.id.sym.to_string()),
        Pat::Array(arr) => {
            for elem in arr.elems.iter().flatten() {
                collect_pat_names(elem, out);
            }
        }
        Pat::Object(obj) => {
            for prop in &obj.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => collect_pat_names(&kv.value, out),
                    ObjectPatProp::Assign(a) => out.push(a.key.sym.to_string()),
                    ObjectPatProp::Rest(r) => collect_pat_names(&r.arg, out),
                }
            }
        }
        Pat::Rest(r) => collect_pat_names(&r.arg, out),
        Pat::Assign(a) => collect_pat_names(&a.left, out),
        _ => {}
    }
}

/// Pure-init shapes safe to DCE. The point isn't to be exhaustive — it
/// is to admit the common cases that account for the vast majority of
/// vendor-blob declarations (literals, function/arrow/class expressions,
/// object/array literals composed of pure parts, simple member access
/// off a pure receiver). Anything else (calls, `new`, template tags,
/// spreads, computed members on side-effecting bases) is treated as a
/// side-effect anchor and kept.
fn is_pure_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Lit(_)
        | Expr::Ident(_)
        | Expr::This(_)
        | Expr::Fn(_)
        | Expr::Arrow(_)
        | Expr::Class(_)
        | Expr::Tpl(_)
        | Expr::PrivateName(_) => true,
        Expr::Paren(p) => is_pure_expr(&p.expr),
        Expr::Unary(u) => matches!(u.op, UnaryOp::Void | UnaryOp::TypeOf) || is_pure_expr(&u.arg),
        Expr::Array(arr) => arr
            .elems
            .iter()
            .flatten()
            .all(|elem| elem.spread.is_none() && is_pure_expr(&elem.expr)),
        Expr::Object(obj) => obj.props.iter().all(|prop| match prop {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(p) => is_pure_prop(p),
        }),
        Expr::Member(m) => is_pure_expr(&m.obj),
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(m) => is_pure_expr(&m.obj),
            OptChainBase::Call(_) => false,
        },
        Expr::Cond(c) => is_pure_expr(&c.test) && is_pure_expr(&c.cons) && is_pure_expr(&c.alt),
        Expr::Bin(b) => is_pure_expr(&b.left) && is_pure_expr(&b.right),
        Expr::Seq(s) => s.exprs.iter().all(|e| is_pure_expr(e)),
        _ => false,
    }
}

fn is_pure_prop(prop: &Prop) -> bool {
    match prop {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => is_pure_expr(&kv.value),
        Prop::Method(_) | Prop::Getter(_) | Prop::Setter(_) => true,
        Prop::Assign(a) => is_pure_expr(&a.value),
    }
}

/// Walk any AST node and collect every `Ident` symbol that appears.
/// Over-approximation: returns names regardless of whether they refer
/// to a chunk-local top-level binding or a lexical local. For DCE
/// reachability that's safe (we err on the side of keeping things).
fn collect_idents<T>(node: &T, out: &mut BTreeSet<String>)
where
    for<'a> T: VisitWith<IdentCollector<'a>>,
{
    let mut visitor = IdentCollector { out };
    node.visit_with(&mut visitor);
}

struct IdentCollector<'a> {
    out: &'a mut BTreeSet<String>,
}

impl Visit for IdentCollector<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        self.out.insert(ident.sym.to_string());
    }

    fn visit_member_prop(&mut self, prop: &MemberProp) {
        // `obj.x` — `x` is not a free variable reference. Only recurse
        // into computed `obj[x]`.
        if let MemberProp::Computed(c) = prop {
            c.expr.visit_with(self);
        }
    }

    fn visit_prop_name(&mut self, name: &PropName) {
        // Object literal keys: `{ x: 1 }` — `x` is a property key, not
        // a reference. Computed `{ [k]: 1 }` still reads `k`.
        if let PropName::Computed(c) = name {
            c.expr.visit_with(self);
        }
    }

    fn visit_prop(&mut self, prop: &Prop) {
        // `{ x }` shorthand reads `x` as an identifier; default Visit
        // already handles that via `Prop::Shorthand(Ident)`.
        prop.visit_children_with(self);
    }
}

fn module_export_name(name: &ModuleExportName) -> String {
    name.atom().to_string()
}

/// Subset of [`collect_exported_names`] in `vendor.rs`: returns the
/// post-mutation export surface of `module`. Local re-exports
/// (`export { x as y }`), `export const x = …`, `export function`,
/// `export class`, `export default …` all count.
fn collect_exported_names(module: &Module) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
                out.insert("default".to_string());
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                match &export_decl.decl {
                    Decl::Fn(f) => {
                        out.insert(f.ident.sym.to_string());
                    }
                    Decl::Class(c) => {
                        out.insert(c.ident.sym.to_string());
                    }
                    Decl::Var(v) => {
                        for d in &v.decls {
                            let mut names = Vec::new();
                            collect_pat_names(&d.name, &mut names);
                            out.extend(names);
                        }
                    }
                    _ => {}
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                for spec in &named.specifiers {
                    if let ExportSpecifier::Named(named_spec) = spec {
                        out.insert(
                            named_spec
                                .exported
                                .as_ref()
                                .map(module_export_name)
                                .unwrap_or_else(|| module_export_name(&named_spec.orig)),
                        );
                    }
                }
            }
            _ => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use spec::{PartialSwapKind, PartialSwapSymbol};
    use swc_common::sync::Lrc;
    use swc_common::{FileName, SourceMap};
    use swc_ecma_ast::EsVersion;
    use swc_ecma_codegen::text_writer::JsWriter;
    use swc_ecma_codegen::{Config, Emitter};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    use super::*;

    fn parse(source: &str) -> Module {
        let cm: Lrc<SourceMap> = Default::default();
        let fm = cm.new_source_file(Lrc::new(FileName::Anon), source.to_string());
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            EsVersion::latest(),
            StringInput::from(&*fm),
            None,
        );
        let mut parser = Parser::new_from(lexer);
        parser.parse_module().expect("parse")
    }

    fn emit(module: &Module) -> String {
        let cm: Lrc<SourceMap> = Default::default();
        let mut buf = Vec::new();
        {
            let writer = JsWriter::new(cm.clone(), "\n", &mut buf, None);
            let mut emitter = Emitter {
                cfg: Config::default(),
                cm,
                comments: None,
                wr: writer,
            };
            emitter.emit_module(module).expect("emit");
        }
        String::from_utf8(buf).expect("utf8")
    }

    fn mk_symbols(swapped: &[&str]) -> BTreeMap<String, PartialSwapSymbol> {
        let mut symbols = BTreeMap::new();
        for s in swapped {
            symbols.insert(
                (*s).to_string(),
                PartialSwapSymbol {
                    package: "pkg".to_string(),
                    kind: PartialSwapKind::Named,
                    upstream_export: Some((*s).to_string()),
                },
            );
        }
        symbols
    }

    #[test]
    fn strips_named_export_specifier() {
        let mut module = parse("const a = 1;\nconst b = 2;\nexport { a as foo, b as bar };\n");
        let stats = strip_one_chunk(&mut module, &mk_symbols(&["foo"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(!emitted.contains("foo"), "stripped name leaked:\n{emitted}");
        assert!(emitted.contains("bar"), "kept name missing:\n{emitted}");
        assert_eq!(stats.stripped_export_specifiers, 1);
    }

    #[test]
    fn drops_inline_export_decl_and_dce_kills_pure_body() {
        let mut module = parse("export const e6 = () => true;\nexport const k = 7;\n");
        strip_one_chunk(&mut module, &mk_symbols(&["e6"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            !emitted.contains("e6"),
            "swapped const should be DCE'd:\n{emitted}",
        );
        assert!(
            emitted.contains("export const k"),
            "non-swapped const dropped:\n{emitted}",
        );
    }

    #[test]
    fn keeps_implementation_when_cross_referenced() {
        let mut module = parse(
            "class ZodObject {}\nconst object = ()=>new ZodObject();\nexport { object as o, ZodObject as Z };\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["o"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("class ZodObject"),
            "live cross-ref should keep ZodObject:\n{emitted}",
        );
        assert!(
            !emitted.contains("const object"),
            "dead `object` body should be removed:\n{emitted}",
        );
        assert!(
            emitted.contains("ZodObject as Z"),
            "residual export of ZodObject should remain:\n{emitted}",
        );
    }

    #[test]
    fn retains_side_effect_init_among_swapped() {
        let mut module = parse(
            "const carrier = {};\nObject.defineProperty(carrier, \"_zod\", { value: {} });\nexport const e6 = ()=>true;\n",
        );
        strip_one_chunk(&mut module, &mk_symbols(&["e6"]), "chunk.js").unwrap();
        let emitted = emit(&module);
        assert!(
            emitted.contains("Object.defineProperty"),
            "side-effect should be retained:\n{emitted}",
        );
    }

    #[test]
    fn bails_when_swapped_name_not_locally_exported() {
        let mut module = parse("export { stuff } from \"./peer.js\";\n");
        let err = strip_one_chunk(&mut module, &mk_symbols(&["stuff"]), "chunk.js")
            .expect_err("should fail");
        assert!(
            err.to_string()
                .contains("not found in any chunk-local export"),
            "wrong error: {err}",
        );
    }

    #[test]
    fn call_init_classifies_as_side_effect() {
        let module = parse("const a = sideEffect();\n");
        let an = classify_item(&module.body[0]);
        assert!(
            an.is_side_effect,
            "call init should be a side-effect anchor"
        );
        assert_eq!(an.declared, vec!["a".to_string()]);
        assert!(an.reads.contains("sideEffect"));
    }

    #[test]
    fn pure_object_literal_init_is_not_side_effect() {
        let module = parse("const a = { x: 1 };\n");
        let an = classify_item(&module.body[0]);
        assert!(
            !an.is_side_effect,
            "object literal init should be a pure decl",
        );
    }
}
