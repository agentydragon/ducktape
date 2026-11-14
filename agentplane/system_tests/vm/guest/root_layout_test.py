import json
import os
import subprocess

from python.runfiles import Runfiles


def test_root_run_is_the_fixture_symlink_in_all_images() -> None:
    runfiles_root = Runfiles.CreateOrRaise().root()
    closure = runfiles_root / os.environ["EROFS_CLOSURE_RLOCATION"]
    tool = runfiles_root / os.environ["EROFS_TOOL_RLOCATION"]

    for image_key in ("ROOT_IMAGE_RLOCATION", "ROOT_PROFILE_IMAGE_RLOCATION", "ROOT_PLATFORM_IMAGE_RLOCATION"):
        image = runfiles_root / os.environ[image_key]

        inode = subprocess.run(
            [tool, closure, "dump.erofs", "--path=/var/run", image], check=True, capture_output=True, text=True
        ).stdout
        assert "symlink file" in inode, f"{image_key}: {inode}"

        target = subprocess.run(
            [tool, closure, "dump.erofs", "--cat", "--path=/var/run", image], check=True, capture_output=True, text=True
        ).stdout
        assert target == "/run", f"{image_key}: {target}"


def test_only_the_setup_platform_image_omits_packaged_harnesses() -> None:
    runfiles_root = Runfiles.CreateOrRaise().root()
    closure = runfiles_root / os.environ["EROFS_CLOSURE_RLOCATION"]
    tool = runfiles_root / os.environ["EROFS_TOOL_RLOCATION"]
    for image_key in ("ROOT_IMAGE_RLOCATION", "ROOT_PROFILE_IMAGE_RLOCATION", "ROOT_PLATFORM_IMAGE_RLOCATION"):
        image = runfiles_root / os.environ[image_key]
        for path in ("/opt/harnesses/claude", "/opt/harnesses/codex/bin/codex"):
            result = subprocess.run(
                [tool, closure, "dump.erofs", f"--path={path}", image], check=True, capture_output=True, text=True
            )
            if image_key == "ROOT_PLATFORM_IMAGE_RLOCATION":
                # dump.erofs reports a missing path on stderr but still exits zero.
                assert not result.stdout, result
                assert f"read inode failed @ {path}" in result.stderr, result
            else:
                assert result.stdout, result
                assert not result.stderr, result
        if image_key == "ROOT_PLATFORM_IMAGE_RLOCATION":
            prepare = subprocess.run(
                [
                    tool,
                    closure,
                    "dump.erofs",
                    "--cat",
                    "--path=/etc/systemd/system/agentplane-guest-prepare.service.d/guest-prepare-platform.conf",
                    image,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert "ExecStart=/usr/sbin/agentplane-guest-prepare platform" in prepare


def test_workload_trust_mount_matches_guest_contract() -> None:
    runfiles_root = Runfiles.CreateOrRaise().root()
    contract = json.loads((runfiles_root / os.environ["VM_GUEST_CONTRACT_RLOCATION"]).read_text(encoding="utf-8"))
    workload_trust = contract["environment_runtime_workload_trust"]
    fstab = (runfiles_root / os.environ["FSTAB_RLOCATION"]).read_text(encoding="utf-8")
    fstab_entries = [
        entry.split() for entry in fstab.splitlines() if entry.strip() and not entry.lstrip().startswith("#")
    ]
    mounts = [entry for entry in fstab_entries if len(entry) >= 3 and entry[1] == workload_trust["mount"]]
    assert len(mounts) == 1, f"expected one workload trust mount for {workload_trust['mount']}, got {mounts}"
    mount = mounts[0]
    assert mount[:3] == [workload_trust["volume_name"], workload_trust["mount"], workload_trust["filesystem"]]

    tmpfiles = (runfiles_root / os.environ["RUNTIME_TMPFILES_RLOCATION"]).read_text(encoding="utf-8")
    tmpfiles_entries = [
        entry.split() for entry in tmpfiles.splitlines() if entry.strip() and not entry.lstrip().startswith("#")
    ]
    assert ["d", workload_trust["mount"], "0755", "root", "root", "-"] in tmpfiles_entries


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
