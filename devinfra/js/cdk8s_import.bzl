"""Macro: cdk8s_import — generate typed Python cdk8s constructs from a CRD YAML.

Wraps `cdk8s import` (cdk8s-cli), which generates jsii-backed constructs the same way
upstream cdk8s_plus_34 was generated for core Kubernetes types. Unlike the manifest
files under cluster/k8s (committed because Flux reads them from git), this is a
compile-time codegen dependency -- outputs are pure build artifacts, never committed.

"Generated bindings load and synthesize under Bazel" is a property of this mechanism,
not of any one CRD -- proven once by //cluster/cdk8s/crd_bindings/flux:test_kustomization_import.
A caller importing another CRD doesn't need its own copy of that smoke test.
"""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("@npm_ducktape//:cdk8s-cli/package_json.bzl", cdk8s_bin = "bin")
load("//devinfra/python:defs.bzl", "py_library")

def cdk8s_import(
        name,
        crd,
        module_name,
        module_path,
        jsii_module_path = None,
        visibility = None,
        crd_name = None,
        crd_version = None,
        crd_remove_paths = []):
    """Generate a py_library of typed cdk8s constructs from an upstream CRD YAML.

    Args:
        name:        Name of the output py_library target.
        crd:         Label of a CRD YAML (apiextensions.k8s.io CustomResourceDefinition), or
                      an upstream CRD bundle when crd_name is set. Usually an http_file target
                      (MODULE.bazel), never a vendored in-tree copy.
        crd_name:    Metadata name of the CRD to extract from a multi-document bundle. When
                      omitted, crd is passed directly to cdk8s import.
        crd_version: Optional CRD version to keep when extracting. Useful when the bundle lists
                      a deprecated version after its storage version.
        crd_remove_paths: Optional dotted paths to remove from the extracted schema before
                      importing. Use only to work around cdk8s/jsii generator limitations; this
                      does not modify the installed CRD.
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
                      A group segment containing a dash (e.g. `external-secrets.io`) gets
                      underscored here (`io/external_secrets`) since it's also a Python package
                      path -- see jsii_module_path for the one place that dash survives.
        jsii_module_path: The same reverse-DNS path, but exactly as cdk8s names the `_jsii/*.jsii.tgz`
                      assembly tarball -- unlike the Python package directory, that name keeps a
                      dashed group segment as-is (an npm/jsii package name, not a Python one).
                      Defaults to module_path, which is correct whenever the group has no dashes;
                      pass the dashed variant explicitly when it does (checked the same way as
                      module_path: run `cdk8s import` locally once and read the actual filename
                      under `_jsii/`).
        visibility:  Visibility of the output py_library target.
    """
    if crd_name != None:
        extracted_crd = "_" + name + "_crd"
        extract_args = [
            "$(location //devinfra/k8s:extract_crd_bin)",
            "--name",
            crd_name,
        ]
        if crd_version != None:
            extract_args.extend(["--version", crd_version])
        for path in crd_remove_paths:
            extract_args.extend(["--remove-path", path])
        extract_args.extend(["$(location {})".format(crd), "$@"])
        native.genrule(
            name = extracted_crd,
            srcs = [crd],
            outs = [extracted_crd + ".yaml"],
            cmd = " ".join(extract_args),
            tools = ["//devinfra/k8s:extract_crd_bin"],
            visibility = ["//visibility:private"],
        )
        crd = ":" + extracted_crd
    elif crd_version != None or crd_remove_paths:
        fail("cdk8s_import: crd_version/crd_remove_paths require crd_name")

    jsii_module_path = jsii_module_path if jsii_module_path != None else module_path
    out_dir = "_" + name + "_imports"
    module_dir = out_dir + "/" + module_name + "/" + module_path
    jsii_tarball = module_dir + "/_jsii/" + module_name + "_" + jsii_module_path.replace("/", "") + "@0.0.0.jsii.tgz"
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
