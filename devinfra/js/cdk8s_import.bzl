"""Macro: cdk8s_import — generate typed Python cdk8s constructs from a CRD YAML.

Wraps `cdk8s import` (cdk8s-cli), which generates jsii-backed constructs the same way
upstream cdk8s_plus_33 was generated for core Kubernetes types. Unlike the manifest
files under cluster/k8s (committed because Flux reads them from git), this is a
compile-time codegen dependency -- outputs are pure build artifacts, never committed.

"Generated bindings load and synthesize under Bazel" is a property of this mechanism,
not of any one CRD -- proven once by //third_party/flux:test_kustomization_import.
A caller importing another CRD doesn't need its own copy of that smoke test.
"""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("@npm_ducktape//:cdk8s-cli/package_json.bzl", cdk8s_bin = "bin")
load("//devinfra/python:defs.bzl", "py_library")

def cdk8s_import(name, crd, module_name, module_path, visibility = None):
    """Generate a py_library of typed cdk8s constructs from a vendored CRD YAML.

    Args:
        name:        Name of the output py_library target.
        crd:         Label of the CRD YAML (apiextensions.k8s.io CustomResourceDefinition) --
                      an http_file target (MODULE.bazel), never a vendored in-tree copy.
        module_name: A unique top-level Python package name for this import (the `NAME` half of
                      `cdk8s import NAME:=SPEC`). Required -- cdk8s's default naming reverse-DNSes
                      the CRD's `spec.group` into a bare top-level path (e.g. `io/fluxcd/...` for
                      group `kustomize.toolkit.fluxcd.io`), and `io` collides with the Python
                      stdlib `io` module (not a namespace package, so `import io.fluxcd...` fails).
                      Prefixing with a real module_name keeps the reverse-DNS path as a harmless
                      subpackage instead.
        module_path: The jsii-derived import path cdk8s assigns from the CRD's `spec.group`
                      (reverse-DNS, dots to slashes -- e.g. group `kustomize.toolkit.fluxcd.io`
                      and kind `Kustomization` import as `io/fluxcd/toolkit/kustomize`). Run
                      `cdk8s import NAME:=<crd> --language python -o imports` locally once to
                      find it; wrong values fail loudly (declared outs the action doesn't produce).
        visibility:  Visibility of the output py_library target.
    """
    out_dir = "_" + name + "_imports"
    module_dir = out_dir + "/" + module_name + "/" + module_path
    jsii_tarball = module_dir + "/_jsii/" + module_name + "_" + module_path.replace("/", "") + "@0.0.0.jsii.tgz"
    py_srcs = [
        module_dir + "/__init__.py",
        module_dir + "/_jsii/__init__.py",
    ]
    data_files = [
        module_dir + "/py.typed",
        jsii_tarball,
    ]

    cdk8s_bin.cdk8s_binary(
        name = "_" + name + "_cdk8s_bin",
        # jsii-pacmak shells out to a real `npm pack` to build the _jsii/*.jsii.tgz
        # assembly tarball -- include_npm puts a real npm on PATH for it to find.
        include_npm = True,
        visibility = ["//visibility:private"],
    )

    js_run_binary(
        name = "_" + name + "_generate",
        srcs = [crd],
        # crd is an http_file (MODULE.bazel) in an external repo -- copy_to_bin can't
        # (and doesn't need to) copy it into the output tree first.
        copy_srcs_to_bin = False,
        outs = py_srcs + data_files,
        args = [
            "import",
            # js_binary tools default to running with cwd = bazel-out/<config>/bin (the
            # output tree root); $(location) expands to an exec-root-relative path, so
            # climb back out of bindir (3 levels) before descending into external/.
            "{}:=../../../$(location {})".format(module_name, crd),
            "--language",
            "python",
            "--no-check-upgrade",
            "--no-save",
            "-o",
            native.package_name() + "/" + out_dir,
        ],
        tool = ":_" + name + "_cdk8s_bin",
    )

    py_library(
        name = name,
        srcs = py_srcs,
        data = data_files,
        imports = [out_dir],
        deps = [
            "@pypi//cdk8s",
            "@pypi//constructs",
            "@pypi//jsii",
            "@pypi//publication",
            "@pypi//typing_extensions",
        ],
        visibility = visibility,
    )
