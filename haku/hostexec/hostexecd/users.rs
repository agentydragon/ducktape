//! Resolve a POSIX username to its numeric credentials via `getpwnam` + `getgrouplist`.
//!
//! `hostexecd` runs as root and drops to the resolved uid/gid/supplementary groups when spawning
//! the approved command (the drop itself is in `exec.rs`). Isolated here so the unsafe libc calls
//! stay small and unit-tested. Group resolution is a read, so it is testable without privilege; the
//! drop that consumes it needs root, so that is validated on a host.

use std::ffi::{CStr, CString};
use std::io;

/// A resolved account's numeric credentials, including supplementary groups.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    pub uid: u32,
    pub gid: u32,
    /// Supplementary group GIDs (from `getgrouplist`, includes the primary gid); applied via
    /// `setgroups` before the setgid/setuid drop so the child gets the account's full group set.
    pub groups: Vec<u32>,
}

/// Look up `username` in the passwd database and its group memberships. Errors if the user does not
/// exist.
pub fn resolve(username: &str) -> io::Result<Credentials> {
    let c_name = CString::new(username)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "username contains a NUL byte"))?;

    // getpwnam_r: reentrant lookup into a caller-provided buffer. On a hit, `result` is set to
    // point at `pwd`; on a clean miss, `result` stays null and the return code is 0.
    let mut pwd: libc::passwd = unsafe { std::mem::zeroed() };
    let mut buf: Vec<libc::c_char> = vec![0; 4096];
    let mut result: *mut libc::passwd = std::ptr::null_mut();
    let rc = unsafe {
        libc::getpwnam_r(
            c_name.as_ptr(),
            &mut pwd,
            buf.as_mut_ptr(),
            buf.len(),
            &mut result,
        )
    };
    if rc != 0 {
        return Err(io::Error::from_raw_os_error(rc));
    }
    if result.is_null() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("no such user: {username}"),
        ));
    }
    Ok(Credentials {
        uid: pwd.pw_uid,
        gid: pwd.pw_gid,
        groups: supplementary_groups(&c_name, pwd.pw_gid)?,
    })
}

/// The full group list for `name` (primary + supplementary), via `getgrouplist`. Grows the buffer
/// and retries on the "too small" signal (`-1`, with the required size written back).
fn supplementary_groups(name: &CStr, gid: u32) -> io::Result<Vec<u32>> {
    let mut count: libc::c_int = 32;
    loop {
        let mut groups: Vec<libc::gid_t> = vec![0; count as usize];
        let mut n = count;
        let rc = unsafe { libc::getgrouplist(name.as_ptr(), gid, groups.as_mut_ptr(), &mut n) };
        if rc >= 0 {
            groups.truncate(n as usize);
            return Ok(groups);
        }
        // rc == -1: the buffer was too small; `n` now holds the required size. Guard against a
        // kernel that does not grow `n` so the loop always terminates.
        if n <= count {
            return Err(io::Error::other(
                "getgrouplist did not report a larger buffer size",
            ));
        }
        count = n;
    }
}
