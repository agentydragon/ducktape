"""Generate the Go deepcopy methods and Kubernetes CRDs for an Agentplane API package."""

load("@io_bazel_rules_go//go:def.bzl", "go_path")
load("//devinfra/go:controller_gen.bzl", "controller_crds", "controller_deepcopy")

def agentplane_api_codegen(src, importpath):
    """Generate both artifacts from the same source library, before its deepcopy methods exist."""
    go_path(
        name = "deepcopy_gopath",
        mode = "copy",
        deps = [src],
    )

    controller_deepcopy(
        name = "deepcopy",
        gopath = ":deepcopy_gopath",
        importpath = importpath,
    )

    controller_crds(
        name = "generated_crds",
        gopath = ":deepcopy_gopath",
        importpath = importpath,
        visibility = ["//agentplane/crds:__pkg__"],
    )
