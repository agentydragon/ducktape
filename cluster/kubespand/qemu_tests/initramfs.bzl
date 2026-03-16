"""Macro for building Alpine-based initramfs images for QEMU test VMs."""

def initramfs(
        name,
        init,
        extra_binaries = {},
        extra_dirs = [],
        **kwargs):
    """Build a cpio.gz initramfs from Alpine minirootfs + init binary + extras.

    Args:
        name: Target name (should be "initramfs").
        init: Label of the Go init binary.
        extra_binaries: Dict of {label: "/path/in/initramfs"} for extra binaries.
        extra_dirs: List of directory paths to create inside the initramfs.
        **kwargs: Forwarded to genrule (testonly, visibility, etc).
    """
    srcs = [
        "@alpine_minirootfs_x86_64//file",
        init,
        "//cluster/kubespand/qemu_tests:modules_tree",
    ] + list(extra_binaries.keys())

    # Build the shell commands for extra dirs and binaries.
    cmds = [
        "set -e",
        "D=$$(mktemp -d)",
        "tar -xzf $(location @alpine_minirootfs_x86_64//file) -C $$D",
    ]
    for d in extra_dirs:
        cmds.append("mkdir -p $$D" + d)
    cmds.append("cp $(location {}) $$D/init && chmod +x $$D/init".format(init))
    for label, dest in extra_binaries.items():
        cmds.append("cp $(location {}) $$D{} && chmod +x $$D{}".format(label, dest, dest))
    cmds += [
        "tar -xf $(location //cluster/kubespand/qemu_tests:modules_tree) -C $$D",
        "(cd $$D && find . -print0 | cpio --null -o -H newc 2>/dev/null) | gzip -9 > $@",
        "rm -rf $$D",
    ]

    native.genrule(
        name = name,
        srcs = srcs,
        outs = ["initramfs.cpio.gz"],
        cmd = "\n".join(cmds),
        **kwargs
    )
