//! `Error` construction runs no user code once its message converts silently,
//! so a published callback cannot reenter through it.

use debundle_e2e_support::*;

fn opts<'a>(source: &'a str, anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/failure", &[Member::new("failure")], anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn source(mutations: &[&str], declaration: &str) -> String {
    format!(
        "globalThis.saved = () => later;\n{}\n{declaration}\nconst later = 42;\nconsole.log(failure.message, later);\n",
        mutations.join("\n")
    )
}

fn anonymous<'a>(mutations: &[&'a str]) -> Vec<&'a str> {
    [&["globalThis.saved = () => later;"], mutations].concat()
}

#[test]
fn error_with_a_primitive_message_cannot_reenter() {
    for (mutations, declaration) in [
        (&[][..], "const failure = new Error(\"boom\");"),
        (&[][..], "const failure = Error(\"boom\", 0);"),
        // V8 formats the stack lazily: construction does not call it.
        (
            &["Error.prepareStackTrace = () => globalThis.saved();"][..],
            "const failure = new Error(\"boom\");",
        ),
    ] {
        let source = source(mutations, declaration);
        let fixture = run_fixture(opts(&source, &anonymous(mutations)));
        assert_preserved_entry_output(&fixture, "boom 42\n");
        assert_all_emitted_js_checks(&fixture);
    }
}

#[test]
fn error_message_conversion_or_replacement_can_reenter() {
    for (mutations, declaration) in [
        (
            &[][..],
            "const failure = new Error({ toString() { try { globalThis.saved(); return \"ready\"; } catch (error) { return error.name; } } });",
        ),
        (
            &[][..],
            "const failure = new Error(\"boom\", { get cause() { try { globalThis.saved(); } catch (error) { return error.name; } } });",
        ),
        (
            &[
                "Error = function Error() { try { globalThis.saved(); return { message: \"ready\" }; } catch (error) { return { message: error.name }; } };",
            ][..],
            "const failure = new Error(\"boom\");",
        ),
    ] {
        let source = source(mutations, declaration);
        expect_atomic_conflict_rejection(
            opts(&source, &anonymous(mutations)),
            &["values/failure", "values/later"],
            &[],
        );
    }
}
