//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

mod bounded_callback_fallback;
mod class_member_await;
mod constructor_effects;
mod escaped_class;
mod local_constructor;
mod parameter_flow;
mod returned_closures;
mod setter_reentry;

use debundle_e2e_support::*;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};

/// Run one case of a table, naming it when an assertion inside fails.
fn in_case(case: &str, check: impl FnOnce()) {
    if let Err(panic) = catch_unwind(AssertUnwindSafe(check)) {
        eprintln!("case {case:?} failed");
        resume_unwind(panic);
    }
}

fn members(names: &[&'static str]) -> Vec<Member> {
    names.iter().copied().map(Member::new).collect()
}

// ─── constructors receiving callbacks ───────────────────────────────────

fn command_later_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("command", &[Member::new("command")]),
        logical_module("value", &[Member::new("later")]),
    ]
}

/// `command` before `later`: accepted only if building `command` cannot run
/// a callback that reads `later`.
fn command_later_split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(source, command_later_split_modules())
        .with_import_order(&["command", "value", "residual"])
}
