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
    /// Top-level `await` sites in any chunk of the program.
    pub top_level_awaits: usize,
    /// Chunks that did not parse. Each is taken to write every global, to
    /// await at its top level, and to load any chunk.
    pub unparsed_chunks: usize,
    /// Relative or rooted import specifiers, in any chunk, that name no
    /// chunk read: each is an edge the cycle verdict does not see.
    pub unresolved_specifiers: usize,
    resolution: Resolution,
}

/// How an import specifier names a chunk file.
#[derive(Default)]
struct Resolution {
    /// Imported file name to the name the chunk is stored under.
    aliases: FxHashMap<PathBuf, PathBuf>,
    /// Prefixes of absolute specifiers that stand for the chunk directory.
    roots: Vec<String>,
    directory: PathBuf,
}

impl ProgramFacts {
    /// The index in `modules` of the chunk `specifier` names in `from`.
    pub fn resolve(&self, from: &Path, specifier: &str) -> Option<usize> {
        let target = resolve(from, specifier, &self.resolution)?;
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
    /// `await` and `for await` outside any function.
    top_level_awaits: usize,
    function_depth: usize,
}

impl Visit for Imports {
    fn visit_function(&mut self, function: &Function) {
        self.function_depth += 1;
        function.visit_children_with(self);
        self.function_depth -= 1;
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.function_depth += 1;
        arrow.visit_children_with(self);
        self.function_depth -= 1;
    }

    fn visit_await_expr(&mut self, awaited: &AwaitExpr) {
        self.top_level_awaits += usize::from(self.function_depth == 0);
        awaited.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, looped: &ForOfStmt) {
        self.top_level_awaits += usize::from(looped.is_await && self.function_depth == 0);
        looped.visit_children_with(self);
    }

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

/// The chunk file a specifier names, without touching the file system:
/// `from`'s directory joined with a relative specifier, or the chunk
/// directory joined with what follows a declared root prefix. `None` for
/// any other (package) specifier.
fn resolve(from: &Path, specifier: &str, resolution: &Resolution) -> Option<PathBuf> {
    let rooted = resolution
        .roots
        .iter()
        .find_map(|root| specifier.strip_prefix(root.as_str()));
    let joined = match rooted {
        Some(rest) => resolution.directory.join(rest.trim_start_matches('/')),
        None if specifier.starts_with('.') => {
            from.parent().unwrap_or(Path::new("")).join(specifier)
        }
        None => return None,
    };
    let mut out = PathBuf::new();
    for component in joined.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    if let Some(alias) = resolution.aliases.get(&out) {
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
/// `specifier_roots` are prefixes of absolute import specifiers that stand
/// for `root`.
pub fn load(
    root: &Path,
    chunk: &Path,
    file_list: Option<&Path>,
    specifier_roots: &[String],
) -> Result<ProgramFacts> {
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
        // The list names the program's chunks; other files in the
        // directory are not part of it.
        paths.clear();
        for line in text.lines().filter(|line| !line.trim().is_empty()) {
            let stored = match line.split_once('\t') {
                Some((alias, original)) => {
                    aliases.insert(canonical_root.join(original), canonical_root.join(alias));
                    alias
                }
                None => line,
            };
            if stored.ends_with(".js") {
                paths.push(root.join(stored));
            }
        }
        paths.sort();
    }
    let resolution = Resolution {
        aliases,
        roots: specifier_roots.to_vec(),
        directory: canonical_root,
    };
    let mut modules = Vec::new();
    let mut unparsed_chunks = 0;
    let chunk = chunk
        .canonicalize()
        .with_context(|| format!("resolving {}", chunk.display()))?;
    let mut graph: BTreeMap<PathBuf, Imports> = BTreeMap::new();
    let mut merged = Census::default();
    for path in &paths {
        let path = path.canonicalize()?;
        // A chunk may embed bytes that are not UTF-8 in a string.
        let bytes = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
        let source = String::from_utf8_lossy(&bytes);
        // A chunk that does not parse may do anything: write any global,
        // await, and load any chunk.
        let Ok(parsed) = js_ast::parse_js_module(&path.display().to_string(), &source) else {
            anyhow::ensure!(path != chunk, "{} does not parse", path.display());
            unparsed_chunks += 1;
            merged.refuse_everything();
            graph.insert(
                path,
                Imports {
                    unknown_requires: 1,
                    top_level_awaits: 1,
                    ..Imports::default()
                },
            );
            continue;
        };
        let unresolved = SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
        let mut imports = Imports::default();
        parsed.module.visit_with(&mut imports);
        let object_aliases = prepass(&parsed.module, unresolved).object_aliases;
        let mut written = census(&parsed.module, unresolved, &object_aliases);
        written.locate(modules.len());
        merged.merge(written);
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
                if let Some(target) = resolve(&path, specifier, &resolution)
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
    // A dynamic `import()` settles in a later job. Without a top-level
    // `await` anywhere in the program, a module graph evaluates in one job,
    // so that job cannot run while this chunk is half evaluated. With one,
    // evaluation suspends, and a dependency that dynamically imports this
    // chunk, or a chunk that statically reaches it, or an unknown specifier,
    // may run against it.
    let top_level_awaits: usize = graph.values().map(|imports| imports.top_level_awaits).sum();
    let dynamic_cycle = top_level_awaits > 0
        && dependencies.iter().any(|dependency| {
            graph.get(dependency).is_some_and(|imports| {
                imports.dynamics.iter().any(|specifier| {
                    match specifier
                        .as_deref()
                        .and_then(|specifier| resolve(dependency, specifier, &resolution))
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
        top_level_awaits,
        unparsed_chunks,
        unresolved_specifiers: graph
            .iter()
            .flat_map(|(path, imports)| {
                let dynamics = imports.dynamics.iter().flatten();
                imports
                    .statics
                    .iter()
                    .chain(dynamics)
                    .map(move |specifier| (path, specifier))
            })
            .filter(|(path, specifier)| {
                resolve(path, specifier, &resolution)
                    .is_some_and(|target| !graph.contains_key(&target))
            })
            .count(),
        resolution,
    })
}
