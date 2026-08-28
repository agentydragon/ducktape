load("@rules_python//python:defs.bzl", "py_binary")

def js_debundle_live_proxy_binary(name, data = [], fixed_args = [], no_copy_to_bin = [], **kwargs):
    py_binary(
        name = name,
        args = fixed_args,
        data = data + no_copy_to_bin,
        main_module = "devinfra.js.debundle.live_proxy.serve",
        deps = [Label("//devinfra/js/debundle/live_proxy:addon"), Label("//devinfra/js/debundle/live_proxy:core"), Label("//devinfra/js/debundle/live_proxy:mitmproxy_script"), Label("//devinfra/js/debundle/live_proxy:package_tree"), Label("//devinfra/js/debundle/live_proxy:responses"), Label("//devinfra/js/debundle/live_proxy:serve"), Label("//devinfra/js/debundle/live_proxy:server"), Label("//devinfra/js/debundle/live_proxy:vendor_runtime")],
        **kwargs
    )
