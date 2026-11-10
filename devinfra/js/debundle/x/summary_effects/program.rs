//! Facts about the other chunks of the program a chunk belongs to: whether
//! the chunk is on an import cycle, and what any chunk writes on globals.

use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

use anyhow::{Context, Result};
use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::helpers::prepass;
use crate::intrinsics::{Census, census};

pub struct ProgramFacts {
    pub chunks: usize,
    /// Another chunk can run while this one initializes and reach it: a
    /// static import cycle through the chunk, or a dynamic `import()` in the
    /// chunk's static dependencies that may load it.
    pub on_import_cycle: bool,
    /// What any chunk of the program writes on globals.
    pub census: Census,
    /// Every chunk, parsed, by the path imports resolve to.
    pub modules: Vec<(PathBuf, js_ast::ParsedJsModule)>,
    /// The analysed chunk's path among them.
    pub chunk: PathBuf,
    /// Imported file name to the name the chunk is stored under.
    aliases: FxHashMap<PathBuf, PathBuf>,
}

impl ProgramFacts {
    /// The index in `modules` of the chunk `specifier` names in `from`.
    pub fn resolve(&self, from: &Path, specifier: &str) -> Option<usize> {
        let target = resolve(from, specifier, &self.aliases)?;
        self.modules.iter().position(|(path, _)| *path == target)
    }
}

#[derive(Default)]
struct Imports {
    statics: Vec<String>,
    /// `None` for a specifier that is not a string literal.
    dynamics: Vec<Option<String>>,
    /// `import.meta.require` of something other than a string literal.
    unknown_requires: usize,
}

impl Visit for Imports {
    fn visit_import_decl(&mut self, import: &ImportDecl) {
        self.statics.push(js_ast::str_value(&import.src));
    }

    fn visit_named_export(&mut self, export: &NamedExport) {
        if let Some(source) = &export.src {
            self.statics.push(js_ast::str_value(source));
        }
    }

    fn visit_export_all(&mut self, export: &ExportAll) {
        self.statics.push(js_ast::str_value(&export.src));
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        // `import.meta.require("./x.js")` evaluates the chunk before it
        // returns, wherever the call is written.
        let requires = matches!(&call.callee, Callee::Expr(callee)
            if matches!(&**callee, Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(method),
                ..
            }) if &*method.sym == "require"
                && matches!(&**obj, Expr::MetaProp(meta) if meta.kind == MetaPropKind::ImportMeta)));
        if requires {
            match call.args.first().map(|arg| &*arg.expr) {
                Some(Expr::Lit(Lit::Str(specifier))) => {
                    self.statics.push(js_ast::str_value(specifier));
                }
                _ => self.unknown_requires += 1,
            }
        }
        if matches!(call.callee, Callee::Import(_)) {
            self.dynamics
                .push(match call.args.first().map(|arg| &*arg.expr) {
                    Some(Expr::Lit(Lit::Str(specifier))) => Some(js_ast::str_value(specifier)),
                    _ => None,
                });
        }
        call.visit_children_with(self);
    }
}

/// `from`'s directory joined with a relative specifier, without touching the
/// file system; `None` for a bare (package) specifier.
fn resolve(from: &Path, specifier: &str, aliases: &FxHashMap<PathBuf, PathBuf>) -> Option<PathBuf> {
    if !specifier.starts_with('.') && !specifier.starts_with('/') {
        return None;
    }
    let mut out = PathBuf::new();
    let joined = from.parent().unwrap_or(Path::new("")).join(specifier);
    for component in joined.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    if let Some(alias) = aliases.get(&out) {
        return Some(alias.clone());
    }
    Some(out)
}

fn files(root: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in std::fs::read_dir(root).with_context(|| format!("listing {}", root.display()))? {
        let path = entry?.path();
        if path.is_dir() {
            files(&path, out)?;
        } else if path.extension().is_some_and(|extension| extension == "js") {
            out.push(path);
        }
    }
    Ok(())
}

/// Reads every `.js` file under `root` and relates them to `chunk`.
/// `file_list` is the pipeline's `js-files.txt`: its `alias<TAB>original`
/// lines name chunks stored under `alias` that imports name `original`.
pub fn load(root: &Path, chunk: &Path, file_list: Option<&Path>) -> Result<ProgramFacts> {
    let mut paths = Vec::new();
    files(root, &mut paths)?;
    paths.sort();
    let canonical_root = root
        .canonicalize()
        .with_context(|| format!("resolving {}", root.display()))?;
    let mut aliases = FxHashMap::default();
    if let Some(list) = file_list {
        let text =
            std::fs::read_to_string(list).with_context(|| format!("reading {}", list.display()))?;
        for line in text.lines() {
            if let Some((alias, original)) = line.split_once('\t') {
                aliases.insert(canonical_root.join(original), canonical_root.join(alias));
            }
        }
    }
    let mut modules = Vec::new();
    let chunk = chunk
        .canonicalize()
        .with_context(|| format!("resolving {}", chunk.display()))?;
    let mut graph: BTreeMap<PathBuf, Imports> = BTreeMap::new();
    let mut merged = Census::default();
    for path in &paths {
        let path = path.canonicalize()?;
        let source = std::fs::read_to_string(&path)
            .with_context(|| format!("reading {}", path.display()))?;
        let parsed = js_ast::parse_js_module(&path.display().to_string(), &source)?;
        let unresolved = SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
        let mut imports = Imports::default();
        parsed.module.visit_with(&mut imports);
        let aliases = prepass(&parsed.module, unresolved).object_aliases;
        merged.merge(census(&parsed.module, unresolved, &aliases));
        graph.insert(path.clone(), imports);
        modules.push((path, parsed));
    }
    anyhow::ensure!(
        graph.contains_key(&chunk),
        "{} is not under {}",
        chunk.display(),
        root.display()
    );
    let static_closure = |start: &Path| -> FxHashSet<PathBuf> {
        let mut seen = FxHashSet::default();
        let mut pending = vec![start.to_path_buf()];
        while let Some(path) = pending.pop() {
            let Some(imports) = graph.get(&path) else {
                continue;
            };
            for specifier in &imports.statics {
                if let Some(target) = resolve(&path, specifier, &aliases)
                    && seen.insert(target.clone())
                {
                    pending.push(target);
                }
            }
        }
        seen
    };
    let dependencies = static_closure(&chunk);
    let static_cycle = dependencies.contains(&chunk);
    // A dependency that dynamically imports this chunk, or a chunk that
    // statically reaches it, or an unknown specifier, may start it early.
    let dynamic_cycle = dependencies.iter().any(|dependency| {
        graph.get(dependency).is_some_and(|imports| {
            imports.dynamics.iter().any(|specifier| {
                match specifier
                    .as_deref()
                    .and_then(|specifier| resolve(dependency, specifier, &aliases))
                {
                    Some(target) => target == chunk || static_closure(&target).contains(&chunk),
                    None => true,
                }
            })
        })
    });
    // A synchronous load of an unknown chunk, by this chunk or one it has
    // loaded, may be of a chunk that imports this one.
    let unknown_require = std::iter::once(&chunk).chain(&dependencies).any(|path| {
        graph
            .get(path)
            .is_some_and(|imports| imports.unknown_requires > 0)
    });
    Ok(ProgramFacts {
        chunks: graph.len(),
        on_import_cycle: static_cycle || dynamic_cycle || unknown_require,
        census: merged,
        modules,
        chunk,
        aliases,
    })
}
