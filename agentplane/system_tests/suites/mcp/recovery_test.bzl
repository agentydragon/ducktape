"""Bazel recipe for the isolated MCP recovery scenarios."""

load("//agentplane/system_tests/k3s:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_PERSISTENCE_PROPERTIES", "K3S_TAGS", "k3s_recycling_key")
load("//devinfra/python:defs.bzl", "py_test")

def mcp_recovery_test(name, fixture_manifest, image_manifest):
    """Declare one K3s test action for a disruptive recovery scenario.

    Args:
        name: Test target and Python source basename.
        fixture_manifest: Rendered Kubernetes fixture label.
        image_manifest: Label for the image list that fixture needs.
    """
    cert_manager = "@cert_manager_v1_21_2_release//file"
    trust_manager = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"
    py_test(
        name = name,
        size = "enormous",
        srcs = [name + ".py"],
        data = [
            "//agentplane/crds",
            K3S_BINARY,
            image_manifest,
            trust_manager,
            fixture_manifest,
            cert_manager,
        ],
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath {})".format(cert_manager),
            "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath {})".format(trust_manager),
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath {})".format(fixture_manifest),
            "K3S_IMAGE_MANIFEST_RLOCATION": "$(rlocationpath {})".format(image_manifest),
        },
        exec_properties = K3S_EXEC_PROPERTIES | K3S_PERSISTENCE_PROPERTIES | {
            "test.runner-recycling-key": k3s_recycling_key(vm = False),
        },
        tags = K3S_TAGS,
        deps = [
            ":conftest",
            "//:conftest",
            "//agentplane/system_tests:conftest",
            "//agentplane/system_tests/deployment:pytest_plugin",
            "//agentplane/system_tests/driver:cluster",
            "//agentplane/system_tests/driver:executor",
            "//agentplane/system_tests/driver:sandbox",
            "//agentplane/system_tests/driver:scripted_upstream",
            "//agentplane/system_tests/scenarios:central_sampler",
            "@pypi//pytest",
        ],
    )
