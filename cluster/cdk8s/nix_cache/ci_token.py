"""Identities of the CI Attic tokens published by the rotator."""

from cluster.cdk8s.secret_ref import SecretRef

WRITER = SecretRef(namespace="nix-cache", name="attic-ducktape-ci-writer")
READER = SecretRef(namespace="nix-cache", name="attic-ducktape-ci-reader")
WRITER_TOKEN = WRITER.key("attic_token")
READER_TOKEN = READER.key("attic_token")
