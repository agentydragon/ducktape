"""What every test that starts its own k3s stack runs with: one RBE action with room for k3s."""

K3S_EXEC_PROPERTIES = {
    "dockerNetwork": "bridge",
    "recycle-runner": "false",
    "EstimatedComputeUnits": "4",
    "EstimatedFreeDiskBytes": "50GB",
}

K3S_TAGS = [
    "external",
    "manual",
    "no-cache",
    "requires-network",
]

K3S_BINARY = "@k3s_linux_amd64//file:k3s"

K3S_BINARY_RLOCATION = "$(rlocationpath " + K3S_BINARY + ")"
