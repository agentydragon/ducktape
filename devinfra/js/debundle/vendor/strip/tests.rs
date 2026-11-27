use spec::PartialSwapKind;
use spec::vendor_marks::PartialSwapSymbol;
use swc_common::sync::Lrc;
use swc_common::{DUMMY_SP, FileName, SourceMap};
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
                local: None,
            },
        );
    }
    symbols
}

fn mk_symbols_with_packages(swapped: &[(&str, &str)]) -> BTreeMap<String, PartialSwapSymbol> {
    let mut symbols = BTreeMap::new();
    for (name, package) in swapped {
        symbols.insert(
            (*name).to_string(),
            PartialSwapSymbol {
                package: (*package).to_string(),
                kind: PartialSwapKind::Named,
                upstream_export: Some((*name).to_string()),
                local: None,
            },
        );
    }
    symbols
}

fn id(name: &str) -> Id {
    Ident::new_no_ctxt(name.into(), DUMMY_SP).to_id()
}

#[test]
fn local_swap_split_brain_reports_unrewritten_residual_call() {
    let mut module = parse(
        "function nY(t) { return `vendor:${t.name}`; }\nconst schema = nY(URL);\nexport { schema };\n",
    );
    let symbols = BTreeMap::from([(
        "zodInstanceof".to_string(),
        PartialSwapSymbol {
            package: "zod".to_string(),
            kind: PartialSwapKind::Named,
            upstream_export: Some("instanceof".to_string()),
            local: Some("nY".to_string()),
        },
    )]);

    let err = strip_one_chunk(&mut module, &symbols, "chunk.js")
        .expect_err("unrewritten local call should be split-brain");

    assert!(
        err.to_string().contains("split-brain vendor swap"),
        "wrong error: {err}",
    );
}

