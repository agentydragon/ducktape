"""Identity of the CI writer token published by the Attic rotator."""

from cluster.cdk8s.secret_ref import SecretRef

WRITER = SecretRef(namespace="nix-cache", name="attic-ducktape-ci-writer")
TOKEN = WRITER.key("attic_token")
