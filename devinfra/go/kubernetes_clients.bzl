"""Bazel actions for Kubernetes typed clients, apply configurations, listers, and informers."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@io_bazel_rules_go//go:def.bzl", "GoPath", "go_library", "go_path")
load(":gopath_codegen.bzl", "GO_TOOLCHAIN", "run_gopath_codegen")

def _generate_impl(ctx):
    gopath = ctx.attr.gopath[GoPath]
    groups = {}
    outputs = []
    for package, paths in ctx.attr.packages.items():
        files = [ctx.actions.declare_file(ctx.label.name + "/" + path) for path in paths]
        groups[package] = depset(files)
        outputs.extend(files)
    run_gopath_codegen(
        ctx = ctx,
        gopath = gopath,
        tools = [ctx.attr._applyconfiguration_gen[DefaultInfo].files_to_run, ctx.attr._client_gen[DefaultInfo].files_to_run, ctx.attr._lister_gen[DefaultInfo].files_to_run, ctx.attr._informer_gen[DefaultInfo].files_to_run],
        outputs = outputs,
        command = """
OUT="$PWD"/{out}
{applyconfiguration_gen} --go-header-file /dev/null --output-dir "$OUT/applyconfigurations" --output-pkg {package}/applyconfigurations {input}
{client_gen} --go-header-file /dev/null --input-base {input_base} --input api/v1alpha1 \
    --output-dir "$OUT/clientset" --output-pkg {package}/clientset --clientset-name versioned --fake-clientset=false \
    --apply-configuration-package {package}/applyconfigurations
{lister_gen} --go-header-file /dev/null --output-dir "$OUT/listers" --output-pkg {package}/listers {input}
{informer_gen} --go-header-file /dev/null --output-dir "$OUT/informers" --output-pkg {package}/informers \
    --single-directory --versioned-clientset-package {package}/clientset/versioned --listers-package {package}/listers {input}
""".format(
            out = shell.quote(ctx.bin_dir.path + "/" + ctx.label.package + "/" + ctx.label.name),
            applyconfiguration_gen = shell.quote(ctx.executable._applyconfiguration_gen.path),
            client_gen = shell.quote(ctx.executable._client_gen.path),
            lister_gen = shell.quote(ctx.executable._lister_gen.path),
            informer_gen = shell.quote(ctx.executable._informer_gen.path),
            input_base = shell.quote(ctx.attr.input_base),
            input = shell.quote(ctx.attr.input_base + "/api/v1alpha1"),
            package = shell.quote(ctx.attr.importpath),
        ),
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
        "_applyconfiguration_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/applyconfiguration-gen"), cfg = "exec", executable = True),
        "_client_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/client-gen"), cfg = "exec", executable = True),
        "_lister_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/lister-gen"), cfg = "exec", executable = True),
        "_informer_gen": attr.label(default = Label("@io_k8s_code_generator//cmd/informer-gen"), cfg = "exec", executable = True),
    },
    toolchains = [GO_TOOLCHAIN],
)

def kubernetes_clients(name, api, input_base, importpath, kinds, apply_configuration_types):
    """Generate build-only clients and apply configurations for one API's v1alpha1 kinds.

    The source API remains authoritative; generated files are declared action outputs,
    just like controller_deepcopy, and never require a developer's Go module cache.

    apply_configuration_types lists every configuration emitted for the client kinds,
    including nested API structs. Keep it in sync when an API adds fields whose types are structs.
    """
    packages = {
        "clientset": ["clientset/versioned/clientset.go"],
        "scheme": ["clientset/versioned/scheme/doc.go", "clientset/versioned/scheme/register.go"],
        "typed": ["clientset/versioned/typed/api/v1alpha1/" + f for f in ["doc.go", "api_client.go", "generated_expansion.go"] + [kind.lower() + ".go" for kind in kinds]],
        "applyconfiguration_api": ["applyconfigurations/api/v1alpha1/" + kind.lower() + ".go" for kind in apply_configuration_types],
        # These generator outputs are currently not imported by the generated typed clients.
        # Keep them declared because applyconfiguration-gen always emits both packages.
        "applyconfiguration_root": ["applyconfigurations/utils.go"],
        "applyconfiguration_internal": ["applyconfigurations/internal/internal.go"],
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
        "typed": [api, ":applyconfiguration_api", ":scheme", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/types", "@io_k8s_apimachinery//pkg/watch", "@io_k8s_client_go//gentype", "@io_k8s_client_go//rest"],
        "applyconfiguration_api": [api, "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/types", "@io_k8s_client_go//applyconfigurations/meta/v1:meta"],
        "clientset": [":typed", "@io_k8s_client_go//discovery", "@io_k8s_client_go//rest", "@io_k8s_client_go//util/flowcontrol"],
        "listers": [api, "@io_k8s_apimachinery//pkg/labels", "@io_k8s_client_go//listers", "@io_k8s_client_go//tools/cache"],
        "internalinterfaces": [":clientset", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_client_go//tools/cache"],
        "informer_version": [api, ":clientset", ":internalinterfaces", ":listers", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/runtime/schema", "@io_k8s_apimachinery//pkg/watch", "@io_k8s_client_go//tools/cache"],
        "informer_group": [":internalinterfaces", ":informer_version"],
        "informers": [api, ":clientset", ":internalinterfaces", ":informer_group", "@io_k8s_apimachinery//pkg/apis/meta/v1:meta", "@io_k8s_apimachinery//pkg/runtime", "@io_k8s_apimachinery//pkg/runtime/schema", "@io_k8s_apimachinery//pkg/util/wait", "@io_k8s_client_go//tools/cache"],
    }
    for package, files in packages.items():
        native.filegroup(name = package + "_sources", srcs = [":" + name], output_group = package)
        if package not in dependencies:
            continue
        go_library(
            name = package,
            srcs = [":" + package + "_sources"],
            importpath = importpath + "/" + files[0].rpartition("/")[0],
            deps = dependencies[package],
        )
