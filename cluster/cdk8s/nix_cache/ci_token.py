"""Identities of the CI Attic tokens published by the rotator."""

from cluster.cdk8s.secret_ref import SecretRef

WRITER = SecretRef(namespace="nix-cache", name="attic-ducktape-ci-writer")
READER = SecretRef(namespace="nix-cache", name="attic-ducktape-ci-reader")
GAFFER_WRITER = SecretRef(namespace="nix-cache", name="attic-gaffer-ci-writer")
WRITER_TOKEN = WRITER.key("attic_token")
READER_TOKEN = READER.key("attic_token")
GAFFER_WRITER_TOKEN = GAFFER_WRITER.key("attic_token")
