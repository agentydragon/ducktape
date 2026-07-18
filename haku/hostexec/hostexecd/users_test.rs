//! User-resolution tests: `root` resolves to uid/gid 0; a nonexistent user is a clean NotFound.

use std::io;

use users::resolve;

#[test]
fn resolves_root_to_zero() {
    let creds = resolve("root").unwrap();
    assert_eq!(creds.uid, 0);
    assert_eq!(creds.gid, 0);
}

#[test]
fn missing_user_is_not_found() {
    let err = resolve("no-such-user-hostexec-xyz").unwrap_err();
    assert_eq!(err.kind(), io::ErrorKind::NotFound);
}