#[test]
fn strips_swapped_island() {
    // Swapping `swapped` out removes the declarations, side effects and
    // mutations only that export reaches, and keeps what a residual export
    // still reads. Each case lists the text that must survive and the text
    // that must be gone. The first four cases differ only in how the shared
    // helper is declared (one `item_is_shareable_helper` branch each).
    let cases: &[(&str, &str, &[&str], &[&str])] = &[
        (
            "shared function declaration",
            "function helper(x) { return x; }\nconst oldImpl = () => helper(\"old\");\nconst keep = () => helper(\"keep\");\nexport { oldImpl as swapped, keep };\n",
            &["function helper", "keep"],
            &["oldImpl"],
        ),
        (
            "shared function expression",
            "const helper = function(x) { return x; };\nconst oldImpl = () => helper(\"old\");\nconst keep = () => helper(\"keep\");\nexport { oldImpl as swapped, keep };\n",
            &["function", "keep"],
            &["oldImpl"],
        ),
        (
            "shared intrinsic alias",
            "const assign = Object.assign;\nconst oldImpl = () => assign({}, { old: true });\nconst keep = () => assign({}, { keep: true });\nexport { oldImpl as swapped, keep };\n",
            &["Object.assign", "keep"],
            &["oldImpl"],
        ),
        (
            "shared primitive literal",
            "const preloadRel = \"modulepreload\";\nconst oldImpl = () => preloadRel;\nconst keep = () => preloadRel;\nexport { oldImpl as swapped, keep };\n",
            &["modulepreload", "keep"],
            &["oldImpl"],
        ),
        (
            "shared Vite dependency map helper",
            "const mapDeps = (i, m = mapDeps, d = m.f || (m.f = [\"a.js\", \"b.js\"])) => i.map((i) => d[i]);\n\
             const oldImpl = () => mapDeps([0]);\n\
             const keep = () => mapDeps([1]);\n\
             export { oldImpl as swapped, keep };\n",
            &["mapDeps"],
            &["oldImpl"],
        ),
        (
            "shared Vite preload helper cluster",
            "const rel = \"modulepreload\";\n\
             const base = function(path) { return \"/\" + path; };\n\
             const seen = {};\n\
             const preload = function(load, deps) {\n\
                 let promise = Promise.resolve();\n\
                 if (deps && deps.length > 0) {\n\
                     promise = Promise.allSettled(deps.map((dep) => {\n\
                         dep = base(dep);\n\
                         if (dep in seen) return;\n\
                         seen[dep] = true;\n\
                         const link = document.createElement(\"link\");\n\
                         link.rel = rel;\n\
                     }));\n\
                 }\n\
                 function onError(error) { window.dispatchEvent(error); throw error; }\n\
                 return promise.then(() => load().catch(onError));\n\
             };\n\
             const oldImpl = () => preload(() => Promise.resolve(\"old\"), [\"old.js\"]);\n\
             const keep = () => preload(() => Promise.resolve(\"keep\"), [\"keep.js\"]);\n\
             export { oldImpl as swapped, keep };\n",
            &["modulepreload", "seen"],
            &["oldImpl"],
        ),
        (
            "side effect touching only the swapped island",
            "const internals = {};\n\
             const oldImpl = () => internals;\n\
             if (globalThis.__HOOK__) globalThis.__HOOK__.inject({ internals });\n\
             export { oldImpl as swapped };\n\
             export const keep = 1;\n",
            &["export const keep"],
            &["__HOOK__"],
        ),
        (
            "private registration chain of the swapped island",
            "const table = {};\n\
             const internals = {};\n\
             function register(name) { table[name] = internals; }\n\
             for (var i = 0; i < 2; i++) register(String(i));\n\
             const oldImpl = () => internals;\n\
             export { oldImpl as swapped };\n\
             export const keep = 1;\n",
            &["export const keep"],
            &["register", "table", "internals"],
        ),
        (
            "local `Object.freeze` of a dropped target",
            "const EMPTY = {};\nObject.freeze(EMPTY);\nconst make = () => EMPTY;\nexport { make as swapped };\n",
            &[],
            &["EMPTY", "freeze"],
        ),
        (
            "intrinsic `Object.assign` mutation",
            "const tag = \"computed\";\nconst decorator = make(tag);\nconst computed = () => decorator;\nObject.assign(computed, decorator);\ncomputed.struct = wrap(decorator);\nexport { computed as swapped };\n",
            &[],
            &["computed", "decorator", "Object.assign"],
        ),
        (
            "prototype inheritance initializer",
            "function Base() {}\nfunction Derived() {}\nvar proto = (Derived.prototype = new Base());\nconst make = () => Derived;\nexport { make as swapped };\n",
            &[],
            &["Derived", "prototype", "Base"],
        ),
        (
            "local binding assignment",
            "let assigned;\nconst source = { value: 1 };\nassigned = source.value;\nconst make = () => assigned;\nexport { make as swapped };\n",
            &[],
            &["assigned", "source"],
        ),
        (
            "CommonJS module wrapper",
            "var module = { exports: {} };\n(function (target) { (function () { var has = {}.hasOwnProperty; function clsx() {} target.exports ? ((clsx.default = clsx), (target.exports = clsx)) : (window.classNames = clsx); })(); })(module);\nvar clsx = module.exports;\nconst make = () => clsx;\nexport { make as swapped };\n",
            &[],
            &["module", "clsx", "classNames"],
        ),
        (
            "CommonJS wrapper reading the shared global fallback",
            "var root = typeof globalThis < \"u\" ? globalThis : typeof window < \"u\" ? window : typeof global < \"u\" ? global : typeof self < \"u\" ? self : {};\n\
             var module = { exports: {} };\n\
             (function (target) { (function (global, factory) { target.exports = factory(); })(root, function () { function dayjs() {} return dayjs; }); })(module);\n\
             var dayjs = module.exports;\n\
             const keep = root;\n\
             export { dayjs as swapped, keep };\n",
            &["globalThis", "keep"],
            &["dayjs", "factory", "module"],
        ),
        (
            "object-iteration prototype mutation",
            "const define = Object.defineProperty;\nvar methods = { clear: function () { return this.splice(0); } };\nfunction ObservableArray() {}\nObject.entries(methods).forEach(function (entry) { var key = entry[0], value = entry[1]; key !== \"concat\" && define(ObservableArray.prototype, key, value); });\nconst make = () => ObservableArray;\nexport { make as swapped };\n",
            &[],
            &["ObservableArray", "Object.entries", "methods"],
        ),
        (
            "object-iteration wrapper mutation",
            "function define(target, key, value) { Object.defineProperty(target, key, { configurable: true, value }); }\nvar methods = { clear: function () { return this.splice(0); } };\nfunction ObservableArray() {}\nObject.entries(methods).forEach(function (entry) { var key = entry[0], value = entry[1]; key !== \"concat\" && define(ObservableArray.prototype, key, value); });\nconst make = () => ObservableArray;\nexport { make as swapped };\n",
            &[],
            &["ObservableArray", "Object.entries", "methods"],
        ),
        (
            "local namespace augmentation",
            "var ns = {};\n(function (target) { target.reject = wrap(\"reject\"); function resolve() {} target.resolve = wrap(resolve); })(ns || (ns = {}));\nconst make = () => ns;\nexport { make as swapped };\n",
            &[],
            &["reject", "resolve", "ns"],
        ),
    ];
    for &(label, source, retained, dropped) in cases {
        let mut module = parse(source);
        strip_one_chunk(&mut module, &mk_symbols(&["swapped"]), "chunk.js").expect(label);
        let emitted = emit(&module);
        for &text in retained {
            assert!(
                emitted.contains(text),
                "{label}: `{text}` should be retained:\n{emitted}",
            );
        }
        for &text in dropped {
            assert!(
                !emitted.contains(text),
                "{label}: `{text}` should be dropped:\n{emitted}",
            );
        }
    }
}

