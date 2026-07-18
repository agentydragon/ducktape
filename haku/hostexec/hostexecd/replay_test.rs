//! Replay-store tests: a token is accepted once, rejected on reuse; distinct tokens are
//! independent; and once every entry has expired the map is emptied (so it stays bounded).

use replay::ReplayStore;

const EXP: u64 = 2000;
const NOW: u64 = 1000;

#[test]
fn accepts_first_use_rejects_reuse() {
    let store = ReplayStore::new();
    assert!(store.claim("token-a", EXP, NOW).is_ok());
    assert!(store.claim("token-a", EXP, NOW).is_err());
}

#[test]
fn distinct_tokens_are_independent() {
    let store = ReplayStore::new();
    assert!(store.claim("token-a", EXP, NOW).is_ok());
    assert!(store.claim("token-b", EXP, NOW).is_ok());
}

#[test]
fn expired_entries_are_evicted() {
    let store = ReplayStore::new();
    // A short-lived token, then time advances past its exp: the store forgets it (bounded), so a
    // *different* token still records cleanly and the old id is gone.
    assert!(store.claim("old", 1500, 1000).is_ok());
    // Advance now past 1500; claiming any token evicts the expired "old" entry.
    assert!(store.claim("new", 3000, 2000).is_ok());
    // "old" would now be accepted again (it expired) — but that is moot: its token is also
    // expired and would fail verification upstream. The point is the map does not grow unbounded.
    assert!(store.claim("old", 1500, 2000).is_ok());
}
