//! Resolve a POSIX username to its numeric credentials via `getpwnam`.
//!
//! `hostexecd` runs as root and drops to the resolved uid/gid when spawning the approved command.
//! Isolated here so the one unsafe libc call stays small and unit-tested. (Supplementary groups
//! via `initgroups` are a known gap — see `exec.rs` — deferred to the root-capable host test.)

use std::ffi::CString;
use std::io;

/// A resolved account's numeric credentials.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Credentials {
    pub uid: u32,
    pub gid: u32,
}

/// Look up `username` in the passwd database. Errors if the user does not exist.
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
    })
}
