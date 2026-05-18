//! Pipeline rejects emitted JS that would link-fail on duplicate
//! exports.
//!
//! `validate_emitted_exports` runs after materialize / strip and bails
//! before any file hits disk. Without it, a chunk that ships two
//! exports under the same public name would silently make it through
//! the pipeline, get written, and then fail at module-link time in
//! the browser — Chromium reports it as a synthetic empty `pageerror`
//! with no further child-chunk loads, which is essentially silent.
//!
//! Pin the contract: the pipeline must bail with a message that
//! names the file and the offending public name.

use debundle_e2e_support::*;

#[test]
fn rejects_duplicate_export_from_source() {
    // Two exports of the same public name `av`: one from an inline
    // `export const`, the other from a `export { … as av }` of a
    // separate local. swc parses this fine; the runtime spec rejects
    // it at module-link time.
    let opts = FixtureOpts::new(
        r#"export const av = 1;
const helper = 2;
export { helper as av };
console.log(av, helper);
"#,
        vec![],
    );
    expect_rejection_containing_all(
        opts,
        &["validate_emitted_exports", "duplicate", "`av`", "entry.js"],
    );
}
