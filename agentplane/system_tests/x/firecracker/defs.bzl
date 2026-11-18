"""Manual matched Firecracker fixture-lifetime targets."""

load("//agentplane/system_tests/k3s:defs.bzl", "GENERIC_DEVICE_PLUGIN_IMAGE", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_TAGS", "KVM_EXEC_PROPERTIES", "k3s_recycling_key")
load("//devinfra/python:defs.bzl", "py_test")

def firecracker_bench(name, recycle):
    py_test(
        name = name,
        size = "enormous",
        srcs = ["//agentplane/system_tests/suites:test_firecracker.py"],
        main = "//agentplane/system_tests/suites:test_firecracker.py",
        args = ["-o", "junit_duration_report=call", "--durations=0"] + ([] if recycle else ["--discard-recycled-runner"]),
        data = [
            "//agentplane/crds",
            "//agentplane/system_tests/x/vm_kvm:data_disk",
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
            "AGENTPLANE_FIRECRACKER_PERSISTENCE_EXPERIMENT": "1",
            "PYTEST_PLUGINS": "agentplane.system_tests.x.vm_kvm.recycling_probe",
            "K3S_RECYCLING_DISK_RLOCATION": "$(rlocationpath //agentplane/system_tests/x/vm_kvm:data_disk)",
        },
        exec_properties = {
            "dockerNetwork": "bridge",
            "test.EstimatedCPU": "4",
            "test.EstimatedFreeDiskBytes": "20GB",
            "test.EstimatedMemory": "6GB",
        } | KVM_EXEC_PROPERTIES | {
            "test.recycle-runner": "true" if recycle else "false",
            "test.runner-recycling-key": "firecracker-ext4-bench-v1-" + k3s_recycling_key(vm = False),
            "test.runner-recycling-max-wait": "5s",
            "test.env-overrides": "AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1,AGENTPLANE_K3S_DATA_DIR=/var/lib/k3s-recycled",
        },
        tags = K3S_TAGS + ["manual", "bench"],
        deps = [
            "//:conftest",
            "//agentplane/system_tests/x/vm_kvm:recycling_probe",
            "//agentplane/app/testing:client",
            "//agentplane/session_dispatcher/dispatcher:testing_client",
            "//agentplane/system_tests:app_fixtures",
            "//agentplane/system_tests:conftest",
            "//agentplane/system_tests/deployment:pytest_plugin",
            "//agentplane/system_tests/driver:cluster",
            "//agentplane/system_tests/driver:sandbox",
            "//agentplane/system_tests/driver:scripted_upstream",
            "//agentplane/system_tests/scenarios:firecracker",
            "//agentplane/system_tests/scenarios:workload_trust",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
        ],
    )
