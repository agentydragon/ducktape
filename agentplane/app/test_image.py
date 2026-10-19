"""Exercise the packaged Agentplane app image using its production entrypoint."""

import pytest_bazel

from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer

_APP_IMAGE = OciImage("_main/agentplane/app/app_image_test_layout.rloc", "agentplane-app:smoke")


def _run_image(image: str, *, test_name: str, command: list[str], entrypoint: str | None = None) -> str:
    container = (
        LoggedContainer(image, test_name=test_name)
        .with_command(command)
        .with_kwargs(network_mode="none", read_only=True)
        .with_tmpfs_mount("/tmp", "rw,nosuid,size=32m,uid=1000,gid=1000,mode=1777")
    )
    if entrypoint is not None:
        container.with_kwargs(entrypoint=entrypoint)

    with container:
        wrapped = container.get_wrapped_container()
        assert wrapped.attrs["Config"]["User"] == "1000:1000"
        result = wrapped.wait(timeout=60)
        output: bytes = wrapped.logs(stdout=True, stderr=True)
        assert result["StatusCode"] == 0, f"{test_name} failed; see {test_name}/container.log in undeclared outputs"
        return output.decode(errors="replace")


def test_app_image_entrypoint_and_python_runtime() -> None:
    image = load_oci_image(_APP_IMAGE)
    output = _run_image(image, test_name="app-entrypoint", command=["--help"])
    # Importing the app also imports generated gRPC stubs, including google.api annotations.
    assert "--database-url" in output

    version = _run_image(image, test_name="app-python3", entrypoint="python3", command=["--version"])
    assert version.startswith("Python 3.14.")


if __name__ == "__main__":
    pytest_bazel.main()
