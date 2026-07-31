//! User-resolution tests: `root` resolves to uid/gid 0; a nonexistent user is a clean NotFound;
//! the passwd entry's home and shell come back too, because `exec.rs` builds the child's
//! `HOME`/`SHELL` from them.

use std::io;

use users::resolve;

#[test]
fn resolves_root_to_zero() {
    let creds = resolve("root").unwrap();
    assert_eq!(creds.uid, 0);
    assert_eq!(creds.gid, 0);
}

#[test]
fn resolves_supplementary_groups() {
    // `getgrouplist` returns at least the primary group; root is a member of group 0.
    let creds = resolve("root").unwrap();
    assert!(
        creds.groups.contains(&0),
        "root's group list should include gid 0: {:?}",
        creds.groups
    );
}

#[test]
fn missing_user_is_not_found() {
    let err = resolve("no-such-user-hostexec-xyz").unwrap_err();
    assert_eq!(err.kind(), io::ErrorKind::NotFound);
}

#[test]
fn resolves_home_and_shell() {
    // Both come from the same passwd entry the uid/gid do. `exec.rs` sets HOME/USER/LOGNAME/SHELL
    // from these, so an empty value here would silently reproduce the "HOME: unbound variable"
    // failure this field was added to fix.
    let creds = resolve("root").unwrap();
    assert_eq!(creds.name, "root");
    assert!(
        creds.home.is_absolute(),
        "home should be an absolute path: {:?}",
        creds.home
    );
    assert!(
        creds.shell.starts_with('/'),
        "shell should be an absolute path: {:?}",
        creds.shell
    );
}
