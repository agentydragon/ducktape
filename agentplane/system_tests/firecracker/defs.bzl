"""Firecracker acceptance with retained K3s or an explicitly discarded cold baseline."""

load("//agentplane/system_tests/k3s:defs.bzl", "GENERIC_DEVICE_PLUGIN_IMAGE", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_TAGS", "KVM_EXEC_PROPERTIES", "k3s_recycling_key")
load("//devinfra/python:defs.bzl", "py_test")

def firecracker_test(name, recycle, pool = "kvm-vm", tags = []):
    """Run both acceptance scenarios with a retained or explicitly discarded K3s baseline."""
    py_test(
        name = name,
        size = "enormous",
        srcs = ["//agentplane/system_tests/suites:test_firecracker.py"],
        main = "//agentplane/system_tests/suites:test_firecracker.py",
        args = ["-o", "junit_duration_report=call", "--durations=0"] + ([] if recycle else ["--discard-recycled-runner"]),
        data = [
            "//agentplane/crds",
            "//agentplane/system_tests/k3s:recycling_data_disk",
            K3S_BINARY,
            "//agentplane/system_tests/suites:firecracker_image_manifest",
            "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml",
            "//agentplane/system_tests/suites:firecracker_manifests.json",
            "@cert_manager_v1_21_2_release//file",
        ],
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath @cert_manager_v1_21_2_release//file)",
            "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath //agentplane/system_tests/k3s/pki:trust_manager_install_yaml)",
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath //agentplane/system_tests/suites:firecracker_manifests.json)",
            "K3S_IMAGE_MANIFEST_RLOCATION": "$(rlocationpath //agentplane/system_tests/suites:firecracker_image_manifest)",
            "KVM_DEVICE_PLUGIN_IMAGE": GENERIC_DEVICE_PLUGIN_IMAGE["@generic_device_plugin_linux_amd64"],
            "AGENTPLANE_K3S_REQUIRE_KVM": "1",
            "AGENTPLANE_VM_BOOT_EVIDENCE": "0",
            "AGENTPLANE_VM_SHUTDOWN_DIAGNOSTIC": "0",
            "K3S_PROFILE_GUEST_ROOT": "0",
            "PYTEST_PLUGINS": "agentplane.system_tests.k3s.recycling_probe",
            "K3S_RECYCLING_DISK_RLOCATION": "$(rlocationpath //agentplane/system_tests/k3s:recycling_data_disk)",
        },
        exec_properties = {
            "dockerNetwork": "bridge",
            "test.EstimatedCPU": "4",
            "test.EstimatedFreeDiskBytes": "20GB",
            # Both Firecracker cases passed with a 5 GiB cap and swap disabled, peaking at 4.44 GiB:
            # https://app.buildbuddy.io/invocation/3019fd35-3795-41dd-823f-2714ae254ef8
            # At 4 GiB both passed but the test cgroup recorded 34 s of full memory stalls:
            # https://app.buildbuddy.io/invocation/7ff14002-8aac-4ec0-adc0-0ee5f306b5e6
            "test.EstimatedMemory": "5GiB",
        } | KVM_EXEC_PROPERTIES | {
            "test.Pool": pool,
            "test.recycle-runner": "true" if recycle else "false",
            "test.runner-recycling-key": "firecracker-ext4-v1-" + k3s_recycling_key(vm = False),
            "test.runner-recycling-max-wait": "5s",
            "test.env-overrides": "AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1,AGENTPLANE_K3S_DATA_DIR=/var/lib/k3s-recycled",
        },
        tags = K3S_TAGS + tags,
        deps = [
            "//:conftest",
            "//agentplane/system_tests/k3s:recycling_probe",
            "//agentplane/app/testing:client",
            "//agentplane/session_dispatcher/dispatcher:testing_client",
            "//agentplane/system_tests:app_fixtures",
            "//agentplane/system_tests:conftest",
            "//agentplane/system_tests/driver:cluster",
            "//agentplane/system_tests/driver:sandbox",
            "//agentplane/system_tests/driver:scripted_upstream",
            "//agentplane/system_tests/firecracker:pytest_plugin",
            "//agentplane/system_tests/scenarios:firecracker",
            "//agentplane/system_tests/scenarios:workload_trust",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
        ],
    )
