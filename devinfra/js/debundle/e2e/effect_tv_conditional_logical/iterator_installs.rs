//! Iterators installed through bindings, receivers, accessors, chains and classes run where the protocol finds them.

use super::{CALL_FLAG, assert_reader_split_diverges_with, with_reader_split};
use debundle_e2e_support::*;

/// Opposing control: more iterator-protocol sites. A `catch` parameter's
/// array pattern iterates the thrown value; a default below the root of a
/// pattern (declaration, assignment, parameter, `for`-`of` head, `catch`)
/// supplies an iterated value; and `new Map`/`new WeakMap`/
/// `Object.fromEntries`/`new Set` iterate their argument. Each reaches
/// `second` through a generator or a hand-written iterator.
#[test]
fn every_iterator_protocol_site_runs_the_iterator() {
    const GEN: &str = "function* gen() { second(); yield 0; }";
    const ENTRIES: &str = "function* gen() { second(); yield [{}, 1]; }";
    const MK: &str = "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }";
    for (definitions, resume, extra) in [
        (GEN, "try { throw gen(); } catch ([x]) {}", &["gen"][..]),
        (
            GEN,
            "try { throw { a: gen() }; } catch ({ a: [x] }) {}",
            &["gen"][..],
        ),
        (MK, "try { throw mk(); } catch ([x]) {}", &["mk"][..]),
        (MK, "const [[x] = mk()] = [];", &["mk"][..]),
        (MK, "let x; [[x] = mk()] = [];", &["mk"][..]),
        (MK, "const { a: [x] = mk() } = {};", &["mk"][..]),
        (MK, "for (const [[x] = mk()] of [[]]) {}", &["mk"][..]),
        (MK, "try { throw []; } catch ([[x] = mk()]) {}", &["mk"][..]),
        (
            "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }\nfunction take([[x] = mk()]) { return x; }",
            "take([]);",
            &["mk", "take"][..],
        ),
        (ENTRIES, "new Map(gen());", &["gen"][..]),
        (ENTRIES, "new WeakMap(gen());", &["gen"][..]),
        (ENTRIES, "Object.fromEntries(gen());", &["gen"][..]),
        (GEN, "new Set(gen());", &["gen"][..]),
        (GEN, "Array.from(gen());", &["gen"][..]),
        (GEN, "Promise.all(gen());", &["gen"][..]),
    ] {
        for guard in ["", "if (first()) {} "] {
            for run in ["function run() { flag(); return 0; }", CALL_FLAG] {
                assert_reader_split_diverges_with(
                    &format!(
                        "{definitions}\nfunction flag() {{ {guard}{resume} return noop; }}\n{run}"
                    ),
                    extra,
                );
            }
        }
    }
}

/// Positive anchor for the control above: iterating a string, the native
/// pairs of `Object.entries`, and native arrays through patterns keep the
/// split.
#[test]
fn iterating_strings_entries_and_native_patterns_keeps_the_split() {
    for body in [
        "for (const c of 'ab') {}",
        "for (const [k, v] of Object.entries({ a: 1 })) {}",
        "const [[y] = [1]] = [];",
        "try { throw [1]; } catch ([x]) {}",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {body} return noop; }}\nfunction run() {{ flag(); return 0; }}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// Opposing control: an iterator installed where the protocol later finds
