"""VM Sandbox scenarios on hosted KVM or managed QEMU emulation."""

load("//agentplane/system_tests/k3s:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_PERSISTENCE_PROPERTIES", "K3S_TAGS", "KVM_EXEC_PROPERTIES", "k3s_recycling_key")
load("//devinfra/python:defs.bzl", "py_test")

def vm_test(
        name,
        pool = "kvm-vm",
        recycle = False,
        tags = [],
        suite = "//agentplane/system_tests/suites:test_vm.py",
        extra_deps = []):
    """Use a hosted pool for hardware acceleration, or pool=None for the managed TCG control."""
    recycling_properties = {}
    recycling_environment = {}
    if recycle:
        recycling_properties = {
            "test.recycle-runner": "true",
            "test.runner-recycling-key": "kvm-docker-spike-ext4-reclaim-v1-" + k3s_recycling_key(vm = True),
            "test.runner-recycling-max-wait": "5s",
            "test.env-overrides": "AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1,AGENTPLANE_K3S_DATA_DIR=/var/lib/k3s-recycled",
        }
        recycling_environment = {
            "PYTEST_PLUGINS": "agentplane.system_tests.k3s.recycling_probe",
            "K3S_RECYCLING_DISK_RLOCATION": "$(rlocationpath //agentplane/system_tests/k3s:recycling_data_disk)",
        }

    if pool:
        properties = {
            "dockerNetwork": "bridge",
            "test.EstimatedCPU": "4",
            "test.EstimatedFreeDiskBytes": "50GB",
            "test.EstimatedMemory": "7GiB",
        } | KVM_EXEC_PROPERTIES | {"test.Pool": pool} | recycling_properties
    else:
        # The two routes share one PyCompile output. Scope VM sizing to TestRunner
        # so the compiler sees identical execution properties for both targets.
        properties = K3S_PERSISTENCE_PROPERTIES | {
            "dockerNetwork": "bridge",
            "test.EstimatedComputeUnits": "8",
            "test.EstimatedFreeDiskBytes": "50GB",
            "test.runner-recycling-key": k3s_recycling_key(vm = True),
        }

    py_test(
        name = name,
        size = "enormous",
        srcs = [suite],
        args = [
            "-o",
            "junit_duration_report=call",
            "--durations=0",
        ],
        data = [
            "//agentplane/crds",
            K3S_BINARY,
            "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml",
            "//agentplane/system_tests/suites:vm_image_manifest",
            "//agentplane/system_tests/suites:vm_manifests.json",
            "@cert_manager_v1_21_2_release//file",
            "@kubevirt_operator_bundle//file",
            "@kyverno_install//file",
        ] + (["//agentplane/system_tests/k3s:recycling_data_disk"] if recycle else []),
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath @cert_manager_v1_21_2_release//file)",
            "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath //agentplane/system_tests/k3s/pki:trust_manager_install_yaml)",
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath //agentplane/system_tests/suites:vm_manifests.json)",
            "K3S_IMAGE_MANIFEST_RLOCATION": "$(rlocationpath //agentplane/system_tests/suites:vm_image_manifest)",
            "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
            "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
            "AGENTPLANE_K3S_REQUIRE_KVM": "1" if pool else "0",
            "AGENTPLANE_VM_BOOT_EVIDENCE": "0",
            "AGENTPLANE_VM_SHUTDOWN_DIAGNOSTIC": "0",
            "K3S_PROFILE_GUEST_ROOT": "0",
        } | recycling_environment,
        exec_properties = properties,
        main = suite,
        tags = K3S_TAGS + tags,
        deps = [
            "//:conftest",
            "//agentplane/proto:session_pb2",
            "//agentplane/system_tests:conftest",
            "//agentplane/system_tests/deployment:pytest_plugin",
            "//agentplane/system_tests/driver:case_batch",
            "//agentplane/system_tests/driver:cluster",
            "//agentplane/system_tests/driver:dispatcher_transport",
            "//agentplane/system_tests/driver:offered_models",
            "//agentplane/system_tests/driver:sandbox",
            "//agentplane/system_tests/driver:scenario",
            "//agentplane/system_tests/driver:scripted_upstream",
            "//agentplane/system_tests/driver:tracing",
            "//agentplane/system_tests/scenarios:vm_sandbox",
            "//agentplane/system_tests/vm:configuration",
            "//agentplane/system_tests/vm:trace_contract",
            "//devinfra/testing:undeclared_outputs",
            "//util:net",
            "@pypi//protobuf",
            "@pypi//pytest",
            "@pypi//pytest_asyncio",
            "@pypi//pytest_bazel",
        ] + (["//agentplane/system_tests/k3s:recycling_probe"] if recycle else []) + extra_deps,
    )
