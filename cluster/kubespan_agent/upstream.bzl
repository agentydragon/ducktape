"""Macros for pulling upstream Talos source files into kubespand packages.

Upstream Talos internal/ files cannot be imported as Go modules. Instead, they
are pinned via http_archive with patches applied at fetch time (see
MODULE.bazel "talos_internal" and third_party/siderolabs/talos_internal/).
Each consumer package copies the (already-patched) file into its output tree.
"""

# Base paths into the talos_internal http_archive.
TALOS_ADAPTERS_KUBESPAN = "@talos_internal//:internal/app/machined/pkg/adapters/kubespan/"
TALOS_ADAPTERS_NETWORK = "@talos_internal//:internal/app/machined/pkg/adapters/network/"
TALOS_CONTROLLERS_KUBESPAN = "@talos_internal//:internal/app/machined/pkg/controllers/kubespan/"
TALOS_CONTROLLERS_NETWORK = "@talos_internal//:internal/app/machined/pkg/controllers/network/"

def talos_copy(name, src, out):
    """Copy a file from the @talos_internal archive into this package.

    Patches (if any) are already applied at the archive level.

    Args:
        name: genrule name (e.g. "upstream_identity")
        src: source file label relative to a TALOS_* base path
        out: output filename (e.g. "identity.go")
    """
    native.genrule(
        name = name,
        srcs = [src],
        outs = [out],
        cmd = "cp $< $@",
    )
