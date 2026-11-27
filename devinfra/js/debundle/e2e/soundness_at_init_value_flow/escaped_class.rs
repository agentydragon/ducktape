//! Escaped classes and negated constructor values keep callback re-entry and getter effects possible.

use super::{command_later_split, in_case};
use debundle_e2e_support::*;

#[test]
fn escaped_constructorless_class_allocation_does_not_reenter_global_callback() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const saved = () => later;
globalThis.saved = saved;
class Checker { isAllowed() { return true; } }
globalThis.savedChecker = Checker;
class Command {
  constructor(config, checker) { this.config = config; this.checker = checker; }
  execute() { return this.config.execute(); }
}
function create(config) { return new Command(config, new Checker()); }
const command = create({ execute: () => later });
const later = 7;
console.log(command.execute());
export { saved, Checker, Command, create, command, later };
"#,
            vec![
                logical_module_with_anon(
                    "command",
                    &binding_members(["saved", "Checker", "Command", "create", "command"]),
                    &[
                        "globalThis.saved = saved;",
                        "globalThis.savedChecker = Checker;",
                    ],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn escaped_class_work_at_construction_keeps_callback_reentry_possible() {
    for checker in [
        "class Checker { constructor() { globalThis.saved(); } }",
        "class Checker { result = globalThis.saved(); }",
    ] {
        in_case(checker, || {
            expect_atomic_conflict_rejection(
                command_later_split(&format!(
                    r#"const saved = () => later;
globalThis.saved = saved;
{checker}
globalThis.savedChecker = Checker;
class Command {{
  constructor(config, checker) {{ this.config = config; this.checker = checker; }}
}}
function create(config) {{ return new Command(config, new Checker()); }}
const command = create({{ execute: () => later }});
const later = 7;
export {{ saved, Checker, Command, create, command, later }};
"#
                )),
                &["command", "value"],
                &[],
            );
        });
    }
}

#[test]
fn negated_constructor_values_keep_getter_and_call_effects() {
    for value in ["!config.focus", "!config.execute()"] {
        in_case(value, || {
            expect_atomic_conflict_rejection(
                command_later_split(&format!(
                    r#"class Command {{
  constructor(config) {{ this.focus = {value}; }}
}}
const command = new Command({{
  get focus() {{ return later; }},
  execute: () => later,
}});
const later = 7;
export {{ Command, command, later }};
"#
                )),
                &["command", "value"],
                &[],
            );
        });
    }
}