/// it: an own `[Symbol.iterator]` written on `arguments` or by an analysed
/// callee on an array, and `String.prototype[Symbol.iterator]` installed by
/// `Object.defineProperty` or an assignment, found by a string iteration or
/// by an array pattern in a `for`-`in` head; a default in a `for`-`in`
/// assignment head; and `for await` over a generator. Each reaches `second`.
#[test]
fn an_installed_iterator_runs_where_the_protocol_finds_it() {
    const STRING_DEFINE: &str = "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: function* () { second(); } });";
    const STRING_ASSIGN: &str =
        "const patched = (String.prototype[Symbol.iterator] = function* () { second(); });";
    for (definitions, extra) in [
        (
            "function take() { if (first()) {} arguments[Symbol.iterator] = function* () { second(); }; for (const x of arguments) {} }\nfunction flag() { if (first()) {} take(1); return noop; }\nfunction run() { if (first()) {} flag(); return 0; }".to_string(),
            &["take"][..],
        ),
        (
            "function patch(a) { if (first()) {} a[Symbol.iterator] = function* () { second(); }; }\nfunction flag() { if (first()) {} const a = [1]; patch(a); for (const x of a) {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }".to_string(),
            &["patch"][..],
        ),
        (
            format!("{STRING_DEFINE}\nfunction flag() {{ for (const x of 'ab') {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ for (const x of 'ab') {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ for (const [c] in {{ ab: 1 }}) {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            format!("{STRING_ASSIGN}\nfunction flag() {{ let c; for ([c] in {{ ab: 1 }}) {{}} return noop; }}\nfunction run() {{ flag(); return 0; }}"),
            &["patched"][..],
        ),
        (
            "function mk() { return { [Symbol.iterator]() { return { next() { second(); return { done: true }; } }; } }; }\nfunction flag() { let x; for ([, , [x] = mk()] in { ab: 1 }) {} return noop; }\nfunction run() { flag(); return 0; }".to_string(),
            &["mk"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nasync function loop() { for await (const x of gen()) {} }\nfunction flag() { loop(); return noop; }\nfunction run() { flag(); return 0; }".to_string(),
            &["gen", "loop"][..],
        ),
        (
            "function* gen() { second(); yield 0; }\nasync function loop() { if (first()) {} for await (const x of gen()) {} }\nfunction flag() { if (first()) {} loop(); return noop; }\nfunction run() { return flag()(); }".to_string(),
            &["gen", "loop"][..],
        ),
    ] {
        assert_reader_split_diverges_with(&definitions, extra);
    }
}

/// A truncated top-level install publishes what its unexecuted remainder
/// reads: the iterator reaches the protocol through a binding declared
/// before or after it, a class static member, an object method, a factory
/// result, or an inline literal copied by `Object.assign`; and behind a
/// guarded or a returned-callee iteration.
#[test]
fn an_iterator_installed_through_a_binding_runs_where_the_protocol_finds_it() {
    const FLAG: &str = "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }";
    const DEFINE: &str =
        "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: it });";
    for (install, extra) in iterator_installs(DEFINE, "second") {
        assert_reader_split_diverges_with(&format!("{install}\n{FLAG}"), extra);
    }
    for flag in [
        "function flag() { if (first()) {} for (const x of 'ab') {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
        "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { return flag()(); }",
    ] {
        assert_reader_split_diverges_with(
            &format!("function* it() {{ second(); }}\n{DEFINE}\n{flag}"),
            &["it", "patched"],
        );
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_a_binding_without_effects_keeps_the_split() {
    const FLAG: &str = "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }";
    const DEFINE: &str =
        "const patched = Object.defineProperty(String.prototype, Symbol.iterator, { value: it });";
    for (install, extra) in iterator_installs(DEFINE, "first") {
        let fixture = with_reader_split(&format!("{install}\n{FLAG}"), extra, run_fixture);
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn iterator_installs(define: &str, callee: &str) -> Vec<(String, &'static [&'static str])> {
    vec![
        (
            format!(
                "const desc = {{ value: function* () {{ {callee}(); }} }};\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, desc);"
            ),
            &["desc", "patched"][..],
        ),
        (
            format!("function* it() {{ {callee}(); }}\n{define}"),
            &["it", "patched"][..],
        ),
        (
            format!("{define}\nfunction* it() {{ {callee}(); }}"),
            &["patched", "it"][..],
        ),
        (
            format!("const it = function* () {{ {callee}(); }};\n{define}"),
            &["it", "patched"][..],
        ),
        (
            format!(
                "class K {{ static *it() {{ {callee}(); }} }}\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: K.it }});"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "class K {{ static it = function* () {{ {callee}(); }}; }}\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: K.it }});"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "const impl = {{ *it() {{ {callee}(); }} }};\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: impl.it }});"
            ),
            &["impl", "patched"][..],
        ),
        (
            format!(
                "function mkIt() {{ return function* () {{ {callee}(); }}; }}\nconst it = mkIt();\n{define}"
            ),
            &["mkIt", "it", "patched"][..],
        ),
        (
            format!(
                "const patched = Object.assign(String.prototype, {{ [Symbol.iterator]: function* () {{ {callee}(); }} }});"
            ),
            &["patched"][..],
        ),
    ]
}

/// Positive anchor for the control above: the same iterations with no
/// installed iterator keep the split.
#[test]
fn iterations_without_an_installed_iterator_keep_the_split() {
    for body in [
        "for (const [c] in { ab: 1 }) {}",
        "let c; for ([c] in { ab: 1 }) {}",
        "const a = [1]; for (const x of a) {}",
    ] {
        let fixture = with_reader_split(
            &format!(
                "function flag() {{ {body} return noop; }}\nfunction run() {{ flag(); return 0; }}"
            ),
            &[],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// An install publishes the receiver a method reads through `this` (directly
/// or from an arrow), and what a class's field, constructor or base
/// constructor (a class, a function, or a class expression in `extends`)
/// stored on the instance it reads.
#[test]
fn an_iterator_installed_through_a_receiver_runs_where_the_protocol_finds_it() {
    for (install, extra) in receiver_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_a_receiver_without_effects_keeps_the_split() {
    for (install, extra) in receiver_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// A string iteration reached plainly, behind a guard, and through a returned
/// callee.
const ITERATING_FLAGS: [&str; 3] = [
    "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { flag(); return 0; }",
    "function flag() { if (first()) {} for (const x of 'ab') {} return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
    "function flag() { for (const x of 'ab') {} return noop; }\nfunction run() { return flag()(); }",
];

fn receiver_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 7] {
    [
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ return Object.defineProperty(String.prototype, Symbol.iterator, {{ value: this.it }}); }} }};\nconst patched = obj.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{ constructor() {{ this.it = it; }} }}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ const read = () => this.it; return Object.defineProperty(String.prototype, Symbol.iterator, {{ value: read() }}); }} }};\nconst patched = obj.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{ it = it; }}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass Base {{ constructor() {{ this.it = it; }} }}\nclass K extends Base {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "Base", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nfunction Base() {{ this.it = it; }}\nclass K extends Base {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "Base", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K extends (class {{ constructor() {{ this.it = it; }} }}) {{}}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "k", "patched"][..],
        ),
    ]
}

