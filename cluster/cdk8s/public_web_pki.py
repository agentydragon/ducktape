"""Mount the public roots distributed by trust-manager for outbound HTTPS."""

from cdk8s_plus_34 import ConfigMap, Container, Volume, k8s
from constructs import Construct

NAME = "public-web-pki"
KEY = "ca-certificates.crt"
MOUNT_PATH = f"/var/run/{NAME}"
CA_FILE = f"{MOUNT_PATH}/{KEY}"

VOLUME = k8s.Volume(name=NAME, config_map=k8s.ConfigMapVolumeSource(name=NAME))
VOLUME_MOUNT = k8s.VolumeMount(name=NAME, mount_path=MOUNT_PATH, read_only=True)
SSL_CERT_FILE = k8s.EnvVar(name="SSL_CERT_FILE", value=CA_FILE)


def mount(scope: Construct, container: Container) -> None:
    config_map = ConfigMap.from_config_map_name(scope, "public-web-pki-ref", NAME)
    container.mount(MOUNT_PATH, Volume.from_config_map(scope, "public-web-pki-volume", config_map), read_only=True)
