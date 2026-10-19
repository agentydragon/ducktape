"""Bazel actions for Kubernetes typed clients, listers, and shared informers."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath", "go_library", "go_path")

_GO_TOOLCHAIN = "@io_bazel_rules_go//go:toolchain"

def _generate_impl(ctx):
    gopath = ctx.attr.gopath[GoPath]
    sdk = ctx.toolchains[_GO_TOOLCHAIN].sdk
    groups = {}
    outputs = []
    for package, paths in ctx.attr.packages.items():
        files = [ctx.actions.declare_file(ctx.label.name + "/" + path) for path in paths]
        groups[package] = depset(files)
        outputs.extend(files)
    ctx.actions.run_shell(
        inputs = depset(
            direct = [gopath.gopath_file, sdk.root_file, sdk.go, sdk.package_list],
            transitive = [sdk.headers, sdk.libs, sdk.srcs, sdk.tools],
        ),
        tools = [ctx.attr._client_gen[DefaultInfo].files_to_run, ctx.attr._lister_gen[DefaultInfo].files_to_run, ctx.attr._informer_gen[DefaultInfo].files_to_run, sdk.go],
        outputs = outputs,
        command = """
set -euo pipefail
GOROOT=$({go} env GOROOT)
export GOROOT
export PATH="$GOROOT/bin:/usr/bin:/bin"
export CGO_ENABLED=0 GO111MODULE=off GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off
export GOARCH={goarch} GOOS={goos}
export GOPATH="$PWD"/{gopath}
GOCACHE=$(mktemp -d "${{TMPDIR:-/tmp}}/kubernetes-codegen.XXXXXX")
trap 'rm -rf "$GOCACHE"' EXIT
export GOCACHE
OUT="$PWD"/{out}
{client_gen} --go-header-file /dev/null --input-base {input_base} --input api/v1alpha1 \
    --output-dir "$OUT/clientset" --output-pkg {package}/clientset --clientset-name versioned --fake-clientset=false
{lister_gen} --go-header-file /dev/null --output-dir "$OUT/listers" --output-pkg {package}/listers {input}
{informer_gen} --go-header-file /dev/null --output-dir "$OUT/informers" --output-pkg {package}/informers \
    --single-directory --versioned-clientset-package {package}/clientset/versioned --listers-package {package}/listers {input}
""".format(
            go = shell.quote(sdk.go.path),
            goarch = shell.quote(sdk.goarch),
            goos = shell.quote(sdk.goos),
            gopath = shell.quote(gopath.gopath_file.path),
            out = shell.quote(ctx.bin_dir.path + "/" + ctx.label.package + "/" + ctx.label.name),
            client_gen = shell.quote(ctx.executable._client_gen.path),
            lister_gen = shell.quote(ctx.executable._lister_gen.path),
            informer_gen = shell.quote(ctx.executable._informer_gen.path),
            input_base = shell.quote(ctx.attr.input_base),
            input = shell.quote(ctx.attr.input_base + "/api/v1alpha1"),
            package = shell.quote(ctx.attr.importpath),
        ),
        use_default_shell_env = False,
        mnemonic = "KubernetesClientGen",
        progress_message = "Generating Kubernetes clients and informers for %{label}",
    )
    return [DefaultInfo(files = depset(outputs)), OutputGroupInfo(**groups)]

_generate = rule(
    implementation = _generate_impl,
    attrs = {
        "gopath": attr.label(mandatory = True, providers = [GoPath]),
        "input_base": attr.string(mandatory = True),
        "importpath": attr.string(mandatory = True),
        "packages": attr.string_list_dict(mandatory = True),
        "_client_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/client-gen"), cfg = "exec", executable = True),
        "_lister_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/lister-gen"), cfg = "exec", executable = True),
        "_informer_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/informer-gen"), cfg = "exec", executable = True),
    },
    toolchains = [_GO_TOOLCHAIN],
)

def kubernetes_clients(name, api, input_base, importpath, kinds):
    """Generate build-only Go packages for one API's namespaced v1alpha1 kinds.

    The source API remains authoritative; generated files are declared action outputs,
    just like controller_deepcopy, and never require a developer's Go module cache.
    """
    packages = {
        "clientset": ["clientset/versioned/clientset.go"],
        "scheme": ["clientset/versioned/scheme/doc.go", "clientset/versioned/scheme/register.go"],
        "typed": ["clientset/versioned/typed/api/v1alpha1/" + f for f in ["doc.go", "api_client.go", "generated_expansion.go"] + [kind.lower() + ".go" for kind in kinds]],
        "listers": ["listers/api/v1alpha1/" + f for f in ["expansion_generated.go"] + [kind.lower() + ".go" for kind in kinds]],
        "internalinterfaces": ["informers/internalinterfaces/factory_interfaces.go"],
        "informers": ["informers/factory.go", "informers/generic.go"],
        "informer_group": ["informers/api/interface.go"],
        "informer_version": ["informers/api/v1alpha1/interface.go"] + ["informers/api/v1alpha1/" + kind.lower() + ".go" for kind in kinds],
    }
    go_path(name = name + "_gopath", mode = "copy", deps = [api])
    _generate(name = name, gopath = ":" + name + "_gopath", input_base = input_base, importpath = importpath, packages = packages)
    dependencies = {
        "scheme": [api, "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/runtime/schema", "@io_k8s_apimachinery//pkg/runtime/serializer", "@io_k8s_apimachinery//pkg/util/runtime"],
        "typed": [api, ":scheme", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/types", "@io_k8s_apimachinery//pkg/watch", "@io_k8s_client_go//gentype", "@io_k8s_client_go//rest"],
        "clientset": [":typed", "@io_k8s_client_go//discovery", "@io_k8s_client_go//rest", "@io_k8s_client_go//util/flowcontrol"],
        "listers": [api, "@io_k8s_apimachinery//pkg/labels", "@io_k8s_client_go//listers", "@io_k8s_client_go//tools/cache"],
        "internalinterfaces": [":clientset", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_client_go//tools/cache"],
        "informer_version": [api, ":clientset", ":internalinterfaces", ":listers", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/watch", "@io_k8s_client_go//tools/cache"],
        "informer_group": [":internalinterfaces", ":informer_version"],
        "informers": [api, ":clientset", ":internalinterfaces", ":informer_group", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/runtime/schema", "@io_k8s_client_go//tools/cache"],
    }
    for package, files in packages.items():
        native.filegroup(name = package + "_sources", srcs = [":" + name], output_group = package)
        go_library(
            name = package,
            srcs = [":" + package + "_sources"],
            importpath = importpath + "/" + files[0].rpartition("/")[0],
            deps = dependencies[package],
        )
