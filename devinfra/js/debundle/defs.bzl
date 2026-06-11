load("@rules_python//python:defs.bzl", "py_binary")

def js_debundle_live_proxy_binary(name, data = [], fixed_args = [], no_copy_to_bin = [], **kwargs):
    py_binary(
        name = name,
        args = fixed_args,
        data = data + no_copy_to_bin,
        main_module = "devinfra.js.debundle.live_proxy.serve",
        deps = [Label("//devinfra/js/debundle/live_proxy:proxy_lib")],
        **kwargs
    )
