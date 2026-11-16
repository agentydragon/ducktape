//! Keep source-value fact storage bounded for a compact alias/property matrix.
//!
//! The input has O(N + P) declarations and writes, while the receiver may
//! denote any of N object literals and receives P distinct properties. The
//! normal CLI therefore builds N × P property facts from a small source file.

use debundle_e2e_support::*;

const ALIASES: usize = 512;
const PROPERTIES: usize = 512;
// Bound the normal CLI and Node children together. This guards against both
// per-judgment empty metadata and a second per-source property-cell table.
const MAX_CHILD_RSS_KIB: u64 = 200 * 1024;

fn alias_property_matrix() -> String {
    let mut source = String::new();

    for alias in 0..ALIASES {
        source.push_str(&format!("const object{alias} = {{}};\n"));
    }
    source.push_str("let receiver = object0;\n");
    for alias in 1..ALIASES {
        source.push_str(&format!("receiver = object{alias};\n"));
    }
    for property in 0..PROPERTIES {
        source.push_str(&format!("receiver.p{property} = () => {property};\n"));
    }

    source.push_str("function observe() { return [");
    for property in 0..PROPERTIES {
        if property != 0 {
            source.push_str(", ");
        }
        source.push_str(&format!("receiver.p{property}"));
    }
    source.push_str("]; }\nconsole.log(receiver.p0(), receiver.p511());\n");
    source
}

#[test]
fn compact_alias_property_matrix_keeps_peak_child_rss_below_budget() {
    let source = alias_property_matrix();
    let fixture = run_fixture(
        FixtureOpts::new(&source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0 511\n");

    #[cfg(target_os = "linux")]
    assert_linux_child_rss_within_budget();
}

#[cfg(target_os = "linux")]
fn assert_linux_child_rss_within_budget() {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: getrusage initializes the supplied rusage record on success.
    let status = unsafe { libc::getrusage(libc::RUSAGE_CHILDREN, usage.as_mut_ptr()) };
    assert_eq!(status, 0, "getrusage(RUSAGE_CHILDREN) failed");
    // SAFETY: the successful call above initialized the record.
    let peak_kib = u64::try_from(unsafe { usage.assume_init() }.ru_maxrss)
        .expect("Linux ru_maxrss is nonnegative");

    assert!(
        peak_kib <= MAX_CHILD_RSS_KIB,
        "normal Debundle CLI / Node subprocess peak RSS was {} MiB; budget is {} MiB",
        peak_kib / 1024,
        MAX_CHILD_RSS_KIB / 1024,
    );
}