/// An install publishes the receiver an accessor or an optional-chain method
/// reads through `this`, and what another statement stored on a class's
/// prototype that a refused instance inherits.
#[test]
fn an_iterator_installed_through_an_accessor_or_prototype_runs_where_the_protocol_finds_it() {
    for (install, extra) in accessor_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_an_accessor_or_prototype_without_effects_keeps_the_split() {
    for (install, extra) in accessor_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn accessor_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 4] {
    const DEFINE: &str =
        "Object.defineProperty(String.prototype, Symbol.iterator, { value: this.it })";
    [
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, get install() {{ return {DEFINE}; }} }};\nconst patched = obj.install;"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, set install(v) {{ {DEFINE}; }} }};\nconst patched = (obj.install = 1);"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: function* () {{ {callee}(); }}, install() {{ return {DEFINE}; }} }};\nconst patched = obj?.install();"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "function* it() {{ {callee}(); }}\nclass K {{}}\nconst set = (K.prototype.it = it);\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k.it }});"
            ),
            &["it", "K", "set", "k", "patched"][..],
        ),
    ]
}

/// An install publishes the receiver of an accessor found on its prototype
/// chain or on a class (instance or static), and of a setter run by a
/// destructuring target; and a refused instance reaches what another
/// statement stored on its class or prototype, through `new.target`, an
/// alias of `K.prototype`, or `k.constructor`.
#[test]
fn an_iterator_installed_through_a_chain_or_class_surface_runs_where_the_protocol_finds_it() {
    for (install, extra) in chain_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_installed_through_a_chain_or_class_surface_without_effects_keeps_the_split() {
    for (install, extra) in chain_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn chain_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 10] {
    let define = |value: &str| {
        format!("Object.defineProperty(String.prototype, Symbol.iterator, {{ value: {value} }})")
    };
    let generator = format!("function* () {{ {callee}(); }}");
    let named = format!("function* it() {{ {callee}(); }}");
    let this_it = define("this.it");
    let k_it = define("k.it");
    [
        (
            format!(
                "{named}\nclass K {{ constructor() {{ this.it = new.target.gen; }} }}\nconst s = (K.gen = it);\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["it", "K", "s", "k", "patched"][..],
        ),
        (
            format!(
                "const proto = {{ get install() {{ return {this_it}; }} }};\nconst obj = {{ __proto__: proto, it: {generator} }};\nconst patched = obj.install;"
            ),
            &["proto", "obj", "patched"][..],
        ),
        (
            format!(
                "const proto = {{ set install(v) {{ {this_it}; }} }};\nconst obj = {{ __proto__: proto, it: {generator} }};\nconst patched = (obj.install = 1);"
            ),
            &["proto", "obj", "patched"][..],
        ),
        (
            format!(
                "class K {{ it = {generator}; get install() {{ return {this_it}; }} }}\nconst k = new K();\nconst patched = k.install;"
            ),
            &["K", "k", "patched"][..],
        ),
        (
            format!(
                "class K {{ it = {generator}; get install() {{ return {this_it}; }} }}\nconst patched = new K().install;"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "class K {{ static it = {generator}; static get install() {{ return {this_it}; }} }}\nconst patched = K.install;"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "class K {{ static it = {generator}; static set install(v) {{ {this_it}; }} }}\nconst patched = (K.install = 1);"
            ),
            &["K", "patched"][..],
        ),
        (
            format!(
                "const obj = {{ it: {generator}, set install(v) {{ {this_it}; }} }};\nconst patched = ([obj.install] = [1]);"
            ),
            &["obj", "patched"][..],
        ),
        (
            format!(
                "{named}\nclass K {{}}\nconst P = K.prototype;\nconst set = (P.it = it);\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["it", "K", "P", "set", "k", "patched"][..],
        ),
        (
            format!(
                "{named}\nclass K {{}}\nconst set = (K.it = it);\nconst k = new K();\nconst patched = {};",
                define("k.constructor.it")
            ),
            &["it", "K", "set", "k", "patched"][..],
        ),
    ]
}

/// An escaped or unknown instance exposes its class chain's methods: unknown
/// code holding it can call them, and `k.m` reads them as values. Each
/// install reads a generator method (own, inherited, from a class factory,
/// beside a refused constructor, or under a computed key) or a value a
/// method returned to the constructor.
#[test]
fn an_iterator_reached_through_a_class_method_runs_where_the_protocol_finds_it() {
    for (install, extra) in method_iterator_installs("second") {
        for flag in ITERATING_FLAGS {
            assert_reader_split_diverges_with(&format!("{install}\n{flag}"), extra);
        }
    }
}

/// Positive anchor for the control above: the same installs of a generator
/// calling the effect-free `first` keep the split.
#[test]
fn an_iterator_reached_through_a_class_method_without_effects_keeps_the_split() {
    for (install, extra) in method_iterator_installs("first") {
        let fixture = with_reader_split(
            &format!("{install}\n{}", ITERATING_FLAGS[0]),
            extra,
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// Iterating an instance runs its class's own iterator method, with no
/// prototype install.
#[test]
fn iterating_an_instance_runs_its_class_iterator_method() {
    for run in [
        "function run() { if (first()) {} flag(); return 0; }",
        "function run() { return flag()(); }",
    ] {
        assert_reader_split_diverges_with(
            &format!(
                "class K {{ *[Symbol.iterator]() {{ second(); }} }}\nconst k = new K();\nfunction flag() {{ if (first()) {{}} for (const x of k) {{}} return noop; }}\n{run}"
            ),
            &["K", "k"],
        );
    }
}

/// A read or write through a `__proto__` literal whose chain has no
/// accessor reading `this` keeps the split, although another binding holds
/// an uncalled effect.
#[test]
fn a_prototype_literal_without_a_this_reading_accessor_keeps_the_split() {
    for definitions in [
        "const handlers = { onClick: second };\nconst base = { kind: 'b' };\nconst o = { __proto__: base, n: 1 };\nfunction flag() { o.kind; o.n = 2; return noop; }\nfunction run() { flag(); return 0; }",
        "const handlers = { onClick: second };\nconst base = { get kind() { return 'b'; } };\nconst o = { __proto__: base, n: 1 };\nfunction flag() { o.kind; return noop; }\nfunction run() { flag(); return 0; }",
    ] {
        let fixture = with_reader_split(definitions, &["handlers", "base", "o"], run_fixture);
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

fn method_iterator_installs(callee: &str) -> [(String, &'static [&'static str]); 7] {
    let k_it = "Object.defineProperty(String.prototype, Symbol.iterator, { value: k.it })";
    let named = format!("function* it() {{ {callee}(); }}");
    [
        (
            format!(
                "{named}\nclass B {{ gen() {{ return it; }} }}\nclass K extends B {{ constructor() {{ super(); this.it = super.gen(); }} }}\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["it", "B", "K", "k", "patched"][..],
        ),
        (
            format!(
                "{named}\nclass K {{ static gen() {{ return it; }} constructor() {{ this.it = this.constructor.gen(); }} }}\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["it", "K", "k", "patched"][..],
        ),
        (
            format!(
                "class B {{ *it() {{ {callee}(); }} }}\nclass K extends B {{}}\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["B", "K", "k", "patched"][..],
        ),
        (
            format!(
                "function mkK() {{ return class {{ *it() {{ {callee}(); }} }}; }}\nconst K = mkK();\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["mkK", "K", "k", "patched"][..],
        ),
        (
            format!(
                "class K {{ constructor() {{ this.n = JSON.parse('1'); }} *it() {{ {callee}(); }} }}\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["K", "k", "patched"][..],
        ),
        (
            format!(
                "class B {{ constructor() {{ this.n = JSON.parse('1'); }} }}\nclass K extends B {{ *it() {{ {callee}(); }} }}\nconst k = new K();\nconst patched = {k_it};"
            ),
            &["B", "K", "k", "patched"][..],
        ),
        (
            format!(
                "class K {{ *[Symbol.iterator]() {{ {callee}(); }} }}\nconst k = new K();\nconst patched = Object.defineProperty(String.prototype, Symbol.iterator, {{ value: k[Symbol.iterator] }});"
            ),
            &["K", "k", "patched"][..],
        ),
    ]
}
