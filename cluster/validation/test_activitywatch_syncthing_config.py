"""Parity checks for ActivityWatch's static Syncthing config."""

from __future__ import annotations

import base64
import hashlib
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

CONFIG_XML = get_required_path("_main/cluster/k8s/activitywatch/syncthing-config.xml")
CLUSTER_IDENTITY = get_required_path("_main/cluster/k8s/activitywatch/syncthing-identity.yaml")
CLUSTER_KEY = get_required_path("_main/cluster/k8s/activitywatch/syncthing-key.sops.yaml")
SYNCTHING_DEPLOYMENT = get_required_path("_main/cluster/k8s/activitywatch/syncthing-deployment.yaml")
IMPORTER_CRONJOB = get_required_path("_main/cluster/k8s/activitywatch/importer-cronjob.yaml")
PVC_MANIFEST = get_required_path("_main/cluster/k8s/activitywatch/pvc.yaml")
HOST_CERT_SENTINEL = get_required_path("_main/secrets/home/rugged/activitywatch-syncthing.cert.pem")

SYNCTHING_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _luhn32_check_char(chunk: str) -> str:
    factor = 1
    total = 0
    for char in reversed(chunk):
        addend = factor * SYNCTHING_BASE32_ALPHABET.index(char)
        factor = 1 if factor == 2 else 2
        total += (addend // len(SYNCTHING_BASE32_ALPHABET)) + (addend % len(SYNCTHING_BASE32_ALPHABET))
    return SYNCTHING_BASE32_ALPHABET[(-total) % len(SYNCTHING_BASE32_ALPHABET)]


def _device_id_from_cert(pem: str) -> str:
    cert_der = ssl.PEM_cert_to_DER_cert(pem)
    digest = base64.b32encode(hashlib.sha256(cert_der).digest()).decode("ascii").rstrip("=")
    checked = "".join(
        digest[index : index + 13] + _luhn32_check_char(digest[index : index + 13])
        for index in range(0, len(digest), 13)
    )
    return "-".join(checked[index : index + 7] for index in range(0, len(checked), 7))


def _read_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def _host_devices_from_cert_files() -> dict[str, str]:
    home_secrets_dir = HOST_CERT_SENTINEL.parents[1]
    devices = {}
    for cert_path in sorted(home_secrets_dir.glob("*/activitywatch-syncthing.cert.pem")):
        host = cert_path.parent.name
        key_path = cert_path.with_name("activitywatch-syncthing.sops.key")
        assert key_path.exists(), f"{host} is missing {key_path.name}"
        assert "ENC[AES256_GCM" in key_path.read_text(), f"{key_path} is not SOPS-encrypted"
        devices[host] = _device_id_from_cert(cert_path.read_text())
    return devices


def _cluster_device_from_identity() -> dict[str, str]:
    identity = _read_yaml(CLUSTER_IDENTITY)
    key_secret = _read_yaml(CLUSTER_KEY)
    assert "device_id" not in identity["data"]
    assert "ENC[AES256_GCM" in key_secret["data"]["key.pem"]
    return {"activitywatch-cluster": _device_id_from_cert(identity["data"]["cert.pem"])}


def _expected_devices() -> dict[str, str]:
    return _host_devices_from_cert_files() | _cluster_device_from_identity()


def test_syncthing_config_matches_identity_sources() -> None:
    expected_devices = _expected_devices()

    root = ET.parse(CONFIG_XML).getroot()
    assert root.tag == "configuration"
    assert root.attrib["version"] == "51"

    folders = root.findall("folder")
    assert len(folders) == 1
    folder = folders[0]
    assert folder.attrib == {
        "id": "activitywatch",
        "label": "ActivityWatch",
        "path": "/sync-inbox",
        "type": "receiveonly",
        "rescanIntervalS": "60",
        "fsWatcherEnabled": "true",
    }
    assert folder.findtext("filesystemType") == "basic"
    assert {device.attrib["id"] for device in folder.findall("device")} == set(expected_devices.values())

    xml_devices = {device.attrib["name"]: device for device in root.findall("device")}
    assert set(xml_devices) == set(expected_devices)

    for name, expected_device_id in expected_devices.items():
        device = xml_devices[name]
        assert device.attrib["id"] == expected_device_id
        assert device.attrib["compression"] == "metadata"
        assert device.attrib["introducer"] == "false"
        assert device.attrib["skipIntroductionRemovals"] == "false"
        assert [address.text for address in device.findall("address")] == ["dynamic"]

    gui = root.find("gui")
    assert gui is not None
    assert gui.attrib == {"enabled": "false", "tls": "false"}

    options = root.find("options")
    assert options is not None
    assert options.findtext("listenAddress") == "default"
    assert options.findtext("globalAnnounceServer") == "default"
    assert options.findtext("globalAnnounceEnabled") == "true"
    assert options.findtext("localAnnounceEnabled") == "true"
    assert options.findtext("localAnnouncePort") == "21027"
    assert options.findtext("relaysEnabled") == "true"
    assert options.findtext("urAccepted") == "-1"


def test_ovh_workloads_use_the_canonical_zone_label() -> None:
    expected_selector = {"topology.kubernetes.io/zone": "hil-ovh"}
    syncthing = _read_yaml(SYNCTHING_DEPLOYMENT)
    importer = _read_yaml(IMPORTER_CRONJOB)

    assert syncthing["spec"]["template"]["spec"]["nodeSelector"] == expected_selector
    assert importer["spec"]["jobTemplate"]["spec"]["template"]["spec"]["nodeSelector"] == expected_selector


def test_syncthing_index_state_is_persistent() -> None:
    syncthing = _read_yaml(SYNCTHING_DEPLOYMENT)
    pod_spec = syncthing["spec"]["template"]["spec"]
    state_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "state")
    assert state_volume == {"name": "state", "persistentVolumeClaim": {"claimName": "activitywatch-syncthing-state"}}

    pvcs = {manifest["metadata"]["name"]: manifest for manifest in yaml.safe_load_all(PVC_MANIFEST.read_text())}
    state_pvc = pvcs["activitywatch-syncthing-state"]
    assert state_pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert state_pvc["spec"]["storageClassName"] == "local-path-ovh"


def test_importer_is_suspended_pull_only_and_fails_closed() -> None:
    importer = _read_yaml(IMPORTER_CRONJOB)
    assert importer["spec"]["suspend"] is True
    assert importer["spec"]["concurrencyPolicy"] == "Forbid"

    pod_spec = importer["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert {volume["name"] for volume in pod_spec["volumes"]} == {"sync-inbox"}
    assert pod_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "activitywatch-sync-inbox"

    validator = pod_spec["initContainers"][0]
    assert validator["name"] == "validate-canonical-device-databases"
    validator_script = validator["command"][-1]
    assert "-name '*.db'" in validator_script
    assert "!= test.db" in validator_script
    assert "Refusing to import unexpected database" in validator_script
    assert validator["volumeMounts"][0]["readOnly"] is True

    container = pod_spec["containers"][0]
    command = container["command"]
    assert command[-3:] == ["sync", "--mode", "pull"]
    assert "push" not in command
    assert container["volumeMounts"] == [{"name": "sync-inbox", "mountPath": "/sync-inbox"}]


if __name__ == "__main__":
    pytest_bazel.main()
