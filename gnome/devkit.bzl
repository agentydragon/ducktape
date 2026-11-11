"""Local nested GNOME Shell for an extension distribution and desktop daemon."""

load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

def gnome_devkit(name, uuid, extension_zip, daemon, fixture_env, default_fixture = None, daemon_arg = "-", host_bus_env = "-"):
    data = [extension_zip, daemon]
    fixture_arg = "-"
    if default_fixture:
        data.append(default_fixture)
        fixture_arg = "$(rlocationpath %s)" % default_fixture
    sh_binary(
        name = name,
        srcs = ["//gnome:devkit.sh"],
        args = [
            uuid,
            "$(rlocationpath %s)" % extension_zip,
            "$(rlocationpath %s)" % daemon,
            fixture_env,
            fixture_arg,
            daemon_arg,
            host_bus_env,
        ],
        data = data,
        use_bash_launcher = True,
    )
