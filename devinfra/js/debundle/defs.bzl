load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_test")
load("@bazel_skylib//lib:shell.bzl", "shell")

def js_debundle_transform_binary(name, data = [], fixed_args = [], no_copy_to_bin = [], node_options = [], **kwargs):
    runtime_deps = [Label("//devinfra/js/debundle/transforms:libs")] + data
    passthrough_files = runtime_deps + [Label("//devinfra/js/debundle/transforms:run_transform_entry_point")] + no_copy_to_bin
    js_binary(
        name = name,
        data = runtime_deps,
        entry_point = Label("//devinfra/js/debundle/transforms:run_transform_entry_point"),
        fixed_args = fixed_args,
        no_copy_to_bin = passthrough_files,
        node_options = ["--max-old-space-size=8192"] + node_options,
        **kwargs
    )

def js_debundle_live_proxy_binary(name, data = [], fixed_args = [], no_copy_to_bin = [], **kwargs):
    runtime_deps = [Label("//devinfra/js/debundle/live_proxy:libs")] + data
    passthrough_files = runtime_deps + [Label("//devinfra/js/debundle/live_proxy:serve_entry_point")] + no_copy_to_bin
    js_binary(
        name = name,
        data = runtime_deps,
        entry_point = Label("//devinfra/js/debundle/live_proxy:serve_entry_point"),
        fixed_args = fixed_args,
        no_copy_to_bin = passthrough_files,
        **kwargs
    )

def js_debundle_live_proxy_load_test(
        name,
        app_manifest = None,
        browser_binary = None,
        data = [],
        env = {},
        fixed_args = [],
        goto_timeout_ms = None,
        no_copy_to_bin = [],
        wait_for_selector = None,
        wait_timeout_ms = None,
        **kwargs):
    if browser_binary == None:
        browser_binary = "@playwright_browsers//:chromium-headless-shell"

    computed_args = []
    if app_manifest != None:
        computed_args.extend([
            "--app-manifest",
            "\"$$JS_BINARY__RUNFILES\"/$(rlocationpath %s)" % app_manifest,
        ])
    if wait_for_selector != None:
        computed_args.extend([
            "--wait-for-selector",
            shell.quote(wait_for_selector),
        ])
    if wait_timeout_ms != None:
        computed_args.extend([
            "--wait-timeout-ms",
            str(wait_timeout_ms),
        ])
    if goto_timeout_ms != None:
        computed_args.extend([
            "--goto-timeout-ms",
            str(goto_timeout_ms),
        ])

    runtime_deps = [
        Label("//devinfra/js/debundle/live_proxy:libs"),
        Label("//util/testing/frontend_visual:puppeteer_lib"),
    ] + data
    passthrough_files = runtime_deps + [
        Label("//devinfra/js/debundle/live_proxy:browser_load_test_entry_point"),
        browser_binary,
    ] + no_copy_to_bin
    test_env = {
        "PUPPETEER_EXECUTABLE_PATH": "$(rootpath %s)" % browser_binary,
    }
    test_env.update(env)
    js_test(
        name = name,
        data = runtime_deps + [browser_binary],
        entry_point = Label("//devinfra/js/debundle/live_proxy:browser_load_test_entry_point"),
        env = test_env,
        fixed_args = computed_args + fixed_args,
        no_copy_to_bin = passthrough_files,
        **kwargs
    )
