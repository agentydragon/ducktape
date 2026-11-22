"""Container image for a credential rotator that writes SOPS-encrypted YAML."""

load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_image_layer")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("//devinfra/oci:py_image.bzl", "py_image_entrypoint", "py_image_env")

def rotator_image(name, repo_tag, tool_layers):
    """Package the local rotate.py with its command-line tools and YAML formatter."""
    py_binary(
        name = "rotate_bin",
        main = "rotate.py",
        deps = [":rotate"],
    )
    py_image_layer(
        name = "layers",
        binary = ":rotate_bin",
        group = "65534",
        owner = "65534",
    )
    oci_image(
        name = name,
        base = "@debian_trixie_slim_linux_amd64",
        entrypoint = py_image_entrypoint("rotate_bin"),
        env = py_image_env(binary_name = "rotate_bin"),
        labels = {"org.opencontainers.image.source": "https://github.com/agentydragon/ducktape"},
        tars = tool_layers + [
            "//devinfra:node_bin_layer",
            "//devinfra:prettier_cli_layer",
            "//devinfra:prettier_bin_layer",
            ":layers",
        ],
    )
    oci_load(
        name = "load",
        image = ":" + name,
        repo_tags = [repo_tag],
    )