#[test]
fn allows_multi_package_shared_dependency_cell() {
    let mut module = parse(
        "const shared = {};\nconst oldA = () => shared;\nconst oldB = () => shared;\nconst keep = () => shared;\nexport { oldA as swappedA, oldB as swappedB, keep };\n",
    );
    strip_one_chunk(
        &mut module,
        &mk_symbols_with_packages(&[("swappedA", "pkg-a"), ("swappedB", "pkg-b")]),
        "chunk.js",
    )
    .unwrap();
    let emitted = emit(&module);
    assert!(
        emitted.contains("shared"),
        "residual export should keep shared dependency cell:\n{emitted}",
    );
    assert!(
        !emitted.contains("oldA") && !emitted.contains("oldB"),
        "swapped package roots should be removed:\n{emitted}",
    );
}

#[test]
fn drops_mutation_targeting_swapped_import_rewrite() {
    let mut module = parse(
        "import __debundle_bps_swapped from \"./vendor/swapped.js\";\nconst tag = \"computed\";\nconst decorator = makeDecorator(tag);\nfunction swapped() { return decorator; }\nObject.assign(__debundle_bps_swapped, decorator);\nexport { swapped };\n",
    );
    let replacement_import_locals = BTreeSet::from([id("__debundle_bps_swapped")]);
    strip_one_chunk_with_replacement_imports(
        &mut module,
        &mk_symbols(&["swapped"]),
        "chunk.js",
        &replacement_import_locals,
        None,
    )
    .unwrap();
    let emitted = emit(&module);
    assert!(
        !emitted.contains("makeDecorator")
            && !emitted.contains("Object.assign")
            && !emitted.contains("tag"),
        "mutation of the imported swapped facade should not keep the old implementation:\n{emitted}",
    );
}

#[test]
fn allows_residual_use_of_replacement_facade() {
    let mut module = parse(
        "import __debundle_bps_react_default from \"./vendor/react.js\";\nimport __debundle_bps_react_ns from \"./vendor/react.js\";\nfunction merge(ns, extras) { return ns; }\nconst ReactFacade = merge({ __proto__: null, default: __debundle_bps_react_default }, [__debundle_bps_react_ns]);\nconst oldImpl = () => ReactFacade;\nconst residualHook = ReactFacade.useInsertionEffect ? ReactFacade.useInsertionEffect : false;\nexport { oldImpl as swapped, residualHook };\n",
    );
    let replacement_import_locals = BTreeSet::from([
        id("__debundle_bps_react_default"),
        id("__debundle_bps_react_ns"),
    ]);
    strip_one_chunk_with_replacement_imports(
        &mut module,
        &mk_symbols(&["swapped"]),
        "chunk.js",
        &replacement_import_locals,
        None,
    )
    .unwrap();
    let emitted = emit(&module);
    assert!(
        emitted.contains("ReactFacade") && emitted.contains("residualHook"),
        "residual use of replacement facade should remain:\n{emitted}",
    );
    assert!(
        !emitted.contains("oldImpl"),
        "swapped original facade user should be removed:\n{emitted}",
    );
}

#[test]
fn retains_local_namespace_iife_for_residual_export() {
    let mut module = parse(
        "var ns = {};\n(function (target) { target.reject = wrap(\"reject\"); function resolve() {} target.resolve = wrap(resolve); })(ns || (ns = {}));\nexport { ns as keep };\n",
    );
    strip_one_chunk(&mut module, &mk_symbols(&[]), "chunk.js").unwrap();
    let emitted = emit(&module);
    assert!(
        emitted.contains("reject") && emitted.contains("resolve"),
        "residual namespace export should keep augmentation:\n{emitted}",
    );
}

#[test]
fn bails_when_swapped_name_not_locally_exported() {
    let mut module = parse("export { stuff } from \"./peer.js\";\n");
    let err =
        strip_one_chunk(&mut module, &mk_symbols(&["stuff"]), "chunk.js").expect_err("should fail");
    assert!(
        err.to_string()
            .contains("not found in any chunk-local export"),
        "wrong error: {err}",
    );
}

#[test]
fn call_init_classifies_as_side_effect() {
    let module = parse("const a = sideEffect();\n");
    let analyses = analyze_prune_items(&module, "chunk.js").unwrap();
    let an = &analyses[0];
    assert!(
        an.side_effect == SideEffectKind::Hard,
        "call init should be a side-effect anchor"
    );
    assert_eq!(
        an.declared.iter().map(id_name).collect::<Vec<_>>(),
        vec!["a".to_string()]
    );
    assert!(an.reads.iter().any(|id| id_name(id) == "sideEffect"));
}

#[test]
fn pure_object_literal_init_is_not_side_effect() {
    let module = parse("const a = { x: 1 };\n");
    let analyses = analyze_prune_items(&module, "chunk.js").unwrap();
    let an = &analyses[0];
    assert!(
        an.side_effect == SideEffectKind::None,
        "object literal init should be a pure decl",
    );
}
