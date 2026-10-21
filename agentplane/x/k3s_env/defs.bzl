"""What every test that starts its own k3s stack runs with: one RBE action with room for k3s."""

K3S_EXEC_PROPERTIES = {
    "dockerNetwork": "bridge",
    "recycle-runner": "false",
    "EstimatedComputeUnits": "4",
    "EstimatedFreeDiskBytes": "50GB",
}

# A test that runs a k3s stack in its action. Such a test is an ordinary target otherwise: PR CI
# runs it when its inputs change, and a pass caches.
K3S_TAGS = ["requires-network"]

K3S_BINARY = "@k3s_linux_amd64//file:k3s"

K3S_BINARY_RLOCATION = "$(rlocationpath " + K3S_BINARY + ")"
