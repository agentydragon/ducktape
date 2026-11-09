"""Opt-in TestRunner routing shared by the KVM smoke and system suites."""

def canary_exec_properties(properties):
    return select({
        "//devinfra/buildbuddy/kvm/x/canary:cpu4": properties | {
            "test.Pool": "kvm-canary-20261109",
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": "10GB",
        },
        "//devinfra/buildbuddy/kvm/x/canary:cpu8": properties | {
            "test.Pool": "kvm-canary-20261109",
            "test.EstimatedCPU": "8",
            "test.EstimatedMemory": "10GB",
        },
        "//conditions:default": properties,
    })
