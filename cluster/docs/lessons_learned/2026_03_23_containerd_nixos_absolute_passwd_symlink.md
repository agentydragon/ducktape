# Containerd user lookup and NixOS `/etc/passwd` symlinks

When a Pod sets `runAsUser`, containerd resolves the UID against the container
root filesystem's `/etc/passwd`. NixOS images commonly make that path an absolute
symlink into `/nix/store`. With Go 1.24's stricter `os.DirFS` path handling,
containerd 2.2.0–2.2.2 can reject that lookup with `path escapes from parent`.
This was the cause of the Attic container-create failure on the NixOS workers;
the same image worked on the older Talos containerd used at the time.

The upstream fix is in [containerd#12732](https://github.com/containerd/containerd/pull/12732)
and shipped in containerd 2.2.3. The repository fix is also complete: the
default NixOS input now uses 26.05, and commit
[`d422ca9387`](https://github.com/agentydragon/ducktape/commit/d422ca93873c8a030150690dc33657c1c3da0f30)
removed the backport overlay because that release supplies containerd 2.3.0.
See the [upstream report](https://github.com/containerd/containerd/issues/12683)
for the reproducer and affected root-filesystem layout.

The source update does not prove every host has switched generations. The
remaining per-host runtime check is tracked in the
[Attic rollout follow-up](../../debug/attic_containerd_symlink.md).
