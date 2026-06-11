//! End-to-end coverage for the spec-level `comment:` fields on
//! members, modules, and anonymous statements. The lowering pass
//! renders the comments as `// ...` lines: module-level at the top
//! of the generated file (above the lowerer's pragma block),
//! per-member immediately above the binding's owner statement, and
//! anonymous-statement comments immediately above the matched
//! side-effecting statement.

use debundle_e2e_support::*;
use std::fs;

const MODULE_PATH: &str = "static/app/modules/x.js";

fn read_module(fixture: &Fixture, module_path: &str) -> String {
    fs::read_to_string(fixture.out_root.join(module_path))
        .unwrap_or_else(|err| panic!("read {module_path}: {err}"))
}

fn assert_contains_in_order(code: &str, needles: &[&str]) {
    let mut cursor = 0;
    for needle in needles {
        let Some(pos) = code[cursor..].find(needle) else {
            panic!("expected to find {needle:?} after position {cursor} in:\n{code}");
        };
        cursor += pos + needle.len();
    }
}

#[test]
fn single_line_member_comment_lands_above_owner_statement() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function a() { return "a"; }
function b() { return "b"; }
console.log(a(), b());
export { a, b };
"#,
        vec![logical_module(
            "x",
            &[
                Member::new("a").with_comment("Returns the literal a."),
                Member::new("b"),
            ],
        )],
    ));
    let code = read_module(&fixture, MODULE_PATH);
    // The comment lands immediately above the function declaration
    // for `a`. The `b` declaration is untouched.
    assert_contains_in_order(&code, &["// Returns the literal a.", "function a("]);
    assert!(
        !code.contains("// Returns the literal a.\nfunction b("),
        "comment must not be attached to b():\n{code}",
    );
    assert_entry_output(&fixture, "a b\n");
}

#[test]
fn multi_line_member_comment_preserves_paragraph_structure() {
    let comment = "Line one of the doc.\n\nLine three after a blank.\nLine four.";
    let fixture = run_fixture(FixtureOpts::new(
        r#"const value = 42;
console.log(value);
export { value };
"#,
        vec![logical_module(
            "x",
            &[Member::new("value").with_comment(comment)],
        )],
    ));
    let code = read_module(&fixture, MODULE_PATH);
    // Each input line becomes one `// ...` line; the blank input
    // line emits as a bare `//` so paragraph structure survives.
    assert_contains_in_order(
        &code,
        &[
            "// Line one of the doc.",
            "//\n",
            "// Line three after a blank.",
            "// Line four.",
            "const value = 42",
        ],
    );
    assert_entry_output(&fixture, "42\n");
}

#[test]
fn module_level_comment_lands_at_top_of_file_before_imports() {
    let comment = "Module-wide documentation.\nSecond line of the module comment.";
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = "a";
const b = "b";
console.log(a, b);
export { a, b };
"#,
        vec![logical_module_with_comment(
            "x",
            &[Member::new("a"), Member::new("b")],
            comment,
        )],
    ));
    let code = read_module(&fixture, MODULE_PATH);
    // Module comment is the very first content of the file.
    assert!(
        code.starts_with("// Module-wide documentation.\n// Second line of the module comment.\n"),
        "module comment must lead the file:\n{code}",
    );
    // And it lands before the lowerer's pragma block.
    let comment_pos = code.find("// Module-wide documentation.").unwrap();
    let pragma_pos = code.find("// @ducktape-generated").unwrap();
    assert!(
        comment_pos < pragma_pos,
        "module comment must precede generator pragmas:\n{code}",
    );
    assert_entry_output(&fixture, "a b\n");
}

#[test]
fn anonymous_statement_comment_lands_above_matched_statement() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function Foo() {}
Foo.prototype.bar = true;
console.log(Foo.name);
export { Foo };
"#,
        vec![logical_module_with_anon_comment(
            "x",
            &[Member::new("Foo")],
            "Foo.prototype.bar = true;",
            "Enables Foo.bar before consumers import Foo.",
        )],
    ));
    let code = read_module(&fixture, MODULE_PATH);
    assert_contains_in_order(
        &code,
        &[
            "function Foo(",
            "// Enables Foo.bar before consumers import Foo.",
            "Foo.prototype.bar = true;",
        ],
    );
    assert_entry_output(&fixture, "Foo\n");
}

#[test]
fn missing_or_empty_comments_emit_nothing() {
    // No comment on either member, no module-level comment. The
    // emitted file must not gain any spurious `// ...` markers from
    // the comment plumbing.
    let no_comment_fixture = run_fixture(FixtureOpts::new(
        r#"const value = 7;
console.log(value);
export { value };
"#,
        vec![logical_module("x", &[Member::new("value")])],
    ));
    let no_comment_code = read_module(&no_comment_fixture, MODULE_PATH);

    // Empty-string comment must behave the same as absent.
    let empty_comment_fixture = run_fixture(FixtureOpts::new(
        r#"const value = 7;
console.log(value);
export { value };
"#,
        vec![logical_module_with_comment(
            "x",
            &[Member::new("value").with_comment("")],
            "",
        )],
    ));
    let empty_comment_code = read_module(&empty_comment_fixture, MODULE_PATH);

    assert_eq!(
        no_comment_code, empty_comment_code,
        "empty-string comment must emit nothing — output should match the no-comment baseline\n\
         --- no comment ---\n{no_comment_code}\n--- empty comment ---\n{empty_comment_code}",
    );
}

#[test]
fn trailing_whitespace_in_comment_lines_is_trimmed() {
    // Trailing spaces / tabs on each input line shouldn't leak into
    // the emitted comment — keeps diffs clean and matches what most
    // linters expect.
    let comment = "Has trailing spaces.   \nClean line.";
    let fixture = run_fixture(FixtureOpts::new(
        r#"const value = 1;
console.log(value);
export { value };
"#,
        vec![logical_module(
            "x",
            &[Member::new("value").with_comment(comment)],
        )],
    ));
    let code = read_module(&fixture, MODULE_PATH);
    assert!(
        code.contains("// Has trailing spaces.\n"),
        "trailing spaces must be trimmed:\n{code}",
    );
    assert!(
        !code.contains("// Has trailing spaces.   \n"),
        "trailing spaces must not appear in emitted comment:\n{code}",
    );
}
